"""
Обработчики для конвертера валют
"""
from aiogram import Router, F
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup

from handlers.base_handler import BaseHandler
from services.converter_service import converter_service
from buttons import create_back_button
from core.decorators import handle_errors
from core.factories import bot_factory

class ConverterStates(StatesGroup):
    """Состояния для процесса конвертации"""
    waiting_for_usd_amount = State()
    waiting_for_btc_amount = State()

class ConverterHandler(BaseHandler):
    """Обработчик конвертера валют"""
    
    def __init__(self):
        super().__init__()
        self.converter_service = converter_service
        self.router = Router()
        self.dp = bot_factory.get_dispatcher()
    
    def register_handlers(self):
        """Регистрация обработчиков"""
        
        @self.router.callback_query(F.data == "converter")
        @handle_errors("Ошибка открытия конвертера")
        async def handle_converter_menu(call: CallbackQuery):
            user_id = self.get_user_id(call)
            message_id = self.get_message_id(call)
            
            markup = self._create_converter_menu()
            
            await self.edit_message_safely(
                user_id,
                message_id,
                "💱 Конвертер валют:",
                markup
            )
            await call.answer()
        
        @self.router.callback_query(F.data.in_([
            "convert_usd_to_btc", "convert_btc_to_usd"
        ]))
        @handle_errors("Ошибка инициации конвертации")
        async def handle_conversion_start(call: CallbackQuery, state: FSMContext):
            user_id = self.get_user_id(call)
            message_id = self.get_message_id(call)
            
            if call.data == "convert_usd_to_btc":
                await self._start_usd_to_btc_conversion(user_id, message_id, state)
            elif call.data == "convert_btc_to_usd":
                await self._start_btc_to_usd_conversion(user_id, message_id, state)
                
            await call.answer()
        
        @self.router.callback_query(F.data == "back_to_converter")
        @handle_errors("Ошибка возврата в конвертер")
        async def handle_back_to_converter(call: CallbackQuery, state: FSMContext):
            user_id = self.get_user_id(call)
            message_id = self.get_message_id(call)
            
            # Очищаем состояние
            await state.clear()
            
            markup = self._create_converter_menu()
            
            await self.edit_message_safely(
                user_id,
                message_id,
                "💱 Конвертер валют:",
                markup
            )
            await call.answer()
        
        # Обработчики для ввода данных
        @self.router.message(ConverterStates.waiting_for_usd_amount)
        @handle_errors("Ошибка обработки конвертации USD в BTC")
        async def process_usd_to_btc_input(message: Message, state: FSMContext):
            await self._process_usd_to_btc_input(message, state)
            
        @self.router.message(ConverterStates.waiting_for_btc_amount)
        @handle_errors("Ошибка обработки конвертации BTC в USD")
        async def process_btc_to_usd_input(message: Message, state: FSMContext):
            await self._process_btc_to_usd_input(message, state)
        
        # Регистрируем роутер в диспетчере
        self.dp.include_router(self.router)
    
    def _create_converter_menu(self):
        """Создать меню конвертера"""
        markup = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="💵 USD ➡️ BTC", callback_data="convert_usd_to_btc")],
            [InlineKeyboardButton(text="₿ BTC ➡️ USD", callback_data="convert_btc_to_usd")],
            [InlineKeyboardButton(text="🔙 Назад", callback_data="back_to_menu")]
        ])
        return markup
    
    async def _start_usd_to_btc_conversion(self, user_id: int, message_id: int, state: FSMContext):
        """Начать конвертацию USD в BTC"""
        current_price = self.converter_service.get_current_btc_price()
        
        message_text = (
            f"💵 Конвертация USD в BTC\n\n"
            f"💲 Текущая цена BTC: {current_price:,.2f} USD\n\n"
            f"Введите сумму в USD для конвертации:"
        )
        
        await self.edit_message_safely(
            user_id,
            message_id,
            message_text,
            create_back_button("converter")
        )
        
        await state.set_state(ConverterStates.waiting_for_usd_amount)
    
    async def _start_btc_to_usd_conversion(self, user_id: int, message_id: int, state: FSMContext):
        """Начать конвертацию BTC в USD"""
        current_price = self.converter_service.get_current_btc_price()
        
        message_text = (
            f"₿ Конвертация BTC в USD\n\n"
            f"💲 Текущая цена BTC: {current_price:,.2f} USD\n\n"
            f"Введите количество BTC для конвертации:"
        )
        
        await self.edit_message_safely(
            user_id,
            message_id,
            message_text,
            create_back_button("converter")
        )
        
        await state.set_state(ConverterStates.waiting_for_btc_amount)
    
    async def _process_usd_to_btc_input(self, message: Message, state: FSMContext):
        """Обработать ввод суммы USD для конвертации в BTC"""
        user_id = self.get_user_id(message)
        amount_text = message.text.strip()
        
        try:
            usd_amount = float(amount_text)
            
            if usd_amount <= 0:
                raise ValueError("Сумма должна быть положительной")
            
            # Выполняем конвертацию
            result = self.converter_service.convert_usd_to_btc(usd_amount)
            
            result_message = (
                f"💱 Результат конвертации:\n\n"
                f"💵 {usd_amount:,.2f} USD\n"
                f"⬇️\n"
                f"₿ {result['btc_amount']:.8f} BTC\n\n"
                f"💲 Курс: {result['rate']:,.2f} USD/BTC\n"
                f"⏰ Время: {result['timestamp']}"
            )
            
            await self.send_message_safely(
                user_id,
                result_message,
                create_back_button("converter")
            )
            
        except ValueError:
            error_message = "⚠️ Введите корректное положительное число"
            await self.send_message_safely(
                user_id,
                error_message,
                create_back_button("converter")
            )
        except Exception as e:
            error_message = f"⚠️ Ошибка конвертации: {str(e)}"
            await self.send_message_safely(
                user_id,
                error_message,
                create_back_button("converter")
            )
        finally:
            await state.clear()
    
    async def _process_btc_to_usd_input(self, message: Message, state: FSMContext):
        """Обработать ввод количества BTC для конвертации в USD"""
        user_id = self.get_user_id(message)
        amount_text = message.text.strip()
        
        try:
            btc_amount = float(amount_text)
            
            if btc_amount <= 0:
                raise ValueError("Количество должно быть положительным")
            
            # Выполняем конвертацию
            result = self.converter_service.convert_btc_to_usd(btc_amount)
            
            result_message = (
                f"💱 Результат конвертации:\n\n"
                f"₿ {btc_amount:.8f} BTC\n"
                f"⬇️\n"
                f"💵 {result['usd_amount']:,.2f} USD\n\n"
                f"💲 Курс: {result['rate']:,.2f} USD/BTC\n"
                f"⏰ Время: {result['timestamp']}"
            )
            
            await self.send_message_safely(
                user_id,
                result_message,
                create_back_button("converter")
            )
            
        except ValueError:
            error_message = "⚠️ Введите корректное положительное число"
            await self.send_message_safely(
                user_id,
                error_message,
                create_back_button("converter")
            )
        except Exception as e:
            error_message = f"⚠️ Ошибка конвертации: {str(e)}"
            await self.send_message_safely(
                user_id,
                error_message,
                create_back_button("converter")
            )
        finally:
            await state.clear()
