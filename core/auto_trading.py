"""Auto-trading orchestration with deterministic candidates and risk controls."""

from __future__ import annotations

import hashlib
import threading
import time
import traceback
from datetime import datetime, timezone
from decimal import Decimal, ROUND_DOWN, ROUND_UP
from typing import Any, Optional

from api.bybit_api import (
    BybitAPI,
    BybitAPIError,
    BybitOrderConfirmationError,
    BybitOrderNotFilledError,
    TERMINAL_ORDER_STATUSES,
)
from api.deepseek_api import DeepSeekAPI
from api.tg_notify import notify
from config import (
    DRY_RUN,
    BYBIT_MAX_SLIPPAGE_PERCENT,
    FALLBACK_TAKER_FEE_RATE,
    MAX_DAILY_LOSS_PERCENT,
    POLL_INTERVAL,
    TP_SL_MIN_CHANGE_PERCENT,
    TRADABLE_TOKENS,
    validate_config,
)
from core.decision_engine import (
    build_selector_prompt,
    build_trade_snapshot,
    selected_candidate,
    validate_trade_decision,
)
from core.market_data import get_market_analysis
from core.risk_engine import D, TradePlan, build_trade_plan, portfolio_risk_usd
from core.trade_journal import TradeJournal
from storage.database import get_store
from utils.helpers import parse_account_overview, validate_sl_vs_liquidation
from utils.logger_setup import logger


# All exchange mutations, including manual Telegram closes, share this lock.
EXECUTION_LOCK = threading.RLock()
FEE_REFRESH_SECONDS = 3_600
TRADE_HISTORY_SYNC_SECONDS = 15 * 60
MAX_SAFETY_CLOSE_ATTEMPTS = 3
SUPPORTED_AUTO_MARGIN_MODES = {"REGULAR_MARGIN"}
PARTIAL_TERMINAL_ORDER_STATUSES = {
    "PartiallyFilledCanceled",
    "PartiallyFilledCancelled",
}

_runtime_lock = threading.Lock()
_runtime: dict[str, Any] = {
    "state": "stopped",
    "iteration": 0,
    "last_cycle_at": None,
    "last_snapshot_id": None,
    "last_summary": "Ещё не запускался",
    "last_error": None,
}


class FatalExecutionError(RuntimeError):
    """A live position may be unsafe; automation must stop immediately."""


class ExecutionStopped(RuntimeError):
    """The owner stopped automation before a new entry was submitted."""


def execution_lock() -> threading.RLock:
    return EXECUTION_LOCK


def get_runtime_status() -> dict[str, Any]:
    with _runtime_lock:
        return dict(_runtime)


def _set_runtime(**values: Any) -> None:
    with _runtime_lock:
        _runtime.update(values)


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


def _open_position(
    bybit: BybitAPI,
    symbol: str,
    position_idx: int,
) -> Optional[dict[str, Any]]:
    rows = bybit.get_positions(symbol=symbol).get("result", {}).get("list", [])
    return next(
        (
            item
            for item in rows
            if int(item.get("positionIdx", 0)) == int(position_idx)
            and D(item.get("size", 0)) > 0
        ),
        None,
    )


def _confirmed_safety_flatten(
    bybit: BybitAPI,
    *,
    symbol: str,
    side: str,
    position_idx: int,
    reason: str,
    order_prefix: str,
) -> bool:
    """Close a position with bounded retries; return True only for DRY preview."""
    current_side = side
    last_error: Optional[Exception] = None
    for attempt in range(1, MAX_SAFETY_CLOSE_ATTEMPTS + 1):
        if current_side not in {"Buy", "Sell"}:
            raise FatalExecutionError(
                f"{symbol}: неизвестна сторона позиции при аварийном закрытии"
            )
        order_link_id = bybit.new_order_link_id(order_prefix)
        try:
            result = bybit.close_position_market(
                symbol,
                "Sell" if current_side == "Buy" else "Buy",
                position_idx,
                order_link_id=order_link_id,
            )
        except BybitOrderConfirmationError as error:
            # The reduce-only order can still be live.  A second order would
            # make the outcome harder to reconcile, so fail-stop immediately.
            raise FatalExecutionError(
                f"{symbol}: итог аварийного reduce-only ордера {order_link_id} неизвестен"
            ) from error
        except BybitOrderNotFilledError as error:
            # IOC is terminal, so it is safe to read the remainder and submit
            # a fresh reduce-only order with another stable ID.
            last_error = error
        except BybitAPIError as error:
            last_error = error
        except Exception as error:
            raise FatalExecutionError(
                f"{symbol}: аварийное закрытие завершилось неопределённой ошибкой"
            ) from error
        else:
            if result.get("simulated"):
                return True

        try:
            remainder = _open_position(bybit, symbol, position_idx)
        except Exception as error:
            raise FatalExecutionError(
                f"{symbol}: невозможно подтвердить остаток после аварийного закрытия"
            ) from error
        if not remainder:
            return False
        current_side = str(remainder.get("side", current_side))
        logger.warning(
            f"{symbol}: после safety-close попытки {attempt}/"
            f"{MAX_SAFETY_CLOSE_ATTEMPTS} остался size={remainder.get('size')}; "
            f"причина: {reason}"
        )

    raise FatalExecutionError(
        f"{symbol}: после {MAX_SAFETY_CLOSE_ATTEMPTS} confirmed safety-close "
        f"попыток позиция осталась открыта"
    ) from last_error


def _close_for_safety(
    bybit: BybitAPI,
    *,
    symbol: str,
    side: str,
    position_idx: int,
    reason: str,
    order_prefix: str,
) -> str:
    logger.critical(f"{symbol}: {reason}; выполняю confirmed reduce-only закрытие")
    preview = _confirmed_safety_flatten(
        bybit,
        symbol=symbol,
        side=side,
        position_idx=position_idx,
        reason=reason,
        order_prefix=order_prefix,
    )
    notify(
        f"[{symbol}] "
        f"{'🧪' if preview else '✅'} Safety-close: "
        f"{'закрытие рассчитано' if preview else 'позиция закрыта'}\n"
        f"Причина: {reason}"
    )
    return "closed"


def _manage_protection(
    bybit: BybitAPI,
    position: dict[str, Any],
    analysis: dict[str, Any],
) -> Optional[str]:
    """Tighten a stop deterministically; never widen it or update one side alone."""
    try:
        symbol = str(position["symbol"]).strip().upper()
        side = str(position["side"])
        position_idx = int(position.get("positionIdx", 0))
        mark = D(position.get("markPrice") or analysis.get("current_price") or 0)
        entry = D(position.get("avgPrice") or position.get("entryPrice") or 0)
        current_stop = D(position.get("stopLoss") or 0)
        current_target = D(position.get("takeProfit") or 0)
        liquidation = D(position.get("liqPrice") or 0)
    except (KeyError, TypeError, ValueError, BybitAPIError) as error:
        raise FatalExecutionError(
            "Bybit вернул позицию с повреждённым защитным состоянием"
        ) from error
    if not symbol:
        raise FatalExecutionError("Bybit вернул позицию без symbol")
    if side not in {"Buy", "Sell"}:
        raise FatalExecutionError(f"{symbol}: неизвестная сторона позиции {side!r}")
    mandatory_stop_repair = current_stop <= 0
    if mark <= 0:
        if mandatory_stop_repair:
            return _close_for_safety(
                bybit,
                symbol=symbol,
                side=side,
                position_idx=position_idx,
                reason="SL отсутствует, а mark price недоступен для безопасного расчёта",
                order_prefix="no-sl-exit",
            )
        return None
    liquidation_safe, liquidation_reason = validate_sl_vs_liquidation(
        side,
        float(current_stop),
        float(liquidation),
    )
    target_crossed = (
        side == "Buy" and current_target > 0 and mark >= current_target
    ) or (
        side == "Sell" and current_target > 0 and mark <= current_target
    )
    stop_crossed = (
        side == "Buy" and current_stop > 0 and mark <= current_stop
    ) or (
        side == "Sell" and current_stop > 0 and mark >= current_stop
    )
    unsafe_existing_stop = current_stop > 0 and not liquidation_safe
    if target_crossed or stop_crossed or unsafe_existing_stop:
        crossed = (
            "TP"
            if target_crossed
            else "SL"
            if stop_crossed
            else "SL у ликвидации"
        )
        anomaly = (
            f"mark пересёк {crossed}, но позиция осталась открыта"
            if target_crossed or stop_crossed
            else "защитный SL слишком близко к ликвидации"
        )
        outcome = _close_for_safety(
            bybit,
            symbol=symbol,
            side=side,
            position_idx=position_idx,
            reason=anomaly,
            order_prefix="guard-exit",
        )
        if liquidation_reason and unsafe_existing_stop:
            logger.warning(f"{symbol}: {liquidation_reason}")
        return outcome

    if not analysis.get("complete"):
        if mandatory_stop_repair:
            return _close_for_safety(
                bybit,
                symbol=symbol,
                side=side,
                position_idx=position_idx,
                reason="SL отсутствует, а market analysis неполон",
                order_prefix="no-sl-exit",
            )
        return None
    frame = analysis["timeframe_5m"]
    atr = D(frame["atr14"])
    if entry <= 0 or atr <= 0:
        if mandatory_stop_repair:
            return _close_for_safety(
                bybit,
                symbol=symbol,
                side=side,
                position_idx=position_idx,
                reason="SL отсутствует, а entry/ATR непригодны для безопасного расчёта",
                order_prefix="no-sl-exit",
            )
        return None

    if side == "Buy":
        structural = D(frame["swing_low"]) - atr * D("0.10")
        candidate_stop = min(structural, mark - atr * D("1.20"))
        effective_stop = max(current_stop, candidate_stop) if current_stop > 0 else candidate_stop
        target_needs_repair = current_target <= 0
        effective_target = (
            current_target
            if current_target > mark
            else mark + max(mark - effective_stop, atr) * 2
        )
    elif side == "Sell":
        structural = D(frame["swing_high"]) + atr * D("0.10")
        candidate_stop = max(structural, mark + atr * D("1.20"))
        effective_stop = min(current_stop, candidate_stop) if current_stop > 0 else candidate_stop
        target_needs_repair = current_target <= 0
        effective_target = (
            current_target
            if 0 < current_target < mark
            else mark - max(effective_stop - mark, atr) * 2
        )
    else:
        return None
    if effective_target <= 0:
        if mandatory_stop_repair:
            return _close_for_safety(
                bybit,
                symbol=symbol,
                side=side,
                position_idx=position_idx,
                reason="SL отсутствует, а корректный TP рассчитать невозможно",
                order_prefix="no-sl-exit",
            )
        return None

    try:
        take_profit, stop_loss = bybit.prepare_protective_prices(
            symbol,
            side,
            effective_target,
            effective_stop,
        )
    except Exception:
        if mandatory_stop_repair:
            return _close_for_safety(
                bybit,
                symbol=symbol,
                side=side,
                position_idx=position_idx,
                reason="SL отсутствует, а защитные цены не прошли правила Bybit",
                order_prefix="no-sl-exit",
            )
        raise
    if side == "Buy" and not 0 < stop_loss < mark < take_profit:
        if mandatory_stop_repair:
            return _close_for_safety(
                bybit,
                symbol=symbol,
                side=side,
                position_idx=position_idx,
                reason="SL отсутствует, а рассчитанные LONG TP/SL некорректны",
                order_prefix="no-sl-exit",
            )
        return None
    if side == "Sell" and not 0 < take_profit < mark < stop_loss:
        if mandatory_stop_repair:
            return _close_for_safety(
                bybit,
                symbol=symbol,
                side=side,
                position_idx=position_idx,
                reason="SL отсутствует, а рассчитанные SHORT TP/SL некорректны",
                order_prefix="no-sl-exit",
            )
        return None
    safe, reason = validate_sl_vs_liquidation(
        side,
        float(stop_loss),
        float(D(position.get("liqPrice") or 0)),
    )
    if not safe:
        logger.warning(f"{symbol}: защитный stop не обновлён: {reason}")
        if mandatory_stop_repair:
            return _close_for_safety(
                bybit,
                symbol=symbol,
                side=side,
                position_idx=position_idx,
                reason=f"SL отсутствует, а рассчитанный stop небезопасен: {reason}",
                order_prefix="no-sl-exit",
            )
        return None
    if current_stop > 0:
        change_percent = abs(stop_loss - current_stop) / current_stop * 100
        if (
            change_percent < D(TP_SL_MIN_CHANGE_PERCENT)
            and not target_needs_repair
        ):
            return None

    try:
        result = bybit.set_trading_stop_and_verify(
            symbol,
            position_idx,
            take_profit=take_profit,
            stop_loss=stop_loss,
        )
    except Exception as error:
        if not mandatory_stop_repair:
            raise
        logger.error(f"{symbol}: обязательная установка SL не подтверждена: {error}")
        return _close_for_safety(
            bybit,
            symbol=symbol,
            side=side,
            position_idx=position_idx,
            reason="обязательная установка отсутствующего SL не подтверждена",
            order_prefix="no-sl-exit",
        )
    preview = bool(result.get("simulated"))
    notify(
        f"[{symbol}] {'🧪 Защита рассчитана' if preview else '🛡 Защита подтверждена'}\n"
        f"TP: {take_profit} · SL: {stop_loss}"
    )
    return "protected"


def manage_existing_protection(
    bybit: BybitAPI,
    cycle: dict[str, Any],
    stop_event: threading.Event,
) -> list[str]:
    """Run code-owned position safety independently of any AI response."""
    actions: list[str] = []
    for position in cycle["positions"]:
        if stop_event.is_set():
            break
        symbol = str(position.get("symbol", ""))
        token = symbol.removesuffix("USDT")
        analysis = cycle["analyses"].get(token, {})
        with EXECUTION_LOCK:
            if stop_event.is_set():
                break
            outcome = _manage_protection(bybit, position, analysis)
        if outcome:
            actions.append(f"{outcome}:{symbol}")
    return actions


def _urgent_protection_preflight(
    bybit: BybitAPI,
    stop_event: threading.Event,
) -> list[str]:
    """Handle crossed, unsafe, or missing stops before any expensive analysis."""
    response = bybit.get_positions()
    result = response.get("result")
    rows = result.get("list") if isinstance(result, dict) else None
    if not isinstance(rows, list):
        raise FatalExecutionError("Bybit вернул повреждённый список позиций")
    try:
        positions = [item for item in rows if D(item.get("size", 0)) > 0]
    except (AttributeError, TypeError, ValueError, BybitAPIError) as error:
        raise FatalExecutionError(
            "Невозможно проверить срочное защитное состояние позиций"
        ) from error
    return manage_existing_protection(
        bybit,
        {"positions": positions, "analyses": {}},
        stop_event,
    )


def _emergency_flatten_entry(
    bybit: BybitAPI,
    *,
    symbol: str,
    side: str,
    position_idx: int,
    reason: str,
    require_position: bool,
) -> bool:
    """Confirm a newly created position is flat or stop on any uncertainty."""
    position: Optional[dict[str, Any]] = None
    if require_position:
        try:
            position = bybit.wait_for_position(
                symbol,
                position_idx,
                lambda item: D(item.get("size", 0)) > 0,
            )
        except Exception as error:
            raise FatalExecutionError(
                f"{symbol}: Bybit сообщил об исполнении, но позиция не подтверждена"
            ) from error
    else:
        try:
            position = _open_position(bybit, symbol, position_idx)
        except Exception as error:
            raise FatalExecutionError(
                f"{symbol}: невозможно проверить позицию после неопределённого ордера"
            ) from error
    if not position:
        return False
    logger.critical(f"{symbol}: {reason}; выполняю подтверждённое аварийное закрытие")
    _confirmed_safety_flatten(
        bybit,
        symbol=symbol,
        side=side,
        position_idx=position_idx,
        reason=reason,
        order_prefix="entry-exit",
    )
    return True


def _terminal_order_executed(order: dict[str, Any]) -> bool:
    """Interpret terminal execution evidence without treating unknown as zero."""
    if not isinstance(order, dict):
        raise FatalExecutionError(
            "Bybit вернул повреждённый итог entry-ордера"
        )
    status = order.get("orderStatus")
    if not isinstance(status, str) or status not in TERMINAL_ORDER_STATUSES:
        raise FatalExecutionError(
            f"Bybit вернул неизвестный итог entry-ордера: {status!r}"
        )
    raw_executed = order.get("cumExecQty")
    if (
        raw_executed is None
        or raw_executed == ""
        or isinstance(raw_executed, bool)
    ):
        raise FatalExecutionError(
            "Bybit не вернул подтверждённый cumExecQty entry-ордера"
        )
    try:
        executed = D(raw_executed)
    except ValueError as error:
        raise FatalExecutionError(
            "Bybit вернул некорректный cumExecQty entry-ордера"
        ) from error
    if executed < 0:
        raise FatalExecutionError(
            "Bybit вернул отрицательный cumExecQty entry-ордера"
        )
    return (
        status == "Filled"
        or status in PARTIAL_TERMINAL_ORDER_STATUSES
        or executed > 0
    )


def _execute_candidate(
    bybit: BybitAPI,
    candidate: dict[str, Any],
    cycle: dict[str, Any],
    fee_rates: dict[str, Decimal],
    stop_event: threading.Event,
    *,
    journal: Optional[TradeJournal] = None,
    decision_item: Optional[dict[str, Any]] = None,
) -> TradePlan:
    symbol = str(candidate["symbol"])
    try:
        valid_until = datetime.fromisoformat(
            str(cycle["snapshot"]["valid_until"]).replace("Z", "+00:00")
        )
    except (KeyError, ValueError) as error:
        raise ValueError("Snapshot не содержит корректный valid_until") from error
    if datetime.now(timezone.utc) >= valid_until:
        raise ValueError("Snapshot устарел до начала исполнения")

    fresh = _fresh_entry_state(bybit, fee_rates)
    if fresh["entry_block_reason"]:
        raise ValueError(
            f"Свежий entry gate заблокировал вход: {fresh['entry_block_reason']}"
        )
    ticker_response = bybit.get_tickers(symbol)
    ticker = ticker_response.get("result", {}).get("list", [None])[0]
    if not ticker:
        raise BybitAPIError(f"Не удалось перепроверить ticker {symbol}")
    rules = bybit.get_instrument_rules(symbol, refresh=True)

    def build_current_plan(state: dict[str, Any]) -> TradePlan:
        available_usd = D(state["account"]["available_usd"])
        portfolio_risk = D(state["portfolio_risk"])
        if DRY_RUN:
            # DRY writes do not change Bybit state.  Preserve reservations
            # from earlier previews in this cycle.
            cycle_account = cycle.get("account") or {}
            available_usd = min(
                available_usd,
                D(cycle_account.get("available_usd", available_usd)),
            )
            portfolio_risk = max(
                portfolio_risk,
                D(cycle.get("portfolio_risk", portfolio_risk)),
            )
        return build_trade_plan(
            candidate,
            rules=rules,
            ticker=ticker,
            equity_usd=state["account"]["equity_usd"],
            available_usd=available_usd,
            current_portfolio_risk_usd=portfolio_risk,
            taker_fee_rate=fee_rates.get(
                symbol,
                D(FALLBACK_TAKER_FEE_RATE),
            ),
        )

    preliminary_plan = build_current_plan(fresh)
    if any(
        position.get("symbol") == symbol
        for position in fresh["positions"]
    ):
        raise ValueError(f"{symbol}: позиция уже существует; разворот запрещён")
    rows = bybit.get_positions(symbol=symbol).get("result", {}).get("list", [])
    if any(D(position.get("size", 0)) > 0 for position in rows):
        raise ValueError(f"{symbol}: позиция появилась перед отправкой ордера")
    position_idx = 0
    if rows and any(int(position.get("positionIdx", 0)) > 0 for position in rows):
        position_idx = 1 if preliminary_plan.side == "Buy" else 2

    if datetime.now(timezone.utc) >= valid_until:
        raise ValueError("Snapshot устарел до изменения leverage")
    if stop_event.is_set():
        raise ExecutionStopped("Авто-режим остановлен до изменения leverage")
    try:
        bybit.set_leverage(
            symbol,
            preliminary_plan.leverage,
            preliminary_plan.leverage,
        )
    except BybitAPIError as error:
        if error.code != 110043:
            raise
    if datetime.now(timezone.utc) >= valid_until:
        raise ValueError("Snapshot устарел непосредственно перед отправкой ордера")
    if stop_event.is_set():
        raise ExecutionStopped("Авто-режим остановлен до отправки entry-ордера")

    # These account-wide reads happen after the leverage mutation and remain
    # adjacent to create-order.  The plan is recalculated from their values.
    final_state = _final_entry_state(bybit, fee_rates)
    if final_state["entry_block_reason"]:
        raise ValueError(
            "Финальная проверка экспозиции заблокировала вход: "
            f"{final_state['entry_block_reason']}"
        )
    if any(
        position.get("symbol") == symbol
        for position in final_state["positions"]
    ):
        raise ValueError(f"{symbol}: позиция появилась перед отправкой ордера")
    plan = build_current_plan(final_state)
    if plan.leverage != preliminary_plan.leverage:
        raise ValueError(
            f"{symbol}: требуемое leverage изменилось при финальной "
            "проверке; вход отменён"
        )

    slippage = D(BYBIT_MAX_SLIPPAGE_PERCENT) / 100
    if plan.side == "Buy":
        entry_limit = rules.price(
            plan.entry_price * (Decimal("1") + slippage),
            ROUND_DOWN,
        )
        if not plan.stop_loss < entry_limit < plan.take_profit:
            raise ValueError(f"{symbol}: price cap конфликтует с TP/SL")
    else:
        entry_limit = rules.price(
            plan.entry_price * (Decimal("1") - slippage),
            ROUND_UP,
        )
        if not plan.take_profit < entry_limit < plan.stop_loss:
            raise ValueError(f"{symbol}: price floor конфликтует с TP/SL")

    if datetime.now(timezone.utc) >= valid_until:
        raise ValueError("Snapshot устарел непосредственно перед отправкой ордера")
    if stop_event.is_set():
        raise ExecutionStopped("Авто-режим остановлен до отправки entry-ордера")

    order_link_id = f"open-{plan.candidate_id}"[:36]
    if journal is not None:
        # This is intentionally the last durable operation before create-order.
        # If it fails, a LIVE order must not be sent: otherwise a later audit
        # could never reconstruct the exact plan approved by the risk engine.
        journal.prepare_entry(
            candidate=candidate,
            plan=plan,
            cycle=cycle,
            decision=decision_item,
            order_link_id=order_link_id,
            sizing_context={
                "entry_limit": str(entry_limit),
                "position_idx": position_idx,
                "taker_fee_rate": str(
                    fee_rates.get(symbol, D(FALLBACK_TAKER_FEE_RATE))
                ),
                "equity_usd": str(final_state["account"]["equity_usd"]),
                "available_usd": str(final_state["account"]["available_usd"]),
                "portfolio_risk_usd": str(final_state["portfolio_risk"]),
                "instrument": {
                    "tick_size": str(rules.tick_size),
                    "min_qty": str(rules.min_qty),
                    "qty_step": str(rules.qty_step),
                    "min_notional": str(rules.min_notional),
                    "max_market_qty": str(rules.max_market_qty),
                    "max_leverage": str(rules.max_leverage),
                    "leverage_step": str(rules.leverage_step),
                },
            },
            dry_run=DRY_RUN,
        )

    def update_journal(**changes: Any) -> None:
        if journal is None:
            return
        try:
            journal.update_setup(plan.candidate_id, **changes)
        except Exception as error:
            # Once an exchange mutation has happened, journal availability may
            # never interrupt position confirmation, protection, or flattening.
            logger.error(
                f"{symbol}: не удалось обновить trade journal после entry: {error}"
            )

    if datetime.now(timezone.utc) >= valid_until:
        update_journal(
            status="failed",
            last_error="Snapshot устарел во время записи trade journal",
        )
        raise ValueError("Snapshot устарел непосредственно перед отправкой ордера")
    if stop_event.is_set():
        update_journal(
            status="stopped",
            last_error="Авто-режим остановлен до отправки entry-ордера",
        )
        raise ExecutionStopped("Авто-режим остановлен до отправки entry-ордера")

    try:
        result = bybit.place_order_and_confirm(
            symbol=symbol,
            side=plan.side,
            # Aggressive IOC limit behaves like a marketable order while
            # bounding the fill price and still allowing attached Full TP/SL.
            order_type="Limit",
            qty=plan.quantity,
            price=entry_limit,
            time_in_force="IOC",
            take_profit=plan.take_profit,
            stop_loss=plan.stop_loss,
            position_idx=position_idx,
            order_link_id=order_link_id,
        )
    except BybitOrderNotFilledError as error:
        try:
            executed = _terminal_order_executed(error.order)
        except FatalExecutionError:
            update_journal(
                status="reconcile_required",
                entry_order_id=error.order.get("orderId"),
                last_error=str(error)[:500],
            )
            _emergency_flatten_entry(
                bybit,
                symbol=symbol,
                side=plan.side,
                position_idx=position_idx,
                reason="неизвестный итог исполнения входа",
                require_position=False,
            )
            raise
        if executed:
            update_journal(
                status="reconcile_required",
                entry_order_id=error.order.get("orderId"),
                actual_entry_qty=error.order.get("cumExecQty"),
                actual_entry_price=error.order.get("avgPrice"),
                opened_at_ms=(
                    error.order.get("updatedTime")
                    or error.order.get("createdTime")
                    or int(time.time() * 1_000)
                ),
                last_error=str(error)[:500],
            )
            _emergency_flatten_entry(
                bybit,
                symbol=symbol,
                side=plan.side,
                position_idx=position_idx,
                reason="частичное исполнение входа",
                require_position=True,
            )
        else:
            update_journal(
                status="not_filled",
                entry_order_id=error.order.get("orderId"),
                last_error=str(error)[:500],
            )
        raise
    except BybitOrderConfirmationError as error:
        update_journal(
            status="reconcile_required",
            entry_order_link_id=error.order_link_id,
            entry_order_id=error.order.get("orderId"),
            actual_entry_qty=error.order.get("cumExecQty"),
            actual_entry_price=error.order.get("avgPrice"),
            last_error=str(error)[:500],
        )
        try:
            final = bybit.cancel_order_and_confirm(
                symbol=symbol,
                order_link_id=error.order_link_id,
            )
        except Exception as cancel_error:
            # Flatten anything already visible, but remain fail-stopped because
            # the still-unknown order could fill later.
            _emergency_flatten_entry(
                bybit,
                symbol=symbol,
                side=plan.side,
                position_idx=position_idx,
                reason="неопределённый вход",
                require_position=False,
            )
            raise FatalExecutionError(
                f"{symbol}: итог входа и его отмены не подтверждены"
            ) from cancel_error
        try:
            executed = _terminal_order_executed(final)
        except FatalExecutionError:
            _emergency_flatten_entry(
                bybit,
                symbol=symbol,
                side=plan.side,
                position_idx=position_idx,
                reason="неизвестный итог отменённого входа",
                require_position=False,
            )
            raise
        update_journal(
            status="reconcile_required" if executed else "not_filled",
            entry_order_id=final.get("orderId"),
            entry_order_link_id=final.get("orderLinkId") or error.order_link_id,
            actual_entry_qty=final.get("cumExecQty"),
            actual_entry_price=final.get("avgPrice"),
            opened_at_ms=(
                (
                    final.get("updatedTime")
                    or final.get("createdTime")
                    or int(time.time() * 1_000)
                )
                if executed
                else None
            ),
            last_error=str(error)[:500],
        )
        _emergency_flatten_entry(
            bybit,
            symbol=symbol,
            side=plan.side,
            position_idx=position_idx,
            reason="вход после неопределённого подтверждения",
            require_position=executed,
        )
        raise
    except Exception as error:
        update_journal(status="failed", last_error=str(error)[:500])
        raise
    if result.get("simulated"):
        update_journal(status="previewed")
        notify(
            f"[{symbol}] 🧪 PREVIEW {plan.side}\n"
            f"qty {plan.quantity} · entry ≈ {plan.entry_price} · cap {entry_limit}\n"
            f"TP {plan.take_profit} · SL {plan.stop_loss}\n"
            f"risk ${plan.risk_usd:.2f} · net R/R {plan.net_risk_reward:.2f}"
        )
        return plan

    update_journal(
        status="entry_filled",
        entry_order_id=result.get("orderId"),
        entry_order_link_id=result.get("orderLinkId") or order_link_id,
        actual_entry_qty=result.get("cumExecQty") or plan.quantity,
        actual_entry_price=result.get("avgPrice") or plan.entry_price,
        opened_at_ms=(
            result.get("updatedTime")
            or result.get("createdTime")
            or int(time.time() * 1_000)
        ),
    )

    try:
        position = bybit.wait_for_position(
            symbol,
            position_idx,
            lambda item: D(item.get("size", 0)) > 0,
        )
    except Exception as position_error:
        update_journal(
            status="reconcile_required",
            last_error=str(position_error)[:500],
        )
        # A Filled order without a visible position is an inconsistent state.
        # Stop the worker instead of proceeding to another cycle.
        raise FatalExecutionError(
            f"{symbol}: fill подтверждён, но позиция недоступна для защиты"
        ) from position_error
    stop_safe, stop_reason = validate_sl_vs_liquidation(
        plan.side,
        float(plan.stop_loss),
        float(D(position.get("liqPrice") or 0)),
    )
    if not stop_safe:
        update_journal(
            status="reconcile_required",
            actual_entry_qty=position.get("size"),
            actual_entry_price=position.get("avgPrice"),
            last_error=str(stop_reason)[:500],
        )
        _emergency_flatten_entry(
            bybit,
            symbol=symbol,
            side=plan.side,
            position_idx=position_idx,
            reason=f"расчётный SL небезопасен: {stop_reason}",
            require_position=False,
        )
        raise BybitAPIError(f"{symbol}: расчётный SL небезопасен: {stop_reason}")
    protected = (
        D(position.get("takeProfit") or 0) == plan.take_profit
        and D(position.get("stopLoss") or 0) == plan.stop_loss
    )
    if not protected:
        try:
            position = bybit.set_trading_stop_and_verify(
                symbol,
                position_idx,
                take_profit=plan.take_profit,
                stop_loss=plan.stop_loss,
            )
            protected = not position.get("simulated")
        except Exception as protection_error:
            update_journal(
                status="reconcile_required",
                actual_entry_qty=position.get("size"),
                actual_entry_price=position.get("avgPrice"),
                last_error=str(protection_error)[:500],
            )
            # A filled but unprotected position is more dangerous than a
            # missed setup.  Attempt a confirmed reduce-only exit.
            logger.critical(f"{symbol}: protection failed; emergency close")
            _confirmed_safety_flatten(
                bybit,
                symbol=symbol,
                side=plan.side,
                position_idx=position_idx,
                reason="защита новой позиции не подтвердилась",
                order_prefix="protection-exit",
            )
            raise protection_error
    if not protected:
        update_journal(
            status="reconcile_required",
            last_error="Bybit не подтвердил TP/SL новой позиции",
        )
        raise BybitAPIError(f"{symbol}: защита позиции не подтверждена")
    update_journal(
        status="open",
        actual_entry_qty=position.get("size") or result.get("cumExecQty"),
        actual_entry_price=position.get("avgPrice") or result.get("avgPrice"),
        opened_at_ms=(
            result.get("updatedTime")
            or result.get("createdTime")
            or int(time.time() * 1_000)
        ),
        last_error="",
    )
    notify(
        f"[{symbol}] ✅ {plan.side} исполнен и защищён\n"
        f"qty {plan.quantity} · fill {result.get('avgPrice') or plan.entry_price}\n"
        f"TP {plan.take_profit} · SL {plan.stop_loss}\n"
        f"risk ${plan.risk_usd:.2f} · net R/R {plan.net_risk_reward:.2f}"
    )
    return plan


def execute_decisions(
    bybit: BybitAPI,
    decision: dict[str, Any],
    cycle: dict[str, Any],
    fee_rates: dict[str, Decimal],
    stop_event: threading.Event,
    *,
    journal: Optional[TradeJournal] = None,
) -> list[str]:
    """Serialize code-approved candidate entries and reserve each signal once."""
    actions: list[str] = []
    decisions = decision["decisions"]

    if cycle["entry_block_reason"]:
        logger.warning(f"Новые входы заблокированы: {cycle['entry_block_reason']}")
        return actions

    store = journal.store if journal is not None else get_store()
    trade_journal = journal or TradeJournal(bybit, store)

    for item in decisions:
        if item["action"] != "select_candidate" or stop_event.is_set():
            continue
        candidate = selected_candidate(item, cycle["snapshot"])
        if not candidate:
            continue
        if not store.reserve_execution_signal(candidate["id"], candidate["symbol"]):
            logger.info(f"{candidate['symbol']}: кандидат уже обрабатывался")
            continue
        try:
            with EXECUTION_LOCK:
                if stop_event.is_set():
                    store.update_execution_signal(candidate["id"], "stopped")
                    return actions
                plan = _execute_candidate(
                    bybit,
                    candidate,
                    cycle,
                    fee_rates,
                    stop_event,
                    journal=trade_journal,
                    decision_item=item,
                )
            store.update_execution_signal(
                candidate["id"],
                "previewed" if DRY_RUN else "filled",
            )
            actions.append(f"{'preview' if DRY_RUN else 'opened'}:{plan.symbol}")
            cycle["portfolio_risk"] += plan.risk_usd
            cycle["account"]["available_usd"] = max(
                0.0,
                float(D(cycle["account"]["available_usd"]) - plan.margin_with_buffer),
            )
        except ExecutionStopped:
            store.update_execution_signal(candidate["id"], "stopped")
            return actions
        except Exception:
            store.update_execution_signal(candidate["id"], "failed")
            raise
    return actions


def _wait(stop_event: threading.Event, seconds: int) -> bool:
    return stop_event.wait(max(1, seconds))


def main_loop(
    stop_event: Optional[threading.Event] = None,
    *,
    once: bool = False,
) -> None:
    """Run until stopped; check the stop event before every exchange mutation."""
    event = stop_event or threading.Event()
    errors = validate_config("auto")
    if errors:
        message = "Некорректная конфигурация: " + "; ".join(errors)
        _set_runtime(
            state="stopped",
            last_error=message[:300],
            last_summary="Авто-режим заблокирован конфигурацией",
        )
        raise ValueError(message)
    _set_runtime(state="starting", last_error=None)
    bybit: Optional[BybitAPI] = None
    deepseek: Optional[DeepSeekAPI] = None
    trade_journal: Optional[TradeJournal] = None
    fatal_error: Optional[FatalExecutionError] = None
    try:
        bybit = BybitAPI()
        _set_runtime(state="running")
        notify(f"🤖 Авто-режим запущен · {'DRY preview' if DRY_RUN else 'LIVE'}")
        pending_preflight = _urgent_protection_preflight(bybit, event)
        if any(item.startswith("closed:") for item in pending_preflight):
            summary = "Срочные защитные действия: " + ", ".join(
                pending_preflight
            )
            _set_runtime(last_summary=summary)
            logger.warning(summary)
            if once or _wait(event, POLL_INTERVAL):
                return
            pending_preflight = None
        if event.is_set():
            return
        deepseek = DeepSeekAPI()
        deepseek.validate_model()
        fees = _fee_rates(bybit)
        fees_refreshed_at = time.monotonic()
        trade_history_refreshed_at = 0.0
        # Model validation and fee reads may take time; never reuse the
        # startup safety snapshot for the first trading cycle.
        pending_preflight = None
        iteration = 0
        while not event.is_set():
            iteration += 1
            _set_runtime(
                iteration=iteration,
                last_cycle_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
                last_error=None,
            )
            try:
                urgent_actions = pending_preflight
                pending_preflight = None
                if urgent_actions is None:
                    urgent_actions = _urgent_protection_preflight(bybit, event)
                if any(item.startswith("closed:") for item in urgent_actions):
                    summary = "Срочные защитные действия: " + ", ".join(
                        urgent_actions
                    )
                    _set_runtime(last_summary=summary)
                    logger.warning(summary)
                    if once or _wait(event, POLL_INTERVAL):
                        break
                    continue
                if time.monotonic() - fees_refreshed_at >= FEE_REFRESH_SECONDS:
                    fees = _fee_rates(bybit, previous=fees)
                    fees_refreshed_at = time.monotonic()
                if event.is_set():
                    break
                cycle = collect_cycle(bybit, fees)
                safety_actions = urgent_actions + manage_existing_protection(
                    bybit,
                    cycle,
                    event,
                )
                if (
                    time.monotonic() - trade_history_refreshed_at
                    >= TRADE_HISTORY_SYNC_SECONDS
                ):
                    trade_history_refreshed_at = time.monotonic()
                    try:
                        if trade_journal is None:
                            trade_journal = TradeJournal(bybit, get_store())
                        trade_journal.record_equity(
                            cycle["account"],
                            source="auto_cycle",
                        )
                        # A short rolling sync keeps completed trades durable
                        # even when nobody opens the Telegram history screen.
                        # Longer backfills are loaded on demand by that screen.
                        trade_journal.sync_closed_pnl(lookback_days=7)
                    except Exception as history_error:
                        logger.warning(
                            "Не удалось обновить локальную историю сделок; "
                            f"торговая безопасность не затронута: {history_error}"
                        )
                if any(item.startswith("closed:") for item in safety_actions):
                    summary = "Защитные действия: " + ", ".join(safety_actions)
                    _set_runtime(last_summary=summary)
                    logger.warning(summary)
                    if once or _wait(event, POLL_INTERVAL):
                        break
                    continue
                snapshot = cycle["snapshot"]
                candidate_count = sum(
                    len(item.get("candidates", []))
                    for item in snapshot["symbols"].values()
                )
                _set_runtime(
                    last_snapshot_id=snapshot["snapshot_id"],
                    last_summary=(
                        f"Кандидатов: {candidate_count}"
                        + (
                            f" · входы заблокированы: {cycle['entry_block_reason']}"
                            if cycle["entry_block_reason"]
                            else ""
                        )
                    ),
                )
                if not candidate_count:
                    logger.info("Нет детерминированных кандидатов; AI-вызов не нужен")
                else:
                    raw = deepseek.analyze(build_selector_prompt(), snapshot)
                    decision = validate_trade_decision(raw, snapshot)
                    if event.is_set():
                        logger.info("Stop получен после AI; торговые действия отменены")
                        break
                    actions = safety_actions + execute_decisions(
                        bybit,
                        decision,
                        cycle,
                        fees,
                        event,
                        journal=trade_journal,
                    )
                    summary = "Действия: " + (", ".join(actions) if actions else "нет")
                    _set_runtime(last_summary=summary)
                    logger.info(summary)
            except Exception as error:
                _set_runtime(last_error=str(error)[:300], last_summary="Цикл завершён с ошибкой")
                logger.error(f"Ошибка авто-цикла: {error}")
                logger.debug(traceback.format_exc())
                if isinstance(error, FatalExecutionError):
                    fatal_error = error
                    event.set()
                try:
                    notify(f"⚠️ Авто-цикл остановлен до следующей проверки: {error}")
                    if isinstance(error, FatalExecutionError):
                        notify(
                            "🛑 Авто-режим аварийно остановлен: "
                            "возможна незащищённая позиция"
                        )
                except Exception as notify_error:
                    logger.warning(
                        f"Не удалось отправить уведомление об ошибке auto: {notify_error}"
                    )
            if once or _wait(event, POLL_INTERVAL):
                break
    except Exception as error:
        _set_runtime(
            last_error=str(error)[:300],
            last_summary="Авто-режим не запущен",
        )
        logger.error(f"Ошибка запуска авто-режима: {error}")
        raise
    finally:
        _set_runtime(state="stopped")
        if deepseek is not None:
            try:
                deepseek.close()
            except Exception as error:
                logger.warning(f"Не удалось закрыть DeepSeek session: {error}")
        if bybit is not None:
            try:
                bybit.close()
            except Exception as error:
                logger.warning(f"Не удалось закрыть Bybit session: {error}")
        try:
            notify("⏹ Авто-режим остановлен")
        except Exception as error:
            logger.warning(f"Не удалось отправить stop notification: {error}")
    if fatal_error is not None:
        raise fatal_error


if __name__ == "__main__":
    try:
        main_loop()
    except KeyboardInterrupt:
        logger.info("Авто-режим остановлен пользователем")
