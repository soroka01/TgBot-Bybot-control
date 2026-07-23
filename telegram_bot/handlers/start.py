# telegram_bot/handlers/start.py
"""Обработчики команд /start и главного меню"""

from aiogram import Router, F
from aiogram.fsm.context import FSMContext
from aiogram.types import Message, CallbackQuery
from aiogram.filters import Command
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from telegram_bot.keyboards.main_menu import get_main_menu, get_settings_menu
from api.bybit_api import BybitAPI
from utils.helpers import parse_account_overview
from telegram_bot.ui import render_callback_screen, render_command_screen, render_live_screen

router = Router()


@router.message(Command("start"))
async def cmd_start(message: Message, state: FSMContext):
    """Обработчик команды /start"""
    await state.clear()
    welcome_text = (
        "🤖 <b>Crypto Trading Bot</b>\n\n"
        "Единый экран для Bybit: позиции, риск, безопасный AI-отбор "
        "и график обновляются редактированием этого сообщения.\n\n"
        "🛡 AI не задаёт объём и плечо: исполнение рассчитывает код.\n"
        "📊 Для графика используются только закрытые свечи.\n"
        "🧪 Перед LIVE сначала используйте DRY или Bybit Demo.\n\n"
        "Выберите раздел:"
    )

    await render_command_screen(message, welcome_text, get_main_menu())


@router.message(Command("menu"))
async def cmd_menu(message: Message, state: FSMContext):
    """Обработчик команды /menu"""
    await state.clear()
    await render_command_screen(message, "📊 <b>Главное меню</b>", get_main_menu())


@router.callback_query(F.data == "menu:main")
async def callback_main_menu(callback: CallbackQuery, state: FSMContext):
    """Возврат в главное меню"""
    await state.clear()
    await callback.answer()  # Сразу отвечаем на callback

    await render_callback_screen(callback.message, "📊 <b>Главное меню</b>", get_main_menu())


def build_balance_view():
    """Load the current Unified Account balance for the live dashboard."""
    bybit = BybitAPI()
    try:
        overview = parse_account_overview(bybit.get_wallet_balance())
        return (
            "💰 <b>Баланс аккаунта</b> <i>• обновление 10с</i>\n\n"
            f"💵 <b>Wallet Balance:</b> <code>${overview['balance_usd']:.2f}</code>\n"
            f"📊 <b>Equity:</b> <code>${overview['equity_usd']:.2f}</code>\n"
            f"📈 <b>Unrealized PnL:</b> <code>${overview['unrealized_pnl_usd']:+.2f}</code>\n"
            f"🔒 <b>Маржа позиций:</b> <code>${overview['position_margin_usd']:.2f}</code>\n"
            f"📋 <b>Маржа ордеров:</b> <code>${overview['order_margin_usd']:.2f}</code>\n"
            f"✅ <b>Доступно:</b> <code>${overview['available_usd']:.2f}</code>",
            get_main_menu(),
        )
    finally:
        bybit.close()


@router.callback_query(F.data == "menu:balance")
async def callback_balance(callback: CallbackQuery):
    """Показать баланс аккаунта"""
    await callback.answer("Обновляю баланс...")

    async def load_balance():
        import asyncio
        return await asyncio.to_thread(build_balance_view)

    await render_live_screen(callback.message, load_balance, interval_seconds=10)


@router.callback_query(F.data == "menu:settings")
async def callback_settings(callback: CallbackQuery):
    """Показать меню настроек"""
    await callback.answer()  # Сразу отвечаем на callback

    from config import (
        AUTO_LEVERAGE, BYBIT_ENV, DEEPSEEK_MODEL, MAX_RISK_PER_TRADE_PERCENT,
        MAX_TOTAL_RISK_PERCENT, MIN_NET_RISK_REWARD_RATIO,
        TRADABLE_TOKENS, DRY_RUN
    )

    settings_text = (
        f"⚙️ <b>Текущие настройки</b>\n\n"
        f"⚡ <b>Плечо auto:</b> <code>минимальное, до {AUTO_LEVERAGE}x</code>\n"
        f"🎚️ <b>Риск на сделку:</b> <code>{MAX_RISK_PER_TRADE_PERCENT}%</code>\n"
        f"🧱 <b>Общий риск:</b> <code>{MAX_TOTAL_RISK_PERCENT}%</code>\n"
        f"⚖️ <b>Минимальный net R/R:</b> <code>{MIN_NET_RISK_REWARD_RATIO}</code>\n"
        f"🪙 <b>Токены:</b> <code>{', '.join(TRADABLE_TOKENS)}</code>\n"
        f"🔧 <b>Режим:</b> <code>{'DRY PREVIEW' if DRY_RUN else 'LIVE'} · {BYBIT_ENV}</code>\n"
        f"🧠 <b>AI:</b> <code>{DEEPSEEK_MODEL}</code>\n"
        "🔔 <b>Auto-события:</b> только экранам владельцев\n"
        "\n<i>Настройки доступны только для просмотра в боте.</i>"
    )

    await render_callback_screen(callback.message, settings_text, get_settings_menu())
