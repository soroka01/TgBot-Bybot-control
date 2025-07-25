"""
Обработчики для торговых операций
"""
from aiogram import Router, F
from aiogram.types import Message, CallbackQuery
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup

from handlers.base_handler import BaseHandler
from services.trading_service import trading_service
from services.market_service import market_service
from buttons import create_babit_menu, create_back_button
from core.decorators import handle_errors
from core.factories import bot_factory

class TradeStates(StatesGroup):
    """Состояния для процесса торговли"""
    waiting_for_amount = State()

class TradingHandler(BaseHandler):
    """Обработчик торговых операций"""
    
    def __init__(self):
        super().__init__()
        self.trading_service = trading_service
        self.market_service = market_service
        self.router = Router()
        self.dp = bot_factory.get_dispatcher()
    
    def register_handlers(self):
        """Регистрация обработчиков"""
        
        @self.router.callback_query(F.data == "babit")
        @handle_errors("Ошибка открытия торгового меню")
        async def handle_babit_menu(call: CallbackQuery):
            user_id = self.get_user_id(call)
            message_id = self.get_message_id(call)
            
            await self.edit_message_safely(
                user_id,
                message_id,
                "🛠️ Торговое меню:",
                create_babit_menu()
            )
            await call.answer()
        
        @self.router.callback_query(F.data.in_([
            "balance", "history", "trade", "current_price"
        ]))
        @handle_errors("Ошибка обработки торгового действия")
        async def handle_trading_actions(call: CallbackQuery, state: FSMContext):
            user_id = self.get_user_id(call)
            message_id = self.get_message_id(call)
            
            if call.data == "balance":
                await self._show_balance(user_id, message_id)
            elif call.data == "history":
                await self._show_history(user_id, message_id)
            elif call.data == "trade":
                await self._start_trade(user_id, message_id, state)
            elif call.data == "current_price":
                await self._show_current_price(user_id, message_id)
            
            await call.answer()
        
        @self.router.callback_query(F.data == "back_to_babit")
        @handle_errors("Ошибка возврата в торговое меню")
        async def handle_back_to_babit(call: CallbackQuery, state: FSMContext):
            user_id = self.get_user_id(call)
            message_id = self.get_message_id(call)
            
            # Очищаем состояние
            await state.clear()
            
            await self.edit_message_safely(
                user_id,
                message_id,
                "🛠️ Торговое меню:",
                create_babit_menu()
            )
            await call.answer()
        
        @self.router.message(TradeStates.waiting_for_amount)
        @handle_errors("Ошибка обработки торговой операции")
        async def process_trade_input(message: Message, state: FSMContext):
            """Обработать ввод суммы для торговли"""
            user_id = self.get_user_id(message)
            amount_text = message.text.strip()
            
            try:
                amount_usdt = float(amount_text)
                
                if amount_usdt <= 0:
                    raise ValueError("Сумма должна быть положительной")
                
                # Выполняем торговую операцию
                order_result = self.trading_service.place_market_buy_order(amount_usdt)
                
                success_message = (
                    f"✅ Ордер успешно размещен!\n\n"
                    f"💰 Потрачено: {amount_usdt} USDT\n"
                    f"₿ Получено: {order_result.get('qty', 'N/A')} BTC\n"
                    f"� ID ордера: {order_result.get('orderId', 'N/A')}"
                )
                
                await self.send_message_safely(
                    user_id,
                    success_message,
                    create_back_button("babit")
                )
                
            except ValueError as e:
                error_message = "⚠️ Введите корректное положительное число"
                await self.send_message_safely(
                    user_id,
                    error_message,
                    create_back_button("babit")
                )
            except Exception as e:
                error_message = f"⚠️ Ошибка при размещении ордера: {str(e)}"
                await self.send_message_safely(
                    user_id,
                    error_message,
                    create_back_button("babit")
                )
            finally:
                # Очищаем состояние
                await state.clear()
        
        # Регистрируем роутер в диспетчере
        self.dp.include_router(self.router)
    
    @handle_errors("Ошибка получения баланса")
    async def _show_balance(self, user_id: int, message_id: int):
        """Показать баланс"""
        try:
            btc_balance, usdt_balance = self.trading_service.get_balance()
            
            balance_message = (
                f"💰 Ваш баланс:\n\n"
                f"₿ BTC: {btc_balance}\n"
                f"� USDT: {usdt_balance}"
            )
            
            await self.edit_message_safely(
                user_id,
                message_id,
                balance_message,
                create_back_button("babit")
            )
            
        except Exception as e:
            error_message = f"⚠️ Ошибка при получении баланса: {str(e)}"
            await self.edit_message_safely(
                user_id,
                message_id,
                error_message,
                create_back_button("babit")
            )
    
    @handle_errors("Ошибка получения истории")
    async def _show_history(self, user_id: int, message_id: int):
        """Показать историю торгов"""
        try:
            history_data = self.trading_service.get_trade_history()
            formatted_history = self.trading_service.format_trade_history(history_data)
            
            history_message = f"� История торговых операций:\n\n{formatted_history}"
            
            await self.edit_message_safely(
                user_id,
                message_id,
                history_message,
                create_back_button("babit")
            )
            
        except Exception as e:
            error_message = f"⚠️ Ошибка при получении истории: {str(e)}"
            await self.edit_message_safely(
                user_id,
                message_id,
                error_message,
                create_back_button("babit")
            )
    
    @handle_errors("Ошибка инициации торговли")
    async def _start_trade(self, user_id: int, message_id: int, state: FSMContext):
        """Начать процесс торговли"""
        trade_message = (
            "💵 Введите количество USDT для покупки BTC:\n\n"
            "� Примеры: 10, 50, 100"
        )
        
        await self.edit_message_safely(
            user_id,
            message_id,
            trade_message,
            create_back_button("babit")
        )
        
        # Устанавливаем состояние ожидания ввода суммы
        await state.set_state(TradeStates.waiting_for_amount)
    
    @handle_errors("Ошибка получения текущей цены")
    async def _show_current_price(self, user_id: int, message_id: int):
        """Показать текущую цену"""
        try:
            current_price = self.market_service.get_current_price()
            daily_change = self.market_service.get_daily_change_percent()
            
            price_message = (
                f"💲 Текущая цена BTC:\n\n"
                f"📈 {current_price:,.2f} USDT\n"
                f"📊 Изменение за день: {daily_change}"
            )
            
            await self.edit_message_safely(
                user_id,
                message_id,
                price_message,
                create_back_button("babit")
            )
            
        except Exception as e:
            error_message = f"⚠️ Ошибка при получении цены: {str(e)}"
            await self.edit_message_safely(
                user_id,
                message_id,
                error_message,
                create_back_button("babit")
            )
