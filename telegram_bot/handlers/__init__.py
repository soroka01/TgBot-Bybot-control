# telegram_bot/handlers/__init__.py
"""Обработчики команд и callback'ов Telegram бота"""

from . import start
from . import positions
from . import trading
from . import settings
from . import auto_mode
from . import chart
from . import fallbacks

__all__ = ['start', 'positions', 'trading', 'settings', 'auto_mode', 'chart', 'fallbacks']
