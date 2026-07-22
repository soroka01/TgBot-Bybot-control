# telegram_bot/bot.py
"""
Главный файл Telegram бота для управления торговлей
"""
import asyncio
import sys
from pathlib import Path

# Добавляем корневую директорию в путь
sys.path.insert(0, str(Path(__file__).parent.parent))

from aiogram import Bot, Dispatcher
from aiogram.enums import ParseMode
from aiogram.client.default import DefaultBotProperties

from config import TELEGRAM_TOKEN
from utils.logger_setup import logger

# Импортируем все handlers
from telegram_bot.handlers import (
    activity, alerts, auto_mode, fallbacks, market_overview, positions, settings,
    start, trading,
)
from core.alert_scheduler import AlertScheduler
from storage.database import get_store
from telegram_bot.activity_middleware import TradingAccessMiddleware, UserActivityMiddleware
from telegram_bot.ui import (
    CancelLiveUpdatesMiddleware,
    register_bot,
    restore_screen_targets,
    unregister_bot,
)


async def main():
    """Главная функция запуска бота"""
    if not TELEGRAM_TOKEN:
        logger.error("TELEGRAM_TOKEN не установлен")
        logger.error("Задайте токен перед запуском Telegram-бота")
        return

    # Убедимся что авто-режим НЕ запущен при старте Telegram бота
    from telegram_bot.handlers.auto_mode import AUTO_MODE_STATE
    AUTO_MODE_STATE["is_running"] = False

    # Инициализация бота и диспетчера
    bot = Bot(
        token=TELEGRAM_TOKEN,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML)
    )
    dp = Dispatcher()
    dp.callback_query.outer_middleware(UserActivityMiddleware())
    dp.message.outer_middleware(UserActivityMiddleware())
    dp.callback_query.outer_middleware(TradingAccessMiddleware())
    dp.callback_query.outer_middleware(CancelLiveUpdatesMiddleware())
    dp.message.outer_middleware(CancelLiveUpdatesMiddleware())
    register_bot(bot)
    store = await asyncio.to_thread(get_store)
    restore_screen_targets(await asyncio.to_thread(store.screen_targets))
    alert_scheduler = AlertScheduler()

    # Регистрируем роутеры
    dp.include_router(start.router)
    dp.include_router(positions.router)
    dp.include_router(trading.router)
    dp.include_router(settings.router)
    dp.include_router(auto_mode.router)
    dp.include_router(alerts.router)
    dp.include_router(activity.router)
    dp.include_router(market_overview.router)
    dp.include_router(fallbacks.router)

    logger.info("="*60)
    logger.info("🤖 Telegram Bot запущен")
    logger.info("⏹️ Авто-режим: ВЫКЛЮЧЕН (управление через бот)")
    logger.info("="*60)

    try:
        # Удаляем вебхуки если есть
        await bot.delete_webhook(drop_pending_updates=True)
        alert_scheduler.start()

        # Запускаем polling
        await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())
    except KeyboardInterrupt:
        logger.info("⛔ Получен сигнал остановки")
    except Exception as e:
        logger.error(f"❌ Критическая ошибка: {e}")
    finally:
        await alert_scheduler.stop()
        unregister_bot()
        await bot.session.close()
        logger.info("👋 Telegram Bot остановлен")


def run_telegram_bot():
    """Запуск Telegram бота"""
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("⛔ Бот остановлен пользователем")
    except Exception as e:
        logger.critical(f"💥 Критическая ошибка при запуске бота: {e}")


if __name__ == "__main__":
    run_telegram_bot()
