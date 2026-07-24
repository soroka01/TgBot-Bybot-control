"""Position screens and close actions for the single-message Telegram UI."""

from __future__ import annotations

import asyncio
import html
import sys
from pathlib import Path

from aiogram import F, Router
from aiogram.types import CallbackQuery

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from api.bybit_api import BybitAPI
from config import DRY_RUN
from core.auto_trading import execution_lock
from storage.database import get_store
from telegram_bot.keyboards.main_menu import get_main_menu
from telegram_bot.keyboards.positions_menu import (
    get_close_all_confirmation_menu,
    get_close_confirmation_menu,
    get_position_actions_menu,
    get_positions_list_menu,
)
from telegram_bot.ui import callback_action, render_callback_screen, render_live_screen
from utils.helpers import calculate_position_roi, format_price, to_float
from utils.logger_setup import logger

router = Router()


def _open_positions(positions: list[dict]) -> list[dict]:
    return [position for position in positions if to_float(position.get("size")) > 0]


def build_positions_view():
    """Load the position list. Executed in a worker thread by the live screen."""
    bybit = BybitAPI()
    try:
        positions = _open_positions(
            bybit.get_positions().get("result", {}).get("list", [])
        )
    finally:
        bybit.close()
    if not positions:
        return (
            "📊 <b>Открытые позиции</b> <i>• обновление 8с</i>\n\n"
            "Позиции отсутствуют.",
            get_main_menu(),
        )

    position_data = [
        {
            "symbol": position.get("symbol", ""),
            "side": position.get("side", ""),
            "position_idx": int(to_float(position.get("positionIdx"))),
            "unrealized_pnl": to_float(position.get("unrealisedPnl")),
        }
        for position in positions
    ]
    total_pnl = sum(position["unrealized_pnl"] for position in position_data)
    pnl_emoji = "💚" if total_pnl >= 0 else "❤️"
    text = (
        f"📊 <b>Открытые позиции ({len(position_data)})</b> <i>• обновление 8с</i>\n\n"
        f"{pnl_emoji} <b>Общий PnL:</b> <code>${total_pnl:+.2f}</code>\n\n"
        "Выберите позицию для деталей:"
    )
    return text, get_positions_list_menu(position_data)


def build_position_details_view(symbol: str, position_idx: int):
    """Load one precise position, including its hedge-mode index."""
    bybit = BybitAPI()
    try:
        positions = _open_positions(
            bybit.get_positions(symbol=symbol).get("result", {}).get("list", [])
        )
        position = next(
            (
                candidate
                for candidate in positions
                if int(to_float(candidate.get("positionIdx"))) == position_idx
            ),
            None,
        )
        if not position:
            raise ValueError("Позиция уже закрыта или не найдена")
        ticker = bybit.get_tickers(symbol)["result"]["list"][0]
    finally:
        bybit.close()
    quantity = to_float(position.get("size"))
    side = position.get("side", "")
    entry_price = to_float(position.get("avgPrice", position.get("entryPrice")))
    current_price = to_float(ticker.get("lastPrice"))
    leverage = to_float(position.get("leverage"), 1)
    unrealized_pnl = to_float(position.get("unrealisedPnl"))
    roi = calculate_position_roi(unrealized_pnl, quantity, entry_price, leverage)
    take_profit = to_float(position.get("takeProfit"))
    stop_loss = to_float(position.get("stopLoss"))
    liquidation_price = to_float(position.get("liqPrice"))
    side_emoji = "🟢" if side == "Buy" else "🔴"
    pnl_emoji = "💚" if unrealized_pnl >= 0 else "❤️"

    text = (
        f"{side_emoji} <b>{symbol} — {side}</b> <i>• обновление 8с</i>\n\n"
        f"💰 <b>Размер:</b> <code>{quantity:g}</code>\n"
        f"📊 <b>Вход:</b> <code>{format_price(entry_price)}</code>\n"
        f"💵 <b>Текущая цена:</b> <code>{format_price(current_price)}</code>\n"
        f"💼 <b>Номинал:</b> <code>${quantity * current_price:,.2f}</code>\n"
        f"⚡ <b>Плечо:</b> <code>{leverage:g}x</code>\n\n"
        f"{pnl_emoji} <b>PnL:</b> <code>${unrealized_pnl:+.2f}</code> | "
        f"<b>ROI:</b> <code>{roi:+.2f}%</code>\n"
    )
    if take_profit:
        text += f"🎯 <b>Take Profit:</b> <code>{format_price(take_profit)}</code>\n"
    if stop_loss:
        text += f"🛑 <b>Stop Loss:</b> <code>{format_price(stop_loss)}</code>\n"
    if liquidation_price:
        text += f"⚠️ <b>Ликвидация:</b> <code>{format_price(liquidation_price)}</code>\n"
    return text, get_position_actions_menu(symbol, position_idx)


def close_position(symbol: str, position_idx: int) -> str:
    """Close one selected position and require an explicit exchange confirmation."""
    bybit = BybitAPI()
    try:
        with execution_lock():
            positions = _open_positions(
                bybit.get_positions(symbol=symbol).get("result", {}).get("list", [])
            )
            position = next(
                (
                    candidate
                    for candidate in positions
                    if int(to_float(candidate.get("positionIdx"))) == position_idx
                ),
                None,
            )
            if not position:
                raise ValueError("Позиция уже закрыта или не найдена")
            side = position.get("side", "")
            close_side = "Sell" if side == "Buy" else "Buy"
            bybit.close_position_market(
                symbol,
                close_side,
                position_idx,
                order_link_id=bybit.new_order_link_id("manual-close"),
            )
            return side
    finally:
        bybit.close()


def close_all_positions() -> tuple[int, list[str]]:
    """Close every open position, keeping errors per symbol for the single screen."""
    bybit = BybitAPI()
    try:
        with execution_lock():
            positions = _open_positions(
                bybit.get_positions().get("result", {}).get("list", [])
            )
            closed_count = 0
            errors: list[str] = []
            for position in positions:
                symbol = position.get("symbol", "?")
                side = position.get("side", "")
                position_idx = int(to_float(position.get("positionIdx")))
                try:
                    bybit.close_position_market(
                        symbol,
                        "Sell" if side == "Buy" else "Buy",
                        position_idx,
                        order_link_id=bybit.new_order_link_id("manual-all"),
                    )
                    closed_count += 1
                except Exception as error:
                    errors.append(f"{symbol}: {error}")
            return closed_count, errors
    finally:
        bybit.close()


@router.callback_query(F.data == "menu:positions")
async def callback_positions(callback: CallbackQuery):
    await callback.answer("Обновляю позиции...")

    async def load_positions():
        return await asyncio.to_thread(build_positions_view)

    await render_live_screen(callback.message, load_positions, interval_seconds=8)


@router.callback_query(F.data == "positions:refresh")
async def callback_positions_refresh(callback: CallbackQuery):
    await callback_positions(callback)


@router.callback_query(F.data.startswith("pos:details:"))
async def callback_position_details(callback: CallbackQuery):
    try:
        _, _, symbol, index = callback.data.split(":", 3)
        position_idx = int(index)
    except (AttributeError, ValueError):
        await callback.answer("Экран устарел")
        await render_callback_screen(
            callback.message,
            "🔄 <b>Карточка позиции устарела.</b>\n\nОткройте позицию из актуального списка.",
            get_main_menu(),
        )
        return
    await callback.answer("Обновляю позицию...")

    async def load_details():
        return await asyncio.to_thread(build_position_details_view, symbol, position_idx)

    await render_live_screen(callback.message, load_details, interval_seconds=8)


@router.callback_query(F.data.startswith("pos:close:"))
async def callback_position_close(callback: CallbackQuery):
    try:
        _, _, symbol, index = callback.data.split(":", 3)
        position_idx = int(index)
    except (AttributeError, ValueError):
        await callback.answer("Экран устарел")
        await render_callback_screen(callback.message, "🔄 <b>Экран устарел.</b>", get_main_menu())
        return
    warning = "🧪 <b>DRY RUN:</b> запрос будет только сымитирован." if DRY_RUN else "⚠️ Это действие нельзя отменить."
    await callback.answer()
    await render_callback_screen(
        callback.message,
        f"❓ <b>Закрыть {symbol} (idx {position_idx})?</b>\n\n{warning}",
        get_close_confirmation_menu(symbol, position_idx),
    )


@router.callback_query(F.data.startswith("pos:close_confirm:"))
async def callback_position_close_confirm(callback: CallbackQuery):
    try:
        _, _, symbol, index = callback_action(callback.data).split(":", 3)
        position_idx = int(index)
    except (AttributeError, ValueError):
        await callback.answer("Экран устарел")
        await render_callback_screen(callback.message, "🔄 <b>Экран устарел.</b>", get_main_menu())
        return
    await callback.answer("Отправляю запрос на закрытие...")
    try:
        side = await asyncio.to_thread(close_position, symbol, position_idx)
        result = (
            f"🧪 <b>DRY RUN:</b> {symbol} {side} не отправлялась на биржу."
            if DRY_RUN
            else f"✅ <b>{symbol} {side} закрыта; исполнение подтверждено.</b>"
        )
        await asyncio.to_thread(
            get_store().log_activity,
            callback.message.chat.id,
            "position_close_requested",
            f"Запрошено закрытие {symbol} ({side})",
            symbol=symbol,
        )
        await render_callback_screen(callback.message, result, get_main_menu())
    except Exception as error:
        logger.error(f"Ошибка закрытия {symbol}: {error}")
        await render_callback_screen(
            callback.message,
            f"❌ <b>Не удалось закрыть {symbol}</b>\n\n<code>{html.escape(str(error)[:300])}</code>",
            get_main_menu(),
        )


@router.callback_query(F.data == "positions:close_all")
async def callback_close_all_positions(callback: CallbackQuery):
    warning = "🧪 <b>DRY RUN:</b> запросы будут только сымитированы." if DRY_RUN else "⚠️ Это действие нельзя отменить."
    await callback.answer()
    await render_callback_screen(
        callback.message,
        f"❓ <b>Закрыть все позиции?</b>\n\n{warning}",
        get_close_all_confirmation_menu(),
    )


@router.callback_query(F.data.startswith("positions:close_all_confirm:rev:"))
async def callback_close_all_confirm(callback: CallbackQuery):
    await callback.answer("Отправляю запросы на закрытие...")
    try:
        closed_count, errors = await asyncio.to_thread(close_all_positions)
        title = "🧪 <b>Рассчитано закрытий:</b>" if DRY_RUN else "✅ <b>Подтверждено закрытий:</b>"
        text = f"{title} <code>{closed_count}</code>"
        if errors:
            text += "\n\n❌ <b>Ошибки:</b>\n" + "\n".join(
                f"• <code>{html.escape(error[:180])}</code>" for error in errors[:8]
            )
        await asyncio.to_thread(
            get_store().log_activity,
            callback.message.chat.id,
            "close_all_requested",
            f"Запрошено закрытие позиций: {closed_count}",
            severity="warning" if errors else "info",
        )
        await render_callback_screen(callback.message, text, get_main_menu())
    except Exception as error:
        logger.error(f"Ошибка закрытия всех позиций: {error}")
        await render_callback_screen(
            callback.message,
            f"❌ <b>Не удалось закрыть позиции</b>\n\n<code>{html.escape(str(error)[:300])}</code>",
            get_main_menu(),
        )
