"""Shared screen state, locks, revisions and callback protection."""

from __future__ import annotations

import asyncio
import hashlib
import html
import re
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Awaitable, Callable, Optional, Tuple

from aiogram.types import (
    BufferedInputFile,
    InlineKeyboardMarkup,
    InputMediaPhoto,
    InputRichMessage,
    InputRichMessageMedia,
    Message,
)

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

    @property
    def telegram_media_id(self) -> str:
        """Content-address media so Telegram never reuses a stale rich photo."""
        digest = hashlib.sha256(self.photo).hexdigest()[:12]
        base = self.media_id[: 64 - len(digest) - 1]
        return f"{base}_{digest}"

    @property
    def telegram_filename(self) -> str:
        digest = hashlib.sha256(self.photo).hexdigest()[:8]
        stem = self.filename[:-4]
        return f"{stem[:66]}-{digest}.png"

    def telegram_content(self, *, html_text: Optional[str] = None) -> InputRichMessage:
        source_html = html_text if html_text is not None else self.html
        resolved_html = source_html.replace(
            f"tg://photo?id={self.media_id}",
            f"tg://photo?id={self.telegram_media_id}",
        )
        return InputRichMessage(
            html=resolved_html,
            media=[
                InputRichMessageMedia(
                    id=self.telegram_media_id,
                    media=InputMediaPhoto(
                        media=BufferedInputFile(
                            self.photo,
                            filename=self.telegram_filename,
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


def _compose_rich_with_banners(
    rich_html: str,
    banners: Optional[list[tuple[str, str]]],
) -> str:
    """Add notifications as a distinct rich block above a report."""
    if not banners:
        return rich_html
    selected: list[str] = []
    remaining = 1_000
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
        "<blockquote>"
        "<p>🔔 <b>Последние события</b><br/>"
        f"<code>{html.escape(banner)}</code></p>"
        "</blockquote><hr/>"
        f"{rich_html}"
    )


def _compose(chat_id: int, text: str) -> str:
    return _compose_with_banners(text, _event_banners.get(chat_id))


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
