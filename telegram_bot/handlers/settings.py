"""Read-only global settings and editable per-user alert preferences."""

from __future__ import annotations

import asyncio

from aiogram import F, Router
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup

from config import TRADABLE_TOKENS
from storage.database import get_store
from telegram_bot.ui import render_callback_screen

router = Router()
PROFILE_INTERVALS = ("5", "15", "60", "240", "D")
_INTERVAL_LABELS = {
    "5": "5 минут",
    "15": "15 минут",
    "60": "1 час",
    "240": "4 часа",
    "D": "1 день",
}


def _interval_label(value: object) -> str:
    return _INTERVAL_LABELS.get(str(value), _INTERVAL_LABELS["15"])


def profile_markup(user: dict) -> InlineKeyboardMarkup:
    token_buttons = [
        InlineKeyboardButton(
            text=("✅ " if token == user.get("default_symbol") else "") + token,
            callback_data=f"settings:symbol:{token}",
        ) for token in TRADABLE_TOKENS
    ]
    token_rows = [
        token_buttons[index:index + 3]
        for index in range(0, len(token_buttons), 3)
    ]
    return InlineKeyboardMarkup(inline_keyboard=[
        *token_rows,
        [
            InlineKeyboardButton(
                text=f"Интервал: {_interval_label(user.get('default_interval'))}",
                callback_data="settings:interval",
            ),
            InlineKeyboardButton(
                text=("🔔 Уведомления: вкл" if user.get("notifications_enabled") else "🔕 Уведомления: вык"),
                callback_data="settings:notifications:toggle",
            ),
        ],
        [InlineKeyboardButton(text="◀️ Настройки", callback_data="menu:settings")],
    ])


def profile_text(user: dict) -> str:
    return (
        "👤 <b>Профиль алертов</b>\n\n"
        f"Актив по умолчанию: <code>{user.get('default_symbol', 'BTC')}</code>\n"
        f"Интервал графика/RSI: <code>{_interval_label(user.get('default_interval'))}</code>\n"
        f"Уведомления: <code>{'включены' if user.get('notifications_enabled') else 'выключены'}</code>\n\n"
        "<i>Торговые лимиты общие для одного биржевого аккаунта; "
        "персональными здесь являются только алерты и отображение.</i>"
    )


async def show_profile(callback: CallbackQuery) -> None:
    user = await asyncio.to_thread(get_store().get_user, callback.message.chat.id)
    await render_callback_screen(callback.message, profile_text(user), profile_markup(user))


@router.callback_query(F.data == "settings:tokens")
async def show_tokens(callback: CallbackQuery) -> None:
    await callback.answer()
    tokens_list = "\n".join(f"  • <code>{token}</code>" for token in TRADABLE_TOKENS)
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="◀️ Назад к настройкам", callback_data="menu:settings")]
    ])
    await render_callback_screen(
        callback.message,
        "🪙 <b>Активные токены для торговли</b>\n\n"
        f"{tokens_list}\n\n<i>Список является общим для биржевого аккаунта.</i>",
        keyboard,
    )


@router.callback_query(F.data == "settings:profile")
async def profile(callback: CallbackQuery) -> None:
    await callback.answer()
    await show_profile(callback)


@router.callback_query(F.data.startswith("settings:symbol:"))
async def set_symbol(callback: CallbackQuery) -> None:
    symbol = callback.data.rsplit(":", 1)[-1]
    if symbol not in TRADABLE_TOKENS:
        await callback.answer("Недоступный токен", show_alert=True)
        return
    await asyncio.to_thread(get_store().update_user_settings, callback.message.chat.id, default_symbol=symbol)
    await callback.answer(f"Актив по умолчанию: {symbol}")
    await show_profile(callback)


@router.callback_query(F.data == "settings:interval")
async def cycle_interval(callback: CallbackQuery) -> None:
    user = await asyncio.to_thread(get_store().get_user, callback.message.chat.id)
    current = str(user.get("default_interval", "15"))
    next_interval = (
        PROFILE_INTERVALS[
            (PROFILE_INTERVALS.index(current) + 1) % len(PROFILE_INTERVALS)
        ]
        if current in PROFILE_INTERVALS
        else "15"
    )
    await asyncio.to_thread(
        get_store().update_user_settings, callback.message.chat.id, default_interval=next_interval
    )
    await callback.answer(f"Интервал: {_interval_label(next_interval)}")
    await show_profile(callback)


@router.callback_query(F.data == "settings:notifications:toggle")
async def toggle_notifications(callback: CallbackQuery) -> None:
    user = await asyncio.to_thread(get_store().get_user, callback.message.chat.id)
    enabled = not bool(user.get("notifications_enabled"))
    await asyncio.to_thread(
        get_store().update_user_settings, callback.message.chat.id, notifications_enabled=enabled
    )
    await callback.answer("Уведомления включены" if enabled else "Уведомления выключены")
    await show_profile(callback)
