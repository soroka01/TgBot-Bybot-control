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
from dataclasses import dataclass
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
    BufferedInputFile,
    CallbackQuery,
    InlineKeyboardMarkup,
    InputMediaPhoto,
    InputRichMessage,
    InputRichMessageMedia,
    Message,
    TelegramObject,
    Update,
)

from config import ADMIN_TELEGRAM_IDS
from utils.logger_setup import logger


ScreenContent = Tuple[str, Optional[InlineKeyboardMarkup]]
ScreenLoader = Callable[[], Awaitable[ScreenContent]]


@dataclass(frozen=True)
class RichPhotoScreen:
    """One editable Telegram rich message with an uploaded in-message photo."""

    html: str
    photo: bytes
    fallback_text: str
    filename: str = "chart.png"
    media_id: str = "market_chart"

    def __post_init__(self) -> None:
        if not self.html or len(self.html) > 30_000:
            raise ValueError("Rich screen HTML has an invalid size")
        if not self.photo or len(self.photo) > 8 * 1024 * 1024:
            raise ValueError("Rich screen photo has an invalid size")
        if not self.fallback_text:
            raise ValueError("Rich screen requires an accessible text fallback")
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", self.media_id):
            raise ValueError("Rich screen media_id is invalid")
        if f"tg://photo?id={self.media_id}" not in self.html:
            raise ValueError("Rich screen HTML does not reference its photo")
        if (
            not re.fullmatch(r"[A-Za-z0-9_.-]{1,80}", self.filename)
            or not self.filename.lower().endswith(".png")
        ):
            raise ValueError("Rich screen filename is invalid")

    def telegram_content(self, *, html_text: Optional[str] = None) -> InputRichMessage:
        return InputRichMessage(
            html=html_text if html_text is not None else self.html,
            media=[
                InputRichMessageMedia(
                    id=self.media_id,
                    media=InputMediaPhoto(
                        media=BufferedInputFile(
                            self.photo,
                            filename=self.filename,
                        )
                    ),
                )
            ],
            skip_entity_detection=True,
        )


RichScreenBody = RichPhotoScreen | str
RichScreenContent = Tuple[RichScreenBody, Optional[InlineKeyboardMarkup]]
RichScreenLoader = Callable[[], Awaitable[RichScreenContent]]

_screen_messages: dict[int, int] = {}
_screen_revisions: dict[int, int] = {}
_screen_callbacks: dict[int, set[str]] = {}
_screen_fingerprints: dict[int, str] = {}
_screen_views: dict[int, ScreenContent] = {}
_screen_rich_views: dict[
    int,
    Tuple[RichPhotoScreen, Optional[InlineKeyboardMarkup]],
] = {}
_rich_disabled_revisions: dict[int, int] = {}
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


def _rich_fingerprint(
    screen: RichPhotoScreen,
    html_text: str,
    markup: Optional[InlineKeyboardMarkup],
) -> str:
    markup_json = markup.model_dump_json(exclude_none=True) if markup else ""
    digest = hashlib.sha256()
    digest.update(html_text.encode("utf-8"))
    digest.update(b"\0")
    digest.update(screen.photo)
    digest.update(b"\0")
    digest.update(markup_json.encode("utf-8"))
    return digest.hexdigest()


def _rich_fallback_text(body: RichScreenBody) -> str:
    text = body.fallback_text if isinstance(body, RichPhotoScreen) else body
    if not text:
        raise ValueError("Rich live screen requires non-empty fallback text")
    return text


def _rich_is_disabled(
    chat_id: int,
    revision: Optional[int] = None,
) -> bool:
    target_revision = (
        _screen_revisions.get(chat_id, 0)
        if revision is None
        else revision
    )
    return (
        _rich_disabled_revisions.get(chat_id)
        == target_revision
    )


def _disable_rich_for_revision(chat_id: int, revision: int) -> bool:
    if _screen_revisions.get(chat_id, 0) != revision:
        return False
    _rich_disabled_revisions[chat_id] = revision
    return True


def _restore_event_banners(
    chat_id: int,
    banners: list[tuple[str, str]],
) -> None:
    if banners:
        _event_banners[chat_id] = banners
    else:
        _event_banners.pop(chat_id, None)


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
    _screen_rich_views.clear()
    _rich_disabled_revisions.clear()
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
    _screen_rich_views.pop(chat_id, None)
    _rich_disabled_revisions.pop(chat_id, None)
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
        _rich_disabled_revisions.pop(chat_id, None)


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
        _screen_rich_views.pop(chat_id, None)
        # Revoke every pre-restart keyboard before attempting the network
        # edit.  If Telegram is temporarily unavailable, an old destructive
        # callback must still be rejected locally.
        _screen_callbacks[chat_id] = _callback_values(markup)
        result = await _telegram_edit(_bot, chat_id, message_id, text, markup)
        if result in {"missing", "permanent_failure"}:
            await _deactivate_target(chat_id)


def _advance_revision(chat_id: int) -> int:
    revision = _screen_revisions.get(chat_id, 0) + 1
    _screen_revisions[chat_id] = revision
    _rich_disabled_revisions.pop(chat_id, None)
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


async def _await_telegram_mutation(
    mutation: Awaitable,
    *,
    propagate_cancel: bool = True,
):
    """Let an already-started Telegram edit settle before propagating cancel.

    Cancelling the local HTTP await cannot recall a request that Telegram may
    already be processing.  Waiting for that request while the per-chat lock
    remains held guarantees that a newer route edit is sent afterwards.
    """
    task = asyncio.ensure_future(mutation)
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError as cancelled:
        while not task.done():
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError:
                # Shutdown or navigation may cancel the outer task more than
                # once; the Telegram mutation itself must still settle.
                continue
        try:
            result = task.result()
        except BaseException:
            # The caller is already being cancelled.  Consume the mutation's
            # outcome so it cannot become an unobserved task exception.
            if propagate_cancel:
                raise cancelled
            raise
        if propagate_cancel:
            raise cancelled
        # A send must be recorded as canonical after Telegram accepted it;
        # swallowing cancellation for this short commit path prevents an
        # orphan response and a duplicate replacement on the next update.
        return result


def _is_media_only_forbidden(error: TelegramForbiddenError) -> bool:
    message = str(error).lower()
    return any(
        marker in message
        for marker in (
            "chat_send_photos_forbidden",
            "not allowed to send photo",
            "not allowed to send media",
            "not enough rights to send photo",
            "not enough rights to send media",
            "can't send photo",
            "can not send photo",
            "photo messages are forbidden",
            "media messages are forbidden",
            "send photos is forbidden",
            "sending photos is forbidden",
            "sending media is forbidden",
        )
    )


async def _telegram_edit(
    bot: Bot,
    chat_id: int,
    message_id: int,
    text: str,
    reply_markup: Optional[InlineKeyboardMarkup],
    *,
    verify_exists: bool = False,
) -> str:
    """Edit text and return a classified delivery outcome."""
    safe = _safe_text(text)
    fingerprint = _fingerprint(safe, reply_markup)
    if not verify_exists and _screen_fingerprints.get(chat_id) == fingerprint:
        return "ok"
    for attempt in range(2):
        try:
            await _await_telegram_mutation(
                bot.edit_message_text(
                    chat_id=chat_id,
                    message_id=message_id,
                    text=safe,
                    reply_markup=reply_markup,
                    parse_mode="HTML",
                )
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
                    "message_id_invalid",
                )
            ):
                return "missing"
            if any(
                marker in message
                for marker in (
                    "message can't be edited",
                    "message can not be edited",
                )
            ):
                return "uneditable"
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


async def _telegram_edit_rich(
    bot: Bot,
    chat_id: int,
    message_id: int,
    screen: RichPhotoScreen,
    reply_markup: Optional[InlineKeyboardMarkup],
    *,
    banners: Optional[list[tuple[str, str]]] = None,
    verify_exists: bool = False,
) -> str:
    """Edit the canonical message as rich content without changing its id."""
    html_text = _compose_with_banners(screen.html, banners)
    fingerprint = _rich_fingerprint(screen, html_text, reply_markup)
    if not verify_exists and _screen_fingerprints.get(chat_id) == fingerprint:
        return "ok"
    for attempt in range(2):
        try:
            await _await_telegram_mutation(
                bot.edit_message_text(
                    chat_id=chat_id,
                    message_id=message_id,
                    rich_message=screen.telegram_content(html_text=html_text),
                    parse_mode=None,
                    reply_markup=reply_markup,
                )
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
            if _is_media_only_forbidden(error):
                logger.warning(
                    f"Telegram запретил media rich edit для chat {chat_id}; "
                    "переключаюсь на текстовый экран"
                )
                return "rich_unsupported"
            logger.warning(f"Telegram запретил rich edit для chat {chat_id}: {error}")
            return "permanent_failure"
        except TelegramBadRequest as error:
            error_text = str(error).lower()
            if "message is not modified" in error_text:
                _screen_fingerprints[chat_id] = fingerprint
                _screen_callbacks[chat_id] = _callback_values(reply_markup)
                return "ok"
            if any(
                marker in error_text
                for marker in (
                    "message to edit not found",
                    "message_id_invalid",
                )
            ):
                return "missing"
            if any(
                marker in error_text
                for marker in (
                    "message can't be edited",
                    "message can not be edited",
                )
            ):
                return "uneditable"
            logger.warning(
                f"Telegram отклонил rich edit {chat_id}/{message_id}; "
                f"переключаюсь на текстовый экран: {error}"
            )
            return "rich_unsupported"
        except (TelegramNetworkError, TelegramServerError) as error:
            if attempt:
                logger.warning(
                    f"Временная ошибка Telegram rich edit {chat_id}: {error}"
                )
                return "temporary_failure"
            await asyncio.sleep(0.5)
        except TelegramAPIError as error:
            logger.warning(
                f"Ошибка Telegram rich edit {chat_id}/{message_id}: {error}"
            )
            return "temporary_failure"
    return "temporary_failure"


async def _telegram_edit_rich_or_fallback(
    bot: Bot,
    chat_id: int,
    message_id: int,
    body: RichScreenBody,
    reply_markup: Optional[InlineKeyboardMarkup],
    *,
    banners: Optional[list[tuple[str, str]]] = None,
    verify_exists: bool = False,
    expected_revision: Optional[int] = None,
) -> tuple[str, str]:
    """Return ``(outcome, visible_mode)`` for a rich-or-text delivery."""
    revision = (
        _screen_revisions.get(chat_id, 0)
        if expected_revision is None
        else expected_revision
    )
    if isinstance(body, RichPhotoScreen) and not _rich_is_disabled(
        chat_id,
        revision,
    ):
        result = await _telegram_edit_rich(
            bot,
            chat_id,
            message_id,
            body,
            reply_markup,
            banners=banners,
            verify_exists=verify_exists,
        )
        if result != "rich_unsupported":
            return result, "rich"
        if not _disable_rich_for_revision(chat_id, revision):
            return "stale", "rich"

    fallback = _compose_with_banners(
        _rich_fallback_text(body),
        banners,
    )
    result = await _telegram_edit(
        bot,
        chat_id,
        message_id,
        fallback,
        reply_markup,
        verify_exists=verify_exists,
    )
    return result, "text"


def _commit_rich_body(
    chat_id: int,
    body: RichScreenBody,
    reply_markup: Optional[InlineKeyboardMarkup],
    mode: str,
) -> None:
    """Commit only content that Telegram confirmed as visible."""
    fallback = _rich_fallback_text(body)
    _screen_views[chat_id] = (fallback, reply_markup)
    if mode == "rich":
        if not isinstance(body, RichPhotoScreen):
            raise RuntimeError("Text fallback cannot be committed as rich content")
        _screen_rich_views[chat_id] = (body, reply_markup)
    else:
        _screen_rich_views.pop(chat_id, None)


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
        _screen_rich_views.pop(chat_id, None)
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


async def _edit_rich_view(
    bot: Bot,
    chat_id: int,
    message_id: int,
    body: RichScreenBody,
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
        revision = _screen_revisions.get(chat_id, 0)
        markup = _protect_destructive_callbacks(chat_id, markup)
        previous_banners = list(_event_banners.get(chat_id, []))
        if dismiss_events:
            _dismiss_visible_events(chat_id)
        try:
            result, mode = await _telegram_edit_rich_or_fallback(
                bot,
                chat_id,
                message_id,
                body,
                markup,
                banners=_event_banners.get(chat_id),
                verify_exists=verify_exists,
                expected_revision=revision,
            )
        except BaseException:
            if dismiss_events:
                _restore_event_banners(chat_id, previous_banners)
            raise
        if (
            _screen_messages.get(chat_id) != message_id
            or _screen_revisions.get(chat_id, 0) != revision
        ):
            if dismiss_events:
                _restore_event_banners(chat_id, previous_banners)
            return "stale"
        if result == "ok":
            _commit_rich_body(chat_id, body, markup, mode)
        elif dismiss_events:
            _restore_event_banners(chat_id, previous_banners)
        return result


async def _replacement(
    message: Message,
    text: str,
    markup: Optional[InlineKeyboardMarkup],
) -> Message:
    chat_id = message.chat.id
    async with _lock(chat_id):
        markup = _protect_destructive_callbacks(chat_id, markup)
        replacement = await _await_telegram_mutation(
            message.answer(
                _safe_text(_compose(chat_id, text)),
                reply_markup=markup,
                parse_mode="HTML",
            ),
            propagate_cancel=False,
        )
        _screen_views[chat_id] = (text, markup)
        _screen_rich_views.pop(chat_id, None)
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
        _screen_rich_views.pop(chat_id, None)
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
        replacement = await _await_telegram_mutation(
            message.answer(
                _safe_text(_compose(chat_id, text)),
                reply_markup=reply_markup,
                parse_mode="HTML",
            ),
            propagate_cancel=False,
        )
        _screen_fingerprints.pop(chat_id, None)
        await _remember(replacement)
        _screen_callbacks[chat_id] = _callback_values(reply_markup)
        return replacement


async def render_rich_if_current(
    token: tuple[int, int, int],
    message: Message,
    body: RichScreenBody,
    reply_markup: Optional[InlineKeyboardMarkup],
) -> Optional[Message]:
    """Render a rich image or text fallback only for its originating route."""
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
        previous_banners = list(_event_banners.get(chat_id, []))
        _dismiss_visible_events(chat_id)
        try:
            result, mode = await _telegram_edit_rich_or_fallback(
                message.bot,
                chat_id,
                message_id,
                body,
                reply_markup,
                banners=_event_banners.get(chat_id),
                verify_exists=True,
                expected_revision=revision,
            )
        except BaseException:
            _restore_event_banners(chat_id, previous_banners)
            raise
        if (
            _screen_messages.get(chat_id) != message_id
            or _screen_revisions.get(chat_id, 0) != revision
        ):
            _restore_event_banners(chat_id, previous_banners)
            return None
        if result == "ok":
            _commit_rich_body(chat_id, body, reply_markup, mode)
            return message
        if result == "permanent_failure":
            await _deactivate_target(chat_id)
            return None
        if result == "uneditable":
            _restore_event_banners(chat_id, previous_banners)
            logger.warning(
                f"Canonical Telegram message {chat_id}/{message_id} "
                "больше нельзя редактировать; replacement не создаю"
            )
            return None
        if result != "missing":
            _restore_event_banners(chat_id, previous_banners)
            return message

        replacement_mode = mode
        try:
            if mode == "rich":
                if not isinstance(body, RichPhotoScreen):
                    raise RuntimeError("Text fallback cannot be sent as rich content")
                try:
                    replacement = await _await_telegram_mutation(
                        message.answer_rich(
                            body.telegram_content(
                                html_text=_compose_with_banners(
                                    body.html,
                                    _event_banners.get(chat_id),
                                )
                            ),
                            reply_markup=reply_markup,
                        ),
                        propagate_cancel=False,
                    )
                except TelegramBadRequest as error:
                    logger.warning(
                        f"Telegram отклонил rich replacement {chat_id}; "
                        f"переключаюсь на текстовый экран: {error}"
                    )
                    if not _disable_rich_for_revision(chat_id, revision):
                        _restore_event_banners(chat_id, previous_banners)
                        return None
                    replacement_mode = "text"
                    replacement = await _await_telegram_mutation(
                        message.answer(
                            _safe_text(
                                _compose_with_banners(
                                    _rich_fallback_text(body),
                                    _event_banners.get(chat_id),
                                )
                            ),
                            reply_markup=reply_markup,
                            parse_mode="HTML",
                        ),
                        propagate_cancel=False,
                    )
                except TelegramForbiddenError as error:
                    if not _is_media_only_forbidden(error):
                        await _deactivate_target(chat_id)
                        return None
                    if not _disable_rich_for_revision(chat_id, revision):
                        _restore_event_banners(chat_id, previous_banners)
                        return None
                    replacement_mode = "text"
                    replacement = await _await_telegram_mutation(
                        message.answer(
                            _safe_text(
                                _compose_with_banners(
                                    _rich_fallback_text(body),
                                    _event_banners.get(chat_id),
                                )
                            ),
                            reply_markup=reply_markup,
                            parse_mode="HTML",
                        ),
                        propagate_cancel=False,
                    )
            else:
                replacement = await _await_telegram_mutation(
                    message.answer(
                        _safe_text(
                            _compose_with_banners(
                                _rich_fallback_text(body),
                                _event_banners.get(chat_id),
                            )
                        ),
                        reply_markup=reply_markup,
                        parse_mode="HTML",
                    ),
                    propagate_cancel=False,
                )
        except BaseException:
            _restore_event_banners(chat_id, previous_banners)
            raise
        _commit_rich_body(
            chat_id,
            body,
            reply_markup,
            replacement_mode,
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
        response = await _await_telegram_mutation(
            message.answer(
                _safe_text(text),
                reply_markup=reply_markup,
                parse_mode="HTML",
            ),
            propagate_cancel=False,
        )
        _screen_views[chat_id] = (text, reply_markup)
        _screen_rich_views.pop(chat_id, None)
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
                if result in {"missing", "stale", "uneditable"}:
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


async def start_rich_live_updates(
    message: Message,
    loader: RichScreenLoader,
    interval_seconds: float = 30.0,
) -> None:
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
                    body, markup = await loader()
                    delay = interval_seconds
                except Exception as error:
                    delay = min(90.0, max(interval_seconds, delay * 1.8))
                    logger.warning(
                        f"Rich live-экран {chat_id} временно устарел; "
                        f"повтор через {delay:.0f}с: {error}"
                    )
                    continue
                result = await _edit_rich_view(
                    bot,
                    chat_id,
                    message_id,
                    body,
                    markup,
                    expected_revision=revision,
                )
                if result == "permanent_failure":
                    await _deactivate_target(chat_id)
                    return
                if result in {"missing", "stale", "uneditable"}:
                    return
        except asyncio.CancelledError:
            raise
        finally:
            if _live_tasks.get(chat_id) is asyncio.current_task():
                _live_tasks.pop(chat_id, None)

    _live_tasks[chat_id] = asyncio.create_task(
        refresh_loop(),
        name=f"telegram-rich-live-{chat_id}",
    )


async def render_rich_live_screen(
    message: Message,
    loader: RichScreenLoader,
    interval_seconds: float = 30.0,
) -> None:
    """Refresh rich or text chart content without creating another message."""
    token = current_screen_token(message)
    try:
        body, markup = await loader()
    except Exception as error:
        logger.error(f"Не удалось загрузить rich live-экран: {error}")
        from telegram_bot.keyboards.main_menu import get_main_menu

        await render_if_current(
            token,
            message,
            "❌ <b>График временно недоступен</b>\n\n"
            "Последнее сообщение сохранено. Попробуйте обновить график позже.",
            get_main_menu(),
        )
        return
    canonical = await render_rich_if_current(token, message, body, markup)
    if canonical is None:
        return
    await start_rich_live_updates(canonical, loader, interval_seconds)


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
            delivery_revision = _screen_revisions.get(chat_id, 0)
            previous = list(_event_banners.get(chat_id, []))
            proposed = previous
            if not any(item[0] == key for item in previous):
                proposed = (previous + [(key, text[:1_000])])[-5:]
            rich_base = _screen_rich_views.get(chat_id)
            if rich_base:
                result, mode = await _telegram_edit_rich_or_fallback(
                    _bot,
                    chat_id,
                    message_id,
                    rich_base[0],
                    rich_base[1],
                    banners=proposed,
                    verify_exists=True,
                    expected_revision=delivery_revision,
                )
            else:
                mode = "text"
                result = await _telegram_edit(
                    _bot,
                    chat_id,
                    message_id,
                    _compose_with_banners(base[0], proposed),
                    base[1],
                    verify_exists=True,
                )
            if (
                _screen_messages.get(chat_id) != message_id
                or _screen_revisions.get(chat_id, 0) != delivery_revision
            ):
                result = "stale"
            # Publish banner state only after Telegram confirms that this exact
            # composition became visible.  Update-arrival snapshots therefore
            # never acknowledge an in-flight or failed outbox delivery.
            if result == "ok":
                if rich_base:
                    _commit_rich_body(
                        chat_id,
                        rich_base[0],
                        rich_base[1],
                        mode,
                    )
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
