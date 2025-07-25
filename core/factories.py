"""
Фабрика для создания экземпляров бота и API-сессий
"""
import telebot
from pybit.unified_trading import HTTP
from core.config import config
from typing import Optional

class SingletonMeta(type):
    """Метакласс для реализации паттерна Singleton"""
    _instances = {}

    def __call__(cls, *args, **kwargs):
        if cls not in cls._instances:
            cls._instances[cls] = super(SingletonMeta, cls).__call__(*args, **kwargs)
        return cls._instances[cls]

class BotFactory(metaclass=SingletonMeta):
    """Фабрика для создания единственного экземпляра бота"""
    
    def __init__(self):
        self._bot: Optional[telebot.TeleBot] = None
        
    def get_bot(self) -> telebot.TeleBot:
        """Получить экземпляр бота"""
        if self._bot is None:
            self._bot = telebot.TeleBot(config.TG_TOKEN)
        return self._bot

class BybitSessionFactory(metaclass=SingletonMeta):
    """Фабрика для создания единственного экземпляра сессии Bybit"""
    
    def __init__(self):
        self._session: Optional[HTTP] = None
        
    def get_session(self) -> HTTP:
        """Получить экземпляр сессии Bybit"""
        if self._session is None:
            self._session = HTTP(
                testnet=config.BY_TESTNET,
                api_key=config.BY_KEY,
                api_secret=config.BY_SECRET
            )
        return self._session

# Глобальные экземпляры
bot_factory = BotFactory()
session_factory = BybitSessionFactory()

# Для обратной совместимости
bot = bot_factory.get_bot()
session = session_factory.get_session()
