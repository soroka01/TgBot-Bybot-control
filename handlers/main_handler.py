"""
Обработчики основных команд и навигации
"""
from aiogram import Router, F
from aiogram.types import Message, CallbackQuery
from aiogram.filters import Command, CommandStart

from handlers.base_handler import BaseHandler
from buttons import create_main_menu, create_back_button
from core.decorators import handle_errors
from core.factories import bot_factory

class MainHandler(BaseHandler):
    """Обработчик основных команд"""
    
    WELCOME_MESSAGE = "👋 Добро пожаловать!\nПомощь тут: /help\nВыберите действие:"
    HELP_MESSAGE = (
        "Доступные команды:\n"
        "/start - Начало работы с ботом\n"
        "/help - Список доступных команд\n"
        "📊 Стата - Получить статистику\n"
        "💸 Бабит - Меню для торговли\n"
        "🔔 Уведомления - Управление уведомлениями\n"
        "👤 Аккаунт - Информация об аккаунте\n"
        "📰 Новости - Последние новости о криптовалютах\n"
        "💱 Конвертер - Конвертация валют"
    )
    
    def __init__(self):
        super().__init__()
        self.router = Router()
        self.dp = bot_factory.get_dispatcher()
    
    def register_handlers(self):
        """Регистрация обработчиков"""
        
        @self.router.message(CommandStart())
        @handle_errors("Ошибка обработки команды /start")
        async def handle_start(message: Message):
            user_id = self.get_user_id(message)
            await self.send_message_safely(
                user_id, 
                self.WELCOME_MESSAGE, 
                create_main_menu()
            )
        
        @self.router.message(Command('help'))
        @handle_errors("Ошибка обработки команды /help")
        async def handle_help(message: Message):
            user_id = self.get_user_id(message)
            await self.send_message_safely(
                user_id, 
                self.HELP_MESSAGE, 
                create_back_button("menu")
            )
        
        @self.router.callback_query(F.data.startswith("back_to_"))
        @handle_errors("Ошибка обработки возврата")
        async def handle_back_navigation(call: CallbackQuery):
            user_id = self.get_user_id(call)
            message_id = self.get_message_id(call)
            
            menu_type = call.data.split("_")[2]
            
            if menu_type == "menu":
                await self.edit_message_safely(
                    user_id, 
                    message_id, 
                    self.WELCOME_MESSAGE, 
                    create_main_menu()
                )
            # Другие типы меню будут обрабатываться соответствующими обработчиками
            
            await call.answer()
        
        @self.router.message(F.text.in_([
            "📊 Стата", "💸 Бабит", "🔔 Уведомления", 
            "👤 Аккаунт", "📰 Новости", "💱 Конвертер"
        ]))
        @handle_errors("Ошибка обработки текстового сообщения")
        async def handle_text_menu(message: Message):
            user_id = self.get_user_id(message)
            
            # Эти сообщения будут обрабатываться соответствующими обработчиками
            # через callback данные. Здесь просто отправляем главное меню
            await self.send_message_safely(
                user_id,
                "Используйте кнопки меню для навигации:",
                create_main_menu()
            )
        
        # Регистрируем роутер в диспетчере
        self.dp.include_router(self.router)
