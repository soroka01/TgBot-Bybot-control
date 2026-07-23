"""Live text chart screen."""

from __future__ import annotations

import asyncio

from aiogram import F, Router
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup

from api.bybit_api import BybitAPI
from config import TRADABLE_TOKENS
from core.chart import build_chart_text
from storage.database import get_store
from telegram_bot.ui import render_live_screen

router = Router()
INTERVALS = ("5", "15", "60", "240")


def chart_markup(symbol: str, interval: str) -> InlineKeyboardMarkup:
    token_buttons = [
        InlineKeyboardButton(
            text=("• " if token == symbol else "") + token,
            callback_data=f"chart:symbol:{token}",
        )
        for token in TRADABLE_TOKENS
    ]
    interval_buttons = [
        InlineKeyboardButton(
            text=("• " if value == interval else "") + label,
            callback_data=f"chart:interval:{value}",
        )
        for value, label in zip(INTERVALS, ("5м", "15м", "1ч", "4ч"))
    ]
    rows = [token_buttons[index : index + 3] for index in range(0, len(token_buttons), 3)]
    rows.extend(
        [
            interval_buttons,
            [
                InlineKeyboardButton(text="↻ Обновить", callback_data="chart:refresh"),
                InlineKeyboardButton(text="◀️ Меню", callback_data="menu:main"),
            ],
        ]
    )
    return InlineKeyboardMarkup(inline_keyboard=rows)


def build_chart_view(chat_id: int):
    user = get_store().get_user(chat_id)
    symbol = str(user.get("default_symbol") or TRADABLE_TOKENS[0]).upper()
    if symbol not in TRADABLE_TOKENS:
        symbol = TRADABLE_TOKENS[0]
    interval = str(user.get("default_interval") or "15")
    if interval not in INTERVALS:
        interval = "15"
    bybit = BybitAPI()
    try:
        text = build_chart_text(bybit, f"{symbol}USDT", interval)
        return text, chart_markup(symbol, interval)
    finally:
        bybit.close()


async def show_chart(callback: CallbackQuery) -> None:
    chat_id = callback.message.chat.id

    async def loader():
        return await asyncio.to_thread(build_chart_view, chat_id)

    await render_live_screen(callback.message, loader, interval_seconds=15)


@router.callback_query(F.data == "menu:chart")
async def open_chart(callback: CallbackQuery):
    await callback.answer("Открываю график")
    await show_chart(callback)


@router.callback_query(F.data == "chart:refresh")
async def refresh_chart(callback: CallbackQuery):
    await callback.answer("Обновляю")
    await show_chart(callback)


@router.callback_query(F.data.startswith("chart:symbol:"))
async def select_chart_symbol(callback: CallbackQuery):
    symbol = callback.data.rsplit(":", 1)[-1]
    if symbol not in TRADABLE_TOKENS:
        await callback.answer("Недоступный актив", show_alert=True)
        return
    await asyncio.to_thread(
        get_store().update_user_settings,
        callback.message.chat.id,
        default_symbol=symbol,
    )
    await callback.answer(symbol)
    await show_chart(callback)


@router.callback_query(F.data.startswith("chart:interval:"))
async def select_chart_interval(callback: CallbackQuery):
    interval = callback.data.rsplit(":", 1)[-1]
    if interval not in INTERVALS:
        await callback.answer("Недоступный интервал", show_alert=True)
        return
    await asyncio.to_thread(
        get_store().update_user_settings,
        callback.message.chat.id,
        default_interval=interval,
    )
    await callback.answer("Интервал обновлён")
    await show_chart(callback)
