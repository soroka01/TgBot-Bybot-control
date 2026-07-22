"""Keyboard for the read-only AI recommendation screen."""

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup


def get_trading_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="🤖 Получить AI-анализ", callback_data="trade:ai_suggest")],
            [InlineKeyboardButton(text="📊 Живой обзор рынка", callback_data="menu:market_analysis")],
            [InlineKeyboardButton(text="◀️ Назад", callback_data="menu:main")],
        ]
    )
