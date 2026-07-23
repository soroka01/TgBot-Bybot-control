import asyncio
from datetime import datetime, timezone
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from aiogram.types import CallbackQuery, Chat, Message, User

import telegram_bot.ui as ui


class FakeBot:
    def __init__(self):
        self.edits = []

    async def edit_message_text(self, **kwargs):
        self.edits.append(kwargs)


class FakeCommandMessage:
    def __init__(self, bot):
        self.bot = bot
        self.chat = SimpleNamespace(id=42)
        self.message_id = 900
        self.sent = []

    async def answer(self, text, **kwargs):
        self.sent.append((text, kwargs))
        return SimpleNamespace(
            bot=self.bot,
            chat=self.chat,
            message_id=901,
        )


def callback(data: str) -> CallbackQuery:
    user = User(id=42, is_bot=False, first_name="Tester")
    return CallbackQuery(
        id="callback",
        from_user=user,
        chat_instance="instance",
        data=data,
        message=Message(
            message_id=77,
            date=datetime.now(timezone.utc),
            chat=Chat(id=42, type="private"),
            from_user=user,
            text="screen",
        ),
    )


class SingleMessageUITests(unittest.IsolatedAsyncioTestCase):
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

    async def test_command_send_remembers_base_view_for_events(self):
        bot = FakeBot()
        message = FakeCommandMessage(bot)
        with patch("telegram_bot.ui._remember", new=AsyncMock()):
            response = await ui.render_command_screen(message, "Menu", None)
        self.assertEqual(response.message_id, 901)
        self.assertEqual(ui._screen_views[42], ("Menu", None))

    async def test_live_fingerprint_skips_duplicate_but_explicit_verify_does_not(self):
        bot = FakeBot()
        first = await ui._telegram_edit(bot, 42, 77, "Same", None)
        second = await ui._telegram_edit(bot, 42, 77, "Same", None)
        verified = await ui._telegram_edit(
            bot,
            42,
            77,
            "Same",
            None,
            verify_exists=True,
        )
        self.assertEqual((first, second, verified), ("ok", "ok", "ok"))
        self.assertEqual(len(bot.edits), 2)

    async def test_events_accumulate_in_one_canonical_message(self):
        bot = FakeBot()
        ui._bot = bot
        ui._screen_messages[42] = 77
        ui._screen_views[42] = ("Base", None)
        first = await ui._render_event("one", [42], event_key="event-1")
        second = await ui._render_event("two", [42], event_key="event-2")
        self.assertEqual(first[42], "ok")
        self.assertEqual(second[42], "ok")
        self.assertEqual(
            [text for _, text in ui._event_banners[42]],
            ["one", "two"],
        )
        self.assertEqual(len(bot.edits), 2)
        self.assertIn("one", bot.edits[-1]["text"])
        self.assertIn("two", bot.edits[-1]["text"])

    async def test_identical_alert_text_with_new_outbox_key_is_not_deduplicated(self):
        bot = FakeBot()
        ui._bot = bot
        ui._screen_messages[42] = 77
        ui._screen_views[42] = ("Base", None)
        await ui._render_event("same", [42], event_key="outbox:1")
        await ui._render_event("same", [42], event_key="outbox:2")
        self.assertEqual(
            [key for key, _ in ui._event_banners[42]],
            ["outbox:1", "outbox:2"],
        )
        self.assertEqual(len(bot.edits), 2)

    def test_newest_event_is_visible_when_old_banner_fills_limit(self):
        ui._event_banners[42] = [
            ("old", "x" * 1_000),
            ("new", "NEW"),
        ]
        composed = ui._compose(42, "Base")
        self.assertIn("NEW", composed)

    async def test_navigation_dismisses_only_events_visible_when_it_started(self):
        bot = FakeBot()
        ui._screen_messages[42] = 77
        ui._event_banners[42] = [("old", "old"), ("new", "new")]
        context_token = ui._dismissible_event_keys.set(frozenset({"old"}))
        try:
            result = await ui._edit_view(
                bot,
                42,
                77,
                "Next",
                None,
                dismiss_events=True,
            )
        finally:
            ui._dismissible_event_keys.reset(context_token)
        self.assertEqual(result, "ok")
        self.assertEqual(ui._event_banners[42], [("new", "new")])
        self.assertIn("new", bot.edits[-1]["text"])

    async def test_message_snapshot_preserves_alert_arriving_during_middleware(self):
        middleware = ui.EventBannerSnapshotMiddleware()
        user = User(id=42, is_bot=False, first_name="Tester")
        event = Message(
            message_id=900,
            date=datetime.now(timezone.utc),
            chat=Chat(id=42, type="private"),
            from_user=user,
            text="/menu",
        )
        ui._event_banners[42] = [("visible-at-entry", "old")]

        async def downstream(_event, _data):
            # Simulate an alert delivered while an inner middleware awaits I/O,
            # before command rendering begins.
            ui._event_banners[42].append(("arrived-during-update", "new"))
            ui._dismiss_visible_events(42)
            return "handled"

        result = await middleware(downstream, event, {})

        self.assertEqual(result, "handled")
        self.assertEqual(
            ui._event_banners[42],
            [("arrived-during-update", "new")],
        )
        self.assertIsNone(ui._dismissible_event_keys.get())

    async def test_callback_snapshot_preserves_alert_arriving_during_middleware(self):
        middleware = ui.EventBannerSnapshotMiddleware()
        event = callback("positions:list")
        ui._event_banners[42] = [("visible-at-entry", "old")]

        async def downstream(_event, _data):
            ui._event_banners[42].append(("arrived-during-update", "new"))
            ui._dismiss_visible_events(42)
            return "handled"

        result = await middleware(downstream, event, {})

        self.assertEqual(result, "handled")
        self.assertEqual(
            ui._event_banners[42],
            [("arrived-during-update", "new")],
        )
        self.assertIsNone(ui._dismissible_event_keys.get())

    async def test_async_result_cannot_overwrite_newer_route(self):
        bot = FakeBot()
        message = FakeCommandMessage(bot)
        ui._screen_messages[42] = 900
        ui._screen_revisions[42] = 2
        ui._screen_views[42] = ("Newer", None)
        rendered = await ui.render_if_current(
            (42, 900, 1),
            message,
            "Stale AI result",
            None,
        )
        self.assertIsNone(rendered)
        self.assertEqual(ui._screen_views[42], ("Newer", None))
        self.assertEqual(bot.edits, [])

    async def test_periodic_result_cannot_overwrite_route_changed_during_loader(self):
        bot = FakeBot()
        message = FakeCommandMessage(bot)
        ui._screen_messages[42] = 900
        ui._screen_revisions[42] = 1
        ui._screen_views[42] = ("Original", None)
        loader_started = asyncio.Event()
        release_loader = asyncio.Event()

        async def loader():
            loader_started.set()
            await release_loader.wait()
            return "Stale live result", None

        with patch("telegram_bot.ui._remember", new=AsyncMock()):
            await ui.start_live_updates(message, loader, interval_seconds=0.01)
        task = ui._live_tasks[42]
        await asyncio.wait_for(loader_started.wait(), timeout=1)
        ui._screen_revisions[42] = 2
        ui._screen_views[42] = ("New route", None)
        release_loader.set()
        await asyncio.wait_for(task, timeout=1)

        self.assertEqual(ui._screen_views[42], ("New route", None))
        self.assertEqual(bot.edits, [])

    async def test_known_screen_with_no_buttons_rejects_crafted_callback(self):
        middleware = ui.CancelLiveUpdatesMiddleware()
        event = callback("crafted:action")
        ui._screen_messages[42] = 77
        ui._screen_callbacks[42] = set()
        handler = AsyncMock()
        with patch.object(CallbackQuery, "answer", new=AsyncMock()) as answer:
            result = await middleware(handler, event, {})
        self.assertIsNone(result)
        handler.assert_not_awaited()
        answer.assert_awaited_once_with("Кнопка уже устарела.", show_alert=False)

    async def test_callback_without_canonical_screen_fails_closed(self):
        middleware = ui.CancelLiveUpdatesMiddleware()
        event = callback("positions:close_all_confirm")
        handler = AsyncMock()
        with patch.object(CallbackQuery, "answer", new=AsyncMock()) as answer:
            result = await middleware(handler, event, {})
        self.assertIsNone(result)
        handler.assert_not_awaited()
        answer.assert_awaited_once()

    async def test_restart_revokes_old_keyboard_even_when_edit_fails(self):
        ui._bot = FakeBot()
        ui.restore_screen_targets([(42, 77, 9)])
        with patch(
            "telegram_bot.ui._telegram_edit",
            new=AsyncMock(return_value="temporary_failure"),
        ):
            await ui.refresh_restored_screens()
        self.assertIn("menu:positions", ui._screen_callbacks[42])
        self.assertNotIn("positions:close_all_confirm", ui._screen_callbacks[42])


if __name__ == "__main__":
    unittest.main()
