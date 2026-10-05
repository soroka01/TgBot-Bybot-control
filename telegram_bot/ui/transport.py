"""Telegram edit/replace transport for the single-message UI."""

from __future__ import annotations

import asyncio
from typing import Awaitable, Optional

from aiogram import Bot
from aiogram.exceptions import (
    TelegramAPIError,
    TelegramBadRequest,
    TelegramForbiddenError,
    TelegramNetworkError,
    TelegramNotFound,
    TelegramRetryAfter,
    TelegramServerError,
)
from aiogram.types import InlineKeyboardMarkup, Message

from utils.logger_setup import logger

from .state import (
    RichPhotoScreen,
    RichScreenBody,
    _callback_values,
    _compose,
    _compose_rich_with_banners,
    _compose_with_banners,
    _disable_rich_for_revision,
    _dismiss_visible_events,
    _event_banners,
    _fingerprint,
    _lock,
    _protect_destructive_callbacks,
    _remember,
    _restore_event_banners,
    _rich_fallback_text,
    _rich_fingerprint,
    _rich_is_disabled,
    _safe_text,
    _screen_callbacks,
    _screen_fingerprints,
    _screen_messages,
    _screen_revisions,
    _screen_rich_views,
    _screen_views,
)


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
    html_text = _compose_rich_with_banners(screen.html, banners)
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
