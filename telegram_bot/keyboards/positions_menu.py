# telegram_bot/keyboards/positions_menu.py
"""Клавиатуры для управления позициями"""

from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
from typing import List, Dict


def get_position_actions_menu(symbol: str, position_idx: int) -> InlineKeyboardMarkup:
    """Меню действий для конкретной позиции"""
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="❌ Закрыть позицию", callback_data=f"pos:close:{symbol}:{position_idx}"),
        ],
        [
            InlineKeyboardButton(text="◀️ Назад", callback_data="menu:positions")
        ]
    ])
    return keyboard


def get_positions_list_menu(positions: List[Dict]) -> InlineKeyboardMarkup:
    """Меню со списком открытых позиций"""
    buttons = []

    for pos in positions:
        symbol = pos.get("symbol", "")
        position_idx = int(pos.get("position_idx", 0))
        base_symbol = symbol.replace("USDT", "")
        side = pos.get("side", "")
        pnl = pos.get("unrealized_pnl", 0)

        # Эмодзи в зависимости от направления
        emoji = "🟢" if side == "Buy" else "🔴"
        pnl_emoji = "💚" if pnl >= 0 else "❤️"

        button_text = f"{emoji} {base_symbol} {side[:1]} {pnl_emoji} ${pnl:+.2f}"
        buttons.append([
            InlineKeyboardButton(
                text=button_text,
                callback_data=f"pos:details:{symbol}:{position_idx}"
            )
        ])

    # Добавляем кнопки управления
    buttons.append([
        InlineKeyboardButton(text="🔄 Обновить", callback_data="positions:refresh")
    ])
    buttons.append([
        InlineKeyboardButton(text="❌ Закрыть все", callback_data="positions:close_all")
    ])
    buttons.append([
        InlineKeyboardButton(text="◀️ Назад", callback_data="menu:main")
    ])

    keyboard = InlineKeyboardMarkup(inline_keyboard=buttons)
    return keyboard


def get_close_confirmation_menu(symbol: str, position_idx: int) -> InlineKeyboardMarkup:
    """Подтверждение закрытия позиции"""
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="✅ Да, закрыть", callback_data=f"pos:close_confirm:{symbol}:{position_idx}"),
            InlineKeyboardButton(text="❌ Отмена", callback_data=f"pos:details:{symbol}:{position_idx}")
        ]
    ])
    return keyboard


def get_close_all_confirmation_menu() -> InlineKeyboardMarkup:
    """Подтверждение закрытия всех позиций"""
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="✅ Да, закрыть ВСЕ", callback_data="positions:close_all_confirm"),
            InlineKeyboardButton(text="❌ Отмена", callback_data="menu:positions")
        ]
    ])
    return keyboard
