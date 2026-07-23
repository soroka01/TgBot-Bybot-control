"""Telegram screens for durable price and RSI alerts."""

from __future__ import annotations

import asyncio
import math

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from config import ALERT_DEFAULT_COOLDOWN_SECONDS
from storage.database import get_store
from telegram_bot.keyboards.alerts_menu import (
    get_alert_list_menu,
    get_alert_type_menu,
)
from telegram_bot.ui import (
    callback_action,
    render_callback_screen,
    render_command_screen,
)
from utils.helpers import format_price

router = Router()


class AlertInput(StatesGroup):
    waiting_for_threshold = State()


def _format_alerts(alerts: list[dict]) -> str:
    if not alerts:
        return (
            "🔔 <b>Алерты</b>\n\n"
            "Пока нет активных алертов.\n"
            "Алерты проверяются каждые 15 секунд и срабатывают только при "
            "пересечении уровня."
        )

    lines = ["🔔 <b>Ваши алерты</b>", ""]
    for alert in alerts:
        icon = "💲" if alert["kind"] == "price" else "📊"
        comparator = "≥" if alert["direction"] == "above" else "≤"
        threshold = (
            format_price(alert["threshold"])
            if alert["kind"] == "price" else f"{float(alert['threshold']):.2f}"
        )
        suffix = " USDT" if alert["kind"] == "price" else ""
        interval = f" · {alert['timeframe']}" if alert["kind"] == "rsi" else ""
        mode = "повтор" if alert["repeat_mode"] == "repeat" else "один раз"
        state = "🟢" if alert["is_enabled"] else "⚪"
        lines.append(
            f"{state} #{alert['id']} {icon} <b>{alert['symbol']}{interval}</b>: "
            f"{comparator} <code>{threshold}{suffix}</code> · {mode}"
        )
    lines.extend(["", "Нажмите на строку с 🗑, чтобы удалить алерт."])
    return "\n".join(lines)


def build_alerts_view(chat_id: int) -> tuple[str, object]:
    alerts = get_store().get_alerts(chat_id, include_disabled=True)
    return _format_alerts(alerts), get_alert_list_menu(alerts)


@router.callback_query(F.data == "menu:alerts")
async def show_alerts(callback: CallbackQuery) -> None:
    await callback.answer()
    text, markup = await asyncio.to_thread(build_alerts_view, callback.message.chat.id)
    await render_callback_screen(callback.message, text, markup)


@router.callback_query(F.data == "alerts:list")
async def refresh_alerts(callback: CallbackQuery) -> None:
    await callback.answer("Обновлено")
    text, markup = await asyncio.to_thread(build_alerts_view, callback.message.chat.id)
    await render_callback_screen(callback.message, text, markup)


@router.callback_query(F.data.in_({"alerts:new:price", "alerts:new:rsi"}))
async def choose_alert_type(callback: CallbackQuery) -> None:
    kind = callback.data.rsplit(":", 1)[-1]
    await callback.answer()
    text = (
        f"➕ <b>{'Ценовой' if kind == 'price' else 'RSI'} алерт</b>\n\n"
        "Выберите направление и режим. Повторяемый алерт сможет сработать снова "
        "только после ухода значения за границу и нового пересечения."
    )
    await render_callback_screen(callback.message, text, get_alert_type_menu(kind))


@router.callback_query(F.data.startswith("alerts:create:"))
async def request_threshold(callback: CallbackQuery, state: FSMContext) -> None:
    _, _, kind, direction, repeat_mode = callback.data.split(":")
    await state.set_state(AlertInput.waiting_for_threshold)
    await state.update_data(kind=kind, direction=direction, repeat_mode=repeat_mode)
    label = "цену в USDT" if kind == "price" else "уровень RSI от 0 до 100"
    await callback.answer()
    await render_callback_screen(
        callback.message,
        f"➕ <b>Новый алерт</b>\n\nВведите {label} одним числом.\n"
        "Сообщение с числом будет удалено после обработки.",
        InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text="❌ Отмена", callback_data="alerts:cancel")]
            ]
        ),
    )


@router.callback_query(F.data == "alerts:cancel")
async def cancel_threshold(callback: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await callback.answer("Ввод отменён")
    text, markup = await asyncio.to_thread(build_alerts_view, callback.message.chat.id)
    await render_callback_screen(callback.message, text, markup)


@router.message(AlertInput.waiting_for_threshold)
async def save_threshold(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    try:
        threshold = float((message.text or "").strip().replace(",", "."))
        kind = data["kind"]
        if (
            not math.isfinite(threshold)
            or threshold <= 0
            or (kind == "rsi" and threshold > 100)
        ):
            raise ValueError
    except (ValueError, KeyError):
        try:
            await message.delete()
        except Exception:
            pass
        await render_command_screen(
            message,
            "⚠️ <b>Некорректное значение.</b> Введите положительное число"
            + (" от 0 до 100." if data.get("kind") == "rsi" else "."),
            InlineKeyboardMarkup(
                inline_keyboard=[
                    [
                        InlineKeyboardButton(
                            text="❌ Отмена",
                            callback_data="alerts:cancel",
                        )
                    ]
                ]
            ),
        )
        return

    store = get_store()
    user = await asyncio.to_thread(store.get_user, message.chat.id)
    symbol = user.get("default_symbol", "BTC")
    timeframe = user.get("default_interval", "15") if data["kind"] == "rsi" else None
    alert_id = await asyncio.to_thread(
        store.create_alert,
        message.chat.id,
        kind=data["kind"],
        symbol=symbol,
        direction=data["direction"],
        threshold=threshold,
        timeframe=timeframe,
        repeat_mode=data["repeat_mode"],
        cooldown_seconds=ALERT_DEFAULT_COOLDOWN_SECONDS,
    )
    await asyncio.to_thread(
        store.log_activity,
        message.chat.id,
        "alert_created",
        f"Создан алерт #{alert_id}",
        symbol=symbol,
        payload={"kind": data["kind"], "threshold": threshold},
    )
    await state.clear()
    try:
        await message.delete()
    except Exception:
        pass
    text, markup = await asyncio.to_thread(build_alerts_view, message.chat.id)
    await render_command_screen(message, f"✅ Алерт #{alert_id} сохранён.\n\n{text}", markup)


@router.callback_query(F.data.startswith("alerts:delete:"))
async def delete_alert(callback: CallbackQuery) -> None:
    try:
        alert_id = int(callback_action(callback.data).rsplit(":", 1)[-1])
    except ValueError:
        await callback.answer("Некорректный алерт", show_alert=True)
        return
    deleted = await asyncio.to_thread(get_store().delete_alert, callback.message.chat.id, alert_id)
    if deleted:
        await asyncio.to_thread(
            get_store().log_activity,
            callback.message.chat.id,
            "alert_deleted",
            f"Удалён алерт #{alert_id}",
        )
    await callback.answer("Алерт удалён" if deleted else "Алерт уже отсутствует")
    text, markup = await asyncio.to_thread(build_alerts_view, callback.message.chat.id)
    await render_callback_screen(callback.message, text, markup)
