import asyncio
import unittest
from unittest.mock import Mock, patch

import telegram_bot.handlers.trading as trading


class TradingHandlerTests(unittest.TestCase):
    def test_zero_candidates_skips_paid_ai_call(self):
        bybit = Mock()
        cycle = {
            "snapshot": {
                "symbols": {
                    "BTCUSDT": {
                        "candidates": [],
                    }
                }
            }
        }
        with (
            patch(
                "telegram_bot.handlers.trading.BybitAPI",
                return_value=bybit,
            ),
            patch(
                "telegram_bot.handlers.trading._read_fee_rates",
                return_value={},
            ),
            patch(
                "telegram_bot.handlers.trading.collect_cycle",
                return_value=cycle,
            ),
            patch(
                "telegram_bot.handlers.trading.DeepSeekAPI",
            ) as deepseek,
        ):
            text, _ = trading.build_ai_recommendations()

        deepseek.assert_not_called()
        bybit.close.assert_called_once_with()
        self.assertIn("AI не вызывался", text)


class TradingTaskLifecycleTests(unittest.IsolatedAsyncioTestCase):
    async def test_shutdown_cancels_detached_selector_tasks(self):
        started = asyncio.Event()

        async def pending():
            started.set()
            await asyncio.Event().wait()

        task = asyncio.create_task(pending())
        trading._ai_tasks[42] = task
        await started.wait()
        await trading.shutdown_ai_tasks()
        self.assertTrue(task.cancelled())
        self.assertEqual(trading._ai_tasks, {})


if __name__ == "__main__":
    unittest.main()
