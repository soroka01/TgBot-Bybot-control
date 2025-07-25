"""
Базовый класс для обработчиков
"""
import logging
from abc import ABC, abstractmethod
from typing import Any, Dict

from core.factories import bot_factory
from core.decorators import handle_errors, log_function_call

class BaseHandler(ABC):
    """Базовый класс для всех обработчиков"""
    
    def __init__(self):
        self.bot = bot_factory.get_bot()
        self.logger = logging.getLogger(self.__class__.__name__)
    
    @handle_errors("Ошибка отправки сообщения")
    @log_function_call()
    def send_message_safely(self, chat_id: int, text: str, reply_markup=None, **kwargs):
        """Безопасная отправка сообщения"""
        return self.bot.send_message(chat_id, text, reply_markup=reply_markup, **kwargs)
    
    @handle_errors("Ошибка редактирования сообщения")
    @log_function_call()
    def edit_message_safely(self, chat_id: int, message_id: int, text: str = None, 
                           reply_markup=None, **kwargs):
        """Безопасное редактирование сообщения"""
        try:
            if text:
                return self.bot.edit_message_text(
                    chat_id=chat_id, 
                    message_id=message_id, 
                    text=text, 
                    reply_markup=reply_markup,
                    **kwargs
                )
            else:
                return self.bot.edit_message_reply_markup(
                    chat_id=chat_id, 
                    message_id=message_id, 
                    reply_markup=reply_markup,
                    **kwargs
                )
        except Exception as e:
            # Если редактирование не удалось, пытаемся отправить новое сообщение
            self.logger.warning(f"Не удалось отредактировать сообщение: {e}")
            if text:
                return self.send_message_safely(chat_id, text, reply_markup, **kwargs)
    
    @handle_errors("Ошибка отправки фото")
    @log_function_call()
    def send_photo_safely(self, chat_id: int, photo, caption: str = None, 
                         reply_markup=None, **kwargs):
        """Безопасная отправка фото"""
        return self.bot.send_photo(
            chat_id=chat_id, 
            photo=photo, 
            caption=caption, 
            reply_markup=reply_markup,
            **kwargs
        )
    
    def get_user_id(self, message_or_call) -> int:
        """Получить ID пользователя из сообщения или callback"""
        if hasattr(message_or_call, 'chat'):
            return message_or_call.chat.id
        elif hasattr(message_or_call, 'message'):
            return message_or_call.message.chat.id
        else:
            raise ValueError("Не удалось получить ID пользователя")
    
    def get_message_id(self, message_or_call) -> int:
        """Получить ID сообщения"""
        if hasattr(message_or_call, 'message_id'):
            return message_or_call.message_id
        elif hasattr(message_or_call, 'message'):
            return message_or_call.message.message_id
        else:
            raise ValueError("Не удалось получить ID сообщения")
    
    @abstractmethod
    def register_handlers(self):
        """Зарегистрировать обработчики"""
        pass
