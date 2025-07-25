"""
Обработчики для торговых операций
"""
from handlers.base_handler import BaseHandler
from services.trading_service import trading_service
from services.market_service import market_service
from buttons import create_babit_menu, create_back_button
from core.decorators import handle_errors

class TradingHandler(BaseHandler):
    """Обработчик торговых операций"""
    
    def __init__(self):
        super().__init__()
        self.trading_service = trading_service
        self.market_service = market_service
    
    def register_handlers(self):
        """Регистрация обработчиков"""
        
        @self.bot.callback_query_handler(func=lambda call: call.data == "babit")
        @handle_errors("Ошибка открытия торгового меню")
        def handle_babit_menu(call):
            user_id = self.get_user_id(call)
            message_id = self.get_message_id(call)
            
            self.edit_message_safely(
                user_id,
                message_id,
                "🛠️ Торговое меню:",
                create_babit_menu()
            )
        
        @self.bot.callback_query_handler(func=lambda call: call.data in [
            "balance", "history", "trade", "current_price"
        ])
        @handle_errors("Ошибка обработки торгового действия")
        def handle_trading_actions(call):
            user_id = self.get_user_id(call)
            message_id = self.get_message_id(call)
            
            if call.data == "balance":
                self._show_balance(user_id, message_id)
            elif call.data == "history":
                self._show_history(user_id, message_id)
            elif call.data == "trade":
                self._start_trade(user_id, message_id)
            elif call.data == "current_price":
                self._show_current_price(user_id, message_id)
        
        @self.bot.callback_query_handler(func=lambda call: call.data == "back_to_babit")
        @handle_errors("Ошибка возврата в торговое меню")
        def handle_back_to_babit(call):
            user_id = self.get_user_id(call)
            message_id = self.get_message_id(call)
            
            self.edit_message_safely(
                user_id,
                message_id,
                "🛠️ Торговое меню:",
                create_babit_menu()
            )
    
    @handle_errors("Ошибка получения баланса")
    def _show_balance(self, user_id: int, message_id: int):
        """Показать баланс"""
        try:
            btc_balance, usdt_balance = self.trading_service.get_balance()
            
            balance_message = (
                f"💰 Ваш баланс:\n\n"
                f"₿ BTC: {btc_balance}\n"
                f"💵 USDT: {usdt_balance}"
            )
            
            self.edit_message_safely(
                user_id,
                message_id,
                balance_message,
                create_back_button("babit")
            )
            
        except Exception as e:
            error_message = f"⚠️ Ошибка при получении баланса: {str(e)}"
            self.edit_message_safely(
                user_id,
                message_id,
                error_message,
                create_back_button("babit")
            )
    
    @handle_errors("Ошибка получения истории")
    def _show_history(self, user_id: int, message_id: int):
        """Показать историю торгов"""
        try:
            history_data = self.trading_service.get_trade_history()
            formatted_history = self.trading_service.format_trade_history(history_data)
            
            history_message = f"📜 История торговых операций:\n\n{formatted_history}"
            
            self.edit_message_safely(
                user_id,
                message_id,
                history_message,
                create_back_button("babit")
            )
            
        except Exception as e:
            error_message = f"⚠️ Ошибка при получении истории: {str(e)}"
            self.edit_message_safely(
                user_id,
                message_id,
                error_message,
                create_back_button("babit")
            )
    
    @handle_errors("Ошибка инициации торговли")
    def _start_trade(self, user_id: int, message_id: int):
        """Начать процесс торговли"""
        trade_message = (
            "💵 Введите количество USDT для покупки BTC:\n\n"
            "💡 Примеры: 10, 50, 100"
        )
        
        self.edit_message_safely(
            user_id,
            message_id,
            trade_message,
            create_back_button("babit")
        )
        
        # Регистрируем обработчик следующего сообщения
        self.bot.register_next_step_handler_by_chat_id(
            user_id, 
            self._process_trade_input
        )
    
    @handle_errors("Ошибка обработки торговой операции")
    def _process_trade_input(self, message):
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
                f"📋 ID ордера: {order_result.get('orderId', 'N/A')}"
            )
            
            self.send_message_safely(
                user_id,
                success_message,
                create_back_button("babit")
            )
            
        except ValueError as e:
            error_message = "⚠️ Введите корректное положительное число"
            self.send_message_safely(
                user_id,
                error_message,
                create_back_button("babit")
            )
        except Exception as e:
            error_message = f"⚠️ Ошибка при размещении ордера: {str(e)}"
            self.send_message_safely(
                user_id,
                error_message,
                create_back_button("babit")
            )
    
    @handle_errors("Ошибка получения текущей цены")
    def _show_current_price(self, user_id: int, message_id: int):
        """Показать текущую цену"""
        try:
            current_price = self.market_service.get_current_price()
            daily_change = self.market_service.get_daily_change_percent()
            
            price_message = (
                f"💲 Текущая цена BTC:\n\n"
                f"📈 {current_price:,.2f} USDT\n"
                f"📊 Изменение за день: {daily_change}"
            )
            
            self.edit_message_safely(
                user_id,
                message_id,
                price_message,
                create_back_button("babit")
            )
            
        except Exception as e:
            error_message = f"⚠️ Ошибка при получении цены: {str(e)}"
            self.edit_message_safely(
                user_id,
                message_id,
                error_message,
                create_back_button("babit")
            )
