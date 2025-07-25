"""
Обработчики для статистики и рыночных данных
"""
from aiogram import Router, F
from aiogram.types import Message, CallbackQuery

from handlers.base_handler import BaseHandler
from services.market_service import market_service
from buttons import create_back_button
from core.decorators import handle_errors
from core.config import config
from core.factories import bot_factory

class StatHandler(BaseHandler):
    """Обработчик статистики и рыночных данных"""
    
    def __init__(self):
        super().__init__()
        self.market_service = market_service
        self.router = Router()
        self.dp = bot_factory.get_dispatcher()
    
    def register_handlers(self):
        """Регистрация обработчиков"""
        
        @self.router.callback_query(F.data == "stat")
        @handle_errors("Ошибка получения статистики")
        async def handle_stat_callback(call: CallbackQuery):
            user_id = self.get_user_id(call)
            await self._send_statistics(user_id)
            await call.answer()
        
        @self.router.message(F.text == "📊 Стата")
        @handle_errors("Ошибка получения статистики")
        async def handle_stat_message(message: Message):
            user_id = self.get_user_id(message)
            await self._send_statistics(user_id)
        
        # Регистрируем роутер в диспетчере
        self.dp.include_router(self.router)
    
    @handle_errors("Ошибка формирования статистики")
    async def _send_statistics(self, user_id: int):
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
            await self.send_photo_safely(
                chat_id=user_id,
                photo=screenshot,
                caption=caption,
                reply_markup=create_back_button("menu")
            )
            
        except Exception as e:
            error_message = f"⚠️ Ошибка при получении статистики: {str(e)}"
            await self.send_message_safely(
                user_id,
                error_message,
                create_back_button("menu")
            )
