"""Screen rendering and live-update loops."""

from __future__ import annotations

import asyncio
import html
from typing import Optional

from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError
from aiogram.types import InlineKeyboardMarkup, Message

from utils.logger_setup import logger

from .state import (
    RichPhotoScreen,
    RichScreenBody,
    RichScreenLoader,
    ScreenLoader,
    _advance_revision,
    _callback_values,
    _compose,
    _compose_rich_with_banners,
    _compose_with_banners,
    _deactivate_target,
    _disable_rich_for_revision,
    _dismiss_visible_events,
    _event_banners,
    _live_tasks,
    _lock,
    _persist_screen,
    _protect_destructive_callbacks,
    _remember,
    _restore_event_banners,
    _rich_fallback_text,
    _safe_text,
    _screen_callbacks,
    _screen_fingerprints,
    _screen_messages,
    _screen_revisions,
    _screen_rich_views,
    _screen_views,
    current_screen_token,
)
from .transport import (
    _await_telegram_mutation,
    _commit_rich_body,
    _edit_rich_view,
    _edit_view,
    _is_media_only_forbidden,
    _replacement,
    _telegram_edit,
    _telegram_edit_rich_or_fallback,
)


async def stop_live_updates(chat_id: int) -> None:
    task = _live_tasks.pop(chat_id, None)
    if task and task is not asyncio.current_task():
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


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
                                html_text=_compose_rich_with_banners(
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
    *,
    unavailable_title: str = "Данные временно недоступны",
    unavailable_hint: str = (
        "Последнее сообщение сохранено. Попробуйте обновить экран позже."
    ),
) -> None:
    """Refresh rich or text content without creating another message."""
    token = current_screen_token(message)
    try:
        body, markup = await loader()
    except Exception as error:
        logger.error(f"Не удалось загрузить rich live-экран: {error}")
        from telegram_bot.keyboards.main_menu import get_main_menu

        await render_if_current(
            token,
            message,
            f"❌ <b>{html.escape(unavailable_title[:120])}</b>\n\n"
            f"{html.escape(unavailable_hint[:500])}",
            get_main_menu(),
        )
        return
    canonical = await render_rich_if_current(token, message, body, markup)
    if canonical is None:
        return
    await start_rich_live_updates(canonical, loader, interval_seconds)
