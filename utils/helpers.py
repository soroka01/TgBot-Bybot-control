"""Shared validation, calculations, and presentation helpers.

The exchange returns most numeric values as strings.  Keeping conversion and
risk checks here avoids slightly different formulas in the auto trader and the
Telegram interface.
"""

from __future__ import annotations

import json
import math
from decimal import Decimal, ROUND_DOWN
from typing import Any, Dict, Iterable, List, Optional, Tuple

from loguru import logger
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from config import (
    MAX_LEVERAGE,
    MAX_RISK_PER_TRADE_PERCENT,
    MAX_TOTAL_RISK_PERCENT,
    MIN_ORDER_SIZE_USDT,
    SYMBOL_LIMITS,
)


MIN_RISK_REWARD_RATIO = 1.5


def to_float(value: Any, default: float = 0.0) -> float:
    """Convert an API value to a finite float without leaking conversion errors."""
    try:
        result = float(value)
    except (TypeError, ValueError):
        return default
    return result if math.isfinite(result) else default


def format_price(value: Any) -> str:
    """Format a price without hiding meaningful decimals for low-priced coins."""
    price = to_float(value)
    if price >= 1_000:
        decimals = 2
    elif price >= 1:
        decimals = 4
    else:
        decimals = 6
    return f"${price:,.{decimals}f}"


def calculate_position_roi(
    unrealized_pnl: float,
    quantity: float,
    entry_price: float,
    leverage: float,
) -> float:
    """Return ROI on initial margin, rather than PnL as a share of notional."""
    notional = to_float(quantity) * to_float(entry_price)
    margin = notional / to_float(leverage, 1.0) if leverage else 0.0
    return to_float(unrealized_pnl) / margin * 100 if margin > 0 else 0.0


def parse_account_overview(wallet_response: dict, max_leverage: int = MAX_LEVERAGE) -> dict:
    """Build one account view from a Bybit Unified Account wallet response.

    For UTA cross/portfolio accounts the account-level ``totalAvailableBalance``
    is authoritative.  The old per-USDT ``equity - totalPositionIM`` formula
    ignores order margin and can be wrong when multiple collateral coins exist.
    """
    result_list = wallet_response.get("result", {}).get("list", [])
    if not result_list:
        raise ValueError("Bybit did not return an account balance")

    account = result_list[0]
    coins = account.get("coin", [])
    usdt = next((coin for coin in coins if coin.get("coin") == "USDT"), {})

    equity = to_float(account.get("totalEquity"), to_float(usdt.get("equity")))
    wallet_balance = to_float(
        account.get("totalWalletBalance"), to_float(usdt.get("walletBalance"))
    )
    unrealized_pnl = to_float(
        account.get("totalPerpUPL"), to_float(usdt.get("unrealisedPnl"))
    )
    position_margin = to_float(
        account.get("totalInitialMargin"), to_float(usdt.get("totalPositionIM"))
    )
    order_margin = to_float(account.get("totalOrderIM"), to_float(usdt.get("totalOrderIM")))
    available_fallback = max(0.0, equity - position_margin - order_margin)
    available = max(0.0, to_float(account.get("totalAvailableBalance"), available_fallback))

    return {
        "balance_usd": wallet_balance,
        "available_usd": available,
        "equity_usd": equity,
        "position_margin_usd": position_margin,
        "order_margin_usd": order_margin,
        "unrealized_pnl_usd": unrealized_pnl,
        "max_leverage": max_leverage,
    }


def round_quantity(token: str, quantity: float) -> float:
    """Round a quantity *down* to the exchange lot step using Decimal math."""
    limits = SYMBOL_LIMITS.get(token.upper())
    if not limits:
        logger.warning(f"Токен {token} не найден в SYMBOL_LIMITS")
        return 0.0

    requested = Decimal(str(quantity))
    step = Decimal(str(limits["qty_step"]))
    minimum = Decimal(str(limits["min_qty"]))
    if requested <= 0:
        return 0.0

    rounded = (requested / step).to_integral_value(rounding=ROUND_DOWN) * step
    if rounded < minimum:
        logger.warning(f"Количество {rounded} меньше минимума {minimum} для {token}")
        return 0.0
    return float(rounded)


def validate_trade_risk(
    quantity: float,
    price: float,
    stop_loss: float,
    leverage: int,
    available_balance: float,
    total_risk_usd: float = 0.0,
    *,
    side: str,
    profit_target: float,
    risk_budget_usd: Optional[float] = None,
) -> Tuple[bool, Optional[str]]:
    """Validate an entry using actual stop-distance risk and available margin.

    ``available_balance`` is used only for the margin check.  Risk limits use
    account equity when supplied via ``risk_budget_usd``.
    """
    quantity = to_float(quantity)
    price = to_float(price)
    stop_loss = to_float(stop_loss)
    profit_target = to_float(profit_target)
    available_balance = to_float(available_balance)
    total_risk_usd = max(0.0, to_float(total_risk_usd))
    risk_capital = to_float(risk_budget_usd, available_balance)

    if quantity <= 0 or price <= 0:
        return False, "Количество и текущая цена должны быть больше нуля"
    if leverage < 1 or leverage > MAX_LEVERAGE:
        return False, f"Плечо {leverage} вне допустимого диапазона 1–{MAX_LEVERAGE}x"
    if available_balance <= 0:
        return False, "Нет доступного баланса для новой позиции"
    if risk_capital <= 0:
        return False, "Невозможно рассчитать риск: equity равен нулю"

    if side == "Buy":
        if not 0 < stop_loss < price:
            return False, "Для LONG Stop Loss должен быть ниже текущей цены"
        if profit_target <= price:
            return False, "Для LONG Take Profit должен быть выше текущей цены"
        risk_per_coin = price - stop_loss
        reward_per_coin = profit_target - price
    elif side == "Sell":
        if stop_loss <= price:
            return False, "Для SHORT Stop Loss должен быть выше текущей цены"
        if not 0 < profit_target < price:
            return False, "Для SHORT Take Profit должен быть ниже текущей цены"
        risk_per_coin = stop_loss - price
        reward_per_coin = price - profit_target
    else:
        return False, f"Неизвестная сторона сделки: {side}"

    position_value = quantity * price
    if position_value < MIN_ORDER_SIZE_USDT:
        return False, f"Размер ордера слишком мал: ${position_value:.2f} < ${MIN_ORDER_SIZE_USDT:.2f}"

    margin_required = position_value / leverage
    margin_with_buffer = margin_required * 1.10
    if margin_with_buffer > available_balance:
        return False, (
            f"Недостаточно средств: нужно ${margin_required:.2f} "
            f"(с 10% буфером ${margin_with_buffer:.2f}), доступно ${available_balance:.2f}"
        )

    risk_usd = risk_per_coin * quantity
    risk_percent = risk_usd / risk_capital * 100
    if risk_percent > MAX_RISK_PER_TRADE_PERCENT:
        return False, f"Риск {risk_percent:.2f}% превышает лимит {MAX_RISK_PER_TRADE_PERCENT}%"

    total_risk_percent = (total_risk_usd + risk_usd) / risk_capital * 100
    if total_risk_percent > MAX_TOTAL_RISK_PERCENT:
        return False, f"Общий риск {total_risk_percent:.2f}% превышает лимит {MAX_TOTAL_RISK_PERCENT}%"

    risk_reward_ratio = reward_per_coin / risk_per_coin
    if risk_reward_ratio < MIN_RISK_REWARD_RATIO:
        return False, (
            f"R/R {risk_reward_ratio:.2f} ниже минимального {MIN_RISK_REWARD_RATIO:.1f}"
        )

    return True, None


def validate_sl_vs_liquidation(
    side: str,
    stop_loss: float,
    liquidation_price: float,
    min_distance_percent: float = 5.0,
) -> Tuple[bool, Optional[str]]:
    """Ensure a stop is on the safe side of liquidation with a price buffer.

    A long stop must be *above* liquidation; a short stop must be *below* it.
    The former implementation checked the inverse relation, allowing stops
    beyond liquidation and rejecting only some unsafe values.
    """
    stop_loss = to_float(stop_loss)
    liquidation_price = to_float(liquidation_price)
    if liquidation_price <= 0 or stop_loss <= 0:
        return True, None

    if side == "Buy":
        minimum_safe_stop = liquidation_price * (1 + min_distance_percent / 100)
        if stop_loss < minimum_safe_stop:
            return False, (
                f"SL {stop_loss:.6g} должен быть не ниже {minimum_safe_stop:.6g}: "
                f"ликвидация {liquidation_price:.6g} + буфер {min_distance_percent:.1f}%"
            )
    elif side == "Sell":
        maximum_safe_stop = liquidation_price * (1 - min_distance_percent / 100)
        if stop_loss > maximum_safe_stop:
            return False, (
                f"SL {stop_loss:.6g} должен быть не выше {maximum_safe_stop:.6g}: "
                f"ликвидация {liquidation_price:.6g} - буфер {min_distance_percent:.1f}%"
            )
    else:
        return False, f"Неизвестная сторона позиции: {side}"
    return True, None


def calculate_position_risk(positions: Iterable[dict]) -> float:
    """Calculate loss to valid stops for open positions in USD.

    Positions without a stop are deliberately excluded from the number because
    their downside is not measurable.  Use :func:`find_unprotected_positions`
    to block new auto entries until they are protected.
    """
    total_risk = 0.0
    for position in positions:
        quantity = abs(to_float(position.get("size", position.get("quantity"))))
        entry = to_float(position.get("entryPrice", position.get("entry_price")))
        stop = to_float(position.get("stopLoss", position.get("stop_loss")))
        side = position.get("side", "")
        if quantity <= 0 or entry <= 0 or stop <= 0:
            continue
        if side == "Buy":
            total_risk += max(0.0, entry - stop) * quantity
        elif side == "Sell":
            total_risk += max(0.0, stop - entry) * quantity
        else:
            total_risk += abs(entry - stop) * quantity
    return total_risk


def find_unprotected_positions(positions: Iterable[dict]) -> List[str]:
    """Return symbols of open positions that do not have a valid protective SL."""
    unprotected = []
    for position in positions:
        quantity = abs(to_float(position.get("size", position.get("quantity"))))
        entry = to_float(position.get("entryPrice", position.get("entry_price")))
        stop = to_float(position.get("stopLoss", position.get("stop_loss")))
        side = position.get("side", "")
        # A trailing stop can already be in profit (above entry for a long or
        # below entry for a short), so entry price is not a valid test here.
        is_valid = quantity > 0 and entry > 0 and stop > 0 and side in {"Buy", "Sell"}
        if not is_valid:
            unprotected.append(position.get("symbol", "unknown"))
    return unprotected


def build_context(positions_result: list, tickers_map: dict, account_info: Optional[dict] = None) -> dict:
    """Build a JSON-safe market context for the AI model."""
    positions = []
    for position in positions_result:
        symbol = position.get("symbol", "")
        positions.append(
            {
                "symbol": symbol,
                "base_symbol": symbol.removesuffix("USDT"),
                "side": position.get("side", ""),
                "quantity": abs(to_float(position.get("size"))),
                "entry_price": to_float(position.get("entryPrice", position.get("avgPrice"))),
                "stop_loss": to_float(position.get("stopLoss")),
                "take_profit": to_float(position.get("takeProfit")),
                "liquidation_price": to_float(position.get("liqPrice")) or None,
                "unrealized_pnl": to_float(position.get("unrealisedPnl")),
                "leverage": int(to_float(position.get("leverage"), 1)),
            }
        )

    context = {"positions": positions, "prices": tickers_map}
    if account_info:
        context["account"] = account_info
    return context


def _strip_json_fence(raw: str) -> str:
    cleaned = raw.strip()
    if cleaned.startswith("```"):
        cleaned = cleaned.split("\n", 1)[1] if "\n" in cleaned else ""
    if cleaned.endswith("```"):
        cleaned = cleaned[:-3]
    return cleaned.strip()


def _number(value: Any, field: str, coin: str, *, minimum: Optional[float] = None) -> float:
    number = to_float(value, float("nan"))
    if not math.isfinite(number) or (minimum is not None and number < minimum):
        raise ValueError(f"Некорректное поле '{field}' для {coin}")
    return number


def validate_deepseek_json(raw: str, expected_tokens: Optional[Iterable[str]] = None) -> dict:
    """Parse, validate, and normalize the AI decision into one canonical shape.

    Canonical result: ``{"BTC": {"trade_signal_args": {...}}}``.  Flat
    objects are accepted for compatibility but are normalized before use.
    """
    try:
        payload = json.loads(_strip_json_fence(raw))
    except json.JSONDecodeError as error:
        logger.error(f"Не удалось распарсить JSON DeepSeek: {raw[:1000]}")
        raise ValueError(f"DeepSeek вернул невалидный JSON: {error}") from error

    if not isinstance(payload, dict):
        raise ValueError("Корень JSON от DeepSeek должен быть объектом")

    normalized_payload = {str(coin).upper(): value for coin, value in payload.items()}
    if expected_tokens is not None:
        expected = {str(token).upper() for token in expected_tokens}
        received = set(normalized_payload)
        missing = expected - received
        unexpected = received - expected
        if missing or unexpected:
            details = []
            if missing:
                details.append(f"нет: {', '.join(sorted(missing))}")
            if unexpected:
                details.append(f"лишние: {', '.join(sorted(unexpected))}")
            raise ValueError("DeepSeek вернул другой набор токенов (" + "; ".join(details) + ")")

    normalized = {}
    for coin, item in normalized_payload.items():
        if not isinstance(item, dict):
            raise ValueError(f"Решение для {coin} должно быть объектом")
        args = item.get("trade_signal_args", item)
        if not isinstance(args, dict):
            raise ValueError(f"trade_signal_args для {coin} должен быть объектом")

        required_fields = {
            "signal",
            "quantity",
            "profit_target",
            "stop_loss",
            "invalidation_condition",
            "leverage",
            "confidence",
            "risk_usd",
        }
        missing_fields = required_fields - set(args)
        if missing_fields:
            raise ValueError(f"Для {coin} отсутствуют поля: {', '.join(sorted(missing_fields))}")

        signal = str(args["signal"]).lower().strip()
        if signal not in {"hold", "close", "long", "short"}:
            raise ValueError(f"Неизвестный сигнал '{signal}' для {coin}")

        leverage_value = _number(args["leverage"], "leverage", coin, minimum=1)
        if not leverage_value.is_integer():
            raise ValueError(f"Плечо для {coin} должно быть целым числом")
        confidence = _number(args["confidence"], "confidence", coin, minimum=0)
        if confidence > 1:
            raise ValueError(f"Уверенность для {coin} должна быть от 0 до 1")

        normalized_args = {
            "signal": signal,
            "quantity": _number(args["quantity"], "quantity", coin, minimum=0),
            "profit_target": _number(args["profit_target"], "profit_target", coin, minimum=0),
            "stop_loss": _number(args["stop_loss"], "stop_loss", coin, minimum=0),
            "invalidation_condition": str(args["invalidation_condition"]),
            "leverage": int(leverage_value),
            "confidence": confidence,
            "risk_usd": _number(args["risk_usd"], "risk_usd", coin, minimum=0),
        }
        normalized[coin] = {"trade_signal_args": normalized_args}

    return normalized
