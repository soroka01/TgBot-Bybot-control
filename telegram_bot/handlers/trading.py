"""Read-only AI recommendations and the live market overview."""

from __future__ import annotations

import asyncio
import html
import sys
from pathlib import Path

from aiogram import F, Router
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from api.bybit_api import BybitAPI
from api.deepseek_api import DeepSeekAPI
from core.market_data import enrich_context_with_market_data, get_market_analysis
from core.prompt_builder import build_deepseek_prompt
from telegram_bot.keyboards.main_menu import get_main_menu
from telegram_bot.keyboards.trading_menu import get_trading_menu
from telegram_bot.ui import (
    current_screen_token,
    is_current_screen,
    render_callback_screen,
    render_live_screen,
)
from utils.helpers import (
    build_context,
    format_price,
    parse_account_overview,
    to_float,
    validate_deepseek_json,
)
from utils.logger_setup import logger

router = Router()


def build_ai_recommendations() -> tuple[str, InlineKeyboardMarkup]:
    """Collect data and ask DeepSeek in a worker thread, without placing orders."""
    from config import TRADABLE_TOKENS

    selected_tokens = TRADABLE_TOKENS[:3]
    bybit = BybitAPI()
    positions = bybit.get_positions().get("result", {}).get("list", [])
    tickers: dict[str, dict] = {}
    for token in selected_tokens:
        symbol = f"{token}USDT"
        try:
            ticker = bybit.get_tickers(symbol)
            tickers[symbol] = {
                "lastPrice": to_float(ticker["result"]["list"][0]["lastPrice"]),
                "raw": ticker,
            }
        except Exception as error:
            logger.warning(f"Не удалось получить цену {symbol}: {error}")

    if not tickers:
        raise ValueError("Не удалось получить цены выбранных токенов")

    available_tokens = [token for token in selected_tokens if f"{token}USDT" in tickers]

    account = parse_account_overview(bybit.get_wallet_balance())
    context = build_context(positions, tickers, account)
    context = enrich_context_with_market_data(bybit, context, available_tokens)
    raw = DeepSeekAPI().analyze(build_deepseek_prompt(available_tokens), context, temperature=0.0)
    decision = validate_deepseek_json(raw, expected_tokens=available_tokens)

    sections: list[str] = []
    for token, payload in decision.items():
        args = payload["trade_signal_args"]
        signal = args["signal"]
        if signal not in {"long", "short"}:
            continue
        price = to_float(tickers.get(f"{token}USDT", {}).get("lastPrice"))
        quantity = to_float(args["quantity"])
        take_profit = to_float(args["profit_target"])
        stop_loss = to_float(args["stop_loss"])
        risk = abs(price - stop_loss) * quantity
        reward = abs(take_profit - price) * quantity
        risk_reward = reward / risk if risk else 0.0
        sections.append(
            f"{'📈' if signal == 'long' else '📉'} <b>{token} {signal.upper()}</b>\n"
            f"• Уверенность: <code>{to_float(args['confidence']):.0%}</code>\n"
            f"• TP: <code>{format_price(take_profit)}</code> | SL: <code>{format_price(stop_loss)}</code>\n"
            f"• Объём: <code>{quantity:g}</code> | Плечо: <code>{args['leverage']}x</code>\n"
            f"• Риск до SL: <code>${risk:.2f}</code> | R/R: <code>{risk_reward:.2f}</code>\n"
        )

    text = "🧠 <b>AI-рекомендации</b>\n\n"
    text += "\n".join(sections) if sections else "😴 Нет сильных сигналов для входа.\n"
    text += "\nℹ️ Это анализ, не ордер. Исполнение доступно только через авто-режим."
    return text, get_main_menu()


def build_market_view() -> tuple[str, InlineKeyboardMarkup]:
    """Build a fast live view; indicators are cached in market_data."""
    from config import TRADABLE_TOKENS

    bybit = BybitAPI()
    sections: list[str] = []
    for token in TRADABLE_TOKENS[:5]:
        symbol = f"{token}USDT"
        try:
            ticker = bybit.get_tickers(symbol)["result"]["list"][0]
            current_price = to_float(ticker.get("lastPrice"))
            previous_price = to_float(ticker.get("prevPrice24h"), current_price)
            high = to_float(ticker.get("highPrice24h"))
            low = to_float(ticker.get("lowPrice24h"))
            price_change = current_price - previous_price
            price_change_percent = price_change / previous_price * 100 if previous_price else 0.0
            analysis = get_market_analysis(bybit, symbol, current_price)
            rsi = analysis.get("timeframe_4h", {}).get("rsi14")
            rsi_display = f"{to_float(rsi):.1f}" if rsi is not None else "—"
            sections.append(
                f"🪙 <b>{token}</b>: <code>{format_price(current_price)}</code> "
                f"{'📈' if price_change >= 0 else '📉'} <code>{price_change_percent:+.2f}%</code>\n"
                f"   24ч: <code>{format_price(low)}</code> – <code>{format_price(high)}</code> | "
                f"RSI 4ч: <code>{rsi_display}</code>\n"
            )
        except Exception as error:
            logger.warning(f"Не удалось обновить обзор {token}: {error}")

    text = "🔍 <b>Рынок</b> <i>• обновление цен 2с</i>\n\n"
    text += "\n".join(sections) if sections else "⚠️ Данные рынка временно недоступны."
    return text, get_main_menu()


@router.callback_query(F.data == "menu:open_trade")
async def callback_open_trade(callback: CallbackQuery):
    await callback.answer()
    await render_callback_screen(
        callback.message,
        "📈 <b>AI-рекомендации</b>\n\n"
        "🤖 Анализ включает направление, TP/SL, объём, риск и R/R.\n"
        "ℹ️ Этот экран не открывает сделки. Перед запуском авто-режима проверьте уровни.",
        get_trading_menu(),
    )


@router.callback_query(F.data == "trade:ai_suggest")
async def callback_ai_suggestion(callback: CallbackQuery):
    loading_keyboard = InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="❌ Отмена", callback_data="menu:open_trade")]]
    )
    await callback.answer("Запускаю AI-анализ...")
    await render_callback_screen(
        callback.message,
        "🧠 <b>AI-анализ</b>\n\n⏳ Получаю рынок и формирую рекомендацию…\n"
        "Обычно это занимает 10–30 секунд. Кнопка «Отмена» сразу вернёт в меню.",
        loading_keyboard,
    )
    token = current_screen_token(callback.message)
    try:
        text, markup = await asyncio.to_thread(build_ai_recommendations)
    except Exception as error:
        if not is_current_screen(token):
            return
        logger.error(f"Ошибка AI-анализа: {error}")
        await render_callback_screen(
            callback.message,
            f"❌ <b>AI-анализ не выполнен</b>\n\n<code>{html.escape(str(error)[:300])}</code>",
            get_trading_menu(),
        )
        return

    if is_current_screen(token):
        await render_callback_screen(callback.message, text, markup)


@router.callback_query(F.data == "menu:market_analysis")
async def callback_market_analysis(callback: CallbackQuery):
    await callback.answer("Открываю рынок...")
    await render_callback_screen(
        callback.message,
        "🔍 <b>Рынок</b>\n\n⏳ Загружаю цены и индикаторы…",
        get_main_menu(),
    )

    async def load_market():
        return await asyncio.to_thread(build_market_view)

    await render_live_screen(callback.message, load_market)
