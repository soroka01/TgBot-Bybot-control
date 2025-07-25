"""
Базовый класс для обработчиков
"""
import logging
from abc import ABC, abstractmethod
from typing import Any, Dict

from aiogram import Bot
from aiogram.types import Message, CallbackQuery
from aiogram.exceptions import TelegramBadRequest

from core.factories import bot_factory
from core.decorators import handle_errors, log_function_call

class BaseHandler(ABC):
    """Базовый класс для всех обработчиков"""
    
    def __init__(self):
        self.bot: Bot = bot_factory.get_bot()
        self.dp = bot_factory.get_dispatcher()
        self.logger = logging.getLogger(self.__class__.__name__)
    
    @handle_errors("Ошибка отправки сообщения")
    @log_function_call()
    async def send_message_safely(self, chat_id: int, text: str, reply_markup=None, **kwargs):
        """Безопасная отправка сообщения"""
        return await self.bot.send_message(chat_id, text, reply_markup=reply_markup, **kwargs)
    
    @handle_errors("Ошибка редактирования сообщения")
    @log_function_call()
    async def edit_message_safely(self, chat_id: int, message_id: int, text: str = None, 
                           reply_markup=None, **kwargs):
        """Безопасное редактирование сообщения"""
        try:
            if text:
                return await self.bot.edit_message_text(
                    chat_id=chat_id, 
                    message_id=message_id, 
                    text=text, 
                    reply_markup=reply_markup,
                    **kwargs
                )
            else:
                return await self.bot.edit_message_reply_markup(
                    chat_id=chat_id, 
                    message_id=message_id, 
                    reply_markup=reply_markup,
                    **kwargs
                )
        except TelegramBadRequest as e:
            # Если редактирование не удалось, пытаемся отправить новое сообщение
            self.logger.warning(f"Не удалось отредактировать сообщение: {e}")
            if text:
                return await self.send_message_safely(chat_id, text, reply_markup, **kwargs)
    
    @handle_errors("Ошибка отправки фото")
    @log_function_call()
    async def send_photo_safely(self, chat_id: int, photo, caption: str = None, 
                         reply_markup=None, **kwargs):
        """Безопасная отправка фото"""
        return await self.bot.send_photo(
            chat_id=chat_id, 
            photo=photo, 
            caption=caption, 
            reply_markup=reply_markup,
            **kwargs
        )
    
    def get_user_id(self, message_or_call) -> int:
        """Получить ID пользователя из сообщения или callback"""
        if isinstance(message_or_call, Message):
            return message_or_call.chat.id
        elif isinstance(message_or_call, CallbackQuery):
            return message_or_call.message.chat.id
        else:
            raise ValueError("Не удалось получить ID пользователя")
    
    def get_message_id(self, message_or_call) -> int:
        """Получить ID сообщения"""
        if isinstance(message_or_call, Message):
            return message_or_call.message_id
        elif isinstance(message_or_call, CallbackQuery):
            return message_or_call.message.message_id
        else:
            raise ValueError("Не удалось получить ID сообщения")
    
    @abstractmethod
    def register_handlers(self):
        """Зарегистрировать обработчики"""
        pass
