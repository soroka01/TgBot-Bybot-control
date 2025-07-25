"""
Обработчики для уведомлений и алертов
"""
from telebot import types
from handlers.base_handler import BaseHandler
from services.alert_service import alert_service
from buttons import create_notifications_menu, create_back_button
from core.decorators import handle_errors

class AlertHandler(BaseHandler):
    """Обработчик уведомлений и алертов"""
    
    def __init__(self):
        super().__init__()
        self.alert_service = alert_service
    
    def register_handlers(self):
        """Регистрация обработчиков"""
        
        @self.bot.callback_query_handler(func=lambda call: call.data == "notifications")
        @handle_errors("Ошибка открытия меню уведомлений")
        def handle_notifications_menu(call):
            user_id = self.get_user_id(call)
            message_id = self.get_message_id(call)
            
            self.edit_message_safely(
                user_id,
                message_id,
                "🔔 Управление уведомлениями:",
                create_notifications_menu()
            )
        
        @self.bot.callback_query_handler(func=lambda call: call.data in [
            "set_price_alert", "list_price_alerts", "set_rsi_alert", "list_rsi_alerts",
            "delete_price_alert", "delete_rsi_alert", "delete_all_price_alerts", 
            "delete_all_rsi_alerts", "delete_all_alerts"
        ])
        @handle_errors("Ошибка обработки действия с алертами")
        def handle_alert_actions(call):
            user_id = self.get_user_id(call)
            message_id = self.get_message_id(call)
            
            if call.data == "set_price_alert":
                self._start_price_alert_setup(user_id, message_id)
            elif call.data == "list_price_alerts":
                self._list_price_alerts(user_id, message_id)
            elif call.data == "set_rsi_alert":
                self._start_rsi_alert_setup(user_id, message_id)
            elif call.data == "list_rsi_alerts":
                self._list_rsi_alerts(user_id, message_id)
            elif call.data == "delete_price_alert":
                self._start_delete_price_alert(user_id, message_id)
            elif call.data == "delete_rsi_alert":
                self._start_delete_rsi_alert(user_id, message_id)
            elif call.data == "delete_all_price_alerts":
                self._delete_all_price_alerts(user_id, message_id)
            elif call.data == "delete_all_rsi_alerts":
                self._delete_all_rsi_alerts(user_id, message_id)
            elif call.data == "delete_all_alerts":
                self._delete_all_alerts(user_id, message_id)
        
        @self.bot.callback_query_handler(func=lambda call: call.data == "back_to_notifications")
        @handle_errors("Ошибка возврата в меню уведомлений")
        def handle_back_to_notifications(call):
            user_id = self.get_user_id(call)
            message_id = self.get_message_id(call)
            
            self.edit_message_safely(
                user_id,
                message_id,
                "🔔 Управление уведомлениями:",
                create_notifications_menu()
            )
        
        # Обработчики для callback данных алертов
        @self.bot.callback_query_handler(func=lambda call: call.data.startswith("alert_"))
        @handle_errors("Ошибка обработки настройки алерта")
        def handle_alert_setup_callbacks(call):
            self._handle_alert_type_selection(call)
    
    def _start_price_alert_setup(self, user_id: int, message_id: int):
        """Начать настройку ценового алерта"""
        message_text = (
            "💲 Введите уровень цены для уведомления:\n\n"
            "💡 Примеры: 50000, 75000, 100000"
        )
        
        self.edit_message_safely(
            user_id,
            message_id,
            message_text,
            create_back_button("notifications")
        )
        
        self.bot.register_next_step_handler_by_chat_id(
            user_id,
            lambda msg: self._process_price_alert_input(msg, "price")
        )
    
    def _start_rsi_alert_setup(self, user_id: int, message_id: int):
        """Начать настройку RSI алерта"""
        message_text = (
            "📊 Введите уровень RSI для уведомления:\n\n"
            "💡 Примеры: 30, 70 (значение от 0 до 100)"
        )
        
        self.edit_message_safely(
            user_id,
            message_id,
            message_text,
            create_back_button("notifications")
        )
        
        self.bot.register_next_step_handler_by_chat_id(
            user_id,
            lambda msg: self._process_rsi_alert_input(msg)
        )
    
    @handle_errors("Ошибка обработки ввода ценового алерта")
    def _process_price_alert_input(self, message, alert_type):
        """Обработать ввод ценового алерта"""
        user_id = self.get_user_id(message)
        
        try:
            price_level = float(message.text.strip())
            
            # Создаем кнопки выбора типа алерта
            markup = types.InlineKeyboardMarkup()
            markup.add(
                types.InlineKeyboardButton(
                    "Однократно", 
                    callback_data=f"alert_once_{alert_type}_{price_level}"
                )
            )
            markup.add(
                types.InlineKeyboardButton(
                    "Постоянно", 
                    callback_data=f"alert_permanent_{alert_type}_{price_level}"
                )
            )
            markup.add(
                types.InlineKeyboardButton(
                    "🔙 Назад", 
                    callback_data="back_to_notifications"
                )
            )
            
            self.send_message_safely(
                user_id,
                f"💲 Цена: {price_level} USDT\nВыберите тип уведомления:",
                markup
            )
            
        except ValueError:
            self.send_message_safely(
                user_id,
                "⚠️ Введите корректное число",
                create_back_button("notifications")
            )
    
    @handle_errors("Ошибка обработки ввода RSI алерта")
    def _process_rsi_alert_input(self, message):
        """Обработать ввод RSI алерта"""
        user_id = self.get_user_id(message)
        
        try:
            rsi_level = float(message.text.strip())
            
            if not (0 <= rsi_level <= 100):
                raise ValueError("RSI должен быть в диапазоне 0-100")
            
            # Создаем кнопки выбора условия
            markup = types.InlineKeyboardMarkup()
            markup.add(
                types.InlineKeyboardButton(
                    f"RSI < {rsi_level}", 
                    callback_data=f"alert_rsi_below_{rsi_level}"
                )
            )
            markup.add(
                types.InlineKeyboardButton(
                    f"RSI > {rsi_level}", 
                    callback_data=f"alert_rsi_above_{rsi_level}"
                )
            )
            markup.add(
                types.InlineKeyboardButton(
                    "🔙 Назад", 
                    callback_data="back_to_notifications"
                )
            )
            
            self.send_message_safely(
                user_id,
                f"📊 RSI: {rsi_level}\nВыберите условие:",
                markup
            )
            
        except ValueError as e:
            self.send_message_safely(
                user_id,
                f"⚠️ {str(e)}",
                create_back_button("notifications")
            )
    
    @handle_errors("Ошибка настройки алерта")
    def _handle_alert_type_selection(self, call):
        """Обработать выбор типа алерта"""
        user_id = self.get_user_id(call)
        parts = call.data.split("_")
        
        try:
            if len(parts) >= 4 and parts[1] == "once":
                # alert_once_price_50000
                alert_type = parts[2]  # price
                value = float(parts[3])
                permanent = False
                
                if alert_type == "price":
                    success = self.alert_service.add_price_alert(user_id, value, permanent)
                    if success:
                        message = f"✅ Однократное уведомление на цену {value} USDT установлено"
                    else:
                        message = "⚠️ Ошибка при установке уведомления"
                
            elif len(parts) >= 4 and parts[1] == "permanent":
                # alert_permanent_price_50000
                alert_type = parts[2]  # price
                value = float(parts[3])
                permanent = True
                
                if alert_type == "price":
                    success = self.alert_service.add_price_alert(user_id, value, permanent)
                    if success:
                        message = f"✅ Постоянное уведомление на цену {value} USDT установлено"
                    else:
                        message = "⚠️ Ошибка при установке уведомления"
                        
            elif len(parts) >= 4 and parts[1] == "rsi":
                # alert_rsi_below_30 или alert_rsi_above_70
                condition = parts[2]  # below/above
                value = float(parts[3])
                
                success = self.alert_service.add_rsi_alert(user_id, value, condition, permanent=False)
                condition_text = "<" if condition == "below" else ">"
                if success:
                    message = f"✅ Уведомление RSI {condition_text} {value} установлено"
                else:
                    message = "⚠️ Ошибка при установке уведомления"
            else:
                message = "⚠️ Неверный формат данных"
            
            self.bot.edit_message_text(
                chat_id=user_id,
                message_id=call.message.message_id,
                text=message,
                reply_markup=create_back_button("notifications")
            )
            
        except Exception as e:
            error_message = f"⚠️ Ошибка: {str(e)}"
            self.bot.edit_message_text(
                chat_id=user_id,
                message_id=call.message.message_id,
                text=error_message,
                reply_markup=create_back_button("notifications")
            )
    
    def _list_price_alerts(self, user_id: int, message_id: int):
        """Показать список ценовых алертов"""
        alerts = self.alert_service.get_price_alerts(user_id)
        formatted_alerts = self.alert_service.format_price_alerts(alerts)
        
        self.edit_message_safely(
            user_id,
            message_id,
            formatted_alerts,
            create_back_button("notifications")
        )
    
    def _list_rsi_alerts(self, user_id: int, message_id: int):
        """Показать список RSI алертов"""
        alerts = self.alert_service.get_rsi_alerts(user_id)
        formatted_alerts = self.alert_service.format_rsi_alerts(alerts)
        
        self.edit_message_safely(
            user_id,
            message_id,
            formatted_alerts,
            create_back_button("notifications")
        )
    
    def _start_delete_price_alert(self, user_id: int, message_id: int):
        """Начать удаление ценового алерта"""
        alerts = self.alert_service.get_price_alerts(user_id)
        
        if not alerts:
            self.edit_message_safely(
                user_id,
                message_id,
                "У вас нет активных ценовых алертов",
                create_back_button("notifications")
            )
            return
        
        formatted_alerts = self.alert_service.format_price_alerts(alerts)
        message_text = f"{formatted_alerts}\n\nВведите номер алерта для удаления:"
        
        self.edit_message_safely(
            user_id,
            message_id,
            message_text,
            create_back_button("notifications")
        )
        
        self.bot.register_next_step_handler_by_chat_id(
            user_id,
            lambda msg: self._process_delete_alert(msg, "price_alerts")
        )
    
    def _start_delete_rsi_alert(self, user_id: int, message_id: int):
        """Начать удаление RSI алерта"""
        alerts = self.alert_service.get_rsi_alerts(user_id)
        
        if not alerts:
            self.edit_message_safely(
                user_id,
                message_id,
                "У вас нет активных RSI алертов",
                create_back_button("notifications")
            )
            return
        
        formatted_alerts = self.alert_service.format_rsi_alerts(alerts)
        message_text = f"{formatted_alerts}\n\nВведите номер алерта для удаления:"
        
        self.edit_message_safely(
            user_id,
            message_id,
            message_text,
            create_back_button("notifications")
        )
        
        self.bot.register_next_step_handler_by_chat_id(
            user_id,
            lambda msg: self._process_delete_alert(msg, "rsi_alerts")
        )
    
    @handle_errors("Ошибка удаления алерта")
    def _process_delete_alert(self, message, alert_type):
        """Обработать удаление алерта по номеру"""
        user_id = self.get_user_id(message)
        
        try:
            alert_index = int(message.text.strip()) - 1
            success = self.alert_service.remove_alert(user_id, alert_index, alert_type)
            
            if success:
                message_text = "✅ Алерт успешно удален"
            else:
                message_text = "⚠️ Ошибка при удалении алерта"
                
        except ValueError:
            message_text = "⚠️ Введите корректный номер алерта"
        except Exception as e:
            message_text = f"⚠️ Ошибка: {str(e)}"
        
        self.send_message_safely(
            user_id,
            message_text,
            create_back_button("notifications")
        )
    
    def _delete_all_price_alerts(self, user_id: int, message_id: int):
        """Удалить все ценовые алерты"""
        success = self.alert_service.clear_alerts(user_id, "price_alerts")
        
        if success:
            message = "✅ Все ценовые алерты удалены"
        else:
            message = "⚠️ Ошибка при удалении алертов"
        
        self.edit_message_safely(
            user_id,
            message_id,
            message,
            create_back_button("notifications")
        )
    
    def _delete_all_rsi_alerts(self, user_id: int, message_id: int):
        """Удалить все RSI алерты"""
        success = self.alert_service.clear_alerts(user_id, "rsi_alerts")
        
        if success:
            message = "✅ Все RSI алерты удалены"
        else:
            message = "⚠️ Ошибка при удалении алертов"
        
        self.edit_message_safely(
            user_id,
            message_id,
            message,
            create_back_button("notifications")
        )
    
    def _delete_all_alerts(self, user_id: int, message_id: int):
        """Удалить все алерты"""
        success = self.alert_service.clear_alerts(user_id)
        
        if success:
            message = "✅ Все алерты удалены"
        else:
            message = "⚠️ Ошибка при удалении алертов"
        
        self.edit_message_safely(
            user_id,
            message_id,
            message,
            create_back_button("notifications")
        )
