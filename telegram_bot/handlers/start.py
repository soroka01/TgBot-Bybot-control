# telegram_bot/handlers/start.py
"""Обработчики команд /start и главного меню"""

from aiogram import Router, F
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
async def cmd_start(message: Message):
    """Обработчик команды /start"""
    welcome_text = (
        "🤖 <b>Crypto Trading Bot</b>\n\n"
        "Добро пожаловать! Я помогу вам управлять торговлей на Bybit.\n\n"
        "🎯 <b>Что я умею:</b>\n"
        "• Показывать текущие позиции\n"
        "• Открывать/закрывать сделки\n"
        "• Анализировать рынок с помощью AI\n"
        "• Управлять рисками и настройками\n"
        "• Автоматическая торговля\n\n"
        "Выберите действие:"
    )

    await render_command_screen(message, welcome_text, get_main_menu())


@router.message(Command("menu"))
async def cmd_menu(message: Message):
    """Обработчик команды /menu"""
    await render_command_screen(message, "📊 <b>Главное меню</b>", get_main_menu())


@router.callback_query(F.data == "menu:main")
async def callback_main_menu(callback: CallbackQuery):
    """Возврат в главное меню"""
    await callback.answer()  # Сразу отвечаем на callback

    await render_callback_screen(callback.message, "📊 <b>Главное меню</b>", get_main_menu())


def build_balance_view():
    """Load the current Unified Account balance for the live dashboard."""
    overview = parse_account_overview(BybitAPI().get_wallet_balance())
    return (
        "💰 <b>Баланс аккаунта</b> <i>• обновление 2с</i>\n\n"
        f"💵 <b>Wallet Balance:</b> <code>${overview['balance_usd']:.2f}</code>\n"
        f"📊 <b>Equity:</b> <code>${overview['equity_usd']:.2f}</code>\n"
        f"📈 <b>Unrealized PnL:</b> <code>${overview['unrealized_pnl_usd']:+.2f}</code>\n"
        f"🔒 <b>Маржа позиций:</b> <code>${overview['position_margin_usd']:.2f}</code>\n"
        f"📋 <b>Маржа ордеров:</b> <code>${overview['order_margin_usd']:.2f}</code>\n"
        f"✅ <b>Доступно:</b> <code>${overview['available_usd']:.2f}</code>",
        get_main_menu(),
    )


@router.callback_query(F.data == "menu:balance")
async def callback_balance(callback: CallbackQuery):
    """Показать баланс аккаунта"""
    await callback.answer("Обновляю баланс...")

    async def load_balance():
        import asyncio
        return await asyncio.to_thread(build_balance_view)

    await render_live_screen(callback.message, load_balance)


@router.callback_query(F.data == "menu:settings")
async def callback_settings(callback: CallbackQuery):
    """Показать меню настроек"""
    await callback.answer()  # Сразу отвечаем на callback

    from config import (
        MAX_LEVERAGE, MAX_RISK_PER_TRADE_PERCENT,
        MIN_ORDER_SIZE_USDT, TRADABLE_TOKENS, DRY_RUN
    )

    settings_text = (
        f"⚙️ <b>Текущие настройки</b>\n\n"
        f"⚡ <b>Макс. плечо:</b> <code>{MAX_LEVERAGE}x</code>\n"
        f"🎚️ <b>Риск на сделку:</b> <code>{MAX_RISK_PER_TRADE_PERCENT}%</code>\n"
        f"💵 <b>Мин. размер ордера:</b> <code>${MIN_ORDER_SIZE_USDT}</code>\n"
        f"🪙 <b>Токены:</b> <code>{', '.join(TRADABLE_TOKENS)}</code>\n"
        f"🔧 <b>Режим:</b> <code>{'DRY RUN' if DRY_RUN else 'LIVE'}</code>\n"
        "🔔 <b>События авто-режима:</b> обновляют текущий экран\n"
        "\n<i>Настройки доступны только для просмотра в боте.</i>"
    )

    await render_callback_screen(callback.message, settings_text, get_settings_menu())
