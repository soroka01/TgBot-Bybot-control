"""
Менеджер для регистрации всех обработчиков
"""
from handlers.main_handler import MainHandler
from handlers.stat_handler import StatHandler
from handlers.trading_handler import TradingHandler
from handlers.alert_handler import AlertHandler
from handlers.converter_handler import ConverterHandler
from handlers.news_account_handler import NewsAndAccountHandler

class HandlerManager:
    """Менеджер для управления всеми обработчиками"""
    
    def __init__(self):
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
        return self.handlers[0].bot if self.handlers else None

# Глобальный экземпляр менеджера
handler_manager = HandlerManager()
