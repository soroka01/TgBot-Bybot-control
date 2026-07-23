# telegram_bot/bot.py
"""
Главный файл Telegram бота для управления торговлей
"""
import asyncio
import sys
from contextlib import AsyncExitStack
from pathlib import Path
from typing import Any

# Добавляем корневую директорию в путь
sys.path.insert(0, str(Path(__file__).parent.parent))

from aiogram import Bot, Dispatcher as AiogramDispatcher
from aiogram.enums import ParseMode
from aiogram.client.default import DefaultBotProperties
from aiogram.fsm.storage.memory import SimpleEventIsolation
from aiogram.types import Update

from config import TELEGRAM_TOKEN, validate_config
from utils.logger_setup import logger

# Импортируем все handlers
from telegram_bot.handlers import (
    activity, alerts, auto_mode, chart, fallbacks, market_overview, positions, settings,
    start, trading,
)
from core.alert_scheduler import AlertScheduler
from storage.database import get_store
from telegram_bot.activity_middleware import TradingAccessMiddleware, UserActivityMiddleware
from telegram_bot.ui import (
    CancelLiveUpdatesMiddleware,
    EventBannerSnapshotMiddleware,
    register_bot,
    refresh_restored_screens,
    restore_screen_targets,
    unregister_bot,
)


UPDATE_DRAIN_TIMEOUT_SECONDS = 5.0


class Dispatcher(AiogramDispatcher):
    """Dispatcher with arrival snapshots and bounded graceful update drain."""

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._event_banner_snapshot = EventBannerSnapshotMiddleware()

    async def feed_update(self, bot: Bot, update: Update, **kwargs: Any) -> Any:
        async def feed(event: Update, data: dict) -> Any:
            return await AiogramDispatcher.feed_update(
                self,
                bot,
                event,
                **data,
            )

        # This wraps aiogram's update-level UserContext/FSM middleware, so the
        # snapshot is captured before an update can wait on event isolation.
        return await self._event_banner_snapshot(feed, update, kwargs)

    async def drain_active_updates(self) -> None:
        """Finish quick handlers, then cancel and join the remaining tasks."""
        tasks = {
            task
            for task in self._handle_update_tasks
            if task is not asyncio.current_task() and not task.done()
        }
        if not tasks:
            return
        _, pending = await asyncio.wait(
            tasks,
            timeout=UPDATE_DRAIN_TIMEOUT_SECONDS,
        )
        if pending:
            logger.warning(
                f"Останавливаю {len(pending)} незавершённых "
                "Telegram update-задач"
            )
            for task in pending:
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    async def emit_shutdown(self, *args: Any, **kwargs: Any) -> None:
        # Aiogram normally closes FSM isolation while handle_as_tasks updates
        # may still be alive. Drain them first; the bot session stays open.
        await self.drain_active_updates()
        await super().emit_shutdown(*args, **kwargs)


async def main():
    """Главная функция запуска бота"""
    config_errors = validate_config("telegram")
    if config_errors:
        for error in config_errors:
            logger.error(f"Конфигурация: {error}")
        raise ValueError("Некорректная конфигурация Telegram-бота")

    # Bot owns an aiohttp session as soon as it is constructed.  Register every
    # subsequently acquired resource with the exit stack immediately so an
    # exception during database restore or any other startup step cannot leak it.
    bot = Bot(
        token=TELEGRAM_TOKEN,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML)
    )
    try:
        async with AsyncExitStack() as cleanup:
            cleanup.push_async_callback(bot.session.close)

            dp = Dispatcher(events_isolation=SimpleEventIsolation())
            # Capture visible alert keys before any middleware awaits I/O.
            # Command/callback navigation may dismiss only this frozen set.
            dp.callback_query.outer_middleware(UserActivityMiddleware())
            dp.message.outer_middleware(UserActivityMiddleware())
            dp.callback_query.outer_middleware(TradingAccessMiddleware())
            dp.callback_query.outer_middleware(CancelLiveUpdatesMiddleware())
            dp.message.outer_middleware(CancelLiveUpdatesMiddleware())

            register_bot(bot)
            cleanup.push_async_callback(unregister_bot)

            store = await asyncio.to_thread(get_store)
            restore_screen_targets(await asyncio.to_thread(store.screen_targets))
            await refresh_restored_screens()

            alert_scheduler = AlertScheduler()
            cleanup.push_async_callback(alert_scheduler.stop)

            # Регистрируем роутеры
            dp.include_router(start.router)
            dp.include_router(positions.router)
            dp.include_router(trading.router)
            dp.include_router(chart.router)
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

            # LIFO cleanup keeps order reconciliation ahead of
            # scheduler/UI/network teardown.
            cleanup.push_async_callback(
                asyncio.to_thread,
                auto_mode.stop_auto_mode,
                None,
            )
            cleanup.push_async_callback(trading.shutdown_ai_tasks)

            # Удаляем вебхуки если есть
            await bot.delete_webhook(drop_pending_updates=True)
            alert_scheduler.start()

            # Запускаем polling
            await dp.start_polling(
                bot,
                allowed_updates=dp.resolve_used_update_types(),
                # Keep Telegram alive until auto, scheduler, and UI cleanup
                # have finished; AsyncExitStack closes it exactly once.
                close_bot_session=False,
            )
    except KeyboardInterrupt:
        logger.info("⛔ Получен сигнал остановки")
    except Exception as e:
        logger.error(f"❌ Критическая ошибка: {e}")
        raise
    finally:
        logger.info("👋 Telegram Bot остановлен")


def run_telegram_bot():
    """Запуск Telegram бота"""
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("⛔ Бот остановлен пользователем")
    except Exception as e:
        logger.critical(f"💥 Критическая ошибка при запуске бота: {e}")
        raise


if __name__ == "__main__":
    run_telegram_bot()
