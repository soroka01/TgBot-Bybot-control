"""Activity log screen backed by SQLite."""

from __future__ import annotations

import asyncio
from html import escape

from aiogram import F, Router
from aiogram.types import CallbackQuery

from storage.database import get_store
from telegram_bot.keyboards.main_menu import get_main_menu
from telegram_bot.ui import render_callback_screen

router = Router()


def build_activity_view(chat_id: int) -> tuple[str, object]:
    records = get_store().list_activity(chat_id)
    if not records:
        return "🧾 <b>Журнал действий</b>\n\nЗаписей пока нет.", get_main_menu()
    lines = ["🧾 <b>Журнал действий</b>", ""]
    for record in records:
        icon = {"warning": "⚠️", "error": "❌"}.get(record["severity"], "•")
        timestamp = record["created_at"].replace("T", " ")[:16]
        lines.append(f"{icon} <code>{timestamp}</code> {escape(record['message'][:180])}")
    return "\n".join(lines), get_main_menu()


@router.callback_query(F.data == "menu:activity")
async def show_activity(callback: CallbackQuery) -> None:
    await callback.answer()
    text, markup = await asyncio.to_thread(build_activity_view, callback.message.chat.id)
    await render_callback_screen(callback.message, text, markup)
