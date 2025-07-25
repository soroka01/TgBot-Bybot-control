"""
Обработчики для новостей и аккаунта
"""
from handlers.base_handler import BaseHandler
from services.news_service import news_service
from services.database_service import db_service
from services.trading_service import trading_service
from buttons import create_account_menu, create_back_button
from core.decorators import handle_errors

class NewsAndAccountHandler(BaseHandler):
    """Обработчик новостей и информации об аккаунте"""
    
    def __init__(self):
        super().__init__()
        self.news_service = news_service
        self.db_service = db_service
        self.trading_service = trading_service
    
    def register_handlers(self):
        """Регистрация обработчиков"""
        
        @self.bot.callback_query_handler(func=lambda call: call.data == "news")
        @handle_errors("Ошибка получения новостей")
        def handle_news(call):
            user_id = self.get_user_id(call)
            message_id = self.get_message_id(call)
            
            self._send_news(user_id, message_id)
        
        @self.bot.callback_query_handler(func=lambda call: call.data == "account")
        @handle_errors("Ошибка получения информации об аккаунте")
        def handle_account(call):
            user_id = self.get_user_id(call)
            message_id = self.get_message_id(call)
            
            self._send_account_info(user_id, message_id)
        
        @self.bot.callback_query_handler(func=lambda call: call.data == "back_to_account")
        @handle_errors("Ошибка возврата в аккаунт")
        def handle_back_to_account(call):
            user_id = self.get_user_id(call)
            message_id = self.get_message_id(call)
            
            self._send_account_info(user_id, message_id)
        
        @self.bot.callback_query_handler(func=lambda call: call.data == "change_name")
        @handle_errors("Ошибка изменения имени")
        def handle_change_name(call):
            user_id = self.get_user_id(call)
            message_id = self.get_message_id(call)
            
            self._start_name_change(user_id, message_id)
    
    @handle_errors("Ошибка получения новостей")
    def _send_news(self, user_id: int, message_id: int):
        """Отправить новости"""
        try:
            # Получаем новости и краткую сводку рынка
            news = self.news_service.get_latest_crypto_news()
            market_summary = self.news_service.get_market_summary()
            
            full_news = f"{news}\n\n{market_summary}"
            
            self.edit_message_safely(
                user_id,
                message_id,
                full_news,
                create_back_button("menu")
            )
            
        except Exception as e:
            error_message = f"⚠️ Ошибка при получении новостей: {str(e)}"
            self.edit_message_safely(
                user_id,
                message_id,
                error_message,
                create_back_button("menu")
            )
    
    @handle_errors("Ошибка получения информации об аккаунте")
    def _send_account_info(self, user_id: int, message_id: int):
        """Отправить информацию об аккаунте"""
        try:
            # Получаем данные пользователя
            user_data = self.db_service.get(user_id)
            user_name = user_data.get("name", "Не установлено")
            
            # Получаем баланс
            try:
                btc_balance, usdt_balance = self.trading_service.get_balance()
                balance_info = (
                    f"💰 Баланс:\n"
                    f"₿ BTC: {btc_balance}\n"
                    f"💵 USDT: {usdt_balance}"
                )
            except Exception as e:
                balance_info = f"⚠️ Ошибка получения баланса: {str(e)}"
            
            # Получаем статистику алертов
            price_alerts = self.db_service.get_user_alerts(user_id, "price_alerts")
            rsi_alerts = self.db_service.get_user_alerts(user_id, "rsi_alerts")
            
            account_message = (
                f"👤 Информация об аккаунте\n\n"
                f"🆔 ID: {user_id}\n"
                f"👤 Имя: {user_name}\n"
                f"🔔 Ценовых алертов: {len(price_alerts)}\n"
                f"📊 RSI алертов: {len(rsi_alerts)}\n\n"
                f"{balance_info}"
            )
            
            self.edit_message_safely(
                user_id,
                message_id,
                account_message,
                create_account_menu()
            )
            
        except Exception as e:
            error_message = f"⚠️ Ошибка при получении информации об аккаунте: {str(e)}"
            self.edit_message_safely(
                user_id,
                message_id,
                error_message,
                create_back_button("menu")
            )
    
    def _start_name_change(self, user_id: int, message_id: int):
        """Начать процесс смены имени"""
        message_text = (
            "✏️ Введите новое имя:\n\n"
            "💡 Имя будет отображаться в информации об аккаунте"
        )
        
        self.edit_message_safely(
            user_id,
            message_id,
            message_text,
            create_back_button("account")
        )
        
        self.bot.register_next_step_handler_by_chat_id(
            user_id,
            self._process_name_change
        )
    
    @handle_errors("Ошибка изменения имени")
    def _process_name_change(self, message):
        """Обработать изменение имени"""
        user_id = self.get_user_id(message)
        new_name = message.text.strip()
        
        if not new_name or len(new_name) > 50:
            error_message = "⚠️ Имя должно содержать от 1 до 50 символов"
            self.send_message_safely(
                user_id,
                error_message,
                create_back_button("account")
            )
            return
        
        try:
            # Получаем текущие данные пользователя
            user_data = self.db_service.get(user_id)
            user_data["name"] = new_name
            
            # Сохраняем обновленные данные
            success = self.db_service.save(user_id, user_data)
            
            if success:
                success_message = f"✅ Имя успешно изменено на: {new_name}"
            else:
                success_message = "⚠️ Ошибка при сохранении имени"
            
            self.send_message_safely(
                user_id,
                success_message,
                create_back_button("account")
            )
            
        except Exception as e:
            error_message = f"⚠️ Ошибка при изменении имени: {str(e)}"
            self.send_message_safely(
                user_id,
                error_message,
                create_back_button("account")
            )
