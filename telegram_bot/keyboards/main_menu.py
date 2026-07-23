# telegram_bot/keyboards/main_menu.py
"""Главное меню бота"""

from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton


def get_main_menu() -> InlineKeyboardMarkup:
    """Возвращает главное меню бота"""
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="📊 Позиции", callback_data="menu:positions"),
            InlineKeyboardButton(text="💰 Баланс", callback_data="menu:balance")
        ],
        [
            InlineKeyboardButton(text="📈 Живой график", callback_data="menu:chart"),
            InlineKeyboardButton(text="🔍 Рынок", callback_data="menu:market_analysis")
        ],
        [
            InlineKeyboardButton(text="🧠 AI-сетапы", callback_data="menu:open_trade"),
            InlineKeyboardButton(text="🤖 Авто-режим", callback_data="menu:auto_mode")
        ],
        [
            InlineKeyboardButton(text="🔔 Алерты", callback_data="menu:alerts"),
            InlineKeyboardButton(text="🧾 События", callback_data="menu:activity")
        ],
        [
            InlineKeyboardButton(text="📜 Сделки", callback_data="menu:history"),
            InlineKeyboardButton(text="⚙️ Настройки", callback_data="menu:settings")
        ],
        [
            InlineKeyboardButton(text="🌍 Обзор рынка", callback_data="menu:trends")
        ],
    ])
    return keyboard


def get_settings_menu() -> InlineKeyboardMarkup:
    """Меню справочных настроек без неработающих элементов управления."""
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="🪙 Токены для торговли", callback_data="settings:tokens"),
            InlineKeyboardButton(text="👤 Профиль алертов", callback_data="settings:profile")
        ],
        [
            InlineKeyboardButton(text="◀️ Назад", callback_data="menu:main")
        ]
    ])
    return keyboard


def get_auto_mode_menu(is_active: bool) -> InlineKeyboardMarkup:
    """Меню управления авто-режимом"""
    status_text = "🟢 Остановить" if is_active else "🔴 Запустить"
    status_callback = "auto:stop" if is_active else "auto:start"

    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text=status_text, callback_data=status_callback)
        ],
        [
            InlineKeyboardButton(text="📝 Логи", callback_data="auto:logs")
        ],
        [
            InlineKeyboardButton(text="◀️ Назад", callback_data="menu:main")
        ]
    ])
    return keyboard
