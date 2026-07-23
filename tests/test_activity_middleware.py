from datetime import datetime, timezone
import unittest
from unittest.mock import AsyncMock, patch

from aiogram.types import CallbackQuery, Chat, Message, User

from telegram_bot.activity_middleware import TradingAccessMiddleware


def _callback(data: str, user_id: int) -> CallbackQuery:
    user = User(id=user_id, is_bot=False, first_name="Tester")
    return CallbackQuery(
        id="callback-id",
        from_user=user,
        chat_instance="chat-instance",
        data=data,
        message=Message(
            message_id=1,
            date=datetime.now(timezone.utc),
            chat=Chat(id=user_id, type="private"),
            from_user=user,
            text="screen",
        ),
    )


class TradingAccessMiddlewareTests(unittest.IsolatedAsyncioTestCase):
    async def test_positions_close_all_requires_current_admin(self):
        middleware = TradingAccessMiddleware()
        handler = AsyncMock(return_value="handled")
        event = _callback("positions:close_all_confirm", 9_876_543_210)

        with patch.object(CallbackQuery, "answer", new=AsyncMock()) as answer:
            result = await middleware(handler, event, {})

        self.assertIsNone(result)
        handler.assert_not_awaited()
        answer.assert_awaited_once_with(
            "Торговый контур доступен только администратору бота.",
            show_alert=True,
        )


if __name__ == "__main__":
    unittest.main()
