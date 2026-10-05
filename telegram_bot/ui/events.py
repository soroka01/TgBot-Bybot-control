"""Event banners and bot/loop lifecycle (owns the reassigned globals)."""

from __future__ import annotations

import asyncio
import hashlib
from typing import Optional

from aiogram import Bot

from config import ADMIN_TELEGRAM_IDS
from utils.logger_setup import logger

from .state import (
    _callback_values,
    _compose_with_banners,
    _deactivate_target,
    _event_banners,
    _live_tasks,
    _lock,
    _rich_disabled_revisions,
    _screen_callbacks,
    _screen_fingerprints,
    _screen_locks,
    _screen_messages,
    _screen_revisions,
    _screen_rich_views,
    _screen_views,
)
from .transport import (
    _commit_rich_body,
    _telegram_edit,
    _telegram_edit_rich_or_fallback,
)


_bot: Optional[Bot] = None
_event_loop: Optional[asyncio.AbstractEventLoop] = None
_pending_events: list[str] = []
_event_task: Optional[asyncio.Task] = None


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
