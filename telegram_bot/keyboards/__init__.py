# telegram_bot/keyboards/__init__.py
"""Inline клавиатуры для Telegram бота"""

from .main_menu import get_main_menu
from .positions_menu import get_position_actions_menu
from .trading_menu import get_trading_menu

__all__ = [
    'get_main_menu',
    'get_position_actions_menu',
    'get_trading_menu'
]
