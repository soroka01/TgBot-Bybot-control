"""Async alert loop sharing the same lifecycle as the Telegram bot."""

from __future__ import annotations

import asyncio
from typing import Optional

from config import ALERT_CHECK_INTERVAL_SECONDS
from core.alerts import AlertService
from telegram_bot.ui import publish_event_to_chat
from utils.logger_setup import logger


class AlertScheduler:
    def __init__(self, service: Optional[AlertService] = None) -> None:
        self.service = service or AlertService()
        self._task: Optional[asyncio.Task] = None

    def start(self) -> None:
        if self._task and not self._task.done():
            return
        self._task = asyncio.create_task(self._run(), name="alert-scheduler")
        logger.info("Планировщик алертов запущен")

    async def stop(self) -> None:
        if not self._task:
            return
        self._task.cancel()
        try:
            await self._task
        except asyncio.CancelledError:
            pass
        self._task = None
        logger.info("Планировщик алертов остановлен")

    async def _run(self) -> None:
        while True:
            try:
                events = await asyncio.to_thread(self.service.check_all)
                for event in events:
                    publish_event_to_chat(event.chat_id, event.message)
            except asyncio.CancelledError:
                raise
            except Exception as error:
                logger.exception(f"Ошибка планировщика алертов: {error}")
            await asyncio.sleep(ALERT_CHECK_INTERVAL_SECONDS)
