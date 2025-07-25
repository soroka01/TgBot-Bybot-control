"""
Улучшенная система задач и уведомлений
"""
import schedule
import time
import pytz
from datetime import datetime
from threading import Thread
from typing import List, Dict, Any

from core.base_service import BaseService
from core.config import config
from core.decorators import handle_errors, log_function_call
from core.factories import bot_factory
from services.alert_service import alert_service
from services.market_service import market_service

class TaskService(BaseService):
    """Сервис для управления задачами"""
    
    def __init__(self):
        super().__init__()
        self.bot = bot_factory.get_bot()
        self.alert_service = alert_service
        self.market_service = market_service
        self.moscow_tz = pytz.timezone('Europe/Moscow')
        self._scheduler_thread = None
        self._running = False
    
    def start_scheduler(self):
        """Запустить планировщик задач"""
        if self._running:
            self.logger.warning("Планировщик уже запущен")
            return
        
        self._running = True
        self._scheduler_thread = Thread(target=self._run_scheduler, daemon=True)
        self._scheduler_thread.start()
        self.logger.info("Планировщик задач запущен")
    
    def stop_scheduler(self):
        """Остановить планировщик задач"""
        self._running = False
        if self._scheduler_thread:
            self._scheduler_thread.join(timeout=5)
        self.logger.info("Планировщик задач остановлен")
    
    def _run_scheduler(self):
        """Основной цикл планировщика"""
        self.logger.info("Цикл планировщика начат")
        
        while self._running:
            try:
                schedule.run_pending()
                time.sleep(1)
            except Exception as e:
                self.logger.error(f"Ошибка в планировщике: {e}")
                time.sleep(5)  # Небольшая пауза при ошибке
    
    def setup_tasks(self):
        """Настроить все задачи"""
        # Проверка алертов каждую минуту
        schedule.every().minute.do(self.check_all_alerts)
        
        # Ежедневное обновление в 8:30 по Москве
        schedule.every().day.at("08:30").do(self.daily_update)
        
        # Очистка старых данных раз в день в 2:00
        schedule.every().day.at("02:00").do(self.cleanup_old_data)
        
        self.logger.info("Задачи настроены")
    
    @handle_errors("Ошибка проверки алертов")
    @log_function_call()
    def check_all_alerts(self):
        """Проверить все алерты"""
        try:
            # Проверяем ценовые алерты
            price_alerts = self.alert_service.check_price_alerts()
            for alert_data in price_alerts:
                self._send_price_alert_notification(alert_data)
            
            # Проверяем RSI алерты
            rsi_alerts = self.alert_service.check_rsi_alerts()
            for alert_data in rsi_alerts:
                self._send_rsi_alert_notification(alert_data)
                
        except Exception as e:
            self.logger.error(f"Ошибка при проверке алертов: {e}")
    
    @handle_errors("Ошибка ежедневного обновления")
    @log_function_call()
    def daily_update(self):
        """Ежедневное обновление - отправка статистики всем пользователям"""
        moscow_time = datetime.now(self.moscow_tz)
        self.logger.info(f"Ежедневное обновление в {moscow_time}")
        
        try:
            for user_id in config.TG_IDS:
                self._send_daily_stats(user_id)
                time.sleep(1)  # Небольшая задержка между отправками
                
        except Exception as e:
            self.logger.error(f"Ошибка ежедневного обновления: {e}")
    
    @handle_errors("Ошибка очистки данных")
    @log_function_call()
    def cleanup_old_data(self):
        """Очистка старых данных (можно расширить по необходимости)"""
        self.logger.info("Очистка старых данных выполнена")
        # Здесь можно добавить логику очистки старых алертов, логов и т.д.
    
    @handle_errors("Ошибка отправки уведомления о цене")
    def _send_price_alert_notification(self, alert_data: Dict[str, Any]):
        """Отправить уведомление о цене"""
        user_id = alert_data['user_id']
        alert = alert_data['alert']
        current_price = alert_data['current_price']
        
        message = (
            f"🔔 Ценовой алерт сработал!\n\n"
            f"🎯 Целевая цена: {alert['price']} USDT\n"
            f"💲 Текущая цена: {current_price:,.2f} USDT\n"
            f"📈 Цена достигла вашего уровня!"
        )
        
        try:
            self.bot.send_message(user_id, message)
            self.logger.info(f"Отправлено ценовое уведомление пользователю {user_id}")
        except Exception as e:
            self.logger.error(f"Ошибка отправки ценового уведомления пользователю {user_id}: {e}")
    
    @handle_errors("Ошибка отправки уведомления о RSI")
    def _send_rsi_alert_notification(self, alert_data: Dict[str, Any]):
        """Отправить уведомление о RSI"""
        user_id = alert_data['user_id']
        alert = alert_data['alert']
        current_rsi = alert_data['current_rsi']
        
        condition_text = "<" if alert['condition'] == 'below' else ">"
        
        message = (
            f"📊 RSI алерт сработал!\n\n"
            f"🎯 Условие: RSI {condition_text} {alert['level']}\n"
            f"📊 Текущий RSI: {current_rsi}\n"
            f"⚡ Условие выполнено!"
        )
        
        try:
            self.bot.send_message(user_id, message)
            self.logger.info(f"Отправлено RSI уведомление пользователю {user_id}")
        except Exception as e:
            self.logger.error(f"Ошибка отправки RSI уведомления пользователю {user_id}: {e}")
    
    @handle_errors("Ошибка отправки ежедневной статистики")
    def _send_daily_stats(self, user_id: int):
        """Отправить ежедневную статистику пользователю"""
        try:
            # Получаем данные
            rsi = self.market_service.calculate_rsi(str(config.DEFAULT_TIMEFRAME))
            current_price = self.market_service.get_current_price()
            buy_sell_ratio = self.market_service.get_buy_sell_ratio(str(config.DEFAULT_TIMEFRAME))
            
            # Создаем график
            screenshot, min_price_14d = self.market_service.create_price_chart(weeks=5)
            
            if screenshot is None or min_price_14d is None:
                raise Exception("Не удалось создать график")
            
            # Рассчитываем изменение за 14 дней
            change_percent = round((current_price - min_price_14d) / min_price_14d * 100, 2)
            
            # Формируем подпись
            caption = (
                f"🌅 Ежедневная сводка {config.SYMBOL}\n\n"
                f"📉 Изменение за 14 дней: {change_percent:+.2f}%\n"
                f"📊 RSI: {rsi}\n"
                f"📈 Соотношение: {buy_sell_ratio}\n"
                f"💲 Текущая цена: {current_price:,.2f} USDT"
            )
            
            # Отправляем фото с подписью
            self.bot.send_photo(
                chat_id=user_id,
                photo=screenshot,
                caption=caption
            )
            
            self.logger.info(f"Отправлена ежедневная статистика пользователю {user_id}")
            
        except Exception as e:
            error_message = f"⚠️ Ошибка при получении ежедневной статистики: {str(e)}"
            try:
                self.bot.send_message(user_id, error_message)
            except:
                pass  # Игнорируем ошибки отправки сообщения об ошибке
            
            self.logger.error(f"Ошибка отправки ежедневной статистики пользователю {user_id}: {e}")

# Глобальный экземпляр сервиса
task_service = TaskService()
