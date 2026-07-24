"""Inline controls for the compact trade-history screen."""

from __future__ import annotations

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup


HISTORY_PERIODS = (1, 7, 14, 30, 90, 180, 365)
HISTORY_SCOPES = ("bot", "all")
DEFAULT_HISTORY_PERIOD = 30
DEFAULT_HISTORY_SCOPE = "all"


def _view_callback(days: int, scope: str) -> str:
    return f"history:view:{days}:{scope}"


def get_history_menu(days: int, scope: str) -> InlineKeyboardMarkup:
    """Return period/scope controls while keeping every action in one message."""
    if days not in HISTORY_PERIODS:
        raise ValueError("Неподдерживаемый период истории")
    if scope not in HISTORY_SCOPES:
        raise ValueError("Неподдерживаемая область истории")

    labels = {
        1: "1Д",
        7: "7Д",
        14: "14Д",
        30: "1М",
        90: "3М",
        180: "6М",
        365: "1ГОД",
    }

    def period_button(period: int) -> InlineKeyboardButton:
        marker = "✓ " if period == days else ""
        return InlineKeyboardButton(
            text=f"{marker}{labels[period]}",
            callback_data=_view_callback(period, scope),
        )

    bot_marker = "✓ " if scope == "bot" else ""
    all_marker = "✓ " if scope == "all" else ""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [period_button(period) for period in HISTORY_PERIODS[:4]],
            [period_button(period) for period in HISTORY_PERIODS[4:]],
            [
                InlineKeyboardButton(
                    text=f"{bot_marker}🤖 Бот",
                    callback_data=_view_callback(days, "bot"),
                ),
                InlineKeyboardButton(
                    text=f"{all_marker}🌐 Весь аккаунт",
                    callback_data=_view_callback(days, "all"),
                ),
            ],
            [
                InlineKeyboardButton(
                    text="🔄 Обновить",
                    callback_data=f"history:refresh:{days}:{scope}",
                ),
                InlineKeyboardButton(text="◀️ Назад", callback_data="menu:main"),
            ],
        ]
    )
