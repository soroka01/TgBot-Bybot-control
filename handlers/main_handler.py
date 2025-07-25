"""
Обработчики основных команд и навигации
"""
from handlers.base_handler import BaseHandler
from buttons import create_main_menu, create_back_button
from core.decorators import handle_errors

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
    
    def register_handlers(self):
        """Регистрация обработчиков"""
        
        @self.bot.message_handler(commands=['start', 'help'])
        @handle_errors("Ошибка обработки команды")
        def handle_start_help(message):
            user_id = self.get_user_id(message)
            
            if message.text == "/start":
                self.send_message_safely(
                    user_id, 
                    self.WELCOME_MESSAGE, 
                    create_main_menu()
                )
            elif message.text == "/help":
                self.send_message_safely(
                    user_id, 
                    self.HELP_MESSAGE, 
                    create_back_button("menu")
                )
        
        @self.bot.callback_query_handler(func=lambda call: call.data.startswith("back_to_"))
        @handle_errors("Ошибка обработки возврата")
        def handle_back_navigation(call):
            user_id = self.get_user_id(call)
            message_id = self.get_message_id(call)
            
            # Очищаем обработчики следующего шага
            self.bot.clear_step_handler_by_chat_id(chat_id=user_id)
            
            menu_type = call.data.split("_")[2]
            
            if menu_type == "menu":
                self.edit_message_safely(
                    user_id, 
                    message_id, 
                    self.WELCOME_MESSAGE, 
                    create_main_menu()
                )
            # Другие типы меню будут обрабатываться соответствующими обработчиками
        
        @self.bot.message_handler(func=lambda message: message.text in [
            "📊 Стата", "💸 Бабит", "🔔 Уведомления", 
            "👤 Аккаунт", "📰 Новости", "💱 Конвертер"
        ])
        @handle_errors("Ошибка обработки текстового сообщения")
        def handle_text_menu(message):
            user_id = self.get_user_id(message)
            
            # Эти сообщения будут обрабатываться соответствующими обработчиками
            # через callback данные. Здесь просто отправляем главное меню
            self.send_message_safely(
                user_id,
                "Используйте кнопки меню для навигации:",
                create_main_menu()
            )
