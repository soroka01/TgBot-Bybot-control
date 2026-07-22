"""Keyboards for persistent multi-user alerts."""

from __future__ import annotations

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup


def get_alerts_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="💲 Новый ценовой", callback_data="alerts:new:price"),
            InlineKeyboardButton(text="📊 Новый RSI", callback_data="alerts:new:rsi"),
        ],
        [
            InlineKeyboardButton(text="🔄 Обновить", callback_data="alerts:list"),
            InlineKeyboardButton(text="◀️ Меню", callback_data="menu:main"),
        ],
    ])


def get_alert_type_menu(kind: str) -> InlineKeyboardMarkup:
    label = "Цена" if kind == "price" else "RSI"
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(
                text=f"📈 {label} выше · один раз",
                callback_data=f"alerts:create:{kind}:above:once",
            ),
            InlineKeyboardButton(
                text=f"📉 {label} ниже · один раз",
                callback_data=f"alerts:create:{kind}:below:once",
            ),
        ],
        [
            InlineKeyboardButton(
                text=f"📈 {label} выше · повтор",
                callback_data=f"alerts:create:{kind}:above:repeat",
            ),
            InlineKeyboardButton(
                text=f"📉 {label} ниже · повтор",
                callback_data=f"alerts:create:{kind}:below:repeat",
            ),
        ],
        [InlineKeyboardButton(text="◀️ К алертам", callback_data="menu:alerts")],
    ])


def get_alert_list_menu(alerts: list[dict]) -> InlineKeyboardMarkup:
    rows = []
    for alert in alerts[:12]:
        kind = "💲" if alert["kind"] == "price" else "📊"
        direction = "↑" if alert["direction"] == "above" else "↓"
        state = "" if alert["is_enabled"] else " · завершён"
        rows.append([
            InlineKeyboardButton(
                text=f"🗑 {kind} {alert['symbol']} {direction} {alert['threshold']}{state}",
                callback_data=f"alerts:delete:{alert['id']}",
            )
        ])
    rows.extend([
        [
            InlineKeyboardButton(text="➕ Цена", callback_data="alerts:new:price"),
            InlineKeyboardButton(text="➕ RSI", callback_data="alerts:new:rsi"),
        ],
        [InlineKeyboardButton(text="◀️ Меню", callback_data="menu:main")],
    ])
    return InlineKeyboardMarkup(inline_keyboard=rows)
