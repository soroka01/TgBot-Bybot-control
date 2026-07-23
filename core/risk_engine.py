"""Deterministic position sizing and execution gates.

No model-provided quantity or leverage reaches this module.  All arithmetic
that must obey exchange steps uses :class:`~decimal.Decimal`.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, ROUND_CEILING, ROUND_DOWN, ROUND_UP
from typing import Any, Iterable

from api.bybit_api import BybitAPIError, InstrumentRules
from config import (
    AUTO_LEVERAGE,
    BYBIT_MAX_SLIPPAGE_PERCENT,
    ESTIMATED_SLIPPAGE_PERCENT,
    MAX_POSITION_NOTIONAL_PERCENT,
    MAX_PRICE_DRIFT_PERCENT,
    MAX_RISK_PER_TRADE_PERCENT,
    MAX_SPREAD_PERCENT,
    MAX_TOTAL_RISK_PERCENT,
    MIN_NET_RISK_REWARD_RATIO,
    MIN_ORDER_SIZE_USDT,
)


def D(value: Any) -> Decimal:
    try:
        result = Decimal(str(value))
    except Exception as error:
        raise ValueError(f"Некорректное число: {value!r}") from error
    if not result.is_finite():
        raise ValueError(f"Некорректное число: {value!r}")
    return result


@dataclass(frozen=True)
class TradePlan:
    candidate_id: str
    symbol: str
    side: str
    quantity: Decimal
    entry_price: Decimal
    take_profit: Decimal
    stop_loss: Decimal
    leverage: Decimal
    risk_usd: Decimal
    reward_usd: Decimal
    estimated_cost_usd: Decimal
    net_risk_reward: Decimal
    margin_with_buffer: Decimal


def execution_price_and_spread(side: str, ticker: dict[str, Any]) -> tuple[Decimal, Decimal]:
    bid = D(ticker.get("bid1Price", 0))
    ask = D(ticker.get("ask1Price", 0))
    if bid <= 0 or ask <= 0 or ask < bid:
        raise ValueError("Bybit не вернул корректный bid/ask")
    midpoint = (bid + ask) / 2
    spread_percent = (ask - bid) / midpoint * 100
    if spread_percent > D(MAX_SPREAD_PERCENT):
        raise ValueError(
            f"Спред {spread_percent:.3f}% выше лимита {MAX_SPREAD_PERCENT:.3f}%"
        )
    if side == "Buy":
        return ask, spread_percent
    if side == "Sell":
        return bid, spread_percent
    raise ValueError(f"Неизвестная сторона: {side}")


def validate_price_drift(reference: Any, executable: Decimal) -> Decimal:
    reference_price = D(reference)
    if reference_price <= 0:
        raise ValueError("У кандидата нет корректной reference price")
    drift = abs(executable - reference_price) / reference_price * 100
    if drift > D(MAX_PRICE_DRIFT_PERCENT):
        raise ValueError(
            f"Цена ушла на {drift:.3f}% (лимит {MAX_PRICE_DRIFT_PERCENT:.3f}%)"
        )
    return drift


def _cost_per_coin(
    entry: Decimal,
    target: Decimal,
    stop: Decimal,
    fee_rate: Decimal,
    spread_percent: Decimal,
) -> Decimal:
    # Entry and exit are conservatively treated as taker operations.  The
    # current spread and configured adverse slippage are included as costs.
    exit_reference = max(abs(target), abs(stop), abs(entry))
    fees = entry * fee_rate + exit_reference * fee_rate
    # Hard sizing uses the full allowed entry slippage, not the lower
    # expected value shown in ordinary market conditions.  The exit keeps the
    # configured conservative estimate because it is not an order-time cap.
    slippage = (
        entry * D(BYBIT_MAX_SLIPPAGE_PERCENT)
        + exit_reference * D(ESTIMATED_SLIPPAGE_PERCENT)
    ) / 100
    spread_cost = entry * spread_percent / 100
    return fees + slippage + spread_cost


def build_trade_plan(
    candidate: dict[str, Any],
    *,
    rules: InstrumentRules,
    ticker: dict[str, Any],
    equity_usd: Any,
    available_usd: Any,
    current_portfolio_risk_usd: Any,
    taker_fee_rate: Any,
) -> TradePlan:
    """Validate a selected candidate and size it inside every hard limit."""
    equity = D(equity_usd)
    available = D(available_usd)
    portfolio_risk = max(Decimal("0"), D(current_portfolio_risk_usd))
    fee_rate = max(Decimal("0"), D(taker_fee_rate))
    if equity <= 0 or available <= 0:
        raise ValueError("Нет положительного equity/available balance")

    side = str(candidate["side"])
    entry, spread_percent = execution_price_and_spread(side, ticker)
    validate_price_drift(candidate["entry_ref"], entry)
    if side == "Buy":
        target = rules.price(candidate["target"], ROUND_DOWN)
        stop = rules.price(candidate["stop"], ROUND_UP)
        if not 0 < stop < entry < target:
            raise ValueError("Некорректные уровни LONG после округления к tickSize")
        gross_risk_per_coin = entry - stop
        gross_reward_per_coin = target - entry
    elif side == "Sell":
        target = rules.price(candidate["target"], ROUND_UP)
        stop = rules.price(candidate["stop"], ROUND_DOWN)
        if not 0 < target < entry < stop:
            raise ValueError("Некорректные уровни SHORT после округления к tickSize")
        gross_risk_per_coin = stop - entry
        gross_reward_per_coin = entry - target
    else:
        raise ValueError(f"Неизвестная сторона кандидата: {side}")

    cost_per_coin = _cost_per_coin(
        entry, target, stop, fee_rate, spread_percent
    )
    risk_per_coin = gross_risk_per_coin + cost_per_coin
    reward_per_coin = gross_reward_per_coin - cost_per_coin
    if risk_per_coin <= 0 or reward_per_coin <= 0:
        raise ValueError("Издержки делают ожидаемый результат неположительным")
    net_rr = reward_per_coin / risk_per_coin
    if net_rr < D(MIN_NET_RISK_REWARD_RATIO):
        raise ValueError(
            f"Чистый R/R {net_rr:.2f} ниже {MIN_NET_RISK_REWARD_RATIO:.2f}"
        )

    per_trade_budget = equity * D(MAX_RISK_PER_TRADE_PERCENT) / 100
    portfolio_budget = equity * D(MAX_TOTAL_RISK_PERCENT) / 100
    remaining_budget = portfolio_budget - portfolio_risk
    risk_budget = min(per_trade_budget, remaining_budget)
    if risk_budget <= 0:
        raise ValueError("Лимит общего риска уже исчерпан")

    allowed_leverage = min(
        D(AUTO_LEVERAGE),
        rules.max_leverage,
    )
    if allowed_leverage < 1:
        raise ValueError("Инструмент не допускает безопасное плечо")
    risk_quantity = risk_budget / risk_per_coin
    max_notional = equity * D(MAX_POSITION_NOTIONAL_PERCENT) / 100
    margin_notional = available * allowed_leverage / D("1.10")
    notional_cap = min(max_notional, margin_notional)
    capped_quantity = min(risk_quantity, notional_cap / entry)
    if rules.max_market_qty > 0:
        capped_quantity = min(capped_quantity, rules.max_market_qty)
    quantity = rules.quantity(capped_quantity)
    rules.validate_quantity(quantity, entry)
    notional = quantity * entry
    if notional < D(MIN_ORDER_SIZE_USDT):
        raise BybitAPIError(
            f"Номинал ${notional:.2f} ниже локального минимума "
            f"${MIN_ORDER_SIZE_USDT:.2f}"
        )

    required_leverage = notional * D("1.10") / available
    leverage_steps = (
        required_leverage / rules.leverage_step
    ).to_integral_value(rounding=ROUND_CEILING)
    leverage = max(Decimal("1"), leverage_steps * rules.leverage_step)
    if leverage > allowed_leverage:
        raise ValueError("Недостаточно доступной маржи в разрешённом диапазоне плеча")

    cost = cost_per_coin * quantity
    risk = gross_risk_per_coin * quantity + cost
    reward = gross_reward_per_coin * quantity - cost
    if risk > risk_budget:
        raise ValueError("Округлённая позиция превышает бюджет риска")
    margin_with_buffer = notional / leverage * D("1.10")
    return TradePlan(
        candidate_id=str(candidate["id"]),
        symbol=str(candidate["symbol"]),
        side=side,
        quantity=quantity,
        entry_price=entry,
        take_profit=target,
        stop_loss=stop,
        leverage=leverage,
        risk_usd=risk,
        reward_usd=reward,
        estimated_cost_usd=cost,
        net_risk_reward=reward / risk,
        margin_with_buffer=margin_with_buffer,
    )


def portfolio_risk_usd(
    positions: Iterable[dict[str, Any]],
    *,
    taker_fee_rate: Any,
) -> tuple[Decimal, list[str]]:
    """Return mark-to-stop drawdown plus exit costs and unprotected symbols."""
    fee = max(Decimal("0"), D(taker_fee_rate))
    slippage = D(ESTIMATED_SLIPPAGE_PERCENT) / 100
    total = Decimal("0")
    unprotected: list[str] = []
    for position in positions:
        size = abs(D(position.get("size", 0)))
        if size <= 0:
            continue
        symbol = str(position.get("symbol", "unknown"))
        mark = D(position.get("markPrice") or position.get("lastPrice") or 0)
        stop = D(position.get("stopLoss") or 0)
        liquidation = D(position.get("liqPrice") or 0)
        side = str(position.get("side", ""))
        if mark <= 0 or stop <= 0 or side not in {"Buy", "Sell"}:
            unprotected.append(symbol)
            continue
        direction_valid = (
            side == "Buy" and stop < mark
        ) or (
            side == "Sell" and stop > mark
        )
        liquidation_safe = (
            liquidation <= 0
            or (side == "Buy" and stop >= liquidation * D("1.05"))
            or (side == "Sell" and stop <= liquidation * D("0.95"))
        )
        if not direction_valid or not liquidation_safe:
            unprotected.append(symbol)
            continue
        distance = max(
            Decimal("0"),
            mark - stop if side == "Buy" else stop - mark,
        )
        exit_cost = mark * (fee + slippage)
        total += (distance + exit_cost) * size
    return total, sorted(set(unprotected))
