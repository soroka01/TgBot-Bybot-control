"""Read-only market overview screen."""

from __future__ import annotations

import asyncio
from html import escape

from aiogram import F, Router
from aiogram.types import CallbackQuery

from core.market_overview import get_market_overview
from telegram_bot.keyboards.main_menu import get_main_menu
from telegram_bot.ui import render_live_screen

router = Router()


def build_overview_view() -> tuple[str, object]:
    overview = get_market_overview()
    lines = [
        "🌍 <b>Обзор рынка</b> <i>• кэш 90с</i>",
        "",
        f"Капитализация: <code>${overview['market_cap']:,.0f}</code>",
        f"Объём за 24ч: <code>${overview['volume']:,.0f}</code>",
        f"Доминация BTC: <code>{overview['btc_dominance']:.1f}%</code>",
        "",
        "<b>Трендовые активы</b>",
    ]
    for index, coin in enumerate(overview["trending"], 1):
        rank = "—" if coin["rank"] is None else f"#{coin['rank']}"
        lines.append(f"{index}. {escape(coin['name'])} ({escape(coin['symbol'])}) · {rank}")
    return "\n".join(lines), get_main_menu()


@router.callback_query(F.data == "menu:trends")
async def show_market_overview(callback: CallbackQuery) -> None:
    await callback.answer("Обновляю обзор...")

    async def loader() -> tuple[str, object]:
        return await asyncio.to_thread(build_overview_view)

    await render_live_screen(callback.message, loader, interval_seconds=30)
