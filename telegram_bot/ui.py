"""Single-message UI primitives for the Telegram bot.

Every screen belongs to one editable bot message per chat.  A live screen owns
one refresh task; navigating anywhere else cancels it before the new screen is
rendered, so an outdated task cannot overwrite the user's current view.
"""

from __future__ import annotations

import asyncio
import html
from typing import Awaitable, Callable, Optional, Tuple

from aiogram import BaseMiddleware, Bot
from aiogram.exceptions import TelegramAPIError, TelegramBadRequest
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, Message, TelegramObject

from utils.logger_setup import logger


ScreenContent = Tuple[str, Optional[InlineKeyboardMarkup]]
ScreenLoader = Callable[[], Awaitable[ScreenContent]]

_screen_messages: dict[int, int] = {}
_screen_revisions: dict[int, int] = {}
_live_tasks: dict[int, asyncio.Task] = {}
_bot: Optional[Bot] = None
_event_loop: Optional[asyncio.AbstractEventLoop] = None
_pending_events: list[str] = []
_event_task: Optional[asyncio.Task] = None


def register_bot(bot: Bot) -> None:
    """Make the polling bot available to the single-message notification path."""
    global _bot, _event_loop, _event_task
    _bot = bot
    _event_loop = asyncio.get_running_loop()


def unregister_bot() -> None:
    """Detach a stopped bot and cancel every outstanding UI refresh."""
    global _bot, _event_loop, _event_task
    for task in list(_live_tasks.values()):
        task.cancel()
    _live_tasks.clear()
    _screen_messages.clear()
    _screen_revisions.clear()
    if _event_task:
        _event_task.cancel()
    _event_task = None
    _pending_events.clear()
    _bot = None
    _event_loop = None


def _remember(message: Message) -> None:
    _screen_messages[message.chat.id] = message.message_id

    async def persist() -> None:
        try:
            from storage.database import get_store

            await asyncio.to_thread(
                get_store().save_screen,
                message.chat.id,
                message.message_id,
                _screen_revisions.get(message.chat.id, 0),
            )
        except Exception as error:
            logger.warning(f"Не удалось сохранить экран {message.chat.id}: {error}")

    try:
        asyncio.create_task(
            persist(),
            name=f"persist-screen-{message.chat.id}",
        )
    except RuntimeError:
        # A screen can be remembered during shutdown, when scheduling is no
        # longer possible. The in-memory state is still correct.
        return


def restore_screen_targets(targets: list[tuple[int, int]]) -> None:
    """Restore editable screen ids after a clean bot restart."""
    for chat_id, message_id in targets:
        _screen_messages[chat_id] = message_id
        _screen_revisions.setdefault(chat_id, 0)


def _advance_revision(chat_id: int) -> int:
    revision = _screen_revisions.get(chat_id, 0) + 1
    _screen_revisions[chat_id] = revision
    return revision


def current_screen_token(message: Message) -> tuple[int, int, int]:
    """Token that lets a slow background action detect stale navigation."""
    return message.chat.id, message.message_id, _screen_revisions.get(message.chat.id, 0)


def is_current_screen(token: tuple[int, int, int]) -> bool:
    chat_id, message_id, revision = token
    return _screen_messages.get(chat_id) == message_id and _screen_revisions.get(chat_id) == revision


async def stop_live_updates(chat_id: int) -> None:
    """Cancel the active refresh for a chat, if the user navigated elsewhere."""
    task = _live_tasks.pop(chat_id, None)
    if task and task is not asyncio.current_task():
        task.cancel()


async def _edit_message(
    bot: Bot,
    chat_id: int,
    message_id: int,
    text: str,
    reply_markup: Optional[InlineKeyboardMarkup],
) -> bool:
    try:
        await bot.edit_message_text(
            chat_id=chat_id,
            message_id=message_id,
            text=text[:4096],
            reply_markup=reply_markup,
            parse_mode="HTML",
        )
        return True
    except TelegramBadRequest as error:
        # Telegram rejects a no-op edit.  That is expected on a 2-second loop.
        if "message is not modified" in str(error).lower():
            return True
        logger.warning(f"Не удалось обновить сообщение {chat_id}/{message_id}: {error}")
        return False
    except TelegramAPIError as error:
        logger.warning(f"Ошибка Telegram при обновлении {chat_id}/{message_id}: {error}")
        return False


async def render_callback_screen(
    message: Message,
    text: str,
    reply_markup: Optional[InlineKeyboardMarkup],
) -> None:
    """Replace the callback's current bot message, falling back only if deleted."""
    await stop_live_updates(message.chat.id)
    _remember(message)
    _advance_revision(message.chat.id)
    if await _edit_message(message.bot, message.chat.id, message.message_id, text, reply_markup):
        return

    replacement = await message.answer(text[:4096], reply_markup=reply_markup, parse_mode="HTML")
    _remember(replacement)


async def render_command_screen(
    message: Message,
    text: str,
    reply_markup: Optional[InlineKeyboardMarkup],
) -> None:
    """Use the remembered bot message for /start and /menu instead of adding one."""
    await stop_live_updates(message.chat.id)
    _advance_revision(message.chat.id)
    message_id = _screen_messages.get(message.chat.id)
    if message_id and await _edit_message(message.bot, message.chat.id, message_id, text, reply_markup):
        return

    response = await message.answer(text[:4096], reply_markup=reply_markup, parse_mode="HTML")
    _remember(response)


async def start_live_updates(
    message: Message,
    loader: ScreenLoader,
    interval_seconds: float = 2.0,
    initial_markup: Optional[InlineKeyboardMarkup] = None,
) -> None:
    """Refresh the same screen until the user navigates away."""
    await stop_live_updates(message.chat.id)
    _remember(message)
    chat_id = message.chat.id
    message_id = message.message_id
    bot = message.bot

    async def refresh_loop() -> None:
        last_markup = initial_markup
        try:
            while True:
                await asyncio.sleep(interval_seconds)
                try:
                    text, markup = await loader()
                except Exception as error:
                    logger.warning(f"Ошибка live-обновления для чата {chat_id}: {error}")
                    await _edit_message(
                        bot,
                        chat_id,
                        message_id,
                        "⚠️ <b>Не удалось обновить данные.</b>\n\n"
                        "Следующая попытка будет через 2 секунды.",
                        last_markup,
                    )
                    continue
                last_markup = markup
                if not await _edit_message(bot, chat_id, message_id, text, markup):
                    return
        except asyncio.CancelledError:
            raise
        except Exception as error:
            logger.error(f"Ошибка live-обновления для чата {chat_id}: {error}")
        finally:
            if _live_tasks.get(chat_id) is asyncio.current_task():
                _live_tasks.pop(chat_id, None)

    _live_tasks[chat_id] = asyncio.create_task(refresh_loop(), name=f"telegram-live-{chat_id}")


async def render_live_screen(
    message: Message,
    loader: ScreenLoader,
    interval_seconds: float = 2.0,
) -> None:
    """Load the initial state, render it, then refresh it in-place."""
    token = current_screen_token(message)
    try:
        text, markup = await loader()
    except Exception as error:
        if not is_current_screen(token):
            return
        logger.error(f"Не удалось загрузить live-экран: {error}")
        from telegram_bot.keyboards.main_menu import get_main_menu

        await render_callback_screen(
            message,
            f"❌ <b>Не удалось обновить данные</b>\n\n<code>{html.escape(str(error)[:300])}</code>",
            get_main_menu(),
        )
        return

    if not is_current_screen(token):
        return

    await render_callback_screen(message, text, markup)
    await start_live_updates(message, loader, interval_seconds, markup)


async def _render_event(text: str, chat_ids: Optional[list[int]] = None) -> None:
    """Show an event by editing each existing screen, never sending a new one."""
    if not _bot:
        return
    from telegram_bot.keyboards.main_menu import get_main_menu

    safe_text = html.escape(text[:3_700])
    screen = f"🔔 <b>Событие авто-режима</b>\n\n<code>{safe_text}</code>"
    targets = list(_screen_messages.items())
    for chat_id, message_id in targets:
        if chat_ids is not None and chat_id not in chat_ids:
            continue
        if chat_ids is None:
            try:
                from storage.database import get_store
                await asyncio.to_thread(
                    get_store().log_activity,
                    chat_id,
                    "system_event",
                    text[:500],
                )
            except Exception as error:
                logger.warning(f"Не удалось записать системное событие {chat_id}: {error}")
        await stop_live_updates(chat_id)
        _advance_revision(chat_id)
        await _edit_message(_bot, chat_id, message_id, screen, get_main_menu())


async def _flush_events() -> None:
    """Coalesce a burst of worker-thread notifications into one screen edit."""
    global _event_task
    try:
        await asyncio.sleep(0.3)
        events = _pending_events[-5:]
        _pending_events.clear()
        if events:
            await _render_event("\n\n".join(events))
    finally:
        _event_task = None
        if _pending_events:
            _event_task = asyncio.create_task(_flush_events(), name="telegram-event-coalescer")


def _queue_event(text: str) -> None:
    global _event_task
    _pending_events.append(text)
    if _event_task is None or _event_task.done():
        _event_task = asyncio.create_task(_flush_events(), name="telegram-event-coalescer")


def publish_event(text: str) -> bool:
    """Schedule an in-place event update from async code or the trading thread."""
    if not _bot or not _event_loop or not _screen_messages:
        logger.debug(f"[Telegram UI unavailable] {text}")
        return False
    try:
        _event_loop.call_soon_threadsafe(_queue_event, text)
    except RuntimeError:
        logger.debug(f"[Telegram UI loop stopped] {text}")
        return False
    return True


def publish_event_to_chat(chat_id: int, text: str) -> bool:
    """Schedule an alert edit for one chat without ever creating a new message."""
    if not _bot or not _event_loop or chat_id not in _screen_messages:
        logger.debug(f"[Telegram screen unavailable for {chat_id}] {text}")
        return False

    def queue() -> None:
        asyncio.create_task(
            _render_event(text, [chat_id]),
            name=f"telegram-alert-{chat_id}",
        )

    try:
        _event_loop.call_soon_threadsafe(queue)
    except RuntimeError:
        logger.debug(f"[Telegram UI loop stopped] {text}")
        return False
    return True


class CancelLiveUpdatesMiddleware(BaseMiddleware):
    """Stop an old screen refresh before every user command or button handler."""

    async def __call__(self, handler, event: TelegramObject, data: dict):
        if isinstance(event, CallbackQuery) and isinstance(event.message, Message):
            await stop_live_updates(event.message.chat.id)
            _remember(event.message)
            _advance_revision(event.message.chat.id)
        elif isinstance(event, Message):
            await stop_live_updates(event.chat.id)
            _advance_revision(event.chat.id)
        return await handler(event, data)
