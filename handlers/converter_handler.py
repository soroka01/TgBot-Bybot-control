"""
Обработчики для конвертера валют
"""
from telebot import types
from handlers.base_handler import BaseHandler
from services.converter_service import converter_service
from buttons import create_back_button
from core.decorators import handle_errors

class ConverterHandler(BaseHandler):
    """Обработчик конвертера валют"""
    
    def __init__(self):
        super().__init__()
        self.converter_service = converter_service
    
    def register_handlers(self):
        """Регистрация обработчиков"""
        
        @self.bot.callback_query_handler(func=lambda call: call.data == "converter")
        @handle_errors("Ошибка открытия конвертера")
        def handle_converter_menu(call):
            user_id = self.get_user_id(call)
            message_id = self.get_message_id(call)
            
            markup = self._create_converter_menu()
            
            self.edit_message_safely(
                user_id,
                message_id,
                "💱 Конвертер валют:",
                markup
            )
        
        @self.bot.callback_query_handler(func=lambda call: call.data in [
            "convert_usd_to_btc", "convert_btc_to_usd"
        ])
        @handle_errors("Ошибка инициации конвертации")
        def handle_conversion_start(call):
            user_id = self.get_user_id(call)
            message_id = self.get_message_id(call)
            
            if call.data == "convert_usd_to_btc":
                self._start_usd_to_btc_conversion(user_id, message_id)
            elif call.data == "convert_btc_to_usd":
                self._start_btc_to_usd_conversion(user_id, message_id)
        
        @self.bot.callback_query_handler(func=lambda call: call.data == "back_to_converter")
        @handle_errors("Ошибка возврата в конвертер")
        def handle_back_to_converter(call):
            user_id = self.get_user_id(call)
            message_id = self.get_message_id(call)
            
            markup = self._create_converter_menu()
            
            self.edit_message_safely(
                user_id,
                message_id,
                "💱 Конвертер валют:",
                markup
            )
    
    def _create_converter_menu(self):
        """Создать меню конвертера"""
        markup = types.InlineKeyboardMarkup()
        
        markup.add(
            types.InlineKeyboardButton(
                "💵 USD → ₿ BTC", 
                callback_data="convert_usd_to_btc"
            )
        )
        markup.add(
            types.InlineKeyboardButton(
                "₿ BTC → 💵 USD", 
                callback_data="convert_btc_to_usd"
            )
        )
        markup.add(
            types.InlineKeyboardButton(
                "🔙 Назад", 
                callback_data="back_to_menu"
            )
        )
        
        return markup
    
    def _start_usd_to_btc_conversion(self, user_id: int, message_id: int):
        """Начать конвертацию USD в BTC"""
        message_text = (
            "💵 Введите количество USD для конвертации в BTC:\n\n"
            "💡 Примеры: 100, 500, 1000"
        )
        
        self.edit_message_safely(
            user_id,
            message_id,
            message_text,
            create_back_button("converter")
        )
        
        self.bot.register_next_step_handler_by_chat_id(
            user_id,
            lambda msg: self._process_conversion(msg, "USD", "BTC")
        )
    
    def _start_btc_to_usd_conversion(self, user_id: int, message_id: int):
        """Начать конвертацию BTC в USD"""
        message_text = (
            "₿ Введите количество BTC для конвертации в USD:\n\n"
            "💡 Примеры: 0.001, 0.01, 0.1"
        )
        
        self.edit_message_safely(
            user_id,
            message_id,
            message_text,
            create_back_button("converter")
        )
        
        self.bot.register_next_step_handler_by_chat_id(
            user_id,
            lambda msg: self._process_conversion(msg, "BTC", "USD")
        )
    
    @handle_errors("Ошибка конвертации")
    def _process_conversion(self, message, from_currency: str, to_currency: str):
        """Обработать конвертацию"""
        user_id = self.get_user_id(message)
        
        try:
            amount = float(message.text.strip())
            
            if amount <= 0:
                raise ValueError("Количество должно быть положительным")
            
            # Выполняем конвертацию
            result = self.converter_service.convert(amount, from_currency, to_currency)
            
            # Форматируем результат
            formatted_result = self.converter_service.format_conversion_result(
                amount, from_currency, to_currency, result
            )
            
            self.send_message_safely(
                user_id,
                formatted_result,
                create_back_button("converter")
            )
            
        except ValueError as e:
            if "положительным" in str(e):
                error_message = "⚠️ Введите положительное число"
            else:
                error_message = "⚠️ Введите корректное число"
                
            self.send_message_safely(
                user_id,
                error_message,
                create_back_button("converter")
            )
        except Exception as e:
            error_message = f"⚠️ Ошибка конвертации: {str(e)}"
            self.send_message_safely(
                user_id,
                error_message,
                create_back_button("converter")
            )
