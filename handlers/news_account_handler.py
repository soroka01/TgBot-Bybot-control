"""
Обработчики для новостей и информации об аккаунте
"""
from aiogram import Router, F
from aiogram.types import Message, CallbackQuery
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup

from handlers.base_handler import BaseHandler
from services.news_service import news_service
from services.database_service import db_service
from buttons import create_back_button, create_account_menu
from core.decorators import handle_errors
from core.factories import bot_factory

class AccountStates(StatesGroup):
    """Состояния для процесса изменения имени"""
    waiting_for_name = State()

class NewsAndAccountHandler(BaseHandler):
    """Обработчик новостей и информации об аккаунте"""
    
    def __init__(self):
        super().__init__()
        self.news_service = news_service
        self.db_service = db_service
        self.router = Router()
        self.dp = bot_factory.get_dispatcher()
    
    def register_handlers(self):
        """Регистрация обработчиков"""
        
        @self.router.callback_query(F.data == "news")
        @handle_errors("Ошибка получения новостей")
        async def handle_news(call: CallbackQuery):
            user_id = self.get_user_id(call)
            message_id = self.get_message_id(call)
            
            await self._send_news(user_id, message_id)
            await call.answer()
        
        @self.router.callback_query(F.data == "account")
        @handle_errors("Ошибка получения информации об аккаунте")
        async def handle_account(call: CallbackQuery):
            user_id = self.get_user_id(call)
            message_id = self.get_message_id(call)
            
            await self._send_account_info(user_id, message_id)
            await call.answer()
        
        @self.router.callback_query(F.data == "back_to_account")
        @handle_errors("Ошибка возврата в аккаунт")
        async def handle_back_to_account(call: CallbackQuery, state: FSMContext):
            user_id = self.get_user_id(call)
            message_id = self.get_message_id(call)
            
            # Очищаем состояние
            await state.clear()
            
            await self._send_account_info(user_id, message_id)
            await call.answer()
        
        @self.router.callback_query(F.data == "change_name")
        @handle_errors("Ошибка изменения имени")
        async def handle_change_name(call: CallbackQuery, state: FSMContext):
            user_id = self.get_user_id(call)
            message_id = self.get_message_id(call)
            
            await self._start_name_change(user_id, message_id, state)
            await call.answer()
        
        @self.router.message(AccountStates.waiting_for_name)
        @handle_errors("Ошибка обработки нового имени")
        async def process_name_change(message: Message, state: FSMContext):
            await self._process_name_change(message, state)
        
        # Регистрируем роутер в диспетчере
        self.dp.include_router(self.router)
    
    @handle_errors("Ошибка отправки новостей")
    async def _send_news(self, user_id: int, message_id: int):
        """Отправить новости пользователю"""
        try:
            news_data = self.news_service.get_latest_crypto_news()
            
            if news_data:
                news_message = f"📰 Последние новости о криптовалютах:\n\n{news_data}"
            else:
                news_message = "📭 Новости временно недоступны"
            
            await self.edit_message_safely(
                user_id,
                message_id,
                news_message,
                create_back_button("menu")
            )
            
        except Exception as e:
            error_message = f"⚠️ Ошибка при получении новостей: {str(e)}"
            await self.edit_message_safely(
                user_id,
                message_id,
                error_message,
                create_back_button("menu")
            )
    
    @handle_errors("Ошибка отправки информации об аккаунте")
    async def _send_account_info(self, user_id: int, message_id: int):
        """Отправить информацию об аккаунте"""
        try:
            # Получаем данные пользователя
            user_data = self.db_service.get_user_data(user_id)
            user_name = user_data.get("name", "Не установлено")
            
            # Получаем статистику алертов
            alert_stats = self.db_service.get_user_alert_stats(user_id)
            
            account_message = (
                f"👤 Информация об аккаунте:\n\n"
                f"🆔 ID: {user_id}\n"
                f"👨‍💼 Имя: {user_name}\n"
                f"🔔 Активных алертов: {alert_stats.get('total', 0)}\n"
                f"  • Ценовых: {alert_stats.get('price_alerts', 0)}\n"
                f"  • RSI: {alert_stats.get('rsi_alerts', 0)}\n"
                f"📅 Дата регистрации: {user_data.get('created_at', 'Неизвестно')}"
            )
            
            await self.edit_message_safely(
                user_id,
                message_id,
                account_message,
                create_account_menu()
            )
            
        except Exception as e:
            error_message = f"⚠️ Ошибка при получении информации об аккаунте: {str(e)}"
            await self.edit_message_safely(
                user_id,
                message_id,
                error_message,
                create_back_button("menu")
            )
    
    async def _start_name_change(self, user_id: int, message_id: int, state: FSMContext):
        """Начать процесс изменения имени"""
        message_text = (
            "✏️ Изменение имени\n\n"
            "Введите новое имя пользователя:"
        )
        
        await self.edit_message_safely(
            user_id,
            message_id,
            message_text,
            create_back_button("account")
        )
        
        await state.set_state(AccountStates.waiting_for_name)
    
    async def _process_name_change(self, message: Message, state: FSMContext):
        """Обработать изменение имени"""
        user_id = self.get_user_id(message)
        new_name = message.text.strip()
        
        try:
            if len(new_name) < 2:
                raise ValueError("Имя должно содержать минимум 2 символа")
            
            if len(new_name) > 50:
                raise ValueError("Имя не должно превышать 50 символов")
            
            # Сохраняем новое имя
            success = self.db_service.update_user_name(user_id, new_name)
            
            if success:
                success_message = f"✅ Имя успешно изменено на: {new_name}"
            else:
                success_message = "⚠️ Не удалось изменить имя. Попробуйте позже."
            
            await self.send_message_safely(
                user_id,
                success_message,
                create_back_button("account")
            )
            
        except ValueError as e:
            error_message = f"⚠️ {str(e)}"
            await self.send_message_safely(
                user_id,
                error_message,
                create_back_button("account")
            )
        except Exception as e:
            error_message = f"⚠️ Ошибка при изменении имени: {str(e)}"
            await self.send_message_safely(
                user_id,
                error_message,
                create_back_button("account")
            )
        finally:
            await state.clear()
