"""Auto-trading main loop."""

from __future__ import annotations

import threading
import time
import traceback
from datetime import datetime, timezone
from typing import Optional
from api.bybit_api import BybitAPI
from api.deepseek_api import DeepSeekAPI
from api.tg_notify import notify
from config import DRY_RUN, POLL_INTERVAL, validate_config
from core.decision_engine import build_selector_prompt, validate_trade_decision
from core.trade_journal import TradeJournal
from storage.database import get_store
from utils.logger_setup import logger
from core.auto.runtime import (
    FEE_REFRESH_SECONDS,
    FatalExecutionError,
    TRADE_HISTORY_SYNC_SECONDS,
    _set_runtime,
)
from core.auto.gates import (
    _fee_rates,
    collect_cycle,
)
from core.auto.protection import (
    _urgent_protection_preflight,
    manage_existing_protection,
)
from core.auto.execution import (
    execute_decisions,
)


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
