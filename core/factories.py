"""
Фабрика для создания экземпляров бота и API-сессий
"""
from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
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
        self._bot: Optional[Bot] = None
        self._dp: Optional[Dispatcher] = None
        
    def get_bot(self) -> Bot:
        """Получить экземпляр бота"""
        if self._bot is None:
            self._bot = Bot(
                token=config.TG_TOKEN,
                default=DefaultBotProperties(parse_mode=ParseMode.HTML)
            )
        return self._bot
    
    def get_dispatcher(self) -> Dispatcher:
        """Получить экземпляр диспетчера"""
        if self._dp is None:
            self._dp = Dispatcher()
        return self._dp

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
bot = bot_factory.get_bot()
session = session_factory.get_session()
