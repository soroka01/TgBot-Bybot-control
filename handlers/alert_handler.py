"""
Обработчики для уведомлений и алертов
"""
from aiogram import Router, F
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup

from handlers.base_handler import BaseHandler
from services.alert_service import alert_service
from buttons import create_notifications_menu, create_back_button
from core.decorators import handle_errors
from core.factories import bot_factory

class AlertStates(StatesGroup):
    """Состояния для процесса создания алертов"""
    waiting_for_price = State()
    waiting_for_rsi = State()

class AlertHandler(BaseHandler):
    """Обработчик уведомлений и алертов"""
    
    def __init__(self):
        super().__init__()
        self.alert_service = alert_service
        self.router = Router()
        self.dp = bot_factory.get_dispatcher()
    
    def register_handlers(self):
        """Регистрация обработчиков"""
        
        @self.router.callback_query(F.data == "notifications")
        @handle_errors("Ошибка открытия меню уведомлений")
        async def handle_notifications_menu(call: CallbackQuery):
            user_id = self.get_user_id(call)
            message_id = self.get_message_id(call)
            
            await self.edit_message_safely(
                user_id,
                message_id,
                "🔔 Управление уведомлениями:",
                create_notifications_menu()
            )
            await call.answer()
        
        @self.router.callback_query(F.data.in_([
            "set_price_alert", "list_price_alerts", "set_rsi_alert", "list_rsi_alerts",
            "delete_price_alert", "delete_rsi_alert", "delete_all_price_alerts", 
            "delete_all_rsi_alerts", "delete_all_alerts"
        ]))
        @handle_errors("Ошибка обработки действия с алертами")
        async def handle_alert_actions(call: CallbackQuery, state: FSMContext):
            user_id = self.get_user_id(call)
            message_id = self.get_message_id(call)
            
            if call.data == "set_price_alert":
                await self._start_price_alert_setup(user_id, message_id, state)
            elif call.data == "list_price_alerts":
                await self._list_price_alerts(user_id, message_id)
            elif call.data == "set_rsi_alert":
                await self._start_rsi_alert_setup(user_id, message_id, state)
            elif call.data == "list_rsi_alerts":
                await self._list_rsi_alerts(user_id, message_id)
            elif call.data == "delete_price_alert":
                await self._start_delete_price_alert(user_id, message_id)
            elif call.data == "delete_rsi_alert":
                await self._start_delete_rsi_alert(user_id, message_id)
            elif call.data == "delete_all_price_alerts":
                await self._delete_all_price_alerts(user_id, message_id)
            elif call.data == "delete_all_rsi_alerts":
                await self._delete_all_rsi_alerts(user_id, message_id)
            elif call.data == "delete_all_alerts":
                await self._delete_all_alerts(user_id, message_id)
                
            await call.answer()
        
        @self.router.callback_query(F.data == "back_to_notifications")
        @handle_errors("Ошибка возврата в меню уведомлений")
        async def handle_back_to_notifications(call: CallbackQuery, state: FSMContext):
            user_id = self.get_user_id(call)
            message_id = self.get_message_id(call)
            
            # Очищаем состояние
            await state.clear()
            
            await self.edit_message_safely(
                user_id,
                message_id,
                "🔔 Управление уведомлениями:",
                create_notifications_menu()
            )
            await call.answer()
        
        # Обработчики для callback данных алертов
        @self.router.callback_query(F.data.startswith("alert_"))
        @handle_errors("Ошибка обработки настройки алерта")
        async def handle_alert_setup_callbacks(call: CallbackQuery):
            await self._handle_alert_type_selection(call)
            
        # Обработчики для ввода данных алертов
        @self.router.message(AlertStates.waiting_for_price)
        @handle_errors("Ошибка обработки ценового алерта")
        async def process_price_alert_input(message: Message, state: FSMContext):
            await self._process_price_alert_input(message, state)
            
        @self.router.message(AlertStates.waiting_for_rsi)
        @handle_errors("Ошибка обработки RSI алерта")
        async def process_rsi_alert_input(message: Message, state: FSMContext):
            await self._process_rsi_alert_input(message, state)
        
        # Регистрируем роутер в диспетчере
        self.dp.include_router(self.router)
    
    async def _start_price_alert_setup(self, user_id: int, message_id: int, state: FSMContext):
        """Начать настройку ценового алерта"""
        message_text = (
            "💲 Введите уровень цены для уведомления:\n\n"
            "💡 Примеры: 50000, 75000, 100000"
        )
        
        await self.edit_message_safely(
            user_id,
            message_id,
            message_text,
            create_back_button("notifications")
        )
        
        await state.set_state(AlertStates.waiting_for_price)
    
    async def _start_rsi_alert_setup(self, user_id: int, message_id: int, state: FSMContext):
        """Начать настройку RSI алерта"""
        message_text = (
            "📊 Введите уровень RSI для уведомления:\n\n"
            "💡 Примеры: 30, 70, 80"
        )
        
        await self.edit_message_safely(
            user_id,
            message_id,
            message_text,
            create_back_button("notifications")
        )
        
        await state.set_state(AlertStates.waiting_for_rsi)
    
    async def _process_price_alert_input(self, message: Message, state: FSMContext):
        """Обработать ввод ценового алерта"""
        user_id = self.get_user_id(message)
        price_text = message.text.strip()
        
        try:
            price_level = float(price_text)
            
            if price_level <= 0:
                raise ValueError("Цена должна быть положительной")
            
            # Создаем клавиатуру с типами алертов
            markup = InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="🔔 Одноразовый", callback_data=f"alert_once_{price_level}")],
                [InlineKeyboardButton(text="🔄 Постоянный", callback_data=f"alert_permanent_{price_level}")],
                [InlineKeyboardButton(text="🔙 Назад", callback_data="back_to_notifications")]
            ])
            
            alert_message = (
                f"💰 Уровень цены: {price_level:,.2f} USDT\n\n"
                f"Выберите тип уведомления:"
            )
            
            await self.send_message_safely(
                user_id,
                alert_message,
                markup
            )
            
        except ValueError:
            error_message = "⚠️ Введите корректное положительное число"
            await self.send_message_safely(
                user_id,
                error_message,
                create_back_button("notifications")
            )
        except Exception as e:
            error_message = f"⚠️ Ошибка: {str(e)}"
            await self.send_message_safely(
                user_id,
                error_message,
                create_back_button("notifications")
            )
        finally:
            await state.clear()
    
    async def _process_rsi_alert_input(self, message: Message, state: FSMContext):
        """Обработать ввод RSI алерта"""
        user_id = self.get_user_id(message)
        rsi_text = message.text.strip()
        
        try:
            rsi_level = float(rsi_text)
            
            if not (0 <= rsi_level <= 100):
                raise ValueError("RSI должен быть от 0 до 100")
            
            # Создаем клавиатуру с условиями
            markup = InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="📈 Больше", callback_data=f"rsi_alert_above_{rsi_level}")],
                [InlineKeyboardButton(text="📉 Меньше", callback_data=f"rsi_alert_below_{rsi_level}")],
                [InlineKeyboardButton(text="🔙 Назад", callback_data="back_to_notifications")]
            ])
            
            alert_message = (
                f"📊 Уровень RSI: {rsi_level}\n\n"
                f"Выберите условие срабатывания:"
            )
            
            await self.send_message_safely(
                user_id,
                alert_message,
                markup
            )
            
        except ValueError as e:
            error_message = f"⚠️ {str(e)}"
            await self.send_message_safely(
                user_id,
                error_message,
                create_back_button("notifications")
            )
        except Exception as e:
            error_message = f"⚠️ Ошибка: {str(e)}"
            await self.send_message_safely(
                user_id,
                error_message,
                create_back_button("notifications")
            )
        finally:
            await state.clear()
    
    async def _handle_alert_type_selection(self, call: CallbackQuery):
        """Обработать выбор типа алерта"""
        user_id = self.get_user_id(call)
        
        try:
            if call.data.startswith("alert_once_"):
                price_level = float(call.data.split("_")[2])
                success = self.alert_service.add_price_alert(user_id, price_level, permanent=False)
                
                if success:
                    message = f"✅ Одноразовый алерт на цену {price_level:,.2f} USDT установлен!"
                else:
                    message = "⚠️ Не удалось установить алерт. Возможно, превышен лимит алертов."
                    
            elif call.data.startswith("alert_permanent_"):
                price_level = float(call.data.split("_")[2])
                success = self.alert_service.add_price_alert(user_id, price_level, permanent=True)
                
                if success:
                    message = f"✅ Постоянный алерт на цену {price_level:,.2f} USDT установлен!"
                else:
                    message = "⚠️ Не удалось установить алерт. Возможно, превышен лимит алертов."
                    
            elif call.data.startswith("rsi_alert_above_"):
                rsi_level = float(call.data.split("_")[3])
                success = self.alert_service.add_rsi_alert(user_id, rsi_level, condition="above")
                
                if success:
                    message = f"✅ RSI алерт установлен: когда RSI > {rsi_level}"
                else:
                    message = "⚠️ Не удалось установить RSI алерт."
                    
            elif call.data.startswith("rsi_alert_below_"):
                rsi_level = float(call.data.split("_")[3])
                success = self.alert_service.add_rsi_alert(user_id, rsi_level, condition="below")
                
                if success:
                    message = f"✅ RSI алерт установлен: когда RSI < {rsi_level}"
                else:
                    message = "⚠️ Не удалось установить RSI алерт."
            else:
                message = "⚠️ Неизвестный тип алерта"
            
            await self.bot.edit_message_text(
                chat_id=user_id,
                message_id=call.message.message_id,
                text=message,
                reply_markup=create_back_button("notifications")
            )
            
        except Exception as e:
            error_message = f"⚠️ Ошибка: {str(e)}"
            await self.bot.edit_message_text(
                chat_id=user_id,
                message_id=call.message.message_id,
                text=error_message,
                reply_markup=create_back_button("notifications")
            )
            
        await call.answer()
    
    async def _list_price_alerts(self, user_id: int, message_id: int):
        """Показать список ценовых алертов"""
        try:
            alerts = self.alert_service.get_price_alerts(user_id)
            
            if not alerts:
                message = "📭 У вас нет активных ценовых алертов"
            else:
                alert_list = []
                for i, alert in enumerate(alerts, 1):
                    alert_type = "🔄 Постоянный" if alert.get('permanent', False) else "🔔 Одноразовый"
                    alert_list.append(f"{i}. {alert['price']:,.2f} USDT ({alert_type})")
                
                message = f"💰 Ваши ценовые алерты:\n\n" + "\n".join(alert_list)
            
            await self.edit_message_safely(
                user_id,
                message_id,
                message,
                create_back_button("notifications")
            )
            
        except Exception as e:
            error_message = f"⚠️ Ошибка при получении алертов: {str(e)}"
            await self.edit_message_safely(
                user_id,
                message_id,
                error_message,
                create_back_button("notifications")
            )
    
    async def _list_rsi_alerts(self, user_id: int, message_id: int):
        """Показать список RSI алертов"""
        try:
            alerts = self.alert_service.get_rsi_alerts(user_id)
            
            if not alerts:
                message = "📭 У вас нет активных RSI алертов"
            else:
                alert_list = []
                for i, alert in enumerate(alerts, 1):
                    condition = "📈 больше" if alert.get('condition') == 'above' else "📉 меньше"
                    alert_list.append(f"{i}. RSI {condition} {alert['level']}")
                
                message = f"📊 Ваши RSI алерты:\n\n" + "\n".join(alert_list)
            
            await self.edit_message_safely(
                user_id,
                message_id,
                message,
                create_back_button("notifications")
            )
            
        except Exception as e:
            error_message = f"⚠️ Ошибка при получении RSI алертов: {str(e)}"
            await self.edit_message_safely(
                user_id,
                message_id,
                error_message,
                create_back_button("notifications")
            )
    
    # Заглушки для остальных методов
    async def _start_delete_price_alert(self, user_id: int, message_id: int):
        """Начать удаление ценового алерта"""
        message = "🗑️ Функция удаления отдельных алертов будет добавлена позже"
        await self.edit_message_safely(user_id, message_id, message, create_back_button("notifications"))
    
    async def _start_delete_rsi_alert(self, user_id: int, message_id: int):
        """Начать удаление RSI алерта"""
        message = "🗑️ Функция удаления отдельных RSI алертов будет добавлена позже"
        await self.edit_message_safely(user_id, message_id, message, create_back_button("notifications"))
    
    async def _delete_all_price_alerts(self, user_id: int, message_id: int):
        """Удалить все ценовые алерты"""
        try:
            count = self.alert_service.delete_all_price_alerts(user_id)
            message = f"🗑️ Удалено {count} ценовых алертов"
        except Exception as e:
            message = f"⚠️ Ошибка при удалении алертов: {str(e)}"
        
        await self.edit_message_safely(user_id, message_id, message, create_back_button("notifications"))
    
    async def _delete_all_rsi_alerts(self, user_id: int, message_id: int):
        """Удалить все RSI алерты"""
        try:
            count = self.alert_service.delete_all_rsi_alerts(user_id)
            message = f"🗑️ Удалено {count} RSI алертов"
        except Exception as e:
            message = f"⚠️ Ошибка при удалении RSI алертов: {str(e)}"
        
        await self.edit_message_safely(user_id, message_id, message, create_back_button("notifications"))
    
    async def _delete_all_alerts(self, user_id: int, message_id: int):
        """Удалить все алерты"""
        try:
            price_count = self.alert_service.delete_all_price_alerts(user_id)
            rsi_count = self.alert_service.delete_all_rsi_alerts(user_id)
            total_count = price_count + rsi_count
            message = f"🗑️ Удалено {total_count} алертов (ценовых: {price_count}, RSI: {rsi_count})"
        except Exception as e:
            message = f"⚠️ Ошибка при удалении всех алертов: {str(e)}"
        
        await self.edit_message_safely(user_id, message_id, message, create_back_button("notifications"))
