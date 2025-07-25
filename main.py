"""
Главное приложение с улучшенной архитектурой
"""
import asyncio
import signal
import sys
import time
from pathlib import Path

# Добавляем корневую директорию в путь для импортов
sys.path.insert(0, str(Path(__file__).parent))

from core.logging_config import logger
from core.config import config
from services.database_service import db_service
from handlers.handler_manager import handler_manager
from tasks.task_service import task_service

class TelegramBot:
    """Основной класс телеграм бота"""
    
    def __init__(self):
        self.logger = logger
        self.db_service = db_service
        self.handler_manager = handler_manager
        self.task_service = task_service
        self.bot = None
        self.dp = None
        self._running = False
        
        # Настройка обработки сигналов для корректного завершения
        signal.signal(signal.SIGINT, self._signal_handler)
        signal.signal(signal.SIGTERM, self._signal_handler)
    
    def _signal_handler(self, signum, frame):
        """Обработчик сигналов для корректного завершения"""
        self.logger.info(f"Получен сигнал {signum}, завершение работы...")
        asyncio.create_task(self.stop())
        sys.exit(0)
    
    async def initialize(self):
        """Инициализация всех компонентов"""
        try:
            self.logger.info("=== Инициализация Telegram Bot ===")
            
            # Инициализация базы данных
            self.logger.info("Инициализация базы данных...")
            # База данных уже инициализируется в конструкторе DatabaseService
            
            # Регистрация обработчиков
            self.logger.info("Регистрация обработчиков...")
            self.handler_manager.register_all_handlers()
            self.bot = self.handler_manager.get_bot()
            self.dp = self.handler_manager.get_dispatcher()
            
            if not self.bot:
                raise Exception("Не удалось получить экземпляр бота")
            
            # Настройка задач
            self.logger.info("Настройка планировщика задач...")
            self.task_service.setup_tasks()
            
            self.logger.info("=== Инициализация завершена ===")
            return True
            
        except Exception as e:
            self.logger.error(f"Ошибка инициализации: {e}")
            return False
    
    async def start(self):
        """Запуск бота"""
        if not await self.initialize():
            self.logger.error("Не удалось инициализировать бот")
            return False
        
        try:
            self.logger.info("=== Запуск бота ===")
            
            # Запуск планировщика задач
            self.logger.info("Запуск планировщика задач...")
            self.task_service.start_scheduler()
            
            # Запуск бота
            self.logger.info(f"Запуск Telegram бота (токен: {config.TG_TOKEN[:10]}...)")
            self.logger.info(f"Обслуживаемые пользователи: {config.TG_IDS}")
            
            self._running = True
            
            # Отправка уведомления о запуске администраторам
            await self._send_startup_notification()
            
            # Запуск polling
            await self.dp.start_polling(self.bot)
            
        except Exception as e:
            self.logger.error(f"Ошибка запуска бота: {e}")
            await self.stop()
            return False
    
    async def stop(self):
        """Остановка бота"""
        if not self._running:
            return
        
        self.logger.info("=== Остановка бота ===")
        self._running = False
        
        try:
            # Остановка планировщика
            self.logger.info("Остановка планировщика задач...")
            self.task_service.stop_scheduler()
            
            # Отправка уведомления об остановке
            await self._send_shutdown_notification()
            
            # Остановка бота
            if self.bot:
                self.logger.info("Остановка Telegram бота...")
                await self.bot.session.close()
            
            self.logger.info("=== Бот остановлен ===")
            
        except Exception as e:
            self.logger.error(f"Ошибка при остановке бота: {e}")
    
    async def _send_startup_notification(self):
        """Отправить уведомление о запуске бота"""
        try:
            message = (
                f"🤖 Бот запущен!\n\n"
                f"⏰ Время запуска: {time.strftime('%Y-%m-%d %H:%M:%S')}\n"
                f"🔧 Версия: 2.0 (Refactored)\n"
                f"📊 Символ: {config.SYMBOL}\n"
                f"⚙️ Таймфрейм: {config.DEFAULT_TIMEFRAME}мин"
            )
            
            for admin_id in config.TG_IDS[:1]:  # Отправляем только первому админу
                try:
                    await self.bot.send_message(admin_id, message)
                except Exception as e:
                    self.logger.warning(f"Не удалось отправить уведомление о запуске пользователю {admin_id}: {e}")
                    
        except Exception as e:
            self.logger.error(f"Ошибка отправки уведомления о запуске: {e}")
    
    async def _send_shutdown_notification(self):
        """Отправить уведомление об остановке бота"""
        try:
            message = (
                f"🛑 Бот остановлен\n\n"
                f"⏰ Время остановки: {time.strftime('%Y-%m-%d %H:%M:%S')}"
            )
            
            for admin_id in config.TG_IDS[:1]:  # Отправляем только первому админу
                try:
                    await self.bot.send_message(admin_id, message)
                except Exception as e:
                    self.logger.warning(f"Не удалось отправить уведомление об остановке пользователю {admin_id}: {e}")
                    
        except Exception as e:
            self.logger.error(f"Ошибка отправки уведомления об остановке: {e}")
    
    def get_status(self):
        """Получить статус бота"""
        return {
            "running": self._running,
            "bot_initialized": self.bot is not None,
            "scheduler_running": self.task_service._running if hasattr(self.task_service, '_running') else False,
            "config": {
                "symbol": config.SYMBOL,
                "timeframe": config.DEFAULT_TIMEFRAME,
                "users_count": len(config.TG_IDS),
                "max_alerts": config.MAX_ALERTS_PER_USER
            }
        }

async def main():
    """Главная функция"""
    try:
        # Создание и запуск бота
        bot_app = TelegramBot()
        
        # Вывод информации о конфигурации
        logger.info("=== Конфигурация ===")
        logger.info(f"Символ: {config.SYMBOL}")
        logger.info(f"Таймфрейм: {config.DEFAULT_TIMEFRAME}")
        logger.info(f"Пользователи: {config.TG_IDS}")
        logger.info(f"База данных: {config.DB_PATH}")
        logger.info(f"Максимум алертов на пользователя: {config.MAX_ALERTS_PER_USER}")
        
        # Запуск
        success = await bot_app.start()
        
        if not success:
            logger.error("Не удалось запустить бота")
            return 1
            
        return 0
        
    except KeyboardInterrupt:
        logger.info("Получен сигнал прерывания")
        return 0
    except Exception as e:
        logger.error(f"Критическая ошибка: {e}")
        return 1

if __name__ == '__main__':
    exit_code = asyncio.run(main())
    sys.exit(exit_code)
