"""aiogram middlewares for event snapshots and stale callbacks."""

from __future__ import annotations

from typing import Optional

from aiogram import BaseMiddleware
from aiogram.types import CallbackQuery, Message, TelegramObject, Update

from .state import (
    _advance_revision,
    _callback_revision,
    _dismissible_event_keys,
    _event_banners,
    _is_destructive_callback,
    _remember,
    _screen_callbacks,
    _screen_messages,
    _screen_revisions,
)
from .screens import stop_live_updates


class EventBannerSnapshotMiddleware(BaseMiddleware):
    """Freeze which alerts were visible when a Telegram update arrived."""

    async def __call__(self, handler, event: TelegramObject, data: dict):
        chat_id: Optional[int] = None
        if isinstance(event, Update):
            if event.message:
                chat_id = event.message.chat.id
            elif (
                event.callback_query
                and isinstance(event.callback_query.message, Message)
            ):
                chat_id = event.callback_query.message.chat.id
        elif isinstance(event, Message):
            chat_id = event.chat.id
        elif isinstance(event, CallbackQuery) and isinstance(event.message, Message):
            chat_id = event.message.chat.id

        if chat_id is None:
            return await handler(event, data)
        if _dismissible_event_keys.get() is not None:
            return await handler(event, data)

        dismiss_token = _dismissible_event_keys.set(
            frozenset(key for key, _ in _event_banners.get(chat_id, []))
        )
        try:
            return await handler(event, data)
        finally:
            _dismissible_event_keys.reset(dismiss_token)


class CancelLiveUpdatesMiddleware(BaseMiddleware):
    """Reject stale callbacks and transition only events that reached a handler."""

    async def __call__(self, handler, event: TelegramObject, data: dict):
        if isinstance(event, CallbackQuery):
            if not isinstance(event.message, Message):
                await event.answer(
                    "Экран недоступен. Откройте бот командой /start.",
                    show_alert=False,
                )
                return None
            chat_id = event.message.chat.id
            canonical = _screen_messages.get(chat_id)
            if canonical is None:
                await event.answer(
                    "Экран не зарегистрирован. Откройте бот командой /start.",
                    show_alert=False,
                )
                return None
            if canonical != event.message.message_id:
                await event.answer("Этот экран устарел.", show_alert=False)
                return None
            callback_data = event.data or ""
            current_callbacks = _screen_callbacks.get(chat_id)
            if (
                (
                    _is_destructive_callback(callback_data)
                    and (
                        _callback_revision(callback_data)
                        != _screen_revisions.get(chat_id, 0)
                        or callback_data not in (current_callbacks or set())
                    )
                )
                or (
                    current_callbacks is not None
                    and callback_data not in current_callbacks
                )
            ):
                await event.answer("Кнопка уже устарела.", show_alert=False)
                return None
            # Direct middleware invocation (for example, in isolation tests)
            # keeps the same safety guarantee.  In production the outer
            # EventBannerSnapshotMiddleware has already captured the earlier,
            # update-arrival snapshot and must not be overwritten here.
            dismiss_token = None
            if _dismissible_event_keys.get() is None:
                dismiss_token = _dismissible_event_keys.set(
                    frozenset(
                        key
                        for key, _ in _event_banners.get(chat_id, [])
                    )
                )
            _advance_revision(chat_id)
            await stop_live_updates(chat_id)
            await _remember(event.message)
            try:
                return await handler(event, data)
            finally:
                if dismiss_token is not None:
                    _dismissible_event_keys.reset(dismiss_token)
        return await handler(event, data)
