"""Read-only AI selector and compact live market overview."""

from __future__ import annotations

import asyncio
import html
from decimal import Decimal
from typing import Optional

from aiogram import F, Router
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup

from api.bybit_api import BybitAPI
from api.deepseek_api import DeepSeekAPI
from config import FALLBACK_TAKER_FEE_RATE, TRADABLE_TOKENS
from core.auto_trading import collect_cycle
from core.decision_engine import (
    build_selector_prompt,
    selected_candidate,
    validate_trade_decision,
)
from core.market_report import (
    MARKET_REPORT_MEDIA_ID,
    collect_market_report,
    format_market_report_rich_html,
    format_market_report_text,
    render_market_report_png,
)
from core.risk_engine import D, build_trade_plan
from telegram_bot.keyboards.main_menu import get_main_menu
from telegram_bot.keyboards.trading_menu import get_trading_menu
from telegram_bot.ui import (
    RichPhotoScreen,
    current_screen_token,
    render_callback_screen,
    render_if_current,
    render_rich_live_screen,
)
from utils.helpers import format_price
from utils.logger_setup import logger

router = Router()
_ai_tasks: dict[int, asyncio.Task] = {}


async def render_live_screen(*args, **kwargs):
    """Compatibility seam that preserves the rich one-message renderer."""
    return await render_rich_live_screen(*args, **kwargs)


async def shutdown_ai_tasks() -> None:
    """Prevent detached selector tasks from touching UI during shutdown."""
    tasks = list(_ai_tasks.values())
    for task in tasks:
        task.cancel()
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)
    _ai_tasks.clear()


def _read_fee_rates(bybit: BybitAPI) -> dict[str, Decimal]:
    rates: dict[str, Decimal] = {}
    for token in TRADABLE_TOKENS[:3]:
        symbol = f"{token}USDT"
        try:
            rates[symbol] = bybit.get_fee_rate(symbol)
        except Exception:
            rates[symbol] = D(FALLBACK_TAKER_FEE_RATE)
    return rates


def build_ai_recommendations() -> tuple[str, InlineKeyboardMarkup]:
    """Run the same safe selector as auto mode without sending any order."""
    selected_tokens = TRADABLE_TOKENS[:3]
    bybit: Optional[BybitAPI] = None
    deepseek: Optional[DeepSeekAPI] = None
    try:
        bybit = BybitAPI()
        fees = _read_fee_rates(bybit)
        # collect_cycle uses the configured full list.  Filter only the compact
        # recommendation output; the model still receives a coherent batch.
        cycle = collect_cycle(bybit, fees, tokens=selected_tokens)
        snapshot = cycle["snapshot"]
        candidate_count = sum(
            len(item.get("candidates", []))
            for item in snapshot["symbols"].values()
        )
        if not candidate_count:
            return (
                "🧠 <b>AI-отбор сетапов</b>\n\n"
                "😴 Исполнимых сетапов сейчас нет.\n\n"
                "<i>AI не вызывался: код не нашёл ни одного допустимого "
                "candidate_id.</i>",
                get_main_menu(),
            )
        deepseek = DeepSeekAPI()
        raw = deepseek.analyze(build_selector_prompt(), snapshot)
        decision = validate_trade_decision(raw, snapshot)
        sections: list[str] = []
        for item in decision["decisions"]:
            token = item["symbol"].removesuffix("USDT")
            if token not in selected_tokens:
                continue
            candidate = selected_candidate(item, snapshot)
            if not candidate:
                continue
            symbol = item["symbol"]
            try:
                plan = build_trade_plan(
                    candidate,
                    rules=bybit.get_instrument_rules(symbol),
                    ticker=cycle["tickers"][symbol],
                    equity_usd=cycle["account"]["equity_usd"],
                    available_usd=cycle["account"]["available_usd"],
                    current_portfolio_risk_usd=cycle["portfolio_risk"],
                    taker_fee_rate=fees.get(symbol, D(FALLBACK_TAKER_FEE_RATE)),
                )
            except Exception as error:
                logger.info(f"{symbol}: рекомендация не прошла execution gate: {error}")
                continue
            sections.append(
                f"{'📈' if plan.side == 'Buy' else '📉'} "
                f"<b>{symbol} · {plan.side.upper()}</b>\n"
                f"• Вход ≈ <code>{format_price(plan.entry_price)}</code>\n"
                f"• TP <code>{format_price(plan.take_profit)}</code> · "
                f"SL <code>{format_price(plan.stop_loss)}</code>\n"
                f"• Объём <code>{plan.quantity}</code> · "
                f"плечо <code>{plan.leverage}x</code>\n"
                f"• Риск <code>${plan.risk_usd:.2f}</code> · "
                f"издержки ≈ <code>${plan.estimated_cost_usd:.2f}</code> · "
                f"net R/R <code>{plan.net_risk_reward:.2f}</code>\n"
            )
        text = "🧠 <b>AI-отбор сетапов</b>\n\n"
        text += "\n".join(sections) if sections else "😴 Исполнимых сетапов сейчас нет.\n"
        text += (
            "\n<i>AI выбирает только рассчитанный кодом candidate_id. "
            "Размер, плечо, TP/SL, комиссии и проскальзывание считает risk engine. "
            "Этот экран ничего не исполняет.</i>"
        )
        return text, get_main_menu()
    finally:
        if deepseek is not None:
            deepseek.close()
        if bybit is not None:
            bybit.close()


def _market_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="↻ Обновить",
                    callback_data="menu:market_analysis",
                ),
                InlineKeyboardButton(
                    text="◀️ Меню",
                    callback_data="menu:main",
                ),
            ]
        ]
    )


def build_market_view():
    bybit = BybitAPI()
    try:
        snapshot = collect_market_report(bybit, TRADABLE_TOKENS)
    finally:
        bybit.close()
    fallback_text = format_market_report_text(snapshot)
    markup = _market_menu()
    if snapshot.valid_asset_count < 2:
        return fallback_text, markup
    try:
        png = render_market_report_png(snapshot)
        rich_html = format_market_report_rich_html(snapshot)
    except Exception as error:
        logger.warning(
            "Rich-монитор активов недоступен; используется текстовый fallback "
            f"({type(error).__name__})"
        )
        return fallback_text, markup
    return (
        RichPhotoScreen(
            html=rich_html,
            photo=png,
            fallback_text=fallback_text,
            filename="asset-monitor.png",
            media_id=MARKET_REPORT_MEDIA_ID,
        ),
        markup,
    )


@router.callback_query(F.data == "menu:open_trade")
async def callback_open_trade(callback: CallbackQuery):
    await callback.answer()
    await render_callback_screen(
        callback.message,
        "🧠 <b>AI-отбор сетапов</b>\n\n"
        "Код строит допустимые сетапы по закрытым свечам. AI может только выбрать "
        "один из них или отказаться. Ордер с этого экрана не отправляется.",
        get_trading_menu(),
    )


@router.callback_query(F.data == "trade:ai_suggest")
async def callback_ai_suggestion(callback: CallbackQuery):
    chat_id = callback.message.chat.id
    running = _ai_tasks.get(chat_id)
    if running and not running.done():
        await callback.answer("AI-отбор уже выполняется", show_alert=True)
        return
    loading = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="◀️ Назад", callback_data="menu:open_trade")]
        ]
    )
    await callback.answer("Запускаю безопасный AI-отбор")
    canonical = await render_callback_screen(
        callback.message,
        "🧠 <b>AI-отбор</b>\n\n⏳ Собираю закрытые свечи, комиссии и риск…",
        loading,
    )
    token = current_screen_token(canonical)

    async def run_analysis() -> None:
        try:
            text, markup = await asyncio.to_thread(build_ai_recommendations)
        except asyncio.CancelledError:
            raise
        except Exception as error:
            logger.error(f"Ошибка AI-анализа: {error}")
            await render_if_current(
                token,
                canonical,
                f"❌ <b>AI-анализ не выполнен</b>\n\n"
                f"<code>{html.escape(str(error)[:300])}</code>",
                get_trading_menu(),
            )
            return
        finally:
            if _ai_tasks.get(chat_id) is asyncio.current_task():
                _ai_tasks.pop(chat_id, None)
        await render_if_current(token, canonical, text, markup)

    _ai_tasks[chat_id] = asyncio.create_task(
        run_analysis(),
        name=f"ai-selector-{chat_id}",
    )


@router.callback_query(F.data == "menu:market_analysis")
async def callback_market_analysis(callback: CallbackQuery):
    await callback.answer("Открываю монитор активов")
    canonical = await render_callback_screen(
        callback.message,
        "📊 <b>Монитор активов</b>\n\n"
        "⏳ Загружаю цены и историю RSI по закрытым свечам…",
        _market_menu(),
    )

    async def loader():
        return await asyncio.to_thread(build_market_view)

    await render_live_screen(canonical, loader, interval_seconds=60)
