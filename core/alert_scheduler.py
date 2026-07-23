"""Async alert loop sharing the same lifecycle as the Telegram bot."""

from __future__ import annotations

import asyncio
from collections import defaultdict
from typing import Optional

from config import ALERT_CHECK_INTERVAL_SECONDS
from core.alerts import AlertService
from telegram_bot.ui import deliver_event_to_chat
from storage.database import get_store
from utils.logger_setup import logger


class AlertScheduler:
    def __init__(self, service: Optional[AlertService] = None) -> None:
        self.service = service or AlertService()
        self._task: Optional[asyncio.Task] = None
        self._stop_event = asyncio.Event()

    def start(self) -> None:
        if self._task and not self._task.done():
            return
        self._stop_event.clear()
        self._task = asyncio.create_task(self._run(), name="alert-scheduler")
        logger.info("Планировщик алертов запущен")

    async def stop(self) -> None:
        if self._task:
            # Let an in-flight to_thread HTTP check finish before closing its
            # requests.Session; cancelling the coroutine cannot stop the thread.
            self._stop_event.set()
            await self._task
            self._task = None
        self.service.close()
        logger.info("Планировщик алертов остановлен")

    async def _run(self) -> None:
        while not self._stop_event.is_set():
            try:
                events = await asyncio.to_thread(self.service.check_all)
                by_chat: dict[int, list] = defaultdict(list)
                for event in events:
                    by_chat[event.chat_id].append(event)
                for chat_id, pending in by_chat.items():
                    if self._stop_event.is_set():
                        break
                    # Keep one coalesced batch visible for at least one
                    # scheduler interval; the rest remains durable for a later
                    # fair batch instead of flashing through instantly.
                    batch = pending[:5]
                    text = "\n".join(event.message for event in batch)
                    outcome = await deliver_event_to_chat(
                        chat_id,
                        text,
                        event_key="outbox:" + ",".join(
                            str(event.outbox_id) for event in batch
                        ),
                    )
                    await asyncio.to_thread(
                        get_store().mark_notification_attempt,
                        [event.outbox_id for event in batch],
                        outcome,
                    )
            except Exception as error:
                logger.exception(f"Ошибка планировщика алертов: {error}")
            if self._stop_event.is_set():
                break
            try:
                await asyncio.wait_for(
                    self._stop_event.wait(),
                    timeout=ALERT_CHECK_INTERVAL_SECONDS,
                )
            except asyncio.TimeoutError:
                pass
