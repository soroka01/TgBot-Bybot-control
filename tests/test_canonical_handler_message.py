from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

import telegram_bot.handlers.auto_mode as auto_mode
import telegram_bot.handlers.trading as trading
import telegram_bot.ui as ui


class FakeMessage:
    def __init__(self, bot, message_id: int, replacement_id: int | None = None):
        self.bot = bot
        self.chat = SimpleNamespace(id=42)
        self.message_id = message_id
        self.replacement_id = replacement_id
        self.answers: list[tuple[str, dict]] = []

    async def answer(self, text: str, **kwargs):
        self.answers.append((text, kwargs))
        if self.replacement_id is None:
            raise AssertionError("Unexpected replacement request")
        return FakeMessage(self.bot, self.replacement_id)


def fake_callback(message: FakeMessage):
    return SimpleNamespace(message=message, answer=AsyncMock())


class CanonicalHandlerMessageTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        await trading.shutdown_ai_tasks()
        ui._screen_messages.clear()
        ui._screen_revisions.clear()
        ui._screen_callbacks.clear()
        ui._screen_fingerprints.clear()
        ui._screen_views.clear()
        ui._screen_locks.clear()
        ui._live_tasks.clear()
        ui._event_banners.clear()

    async def asyncTearDown(self):
        await trading.shutdown_ai_tasks()

    async def test_ai_result_reaches_replacement_created_for_missing_message(self):
        bot = object()
        original = FakeMessage(bot, 77, replacement_id=901)
        callback = fake_callback(original)
        ui._screen_messages[42] = 77
        ui._screen_revisions[42] = 1
        store = Mock()

        with (
            patch("storage.database.get_store", return_value=store),
            patch(
                "telegram_bot.ui._telegram_edit",
                new=AsyncMock(side_effect=["missing", "ok"]),
            ) as telegram_edit,
            patch.object(
                trading,
                "build_ai_recommendations",
                return_value=("✅ <b>Готовый результат</b>", None),
            ),
        ):
            await trading.callback_ai_suggestion(callback)
            task = trading._ai_tasks[42]
            await task

        self.assertEqual(ui._screen_messages[42], 901)
        self.assertEqual(
            ui._screen_views[42],
            ("✅ <b>Готовый результат</b>", None),
        )
        self.assertEqual(telegram_edit.await_count, 2)
        self.assertEqual(telegram_edit.await_args_list[0].args[2], 77)
        self.assertEqual(telegram_edit.await_args_list[1].args[2], 901)

    async def test_market_live_loader_uses_loading_replacement(self):
        original = FakeMessage(object(), 77)
        canonical = FakeMessage(original.bot, 901)
        callback = fake_callback(original)

        with (
            patch.object(
                trading,
                "render_callback_screen",
                new=AsyncMock(return_value=canonical),
            ),
            patch.object(
                trading,
                "render_live_screen",
                new=AsyncMock(),
            ) as render_live,
        ):
            await trading.callback_market_analysis(callback)

        self.assertIs(render_live.await_args.args[0], canonical)

    async def test_auto_logs_live_loader_uses_loading_replacement(self):
        original = FakeMessage(object(), 77)
        canonical = FakeMessage(original.bot, 901)
        callback = fake_callback(original)

        with (
            patch.object(
                auto_mode,
                "render_callback_screen",
                new=AsyncMock(return_value=canonical),
            ),
            patch.object(
                auto_mode,
                "render_live_screen",
                new=AsyncMock(),
            ) as render_live,
        ):
            await auto_mode.callback_auto_logs(callback)

        self.assertIs(render_live.await_args.args[0], canonical)


if __name__ == "__main__":
    unittest.main()
