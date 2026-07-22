"""Safe fallback for buttons left in an old, already-sent bot message."""

from aiogram import Router
from aiogram.types import CallbackQuery

from telegram_bot.keyboards.main_menu import get_main_menu
from telegram_bot.ui import render_callback_screen

router = Router()


@router.callback_query()
async def callback_fallback(callback: CallbackQuery):
    """A stale callback must always acknowledge itself instead of spinning forever."""
    await callback.answer("Экран обновлён")
    await render_callback_screen(
        callback.message,
        "🔄 <b>Этот экран устарел.</b>\n\nОткрыл актуальное главное меню.",
        get_main_menu(),
    )
