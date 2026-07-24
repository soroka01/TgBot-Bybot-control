"""Persist user identity and preferences before every handled interaction."""

from __future__ import annotations

import asyncio

from aiogram import BaseMiddleware
from aiogram.types import CallbackQuery, Message, TelegramObject

from config import ADMIN_TELEGRAM_IDS
from storage.database import get_store
from utils.logger_setup import logger


class UserActivityMiddleware(BaseMiddleware):
    async def __call__(self, handler, event: TelegramObject, data: dict):
        if isinstance(event, CallbackQuery) and event.message:
            chat_id, user = event.message.chat.id, event.from_user
        elif isinstance(event, Message):
            chat_id, user = event.chat.id, event.from_user
        else:
            return await handler(event, data)
        if getattr(event.message if isinstance(event, CallbackQuery) else event, "chat", None):
            chat = event.message.chat if isinstance(event, CallbackQuery) else event.chat
            if getattr(chat.type, "value", chat.type) != "private":
                if isinstance(event, CallbackQuery):
                    await event.answer("Бот работает только в личном чате.", show_alert=True)
                return None
        if user:
            try:
                await asyncio.to_thread(
                    get_store().ensure_user,
                    user,
                    chat_id,
                    is_admin=user.id in ADMIN_TELEGRAM_IDS,
                )
            except Exception as error:
                logger.warning(f"Не удалось сохранить профиль {chat_id}: {error}")
        return await handler(event, data)


class TradingAccessMiddleware(BaseMiddleware):
    """Keep one shared exchange account under explicit owner control."""

    _protected_prefixes = (
        "menu:positions",
        "menu:balance",
        "menu:open_trade",
        "menu:market_analysis",
        "menu:history",
        "menu:auto_mode",
        "pos:",
        "positions:",
        "history:",
        "auto:",
        "trade:",
    )

    async def __call__(self, handler, event: TelegramObject, data: dict):
        if not isinstance(event, CallbackQuery) or not event.message:
            return await handler(event, data)
        callback_data = event.data or ""
        if not callback_data.startswith(self._protected_prefixes):
            return await handler(event, data)
        if event.from_user and event.from_user.id in ADMIN_TELEGRAM_IDS:
            return await handler(event, data)
        await event.answer(
            "Торговый контур доступен только администратору бота.",
            show_alert=True,
        )
        return None
