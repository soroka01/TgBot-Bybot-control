"""
Обработчики для статистики и рыночных данных
"""
from handlers.base_handler import BaseHandler
from services.market_service import market_service
from buttons import create_back_button
from core.decorators import handle_errors
from core.config import config

class StatHandler(BaseHandler):
    """Обработчик статистики и рыночных данных"""
    
    def __init__(self):
        super().__init__()
        self.market_service = market_service
    
    def register_handlers(self):
        """Регистрация обработчиков"""
        
        @self.bot.callback_query_handler(func=lambda call: call.data == "stat")
        @handle_errors("Ошибка получения статистики")
        def handle_stat_callback(call):
            user_id = self.get_user_id(call)
            self._send_statistics(user_id)
        
        @self.bot.message_handler(func=lambda message: message.text == "📊 Стата")
        @handle_errors("Ошибка получения статистики")
        def handle_stat_message(message):
            user_id = self.get_user_id(message)
            self._send_statistics(user_id)
    
    @handle_errors("Ошибка формирования статистики")
    def _send_statistics(self, user_id: int):
        """Отправить статистику пользователю"""
        try:
            # Получаем данные
            rsi = self.market_service.calculate_rsi(str(config.DEFAULT_TIMEFRAME))
            current_price = self.market_service.get_current_price()
            buy_sell_ratio = self.market_service.get_buy_sell_ratio(str(config.DEFAULT_TIMEFRAME))
            
            # Создаем график
            screenshot, min_price_14d = self.market_service.create_price_chart(weeks=5)
            
            if screenshot is None or min_price_14d is None:
                raise Exception("Не удалось создать график или получить минимальную цену")
            
            # Рассчитываем изменение за 14 дней
            change_percent = round((current_price - min_price_14d) / min_price_14d * 100, 2)
            
            # Формируем подпись
            caption = (
                f"📊 Статистика {config.SYMBOL}\n\n"
                f"📉 Изменение за 14 дней: {change_percent:+.2f}%\n"
                f"📊 RSI: {rsi}\n"
                f"📈 Соотношение: {buy_sell_ratio}\n"
                f"💲 Текущая цена: {current_price:,.2f} USDT\n"
                f"🔻 Мин. за 14 дней: {min_price_14d:,.2f} USDT"
            )
            
            # Отправляем фото с подписью
            self.send_photo_safely(
                chat_id=user_id,
                photo=screenshot,
                caption=caption,
                reply_markup=create_back_button("menu")
            )
            
        except Exception as e:
            error_message = f"⚠️ Ошибка при получении статистики: {str(e)}"
            self.send_message_safely(
                user_id,
                error_message,
                create_back_button("menu")
            )
