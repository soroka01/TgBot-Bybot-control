"""Atomic auto-worker lifecycle and in-place status screens."""

from __future__ import annotations

import asyncio
import html
import threading
from pathlib import Path
from typing import Optional

from aiogram import F, Router
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup

from config import (
    AUTO_LEVERAGE,
    BYBIT_ENV,
    DEEPSEEK_MODEL,
    DRY_RUN,
    MAX_RISK_PER_TRADE_PERCENT,
    MAX_TOTAL_RISK_PERCENT,
    POLL_INTERVAL,
    TRADABLE_TOKENS,
    validate_config,
)
from core.auto_trading import get_runtime_status, main_loop
from storage.database import get_store
from telegram_bot.keyboards.main_menu import get_auto_mode_menu, get_main_menu
from telegram_bot.ui import render_callback_screen, render_live_screen
from utils.logger_setup import logger

router = Router()

_lifecycle_lock = threading.Lock()
_worker: Optional[threading.Thread] = None
_stop_event: Optional[threading.Event] = None
_lifecycle_state = "stopped"


def _worker_finished() -> None:
    global _worker, _stop_event, _lifecycle_state
    with _lifecycle_lock:
        _worker = None
        _stop_event = None
        _lifecycle_state = "stopped"


def auto_mode_state() -> str:
    global _worker, _lifecycle_state
    with _lifecycle_lock:
        if _worker and not _worker.is_alive() and _lifecycle_state != "stopped":
            _worker = None
            _lifecycle_state = "stopped"
        return _lifecycle_state


def is_auto_mode_running() -> bool:
    return auto_mode_state() in {"starting", "running", "stopping"}


def start_auto_mode() -> bool:
    """Start exactly one worker even when two callbacks arrive together."""
    global _worker, _stop_event, _lifecycle_state
    with _lifecycle_lock:
        if _lifecycle_state != "stopped" or (_worker and _worker.is_alive()):
            return False
        event = threading.Event()
        _lifecycle_state = "starting"

        def run() -> None:
            global _lifecycle_state
            try:
                with _lifecycle_lock:
                    if _lifecycle_state == "starting":
                        _lifecycle_state = "running"
                main_loop(event)
            except Exception as error:
                logger.error(f"Авто-worker завершился с ошибкой: {error}")
            finally:
                _worker_finished()

        worker = threading.Thread(
            target=run,
            # Never let process shutdown cut off order reconciliation or the
            # protection/emergency-close sequence after a live fill.
            daemon=False,
            name="auto-trading",
        )
        _worker = worker
        _stop_event = event
        try:
            worker.start()
        except Exception:
            _worker = None
            _stop_event = None
            _lifecycle_state = "stopped"
            raise
        return True


def stop_auto_mode(timeout: Optional[float] = 0.0) -> bool:
    """Signal stop and optionally wait; return whether the worker has exited."""
    global _lifecycle_state
    with _lifecycle_lock:
        worker = _worker
        event = _stop_event
        if not worker or not worker.is_alive():
            _lifecycle_state = "stopped"
            return True
        _lifecycle_state = "stopping"
        if event:
            event.set()
    if timeout is None:
        worker.join()
    elif timeout > 0:
        worker.join(timeout)
    return not worker.is_alive()


def build_auto_mode_view():
    lifecycle = auto_mode_state()
    runtime = get_runtime_status()
    labels = {
        "stopped": "🔴 Остановлен",
        "starting": "🟡 Запускается",
        "running": "🟢 Активен",
        "stopping": "🟠 Останавливается",
    }
    mode = "🧪 DRY PREVIEW" if DRY_RUN else "⚠️ LIVE"
    last_cycle = runtime.get("last_cycle_at") or "—"
    if last_cycle != "—":
        last_cycle = last_cycle.replace("T", " ").split("+", 1)[0] + " UTC"
    text = (
        "🤖 <b>Авто-режим</b>\n\n"
        f"<b>Статус:</b> {labels[lifecycle]}\n"
        f"<b>Контур:</b> <code>{mode} · {BYBIT_ENV}</code>\n"
        f"<b>Модель:</b> <code>{DEEPSEEK_MODEL}</code>\n"
        f"<b>Цикл:</b> <code>{POLL_INTERVAL}с</code> · "
        f"<b>последний:</b> <code>{last_cycle}</code>\n"
        f"<b>Риск:</b> <code>{MAX_RISK_PER_TRADE_PERCENT}% / "
        f"{MAX_TOTAL_RISK_PERCENT}% портфель</code>\n"
        f"<b>Плечо:</b> <code>минимально нужное, до {AUTO_LEVERAGE}x</code>\n"
        f"<b>Активы:</b> <code>{', '.join(TRADABLE_TOKENS)}</code>\n\n"
        f"<b>Итог:</b> {html.escape(str(runtime.get('last_summary') or '—'))}"
    )
    if runtime.get("last_error"):
        text += f"\n\n⚠️ <code>{html.escape(str(runtime['last_error']))}</code>"
    if lifecycle == "stopping":
        text += "\n\n<i>Новые ордера уже запрещены; завершается текущий HTTP/AI-вызов.</i>"
    return text, get_auto_mode_menu(lifecycle != "stopped")


def build_logs_view():
    log_file = Path(__file__).parent.parent.parent / "crypto_bot.log"
    if not log_file.exists():
        return "📝 <b>Логи авто-режима</b>\n\nЛог пока пуст.", get_main_menu()
    with open(log_file, "r", encoding="utf-8", errors="replace") as file:
        lines = file.readlines()[-100:]
    header = "📝 <b>Логи авто-режима</b>\n\n<code>"
    footer = "</code>"
    limit = 3_900 - len(header) - len(footer)
    selected: list[str] = []
    size = 0
    for line in reversed(lines):
        escaped = html.escape(line)
        if size + len(escaped) > limit:
            break
        selected.insert(0, escaped)
        size += len(escaped)
    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="◀️ К авто-режиму", callback_data="menu:auto_mode")]
        ]
    )
    return header + "".join(selected) + footer, keyboard


async def show_auto_mode(callback: CallbackQuery) -> None:
    async def loader():
        return build_auto_mode_view()

    await render_live_screen(callback.message, loader, interval_seconds=5)


@router.callback_query(F.data == "menu:auto_mode")
async def callback_auto_mode_menu(callback: CallbackQuery):
    await callback.answer()
    await show_auto_mode(callback)


@router.callback_query(F.data == "auto:start")
async def callback_auto_start(callback: CallbackQuery):
    errors = validate_config("auto")
    if errors:
        await callback.answer("Исправьте конфигурацию", show_alert=True)
        await render_callback_screen(
            callback.message,
            "❌ <b>Авто-режим заблокирован</b>\n\n"
            + "\n".join(f"• {html.escape(error)}" for error in errors),
            get_auto_mode_menu(False),
        )
        return
    if not DRY_RUN:
        keyboard = InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text="⚠️ Подтверждаю LIVE",
                        callback_data="auto:confirm_live",
                    )
                ],
                [InlineKeyboardButton(text="◀️ Отмена", callback_data="menu:auto_mode")],
            ]
        )
        await callback.answer()
        await render_callback_screen(
            callback.message,
            "⚠️ <b>Подтверждение LIVE</b>\n\n"
            f"Bybit: <code>{BYBIT_ENV}</code>\n"
            f"Риск на сделку: <code>{MAX_RISK_PER_TRADE_PERCENT}%</code>\n"
            f"Общий риск: <code>{MAX_TOTAL_RISK_PERCENT}%</code>\n"
            f"Токены: <code>{', '.join(TRADABLE_TOKENS)}</code>\n\n"
            "Ордеры будут отправляться на биржу. Проверьте API-ключ без права вывода.",
            keyboard,
        )
        return
    await _start_from_callback(callback)


@router.callback_query(F.data.startswith("auto:confirm_live:rev:"))
async def callback_auto_confirm_live(callback: CallbackQuery):
    if DRY_RUN:
        await callback.answer("Конфигурация уже переключена в DRY", show_alert=True)
        return
    await _start_from_callback(callback)


async def _start_from_callback(callback: CallbackQuery) -> None:
    if not start_auto_mode():
        await callback.answer("Авто-режим уже запускается или работает", show_alert=True)
        return
    await callback.answer("Авто-режим запускается")
    await asyncio.to_thread(
        get_store().log_activity,
        callback.message.chat.id,
        "auto_mode_started",
        "Авто-режим запущен владельцем",
        severity="warning",
    )
    await show_auto_mode(callback)


@router.callback_query(F.data == "auto:stop")
async def callback_auto_stop(callback: CallbackQuery):
    if auto_mode_state() == "stopped":
        await callback.answer("Авто-режим уже остановлен", show_alert=True)
        return
    stop_auto_mode()
    await callback.answer("Останавливаю; новые ордера запрещены")
    await asyncio.to_thread(
        get_store().log_activity,
        callback.message.chat.id,
        "auto_mode_stopping",
        "Владелец запросил остановку авто-режима",
        severity="warning",
    )
    await show_auto_mode(callback)


@router.callback_query(F.data == "auto:logs")
async def callback_auto_logs(callback: CallbackQuery):
    await callback.answer("Открываю логи")
    canonical = await render_callback_screen(
        callback.message,
        "📝 <b>Логи авто-режима</b>\n\n⏳ Загружаю…",
        get_main_menu(),
    )

    async def loader():
        return await asyncio.to_thread(build_logs_view)

    await render_live_screen(canonical, loader, interval_seconds=10)
