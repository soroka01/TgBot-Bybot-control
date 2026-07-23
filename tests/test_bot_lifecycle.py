from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

import telegram_bot.bot as bot_module


class _Observer:
    def outer_middleware(self, _middleware):
        return None


class _Dispatcher:
    def __init__(self, **_kwargs):
        self.callback_query = _Observer()
        self.message = _Observer()
        self.routers = []

    def include_router(self, router):
        self.routers.append(router)

    def resolve_used_update_types(self):
        return []


class BotLifecycleTests(unittest.IsolatedAsyncioTestCase):
    async def test_early_startup_failure_unregisters_and_closes_bot(self):
        fake_bot = SimpleNamespace(
            session=SimpleNamespace(close=AsyncMock()),
        )
        unregister = AsyncMock()
        scheduler = Mock()

        with (
            patch.object(bot_module, "validate_config", return_value=[]),
            patch.object(bot_module, "Bot", return_value=fake_bot),
            patch.object(bot_module, "Dispatcher", _Dispatcher),
            patch.object(bot_module, "register_bot") as register,
            patch.object(bot_module, "unregister_bot", unregister),
            patch.object(
                bot_module,
                "get_store",
                side_effect=RuntimeError("database unavailable"),
            ),
            patch.object(bot_module, "AlertScheduler", scheduler),
        ):
            with self.assertRaisesRegex(RuntimeError, "database unavailable"):
                await bot_module.main()

        register.assert_called_once_with(fake_bot)
        unregister.assert_awaited_once_with()
        fake_bot.session.close.assert_awaited_once_with()
        scheduler.assert_not_called()

    async def test_failure_after_scheduler_creation_cleans_each_resource_once(self):
        fake_bot = SimpleNamespace(
            delete_webhook=AsyncMock(side_effect=RuntimeError("webhook unavailable")),
            session=SimpleNamespace(close=AsyncMock()),
        )
        store = SimpleNamespace(screen_targets=Mock(return_value=[]))
        scheduler_instance = SimpleNamespace(
            start=Mock(),
            stop=AsyncMock(),
        )
        unregister = AsyncMock()
        stop_auto = Mock()

        with (
            patch.object(bot_module, "validate_config", return_value=[]),
            patch.object(bot_module, "Bot", return_value=fake_bot),
            patch.object(bot_module, "Dispatcher", _Dispatcher),
            patch.object(bot_module, "register_bot"),
            patch.object(bot_module, "unregister_bot", unregister),
            patch.object(bot_module, "get_store", return_value=store),
            patch.object(bot_module, "restore_screen_targets"),
            patch.object(
                bot_module,
                "refresh_restored_screens",
                new=AsyncMock(),
            ),
            patch.object(
                bot_module,
                "AlertScheduler",
                return_value=scheduler_instance,
            ),
            patch.object(bot_module.auto_mode, "stop_auto_mode", stop_auto),
        ):
            with self.assertRaisesRegex(RuntimeError, "webhook unavailable"):
                await bot_module.main()

        stop_auto.assert_called_once_with(None)
        scheduler_instance.start.assert_not_called()
        scheduler_instance.stop.assert_awaited_once_with()
        unregister.assert_awaited_once_with()
        fake_bot.session.close.assert_awaited_once_with()


if __name__ == "__main__":
    unittest.main()
