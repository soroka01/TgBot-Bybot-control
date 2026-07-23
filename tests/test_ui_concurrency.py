import asyncio
from contextlib import asynccontextmanager
from datetime import datetime, timezone
import unittest
from unittest.mock import AsyncMock, patch

from aiogram import Bot
from aiogram.fsm.storage.base import BaseEventIsolation
from aiogram.types import CallbackQuery, Chat, Message, Update, User

import telegram_bot.bot as bot_module
import telegram_bot.ui as ui
from telegram_bot.keyboards.positions_menu import (
    get_close_all_confirmation_menu,
)


def _message(text: str = "/menu") -> Message:
    user = User(id=42, is_bot=False, first_name="Tester")
    return Message(
        message_id=77,
        date=datetime.now(timezone.utc),
        chat=Chat(id=42, type="private"),
        from_user=user,
        text=text,
    )


def _callback(data: str) -> CallbackQuery:
    user = User(id=42, is_bot=False, first_name="Tester")
    return CallbackQuery(
        id=f"callback-{data}",
        from_user=user,
        chat_instance="instance",
        data=data,
        message=_message("screen"),
    )


class _BlockingIsolation(BaseEventIsolation):
    def __init__(self) -> None:
        self.entered = asyncio.Event()
        self.release = asyncio.Event()

    @asynccontextmanager
    async def lock(self, key):
        del key
        self.entered.set()
        await self.release.wait()
        yield

    async def close(self) -> None:
        self.release.set()


class _ObservingIsolation(BaseEventIsolation):
    def __init__(self) -> None:
        self.update_task = None
        self.task_done_at_close = False

    @asynccontextmanager
    async def lock(self, key):
        del key
        yield

    async def close(self) -> None:
        self.task_done_at_close = bool(
            self.update_task and self.update_task.done()
        )


class _BlockingEditBot:
    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.edits = []

    async def edit_message_text(self, **kwargs):
        self.edits.append(kwargs)
        self.started.set()
        await self.release.wait()


class _EditBot:
    def __init__(self) -> None:
        self.edits = []

    async def edit_message_text(self, **kwargs):
        self.edits.append(kwargs)


class UIConcurrencyTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        ui._screen_messages.clear()
        ui._screen_revisions.clear()
        ui._screen_callbacks.clear()
        ui._screen_fingerprints.clear()
        ui._screen_views.clear()
        ui._screen_locks.clear()
        ui._live_tasks.clear()
        ui._event_banners.clear()
        ui._pending_events.clear()
        ui._bot = None

    async def asyncTearDown(self):
        ui._bot = None

    async def test_update_snapshot_precedes_fsm_event_isolation(self):
        isolation = _BlockingIsolation()
        dispatcher = bot_module.Dispatcher(events_isolation=isolation)
        bot = Bot(token="123456:ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghi")
        ui._event_banners[42] = [("visible-at-arrival", "old")]

        async def dismiss_handler(_message: Message):
            ui._dismiss_visible_events(42)

        dispatcher.message.register(dismiss_handler)
        update = Update(update_id=1, message=_message())
        task = asyncio.create_task(dispatcher.feed_update(bot, update))
        try:
            await asyncio.wait_for(isolation.entered.wait(), timeout=1)
            ui._event_banners[42].append(("arrived-while-queued", "new"))
            isolation.release.set()
            await asyncio.wait_for(task, timeout=1)
        finally:
            isolation.release.set()
            if not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
            await dispatcher.emit_shutdown()
            await bot.session.close()

        self.assertEqual(
            ui._event_banners[42],
            [("arrived-while-queued", "new")],
        )

    async def test_inflight_banner_is_committed_only_after_successful_edit(self):
        bot = _BlockingEditBot()
        ui._bot = bot
        ui._screen_messages[42] = 77
        ui._screen_views[42] = ("Base", None)
        ui._event_banners[42] = [("visible-at-arrival", "old")]

        delivery = asyncio.create_task(
            ui._render_event("new", [42], event_key="in-flight")
        )
        await asyncio.wait_for(bot.started.wait(), timeout=1)
        self.assertEqual(
            ui._event_banners[42],
            [("visible-at-arrival", "old")],
        )

        async def finish_delivery(_event, _data):
            bot.release.set()
            outcome = await delivery
            self.assertEqual(outcome[42], "ok")
            ui._dismiss_visible_events(42)

        await ui.EventBannerSnapshotMiddleware()(
            finish_delivery,
            _message(),
            {},
        )

        self.assertEqual(ui._event_banners[42], [("in-flight", "new")])

    async def test_old_destructive_nonce_is_rejected_for_same_action(self):
        bot = _EditBot()
        ui._screen_messages[42] = 77
        ui._screen_revisions[42] = 10

        await ui._edit_view(
            bot,
            42,
            77,
            "Confirm",
            get_close_all_confirmation_menu(),
        )
        old_data = bot.edits[-1]["reply_markup"].inline_keyboard[0][0].callback_data

        ui._advance_revision(42)
        await ui._edit_view(
            bot,
            42,
            77,
            "Confirm again",
            get_close_all_confirmation_menu(),
        )
        current_data = (
            bot.edits[-1]["reply_markup"].inline_keyboard[0][0].callback_data
        )

        self.assertEqual(ui.callback_action(old_data), ui.callback_action(current_data))
        self.assertNotEqual(old_data, current_data)
        self.assertNotIn(old_data, ui._screen_callbacks[42])
        self.assertIn(current_data, ui._screen_callbacks[42])
        handler = AsyncMock()
        with patch.object(CallbackQuery, "answer", new=AsyncMock()) as answer:
            result = await ui.CancelLiveUpdatesMiddleware()(
                handler,
                _callback(old_data),
                {},
            )

        self.assertIsNone(result)
        handler.assert_not_awaited()
        answer.assert_awaited_once_with(
            "Кнопка уже устарела.",
            show_alert=False,
        )

    async def test_shutdown_cancels_updates_before_fsm_is_closed(self):
        isolation = _ObservingIsolation()
        dispatcher = bot_module.Dispatcher(events_isolation=isolation)
        started = asyncio.Event()
        finished = asyncio.Event()

        async def active_update():
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                finished.set()

        task = asyncio.create_task(active_update())
        dispatcher._handle_update_tasks.add(task)
        task.add_done_callback(dispatcher._handle_update_tasks.discard)
        isolation.update_task = task
        await asyncio.wait_for(started.wait(), timeout=1)

        with patch.object(
            bot_module,
            "UPDATE_DRAIN_TIMEOUT_SECONDS",
            0.0,
        ):
            await dispatcher.emit_shutdown()

        self.assertTrue(task.cancelled())
        self.assertTrue(finished.is_set())
        self.assertTrue(isolation.task_done_at_close)


if __name__ == "__main__":
    unittest.main()
