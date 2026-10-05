"""Position protection and safety flatten logic for auto-trading."""

from __future__ import annotations

import threading
from typing import Any, Optional
from api.bybit_api import (
    BybitAPI,
    BybitAPIError,
    BybitOrderConfirmationError,
    BybitOrderNotFilledError,
)
from api.tg_notify import notify
from config import TP_SL_MIN_CHANGE_PERCENT
from core.risk_engine import D
from utils.helpers import validate_sl_vs_liquidation
from utils.logger_setup import logger
from core.auto.runtime import (
    EXECUTION_LOCK,
    FatalExecutionError,
    MAX_SAFETY_CLOSE_ATTEMPTS,
)


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
