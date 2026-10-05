"""Rich, read-only global CoinGecko market overview."""

from __future__ import annotations

import asyncio

from aiogram import F, Router
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup

from core.market_overview import get_market_overview
from core.market_overview_report import (
    OVERVIEW_REFRESH_SECONDS,
    OVERVIEW_REPORT_MEDIA_ID,
    build_global_market_snapshot,
    format_global_market_rich_html,
    format_global_market_text,
    render_global_market_png,
)
from telegram_bot.ui import (
    RichPhotoScreen,
    render_callback_screen,
    render_rich_live_screen,
)
from utils.logger_setup import logger

router = Router()


def _overview_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="↻ Обновить",
                    callback_data="menu:trends",
                ),
                InlineKeyboardButton(
                    text="◀️ Меню",
                    callback_data="menu:main",
                ),
            ]
        ]
    )


def build_overview_view():
    """Build every representation from one cached CoinGecko response."""
    markup = _overview_menu()
    try:
        overview = get_market_overview()
    except Exception as error:
        logger.warning(
            "CoinGecko не дал первый global-market snapshot; "
            f"ожидаю следующий live-цикл ({type(error).__name__})"
        )
        return (
            "🌍 <b>Крипторынок</b>\n\n"
            "⚠️ CoinGecko временно не ответил, а сохранённого среза пока нет.\n\n"
            f"<i>Повторю автоматически через {OVERVIEW_REFRESH_SECONDS}с.</i>",
            markup,
        )
    snapshot = build_global_market_snapshot(overview)
    fallback_text = format_global_market_text(snapshot)
    if not snapshot.has_visual_data:
        return fallback_text, markup
    try:
        png = render_global_market_png(snapshot)
        rich_html = format_global_market_rich_html(snapshot)
    except Exception as error:
        logger.warning(
            "Rich-обзор глобального рынка недоступен; используется текстовый "
            f"fallback ({type(error).__name__})"
        )
        return fallback_text, markup
    return (
        RichPhotoScreen(
            html=rich_html,
            photo=png,
            fallback_text=fallback_text,
            filename="global-market-overview.png",
            media_id=OVERVIEW_REPORT_MEDIA_ID,
        ),
        markup,
    )


@router.callback_query(F.data == "menu:trends")
async def show_market_overview(callback: CallbackQuery) -> None:
    await callback.answer("Открываю крипторынок")
    canonical = await render_callback_screen(
        callback.message,
        "🌍 <b>Крипторынок</b>\n\n"
        "⏳ Загружаю глобальные показатели и популярные активы…",
        _overview_menu(),
    )

    async def loader():
        return await asyncio.to_thread(build_overview_view)

    await render_rich_live_screen(
        canonical,
        loader,
        interval_seconds=OVERVIEW_REFRESH_SECONDS,
        unavailable_title="Глобальный рынок временно недоступен",
        unavailable_hint=(
            "CoinGecko не ответил. Последнее сообщение сохранено; "
            "попробуйте обновить экран позже."
        ),
    )
