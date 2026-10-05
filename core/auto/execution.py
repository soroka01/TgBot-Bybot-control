"""Candidate execution for auto-trading."""

from __future__ import annotations

import threading
import time
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
from api.tg_notify import notify
from config import DRY_RUN, BYBIT_MAX_SLIPPAGE_PERCENT, FALLBACK_TAKER_FEE_RATE
from core.decision_engine import selected_candidate
from core.risk_engine import D, TradePlan, build_trade_plan
from core.trade_journal import TradeJournal
from storage.database import get_store
from utils.helpers import validate_sl_vs_liquidation
from utils.logger_setup import logger
from core.auto.runtime import (
    EXECUTION_LOCK,
    ExecutionStopped,
    FatalExecutionError,
    PARTIAL_TERMINAL_ORDER_STATUSES,
)
from core.auto.gates import (
    _final_entry_state,
    _fresh_entry_state,
)
from core.auto.protection import (
    _confirmed_safety_flatten,
    _emergency_flatten_entry,
)


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
