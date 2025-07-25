"""
Менеджер для регистрации всех обработчиков
"""
from aiogram import Dispatcher
from handlers.main_handler import MainHandler
from handlers.stat_handler import StatHandler
from handlers.trading_handler import TradingHandler
from handlers.alert_handler import AlertHandler
from handlers.converter_handler import ConverterHandler
from handlers.news_account_handler import NewsAndAccountHandler
from core.factories import bot_factory

class HandlerManager:
    """Менеджер для управления всеми обработчиками"""
    
    def __init__(self):
        self.dp: Dispatcher = bot_factory.get_dispatcher()
        self.bot = bot_factory.get_bot()
        self.handlers = [
            MainHandler(),
            StatHandler(),
            TradingHandler(),
            AlertHandler(),
            ConverterHandler(),
            NewsAndAccountHandler(),
        ]
    
    def register_all_handlers(self):
        """Зарегистрировать все обработчики"""
        for handler in self.handlers:
            handler.register_handlers()
    
    def get_bot(self):
        """Получить экземпляр бота"""
        return self.bot
    
    def get_dispatcher(self):
        """Получить экземпляр диспетчера"""
        return self.dp

# Глобальный экземпляр менеджера
handler_manager = HandlerManager()
