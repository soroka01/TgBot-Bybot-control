"""Market data collection and entry gating for auto-trading."""

from __future__ import annotations

import hashlib
import time
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Optional
from api.bybit_api import BybitAPI, BybitAPIError
from config import (
    FALLBACK_TAKER_FEE_RATE,
    MAX_DAILY_LOSS_PERCENT,
    TRADABLE_TOKENS,
)
from core.decision_engine import build_trade_snapshot
from core.market_data import get_market_analysis
from core.risk_engine import D, portfolio_risk_usd
from storage.database import get_store
from utils.helpers import parse_account_overview
from utils.logger_setup import logger
from core.auto.runtime import (
    SUPPORTED_AUTO_MARGIN_MODES,
)


def _ticker_rows(
    bybit: BybitAPI,
    tokens: list[str],
) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    for token in tokens:
        symbol = f"{token}USDT"
        response = bybit.get_tickers(symbol)
        rows = response.get("result", {}).get("list", [])
        if not rows:
            raise BybitAPIError(f"Bybit не вернул ticker {symbol}")
        result[symbol] = {
            **rows[0],
            "_snapshot_time_ms": int(response.get("time") or time.time() * 1_000),
        }
    return result


def _fee_rates(
    bybit: BybitAPI,
    previous: Optional[dict[str, Decimal]] = None,
) -> dict[str, Decimal]:
    rates: dict[str, Decimal] = {}
    previous = previous or {}
    for token in TRADABLE_TOKENS:
        symbol = f"{token}USDT"
        try:
            rates[symbol] = bybit.get_fee_rate(symbol)
        except Exception as error:
            rates[symbol] = D(previous.get(symbol, FALLBACK_TAKER_FEE_RATE))
            logger.warning(
                f"{symbol}: не удалось получить персональную taker fee, "
                f"использую {rates[symbol]}: {error}"
            )
    return rates


def _realized_pnl_today(bybit: BybitAPI) -> Decimal:
    now = datetime.now(timezone.utc)
    midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
    response = bybit.get_closed_pnl(
        limit=100,
        start_time=int(midnight.timestamp() * 1_000),
        end_time=int(now.timestamp() * 1_000),
        all_pages=True,
    )
    realized = sum(
        (D(item.get("closedPnl", 0)) for item in response.get("result", {}).get("list", [])),
        Decimal("0"),
    )
    return realized


def _unsupported_derivative_exposure(bybit: BybitAPI) -> list[str]:
    """Return account exposure that the USDT-linear risk model cannot size."""
    exposure: list[str] = []
    for category, settle_coin, label in (
        ("linear", "USDC", "USDC linear"),
        ("inverse", None, "inverse"),
        ("option", None, "options"),
    ):
        positions = bybit.get_positions(
            settle_coin=settle_coin,
            category=category,
        ).get("result", {}).get("list", [])
        if any(abs(D(item.get("size", 0))) > 0 for item in positions):
            exposure.append(f"{label} position")
        orders = bybit.get_open_orders(
            category=category,
            settle_coin=settle_coin,
        ).get("result", {}).get("list", [])
        if any(item.get("reduceOnly") is not True for item in orders):
            exposure.append(f"{label} order")
    return exposure


def _daily_drawdown_block_reason(
    bybit: BybitAPI,
    equity: Decimal,
) -> Optional[str]:
    """Update the account-scoped high-water mark and enforce its loss limit."""
    account_scope = hashlib.sha256(
        f"{bybit.base}|{bybit.api_key}".encode("utf-8")
    ).hexdigest()[:24]
    guard = get_store().update_daily_equity_guard(
        float(equity),
        scope=account_scope,
    )
    high_water = D(guard["high_water_equity"])
    drawdown = D(guard["drawdown"])
    daily_limit = high_water * D(MAX_DAILY_LOSS_PERCENT) / 100
    if drawdown >= daily_limit and daily_limit > 0:
        return (
            f"Дневной equity drawdown ${drawdown:.2f} достиг лимита "
            f"${daily_limit:.2f}"
        )
    return None


def _entry_block_reason(
    bybit: BybitAPI,
    positions: list[dict[str, Any]],
    account: dict[str, Any],
    unprotected: list[str],
) -> Optional[str]:
    equity = D(account.get("equity_usd", 0))
    if equity <= 0:
        return "Equity аккаунта не положителен"
    drawdown_reason = _daily_drawdown_block_reason(bybit, equity)
    if drawdown_reason:
        return drawdown_reason

    try:
        account_mode = bybit.get_account_info().get("result", {})
    except Exception as error:
        logger.warning(f"Entry gate: не удалось проверить режим аккаунта: {error}")
        return "Не удалось проверить режим аккаунта Bybit"
    if not isinstance(account_mode, dict):
        return "Bybit вернул повреждённый режим аккаунта"
    margin_mode = account_mode.get("marginMode")
    if margin_mode not in SUPPORTED_AUTO_MARGIN_MODES:
        return (
            f"Режим маржи {margin_mode!r} не поддержан авто-режимом; "
            "нужен REGULAR_MARGIN"
        )
    unified_status = account_mode.get("unifiedMarginStatus")
    if (
        isinstance(unified_status, bool)
        or not isinstance(unified_status, int)
        or unified_status not in {3, 4, 5, 6}
    ):
        return "Нужен Unified Trading Account"
    try:
        unsupported = _unsupported_derivative_exposure(bybit)
    except Exception as error:
        logger.warning(f"Entry gate: не удалось проверить прочие деривативы: {error}")
        return "Не удалось проверить USDC/inverse/options exposure"
    if unsupported:
        return "Есть неподдерживаемая экспозиция: " + ", ".join(unsupported)
    if unprotected:
        return "Есть позиции без защитного Stop Loss: " + ", ".join(unprotected)
    unsafe = [
        str(position.get("symbol"))
        for position in positions
        if D(position.get("size", 0)) > 0
        and (
            position.get("positionStatus") not in {None, "", "Normal"}
            or bool(position.get("isReduceOnly"))
        )
    ]
    if unsafe:
        return "Bybit ограничил позиции: " + ", ".join(sorted(set(unsafe)))
    try:
        open_orders = bybit.get_open_orders().get("result", {}).get("list", [])
    except Exception as error:
        logger.warning(f"Entry gate: не удалось проверить активные ордера: {error}")
        return "Не удалось проверить активные ордера Bybit"
    # /v5/order/realtime returns active orders.  Conditional `Untriggered`
    # entries are exposure too, so do not maintain an incomplete status list.
    exposed_orders = [
        order for order in open_orders if order.get("reduceOnly") is not True
    ]
    if exposed_orders:
        return "Есть активные увеличивающие позицию ордера"
    try:
        realized_pnl = _realized_pnl_today(bybit)
    except Exception as error:
        logger.warning(f"Entry gate: не удалось проверить дневной PnL: {error}")
        return "Не удалось проверить дневной PnL Bybit"
    if realized_pnl <= -(equity * D(MAX_DAILY_LOSS_PERCENT) / 100) and equity > 0:
        return (
            f"Дневной realized-loss лимит достигнут: ${realized_pnl:.2f}"
        )
    return None


def collect_cycle(
    bybit: BybitAPI,
    fee_rates: dict[str, Decimal],
    *,
    tokens: Optional[list[str]] = None,
) -> dict[str, Any]:
    selected_tokens = list(tokens or TRADABLE_TOKENS)
    positions = [
        position
        for position in bybit.get_positions().get("result", {}).get("list", [])
        if D(position.get("size", 0)) > 0
    ]
    account = parse_account_overview(
        bybit.get_wallet_balance(),
        strict=True,
    )
    ticker_rows = _ticker_rows(bybit, selected_tokens)
    analyses: dict[str, dict[str, Any]] = {}
    for token in selected_tokens:
        symbol = f"{token}USDT"
        analyses[token] = get_market_analysis(
            bybit,
            symbol,
            float(D(ticker_rows[symbol].get("lastPrice", 0))),
        )
    conservative_fee = max(
        [D(FALLBACK_TAKER_FEE_RATE), *fee_rates.values()]
    )
    risk, unprotected = portfolio_risk_usd(
        positions,
        taker_fee_rate=conservative_fee,
    )
    block_reason = _entry_block_reason(bybit, positions, account, unprotected)
    snapshot = build_trade_snapshot(
        tokens=selected_tokens,
        positions=positions,
        tickers=ticker_rows,
        analyses=analyses,
        fee_rates=fee_rates,
        allow_entries=block_reason is None,
        entry_block_reason=block_reason,
    )
    return {
        "positions": positions,
        "account": account,
        "tickers": ticker_rows,
        "analyses": analyses,
        "portfolio_risk": risk,
        "entry_block_reason": block_reason,
        "snapshot": snapshot,
    }


def _fresh_entry_state(
    bybit: BybitAPI,
    fee_rates: dict[str, Decimal],
) -> dict[str, Any]:
    """Recheck all mutable account exposure immediately before an entry."""
    positions = [
        position
        for position in bybit.get_positions().get("result", {}).get("list", [])
        if D(position.get("size", 0)) > 0
    ]
    account = parse_account_overview(
        bybit.get_wallet_balance(),
        strict=True,
    )
    conservative_fee = max(
        [D(FALLBACK_TAKER_FEE_RATE), *fee_rates.values()]
    )
    risk, unprotected = portfolio_risk_usd(
        positions,
        taker_fee_rate=conservative_fee,
    )
    block_reason = _entry_block_reason(bybit, positions, account, unprotected)
    return {
        "positions": positions,
        "account": account,
        "portfolio_risk": risk,
        "entry_block_reason": block_reason,
    }


def _final_entry_state(
    bybit: BybitAPI,
    fee_rates: dict[str, Decimal],
) -> dict[str, Any]:
    """Re-read all USDT exposure used for sizing just before order creation."""
    position_rows = (
        bybit.get_positions().get("result", {}).get("list", [])
    )
    positions = [
        position
        for position in position_rows
        if D(position.get("size", 0)) > 0
    ]
    open_orders = (
        bybit.get_open_orders().get("result", {}).get("list", [])
    )
    account = parse_account_overview(
        bybit.get_wallet_balance(),
        strict=True,
    )
    conservative_fee = max(
        [D(FALLBACK_TAKER_FEE_RATE), *fee_rates.values()]
    )
    risk, unprotected = portfolio_risk_usd(
        positions,
        taker_fee_rate=conservative_fee,
    )

    block_reason: Optional[str] = None
    equity = D(account.get("equity_usd", 0))
    if equity <= 0:
        block_reason = "Equity аккаунта не положителен"
    else:
        block_reason = _daily_drawdown_block_reason(bybit, equity)
    if block_reason is None:
        if D(account.get("available_usd", 0)) <= 0:
            block_reason = "Нет доступной маржи для новой позиции"
        elif unprotected:
            block_reason = (
                "Есть позиции без безопасного Stop Loss: "
                + ", ".join(unprotected)
            )
        else:
            unsafe = [
                str(position.get("symbol"))
                for position in positions
                if (
                    position.get("positionStatus") not in {None, "", "Normal"}
                    or bool(position.get("isReduceOnly"))
                )
            ]
            if unsafe:
                block_reason = (
                    "Bybit ограничил позиции: "
                    + ", ".join(sorted(set(unsafe)))
                )
            elif any(
                order.get("reduceOnly") is not True
                for order in open_orders
            ):
                block_reason = (
                    "Есть активные увеличивающие позицию ордера"
                )

    return {
        "positions": positions,
        "account": account,
        "portfolio_risk": risk,
        "entry_block_reason": block_reason,
    }
