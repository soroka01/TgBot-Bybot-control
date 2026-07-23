"""Serialized single-message Telegram UI.

Every chat owns one canonical bot message.  Network/rate-limit errors never
create duplicates; replacement is allowed only when Telegram explicitly says
that the old message is gone or cannot be edited.
"""

from __future__ import annotations

import asyncio
import hashlib
import html
import re
from contextvars import ContextVar
from typing import Awaitable, Callable, Optional, Tuple

from aiogram import BaseMiddleware, Bot
from aiogram.exceptions import (
    TelegramAPIError,
    TelegramBadRequest,
    TelegramForbiddenError,
    TelegramNetworkError,
    TelegramNotFound,
    TelegramRetryAfter,
    TelegramServerError,
)
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardMarkup,
    Message,
    TelegramObject,
    Update,
)

from config import ADMIN_TELEGRAM_IDS
from utils.logger_setup import logger


ScreenContent = Tuple[str, Optional[InlineKeyboardMarkup]]
ScreenLoader = Callable[[], Awaitable[ScreenContent]]

_screen_messages: dict[int, int] = {}
_screen_revisions: dict[int, int] = {}
_screen_callbacks: dict[int, set[str]] = {}
_screen_fingerprints: dict[int, str] = {}
_screen_views: dict[int, ScreenContent] = {}
_screen_locks: dict[int, asyncio.Lock] = {}
_live_tasks: dict[int, asyncio.Task] = {}
_event_banners: dict[int, list[tuple[str, str]]] = {}
_bot: Optional[Bot] = None
_event_loop: Optional[asyncio.AbstractEventLoop] = None
_pending_events: list[str] = []
_event_task: Optional[asyncio.Task] = None
_dismissible_event_keys: ContextVar[Optional[frozenset[str]]] = ContextVar(
    "dismissible_event_keys",
    default=None,
)
_CALLBACK_REVISION_MARKER = ":rev:"
_DESTRUCTIVE_CALLBACKS = {
    "auto:confirm_live",
    "positions:close_all_confirm",
}
_DESTRUCTIVE_CALLBACK_PREFIXES = (
    "alerts:delete:",
    "pos:close_confirm:",
)


def _lock(chat_id: int) -> asyncio.Lock:
    return _screen_locks.setdefault(chat_id, asyncio.Lock())


def _safe_text(text: str) -> str:
    if len(text) <= 4_096:
        return text
    # On the rare oversized screen, prefer valid plain text over a truncated
    # HTML tag/entity that would make Telegram reject the edit.
    plain = html.unescape(re.sub(r"<[^>]*>", "", text))
    return html.escape(plain[:4_090]) + "…"


def _callback_values(markup: Optional[InlineKeyboardMarkup]) -> set[str]:
    if not markup:
        return set()
    return {
        button.callback_data
        for row in markup.inline_keyboard
        for button in row
        if button.callback_data
    }


def callback_action(callback_data: Optional[str]) -> str:
    """Return callback payload without its UI-revision suffix."""
    value = callback_data or ""
    action, marker, revision = value.rpartition(_CALLBACK_REVISION_MARKER)
    if marker and revision.isdecimal():
        return action
    return value


def _callback_revision(callback_data: Optional[str]) -> Optional[int]:
    value = callback_data or ""
    _, marker, revision = value.rpartition(_CALLBACK_REVISION_MARKER)
    if not marker or not revision.isdecimal():
        return None
    return int(revision)


def _is_destructive_callback(callback_data: Optional[str]) -> bool:
    action = callback_action(callback_data)
    return (
        action in _DESTRUCTIVE_CALLBACKS
        or action.startswith(_DESTRUCTIVE_CALLBACK_PREFIXES)
    )


def _protect_destructive_callbacks(
    chat_id: int,
    markup: Optional[InlineKeyboardMarkup],
) -> Optional[InlineKeyboardMarkup]:
    if not markup:
        return None
    revision = _screen_revisions.get(chat_id, 0)
    changed = False
    rows = []
    for row in markup.inline_keyboard:
        protected_row = []
        for button in row:
            data = button.callback_data
            action = callback_action(data)
            if data and _is_destructive_callback(action):
                protected = f"{action}{_CALLBACK_REVISION_MARKER}{revision}"
                if protected != data:
                    button = button.model_copy(
                        update={"callback_data": protected}
                    )
                    changed = True
            protected_row.append(button)
        rows.append(protected_row)
    if not changed:
        return markup
    return markup.model_copy(update={"inline_keyboard": rows})


def _fingerprint(text: str, markup: Optional[InlineKeyboardMarkup]) -> str:
    markup_json = markup.model_dump_json(exclude_none=True) if markup else ""
    return hashlib.sha256(f"{text}\0{markup_json}".encode("utf-8")).hexdigest()


def _compose_with_banners(
    text: str,
    banners: Optional[list[tuple[str, str]]],
) -> str:
    if not banners:
        return text
    selected: list[str] = []
    remaining = 1_000
    # New deliveries must never be hidden behind older accumulated text.
    for _, event_text in reversed(banners[-5:]):
        separator = 1 if selected else 0
        if remaining <= separator:
            break
        available = remaining - separator
        piece = event_text[:available]
        selected.append(piece)
        remaining -= len(piece) + separator
    banner = "\n".join(selected)
    return (
        "🔔 <b>Последние события</b>\n"
        f"<code>{html.escape(banner)}</code>\n\n{text}"
    )


def _compose(chat_id: int, text: str) -> str:
    return _compose_with_banners(text, _event_banners.get(chat_id))


def register_bot(bot: Bot) -> None:
    global _bot, _event_loop
    _bot = bot
    _event_loop = asyncio.get_running_loop()


async def unregister_bot() -> None:
    global _bot, _event_loop, _event_task
    tasks = list(_live_tasks.values())
    if _event_task:
        tasks.append(_event_task)
    for task in tasks:
        task.cancel()
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)
    _live_tasks.clear()
    _screen_messages.clear()
    _screen_revisions.clear()
    _screen_callbacks.clear()
    _screen_fingerprints.clear()
    _screen_views.clear()
    _screen_locks.clear()
    _event_banners.clear()
    _pending_events.clear()
    _event_task = None
    _bot = None
    _event_loop = None


async def _remember(message: Message) -> None:
    chat_id = message.chat.id
    _screen_messages[chat_id] = message.message_id
    try:
        from storage.database import get_store

        await asyncio.to_thread(
            get_store().save_screen,
            chat_id,
            message.message_id,
            _screen_revisions.get(chat_id, 0),
        )
    except Exception as error:
        logger.warning(f"Не удалось сохранить экран {chat_id}: {error}")


async def _persist_screen(chat_id: int, message_id: int) -> None:
    try:
        from storage.database import get_store

        await asyncio.to_thread(
            get_store().save_screen,
            chat_id,
            message_id,
            _screen_revisions.get(chat_id, 0),
        )
    except Exception as error:
        logger.warning(f"Не удалось сохранить revision экрана {chat_id}: {error}")


async def _deactivate_target(chat_id: int) -> None:
    _screen_messages.pop(chat_id, None)
    _screen_revisions.pop(chat_id, None)
    _screen_callbacks.pop(chat_id, None)
    _screen_fingerprints.pop(chat_id, None)
    _screen_views.pop(chat_id, None)
    _event_banners.pop(chat_id, None)
    try:
        from storage.database import get_store

        await asyncio.to_thread(get_store().deactivate_chat, chat_id)
    except Exception as error:
        logger.warning(f"Не удалось деактивировать Telegram target {chat_id}: {error}")


def restore_screen_targets(targets: list[tuple[int, int, int]]) -> None:
    for chat_id, message_id, revision in targets:
        _screen_messages[chat_id] = message_id
        _screen_revisions[chat_id] = revision


async def refresh_restored_screens() -> None:
    """Remove stale “live update” claims after a process restart."""
    if not _bot:
        return
    from telegram_bot.keyboards.main_menu import get_main_menu

    text = (
        "♻️ <b>Бот перезапущен</b>\n\n"
        "Сохранённый экран восстановлен. Откройте нужный раздел — "
        "live-обновление продолжится в этом же сообщении."
    )
    markup = get_main_menu()
    for chat_id, message_id in list(_screen_messages.items()):
        _screen_views[chat_id] = (text, markup)
        # Revoke every pre-restart keyboard before attempting the network
        # edit.  If Telegram is temporarily unavailable, an old destructive
        # callback must still be rejected locally.
        _screen_callbacks[chat_id] = _callback_values(markup)
        result = await _telegram_edit(_bot, chat_id, message_id, text, markup)
        if result == "permanent_failure":
            await _deactivate_target(chat_id)


def _advance_revision(chat_id: int) -> int:
    revision = _screen_revisions.get(chat_id, 0) + 1
    _screen_revisions[chat_id] = revision
    return revision


def current_screen_token(message: Message) -> tuple[int, int, int]:
    return (
        message.chat.id,
        message.message_id,
        _screen_revisions.get(message.chat.id, 0),
    )


def is_current_screen(token: tuple[int, int, int]) -> bool:
    chat_id, message_id, revision = token
    return (
        _screen_messages.get(chat_id) == message_id
        and _screen_revisions.get(chat_id) == revision
    )


def _dismiss_visible_events(chat_id: int) -> None:
    dismissible = _dismissible_event_keys.get()
    if dismissible is None:
        _event_banners.pop(chat_id, None)
        return
    remaining = [
        item
        for item in _event_banners.get(chat_id, [])
        if item[0] not in dismissible
    ]
    if remaining:
        _event_banners[chat_id] = remaining
    else:
        _event_banners.pop(chat_id, None)


async def stop_live_updates(chat_id: int) -> None:
    task = _live_tasks.pop(chat_id, None)
    if task and task is not asyncio.current_task():
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


async def _telegram_edit(
    bot: Bot,
    chat_id: int,
    message_id: int,
    text: str,
    reply_markup: Optional[InlineKeyboardMarkup],
    *,
    verify_exists: bool = False,
) -> str:
    """Return ``ok``, ``missing`` or ``temporary_failure``."""
    safe = _safe_text(text)
    fingerprint = _fingerprint(safe, reply_markup)
    if not verify_exists and _screen_fingerprints.get(chat_id) == fingerprint:
        return "ok"
    for attempt in range(2):
        try:
            await bot.edit_message_text(
                chat_id=chat_id,
                message_id=message_id,
                text=safe,
                reply_markup=reply_markup,
                parse_mode="HTML",
            )
            _screen_fingerprints[chat_id] = fingerprint
            _screen_callbacks[chat_id] = _callback_values(reply_markup)
            return "ok"
        except TelegramRetryAfter as error:
            if attempt:
                return "temporary_failure"
            await asyncio.sleep(min(float(error.retry_after), 30.0))
        except TelegramNotFound:
            return "missing"
        except TelegramForbiddenError as error:
            logger.warning(f"Telegram запретил edit для chat {chat_id}: {error}")
            return "permanent_failure"
        except TelegramBadRequest as error:
            message = str(error).lower()
            if "message is not modified" in message:
                _screen_fingerprints[chat_id] = fingerprint
                _screen_callbacks[chat_id] = _callback_values(reply_markup)
                return "ok"
            if any(
                marker in message
                for marker in (
                    "message to edit not found",
                    "message can't be edited",
                    "message_id_invalid",
                )
            ):
                return "missing"
            logger.warning(f"Telegram отклонил edit {chat_id}/{message_id}: {error}")
            return "temporary_failure"
        except (TelegramNetworkError, TelegramServerError) as error:
            if attempt:
                logger.warning(f"Временная ошибка Telegram edit {chat_id}: {error}")
                return "temporary_failure"
            await asyncio.sleep(0.5)
        except TelegramAPIError as error:
            logger.warning(f"Ошибка Telegram edit {chat_id}/{message_id}: {error}")
            return "temporary_failure"
    return "temporary_failure"


async def _edit_view(
    bot: Bot,
    chat_id: int,
    message_id: int,
    text: str,
    markup: Optional[InlineKeyboardMarkup],
    *,
    dismiss_events: bool = False,
    verify_exists: bool = False,
    expected_revision: Optional[int] = None,
) -> str:
    async with _lock(chat_id):
        if (
            _screen_messages.get(chat_id, message_id) != message_id
            or (
                expected_revision is not None
                and _screen_revisions.get(chat_id, 0) != expected_revision
            )
        ):
            return "stale"
        markup = _protect_destructive_callbacks(chat_id, markup)
        _screen_views[chat_id] = (text, markup)
        if dismiss_events:
            _dismiss_visible_events(chat_id)
        return await _telegram_edit(
            bot,
            chat_id,
            message_id,
            _compose(chat_id, text),
            markup,
            verify_exists=verify_exists,
        )


async def _replacement(
    message: Message,
    text: str,
    markup: Optional[InlineKeyboardMarkup],
) -> Message:
    chat_id = message.chat.id
    async with _lock(chat_id):
        markup = _protect_destructive_callbacks(chat_id, markup)
        replacement = await message.answer(
            _safe_text(_compose(chat_id, text)),
            reply_markup=markup,
            parse_mode="HTML",
        )
        _screen_views[chat_id] = (text, markup)
        _screen_fingerprints.pop(chat_id, None)
        await _remember(replacement)
        _screen_callbacks[chat_id] = _callback_values(markup)
        return replacement


async def render_callback_screen(
    message: Message,
    text: str,
    reply_markup: Optional[InlineKeyboardMarkup],
) -> Message:
    chat_id = message.chat.id
    await stop_live_updates(chat_id)
    if _screen_messages.get(chat_id) not in {None, message.message_id}:
        return message
    _advance_revision(chat_id)
    await _remember(message)
    result = await _edit_view(
        message.bot,
        chat_id,
        message.message_id,
        text,
        reply_markup,
        dismiss_events=True,
        verify_exists=True,
    )
    if result == "missing":
        return await _replacement(message, text, reply_markup)
    return message


async def render_if_current(
    token: tuple[int, int, int],
    message: Message,
    text: str,
    reply_markup: Optional[InlineKeyboardMarkup],
) -> Optional[Message]:
    """Atomically render an async result only into the route that requested it."""
    chat_id, message_id, revision = token
    async with _lock(chat_id):
        if (
            message.chat.id != chat_id
            or message.message_id != message_id
            or _screen_messages.get(chat_id) != message_id
            or _screen_revisions.get(chat_id) != revision
        ):
            return None
        reply_markup = _protect_destructive_callbacks(chat_id, reply_markup)
        _screen_views[chat_id] = (text, reply_markup)
        _dismiss_visible_events(chat_id)
        result = await _telegram_edit(
            message.bot,
            chat_id,
            message_id,
            _compose(chat_id, text),
            reply_markup,
            verify_exists=True,
        )
        if result == "permanent_failure":
            await _deactivate_target(chat_id)
            return None
        if result != "missing":
            return message
        replacement = await message.answer(
            _safe_text(_compose(chat_id, text)),
            reply_markup=reply_markup,
            parse_mode="HTML",
        )
        _screen_fingerprints.pop(chat_id, None)
        await _remember(replacement)
        _screen_callbacks[chat_id] = _callback_values(reply_markup)
        return replacement


async def render_command_screen(
    message: Message,
    text: str,
    reply_markup: Optional[InlineKeyboardMarkup],
) -> Message:
    chat_id = message.chat.id
    await stop_live_updates(chat_id)
    _advance_revision(chat_id)
    message_id = _screen_messages.get(chat_id)
    if message_id:
        result = await _edit_view(
            message.bot,
            chat_id,
            message_id,
            text,
            reply_markup,
            dismiss_events=True,
            verify_exists=True,
        )
        if result != "missing":
            await _persist_screen(chat_id, message_id)
            return message
    async with _lock(chat_id):
        _event_banners.pop(chat_id, None)
        reply_markup = _protect_destructive_callbacks(chat_id, reply_markup)
        response = await message.answer(
            _safe_text(text),
            reply_markup=reply_markup,
            parse_mode="HTML",
        )
        _screen_views[chat_id] = (text, reply_markup)
        _screen_fingerprints.pop(chat_id, None)
        await _remember(response)
        _screen_callbacks[chat_id] = _callback_values(reply_markup)
        return response


async def start_live_updates(
    message: Message,
    loader: ScreenLoader,
    interval_seconds: float = 10.0,
    initial_markup: Optional[InlineKeyboardMarkup] = None,
) -> None:
    del initial_markup
    await stop_live_updates(message.chat.id)
    await _remember(message)
    chat_id = message.chat.id
    message_id = message.message_id
    bot = message.bot
    revision = _screen_revisions.get(chat_id, 0)

    async def refresh_loop() -> None:
        delay = interval_seconds
        try:
            while True:
                await asyncio.sleep(delay)
                if (
                    _screen_messages.get(chat_id) != message_id
                    or _screen_revisions.get(chat_id) != revision
                ):
                    return
                try:
                    text, markup = await loader()
                    delay = interval_seconds
                except Exception as error:
                    delay = min(60.0, max(interval_seconds, delay * 1.8))
                    logger.warning(
                        f"Live-экран {chat_id} временно устарел; "
                        f"повтор через {delay:.0f}с: {error}"
                    )
                    continue
                result = await _edit_view(
                    bot,
                    chat_id,
                    message_id,
                    text,
                    markup,
                    expected_revision=revision,
                )
                if result == "permanent_failure":
                    await _deactivate_target(chat_id)
                    return
                if result in {"missing", "stale"}:
                    return
        except asyncio.CancelledError:
            raise
        finally:
            if _live_tasks.get(chat_id) is asyncio.current_task():
                _live_tasks.pop(chat_id, None)

    _live_tasks[chat_id] = asyncio.create_task(
        refresh_loop(),
        name=f"telegram-live-{chat_id}",
    )


async def render_live_screen(
    message: Message,
    loader: ScreenLoader,
    interval_seconds: float = 10.0,
) -> None:
    token = current_screen_token(message)
    try:
        text, markup = await loader()
    except Exception as error:
        logger.error(f"Не удалось загрузить live-экран: {error}")
        from telegram_bot.keyboards.main_menu import get_main_menu

        await render_if_current(
            token,
            message,
            f"❌ <b>Данные временно недоступны</b>\n\n"
            f"<code>{html.escape(str(error)[:300])}</code>",
            get_main_menu(),
        )
        return
    canonical = await render_if_current(token, message, text, markup)
    if canonical is None:
        return
    await start_live_updates(canonical, loader, interval_seconds)


async def _render_event(
    text: str,
    chat_ids: list[int],
    *,
    event_key: Optional[str] = None,
) -> dict[int, str]:
    outcomes: dict[int, str] = {}
    if not _bot:
        return outcomes
    key = event_key or hashlib.sha256(text.encode("utf-8")).hexdigest()[:24]
    for chat_id in chat_ids:
        async with _lock(chat_id):
            message_id = _screen_messages.get(chat_id)
            base = _screen_views.get(chat_id)
            if not message_id or not base:
                outcomes[chat_id] = "unavailable"
                continue
            previous = list(_event_banners.get(chat_id, []))
            proposed = previous
            if not any(item[0] == key for item in previous):
                proposed = (previous + [(key, text[:1_000])])[-5:]
            result = await _telegram_edit(
                _bot,
                chat_id,
                message_id,
                _compose_with_banners(base[0], proposed),
                base[1],
                verify_exists=True,
            )
            # Publish banner state only after Telegram confirms that this exact
            # composition became visible.  Update-arrival snapshots therefore
            # never acknowledge an in-flight or failed outbox delivery.
            if result == "ok":
                if proposed:
                    _event_banners[chat_id] = proposed
                else:
                    _event_banners.pop(chat_id, None)
            if result == "permanent_failure":
                await _deactivate_target(chat_id)
            outcomes[chat_id] = result
    return outcomes


async def _flush_events() -> None:
    global _event_task
    try:
        await asyncio.sleep(0.3)
        events = _pending_events[-5:]
        _pending_events.clear()
        targets = [
            chat_id
            for chat_id in _screen_messages
            if chat_id in ADMIN_TELEGRAM_IDS
        ]
        if events and targets:
            await _render_event("\n\n".join(events), targets)
    finally:
        _event_task = None
        if _pending_events:
            _event_task = asyncio.create_task(
                _flush_events(),
                name="telegram-event-coalescer",
            )


def _queue_event(text: str) -> None:
    global _event_task
    _pending_events.append(text)
    if _event_task is None or _event_task.done():
        _event_task = asyncio.create_task(
            _flush_events(),
            name="telegram-event-coalescer",
        )


def publish_event(text: str) -> bool:
    owner_targets = set(_screen_messages).intersection(ADMIN_TELEGRAM_IDS)
    if not _bot or not _event_loop or not owner_targets:
        logger.debug(f"[Telegram owner screen unavailable] {text}")
        return False
    try:
        _event_loop.call_soon_threadsafe(_queue_event, text)
        return True
    except RuntimeError:
        logger.debug(f"[Telegram UI loop stopped] {text}")
        return False


def publish_event_to_chat(chat_id: int, text: str) -> bool:
    if not _bot or not _event_loop or chat_id not in _screen_messages:
        return False

    def queue() -> None:
        asyncio.create_task(
            _render_event(text, [chat_id]),
            name=f"telegram-alert-{chat_id}",
        )

    try:
        _event_loop.call_soon_threadsafe(queue)
        return True
    except RuntimeError:
        return False


async def deliver_event_to_chat(
    chat_id: int,
    text: str,
    *,
    event_key: Optional[str] = None,
) -> str:
    """Await an in-place alert edit and return its durable delivery outcome."""
    if not _bot or chat_id not in _screen_messages:
        return "unavailable"
    outcomes = await _render_event(
        text,
        [chat_id],
        event_key=(
            event_key
            or hashlib.sha256(text.encode("utf-8")).hexdigest()[:24]
        ),
    )
    return outcomes.get(chat_id, "unavailable")


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
