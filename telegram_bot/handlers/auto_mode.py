"""Auto-trading controls and live status/log screens."""

from __future__ import annotations

import asyncio
import html
import sys
import threading
from pathlib import Path

from aiogram import F, Router
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from telegram_bot.keyboards.main_menu import get_auto_mode_menu, get_main_menu
from telegram_bot.ui import render_callback_screen, render_live_screen
from storage.database import get_store
from utils.logger_setup import logger

router = Router()

AUTO_MODE_STATE = {"is_running": False, "task": None}


def is_auto_mode_running() -> bool:
    """Return worker truth instead of a stale UI-only flag."""
    worker = AUTO_MODE_STATE.get("task")
    running = bool(AUTO_MODE_STATE.get("is_running")) and bool(worker and worker.is_alive())
    if AUTO_MODE_STATE.get("is_running") and not running:
        AUTO_MODE_STATE["is_running"] = False
        AUTO_MODE_STATE["task"] = None
    return running


def build_auto_mode_view():
    """Build the compact status screen shown and refreshed in-place."""
    from config import DRY_RUN, MAX_LEVERAGE, MAX_RISK_PER_TRADE_PERCENT, POLL_INTERVAL, TRADABLE_TOKENS

    running = is_auto_mode_running()
    status = "🟢 Активен" if running else "🔴 Остановлен"
    mode = "🧪 DRY RUN" if DRY_RUN else "⚠️ LIVE"
    text = (
        "🤖 <b>Авто-режим</b> <i>• обновление 2с</i>\n\n"
        f"<b>Статус:</b> {status}\n"
        f"<b>Режим:</b> <code>{mode}</code>\n"
        f"<b>Интервал:</b> <code>{POLL_INTERVAL}с</code>\n"
        f"<b>Токенов:</b> <code>{len(TRADABLE_TOKENS)}</code> | "
        f"<b>Плечо до:</b> <code>{MAX_LEVERAGE}x</code>\n"
        f"<b>Риск на сделку:</b> <code>{MAX_RISK_PER_TRADE_PERCENT}%</code>\n\n"
        + (
            "Бот анализирует рынок и управляет TP/SL. Открытые позиции остаются под вашим контролем."
            if running
            else "Перед стартом проверьте баланс, открытые позиции и лимиты риска."
        )
    )
    return text, get_auto_mode_menu(running)


def build_logs_view():
    """Return a Telegram-safe tail of the local log without blocking the event loop."""
    log_file = Path(__file__).parent.parent.parent / "crypto_bot.log"
    if not log_file.exists():
        return "📝 <b>Логи авто-режима</b>\n\nЛог пока пуст.", get_main_menu()

    with open(log_file, "r", encoding="utf-8", errors="replace") as file:
        lines = file.readlines()[-120:]
    header = "📝 <b>Логи авто-режима</b> <i>• обновление 2с</i>\n\n<code>"
    footer = "</code>"
    limit = 4_000 - len(header) - len(footer)
    selected: list[str] = []
    size = 0
    for line in reversed(lines):
        escaped = html.escape(line)
        if size + len(escaped) > limit:
            break
        selected.insert(0, escaped)
        size += len(escaped)
    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="◀️ К авто-режиму", callback_data="menu:auto_mode")]]
    )
    return header + "".join(selected) + footer, keyboard


async def show_auto_mode(callback: CallbackQuery) -> None:
    async def load_auto_mode():
        return build_auto_mode_view()

    await render_live_screen(callback.message, load_auto_mode)


@router.callback_query(F.data == "menu:auto_mode")
async def callback_auto_mode_menu(callback: CallbackQuery):
    await callback.answer()
    await show_auto_mode(callback)


@router.callback_query(F.data == "auto:start")
async def callback_auto_start(callback: CallbackQuery):
    if is_auto_mode_running():
        await callback.answer("Авто-режим уже запущен", show_alert=True)
        return

    try:
        from core.auto_trading import main_loop
    except Exception as error:
        logger.error(f"Не удалось подготовить авто-режим: {error}")
        await callback.answer("Не удалось подготовить авто-режим", show_alert=True)
        await render_callback_screen(
            callback.message,
            f"❌ <b>Не удалось подготовить авто-режим</b>\n\n<code>{html.escape(str(error)[:300])}</code>",
            get_main_menu(),
        )
        return

    def run_auto_trading() -> None:
        try:
            main_loop()
        except Exception as error:
            logger.error(f"Ошибка авто-торговли: {error}")
        finally:
            AUTO_MODE_STATE["is_running"] = False
            AUTO_MODE_STATE["task"] = None

    worker = threading.Thread(target=run_auto_trading, daemon=True, name="auto-trading")
    AUTO_MODE_STATE["is_running"] = True
    AUTO_MODE_STATE["task"] = worker
    try:
        worker.start()
    except Exception as error:
        AUTO_MODE_STATE["is_running"] = False
        AUTO_MODE_STATE["task"] = None
        logger.error(f"Ошибка запуска авто-режима: {error}")
        await callback.answer("Не удалось запустить авто-режим", show_alert=True)
        await render_callback_screen(
            callback.message,
            f"❌ <b>Не удалось запустить авто-режим</b>\n\n<code>{html.escape(str(error)[:300])}</code>",
            get_main_menu(),
        )
        return

    await callback.answer("Авто-режим запущен")
    await asyncio.to_thread(
        get_store().log_activity,
        callback.message.chat.id,
        "auto_mode_started",
        "Авто-режим запущен пользователем",
        severity="warning",
    )
    await show_auto_mode(callback)


@router.callback_query(F.data == "auto:stop")
async def callback_auto_stop(callback: CallbackQuery):
    if not is_auto_mode_running():
        await callback.answer("Авто-режим уже остановлен", show_alert=True)
        return

    AUTO_MODE_STATE["is_running"] = False
    await callback.answer("Останавливаю авто-режим")
    await asyncio.to_thread(
        get_store().log_activity,
        callback.message.chat.id,
        "auto_mode_stopped",
        "Авто-режим остановлен пользователем",
        severity="warning",
    )
    await show_auto_mode(callback)


@router.callback_query(F.data == "auto:logs")
async def callback_auto_logs(callback: CallbackQuery):
    await callback.answer("Открываю логи...")
    await render_callback_screen(callback.message, "📝 <b>Логи авто-режима</b>\n\n⏳ Загружаю…", get_main_menu())

    async def load_logs():
        return await asyncio.to_thread(build_logs_view)

    await render_live_screen(callback.message, load_logs)
