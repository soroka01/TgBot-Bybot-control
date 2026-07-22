# core/auto_trading.py
"""
Автоматический торговый бот для Bybit с использованием DeepSeek AI
"""
import time
import traceback
import sys
from pathlib import Path

# Добавляем корневую директорию в путь
sys.path.insert(0, str(Path(__file__).parent.parent))

from utils.logger_setup import logger
from config import (
    POLL_INTERVAL, TRADABLE_TOKENS, DRY_RUN, MAX_LEVERAGE
)
from api.bybit_api import BybitAPI, BybitAPIError
from api.deepseek_api import DeepSeekAPI
from api.tg_notify import notify
from core.market_data import enrich_context_with_market_data
from utils.helpers import (
    build_context, validate_deepseek_json,
    validate_trade_risk, validate_sl_vs_liquidation,
    calculate_position_risk, calculate_position_roi,
    find_unprotected_positions, parse_account_overview,
    round_quantity, to_float
)
from core.prompt_builder import build_deepseek_prompt, get_prompt_summary

def symbol_to_pair(token: str) -> str:
    """Конвертирует токен в пару для Bybit"""
    return f"{token}USDT"

def execute_decision(bybit: BybitAPI, decision: dict, account_info: dict):
    """
    Выполняет торговые решения от DeepSeek на Bybit
    """
    # Вычисляем текущий риск портфеля
    positions_data = bybit.get_positions()
    positions_list = positions_data.get("result", {}).get("list", [])

    open_positions = [p for p in positions_list if to_float(p.get("size")) > 0]
    current_total_risk = calculate_position_risk(open_positions)
    unprotected_positions = find_unprotected_positions(open_positions)
    available_balance = to_float(account_info.get("available_usd"))
    risk_budget = to_float(account_info.get("equity_usd"), available_balance)

    logger.info(
        f"Текущий риск портфеля до SL: ${current_total_risk:.2f}, "
        f"доступно для маржи: ${available_balance:.2f}, equity: ${risk_budget:.2f}"
    )
    if unprotected_positions:
        logger.warning(
            f"Есть позиции без Stop Loss: {', '.join(unprotected_positions)}. "
            "Новые входы будут заблокированы до их защиты."
        )
        notify("⚠️ Новые сделки пропущены: у открытых позиций нет защитного Stop Loss")

    for coin, payload in decision.items():
        args = payload.get("trade_signal_args", {})
        sig = args.get("signal")
        pair = symbol_to_pair(coin)

        try:
            if sig == "hold":
                handle_hold_signal(bybit, coin, pair, args, positions_list)

            elif sig == "close":
                handle_close_signal(bybit, coin, pair, args, positions_list)

            elif sig in ("long", "short"):
                if unprotected_positions:
                    logger.warning(f"[{coin}] {sig.upper()} пропущен: портфель содержит позицию без SL")
                    continue
                reservation = handle_open_signal(
                    bybit, coin, pair, sig, args, available_balance,
                    current_total_risk, risk_budget
                )
                if reservation:
                    current_total_risk += reservation["risk_usd"]
                    available_balance = max(
                        0.0, available_balance - reservation["margin_with_buffer"]
                    )

            else:
                logger.warning(f"[{coin}] Неизвестный сигнал: {sig}")
                notify(f"[{coin}] ⚠️ Неизвестный сигнал: {sig}")

        except BybitAPIError as e:
            logger.error(f"[{coin}] Ошибка Bybit API: {e}")
            notify(f"[{coin}] ❌ Ошибка API: {e}")
        except Exception as e:
            logger.error(f"[{coin}] Ошибка при исполнении: {e}")
            logger.debug(traceback.format_exc())
            notify(f"[{coin}] ❌ Ошибка: {e}")


def handle_hold_signal(bybit: BybitAPI, coin: str, pair: str, args: dict, positions_list: list):
    """Обработка сигнала HOLD - удержание позиции с обновлением TP/SL"""
    matching_positions = [
        p for p in positions_list if p.get("symbol") == pair and to_float(p.get("size")) > 0
    ]
    if len(matching_positions) > 1:
        logger.warning(f"[{coin}] HOLD пропущен: по {pair} открыты обе стороны hedge-позиции")
        notify(f"[{coin}] ⚠️ HOLD пропущен: одновременно открыты LONG и SHORT")
        return
    current_pos = matching_positions[0] if matching_positions else None

    if not current_pos or float(current_pos.get("size", 0)) == 0:
        logger.info(f"[{coin}] HOLD: позиции нет — пропуск")
        return

    qty = to_float(current_pos.get("size"))
    entry_price = to_float(current_pos.get("entryPrice"))
    side = current_pos.get("side", "")
    leverage = int(current_pos.get("leverage", 1))

    # Определяем position_idx на основе positionIdx из Bybit
    # 0 = one-way mode, 1 = hedge Buy side, 2 = hedge Sell side
    position_idx = int(current_pos.get("positionIdx", 0))

    # Получаем текущую цену
    ticker_data = bybit.get_tickers(pair)
    last_price = float(ticker_data["result"]["list"][0]["lastPrice"])

    # TP/SL из DeepSeek
    tp = to_float(args.get("profit_target")) or None
    sl = to_float(args.get("stop_loss")) or None

    # Never guess by swapping invalid AI values: that can silently set an
    # unintended protective order. Reject the malformed update instead.
    if side == "Buy" and ((tp is not None and tp <= last_price) or (sl is not None and sl >= last_price)):
        logger.error(f"[{coin}] Некорректный TP/SL для LONG: TP={tp}, SL={sl}, цена={last_price}")
        return
    if side == "Sell" and ((tp is not None and tp >= last_price) or (sl is not None and sl <= last_price)):
        logger.error(f"[{coin}] Некорректный TP/SL для SHORT: TP={tp}, SL={sl}, цена={last_price}")
        return

    # Валидация SL относительно ликвидации (строгая проверка)
    liq_price = float(current_pos.get("liqPrice", 0) or 0)
    if liq_price > 0 and sl:
        is_valid, error_msg = validate_sl_vs_liquidation(
            side=side,
            stop_loss=sl,
            liquidation_price=liq_price,
            min_distance_percent=5.0  # Минимум 5% от ликвидации
        )
        if not is_valid:
            logger.error(f"[{coin}] {error_msg}")
            logger.warning(f"[{coin}] Обновлю только TP, SL пропущен из-за близости к ликвидации")
            notify(f"[{coin}] ⚠️ {error_msg}\n⚠️ Обновлен только TP, SL оставлен прежним!")
            sl = None  # Отключаем обновление SL

    # Рассчитываем текущую прибыль/убыток
    position_value = qty * last_price  # Общая сумма позиции
    margin_used = position_value / leverage  # Чистая маржа из кошелька
    unrealized_pnl = to_float(current_pos.get("unrealisedPnl"))
    unrealized_pnl_percent = calculate_position_roi(unrealized_pnl, qty, entry_price, leverage)

    # Получаем текущие TP/SL из позиции
    current_tp = float(current_pos.get("takeProfit", 0) or 0)
    current_sl = float(current_pos.get("stopLoss", 0) or 0)

    # Проверяем, изменились ли TP/SL
    def price_changed(new_value, old_value):
        return new_value is not None and abs(new_value - old_value) > max(1e-8, abs(old_value) * 1e-6)

    tp_changed = price_changed(tp, current_tp)
    sl_changed = price_changed(sl, current_sl)

    # Если ничего не изменилось, пропускаем
    if not tp_changed and not sl_changed:
        logger.info(f"[{coin}] TP/SL не изменились (TP={current_tp}, SL={current_sl}), пропускаю обновление")
        return

    # Если хотя бы один параметр изменился, продолжаем
    if tp_changed:
        logger.info(f"[{coin}] TP изменился: {current_tp} → {tp}")
    if sl_changed:
        logger.info(f"[{coin}] SL изменился: {current_sl} → {sl}")

    # Потенциальная прибыль/убыток до TP/SL
    if tp and sl:
        if side == "Buy":  # LONG
            potential_profit = (tp - last_price) * qty if tp > last_price else 0
            potential_loss = (last_price - sl) * qty if sl < last_price else 0
        else:  # SHORT
            potential_profit = (last_price - tp) * qty if tp < last_price else 0
            potential_loss = (sl - last_price) * qty if sl > last_price else 0
    else:
        potential_profit = 0
        potential_loss = 0

    # Формируем строку для обновления
    update_info = []
    if tp:
        update_info.append(f"TP={tp}")
    if sl:
        update_info.append(f"SL={sl}")
    update_str = ", ".join(update_info) if update_info else "нет изменений"

    logger.info(f"[{coin}] HOLD: qty={qty}, entry={entry_price}, side={side}, position_idx={position_idx}, обновление: {update_str}")
    logger.info(f"[{coin}] Текущий PnL: ${unrealized_pnl:.2f} ({unrealized_pnl_percent:+.2f}%), Потенциал: +${potential_profit:.2f}/-${potential_loss:.2f}")

    if DRY_RUN:
        logger.info(f"[DRY_RUN] {coin} HOLD: обновление {update_str}")
        tp_line = f"🎯 TP: ${tp} (+${potential_profit:.2f})" if tp else f"🎯 TP: ${current_tp} (без изменений)"
        sl_line = f"🛑 SL: ${sl} (-${potential_loss:.2f})" if sl else f"🛑 SL: ${current_sl} (без изменений)"
        notify(
            f"[{coin}] 📊 HOLD (DRY): {side}\n"
            f"💰 Размер: {qty} @ ${last_price:.2f}\n"
            f"📊 Сумма: ${position_value:.2f} (маржа: ${margin_used:.2f})\n"
            f"⚡ Плечо: {leverage}x\n"
            f"📈 PnL: ${unrealized_pnl:.2f} ({unrealized_pnl_percent:+.2f}%)\n"
            f"{tp_line}\n"
            f"{sl_line}"
        )
    else:
        resp = bybit.set_trading_stop(symbol=pair, position_idx=position_idx, take_profit=tp, stop_loss=sl)
        logger.info(f"[{coin}] Установлены: {resp}")
        tp_line = f"🎯 TP: ${tp} (+${potential_profit:.2f})" if tp else f"🎯 TP: ${current_tp} (без изменений)"
        sl_line = f"🛑 SL: ${sl} (-${potential_loss:.2f})" if sl else f"🛑 SL: ${current_sl} (без изменений)"
        notify(
            f"[{coin}] 📊 HOLD: {side}\n"
            f"💰 Размер: {qty} @ ${last_price:.2f}\n"
            f"📊 Сумма: ${position_value:.2f} (маржа: ${margin_used:.2f})\n"
            f"⚡ Плечо: {leverage}x\n"
            f"📈 PnL: ${unrealized_pnl:.2f} ({unrealized_pnl_percent:+.2f}%)\n"
            f"{tp_line}\n"
            f"{sl_line}"
        )

def handle_close_signal(bybit: BybitAPI, coin: str, pair: str, args: dict, positions_list: list):
    """Обработка сигнала CLOSE - закрытие позиции"""
    matching_positions = [
        p for p in positions_list if p.get("symbol") == pair and to_float(p.get("size")) > 0
    ]
    if len(matching_positions) > 1:
        logger.warning(f"[{coin}] CLOSE пропущен: по {pair} открыта hedge-пара, требуется ручное решение")
        notify(f"[{coin}] ⚠️ CLOSE пропущен: одновременно открыты LONG и SHORT")
        return
    current_pos = matching_positions[0] if matching_positions else None

    if not current_pos or float(current_pos.get("size", 0)) == 0:
        logger.info(f"[{coin}] CLOSE: позиции нет — пропуск")
        return

    side = current_pos.get("side", "")
    qty = float(current_pos.get("size", 0))
    entry_price = to_float(current_pos.get("avgPrice", current_pos.get("entryPrice")))
    unrealized_pnl = to_float(current_pos.get("unrealisedPnl"))
    position_idx = int(current_pos.get("positionIdx", 0))

    # Получаем текущую цену для расчетов
    ticker_data = bybit.get_tickers(pair)
    current_price = float(ticker_data["result"]["list"][0]["lastPrice"])

    position_value = qty * current_price
    unrealized_pnl_percent = calculate_position_roi(
        unrealized_pnl, qty, entry_price, to_float(current_pos.get("leverage"), 1)
    )

    # Определяем сторону для закрытия
    close_side = "Sell" if side == "Buy" else "Buy"

    logger.info(f"[{coin}] CLOSE: закрываем {side} позицию qty={qty}, position_idx={position_idx}, PnL=${unrealized_pnl:.2f}")

    if DRY_RUN:
        logger.info(f"[DRY_RUN] {coin} CLOSE: market order {close_side} qty={qty}")
        notify(
            f"[{coin}] 🔴 CLOSE (DRY): {side}\n"
            f"💰 Закрыто: {qty} @ ${current_price:.2f}\n"
            f"📊 Вход был: ${entry_price:.2f}\n"
            f"💵 Итог: ${unrealized_pnl:+.2f} ({unrealized_pnl_percent:+.2f}%)"
        )
    else:
        resp = bybit.close_position_market(symbol=pair, side=close_side, position_idx=position_idx)
        logger.info(f"[{coin}] Позиция закрыта: {resp}")
        notify(
            f"[{coin}] 🔴 CLOSE: {side}\n"
            f"💰 Закрыто: {qty} @ ${current_price:.2f}\n"
            f"📊 Вход был: ${entry_price:.2f}\n"
            f"💵 Итог: ${unrealized_pnl:+.2f} ({unrealized_pnl_percent:+.2f}%)"
        )

def handle_open_signal(bybit: BybitAPI, coin: str, pair: str, sig: str, args: dict,
                       available_balance: float, current_total_risk: float,
                       risk_budget: float):
    """Обработка сигналов LONG/SHORT - открытие новой позиции"""
    qty = to_float(args.get("quantity"))
    tp = to_float(args.get("profit_target"))
    sl = to_float(args.get("stop_loss"))
    leverage = int(to_float(args.get("leverage"), 1))
    confidence = to_float(args.get("confidence"))

    if qty <= 0:
        logger.info(f"[{coin}] {sig.upper()}: quantity не указан — пропуск")
        return

    # Округляем quantity согласно требованиям биржи
    qty_original = qty
    qty = round_quantity(coin, qty)

    if qty == 0:
        logger.warning(f"[{coin}] {sig.upper()}: quantity {qty_original} слишком мал после округления — пропуск")
        notify(f"[{coin}] ⚠️ {sig.upper()}: размер {qty_original} слишком мал (мин. требования биржи)")
        return

    if qty != qty_original:
        logger.info(f"[{coin}] Количество округлено: {qty_original} → {qty}")

    # Получаем текущую цену
    ticker_data = bybit.get_tickers(pair)
    last_price = float(ticker_data["result"]["list"][0]["lastPrice"])

    side = "Buy" if sig == "long" else "Sell"

    # Валидация маржи, направлений TP/SL, реального риска и R/R.
    is_valid, error_msg = validate_trade_risk(
        quantity=qty,
        price=last_price,
        stop_loss=sl,
        leverage=leverage,
        available_balance=available_balance,
        total_risk_usd=current_total_risk,
        side=side,
        profit_target=tp,
        risk_budget_usd=risk_budget,
    )

    if not is_valid:
        logger.warning(f"[{coin}] {sig.upper()} отклонен: {error_msg}")
        notify(f"[{coin}] ⚠️ {sig.upper()} отклонен: {error_msg}")
        return

    # Определяем position_idx для hedge mode
    # Проверяем существующие позиции чтобы узнать режим торговли
    positions_data = bybit.get_positions(symbol=pair)
    positions_list = positions_data.get("result", {}).get("list", [])

    existing_same_side = next(
        (
            position for position in positions_list
            if position.get("symbol") == pair
            and position.get("side") == side
            and to_float(position.get("size")) > 0
        ),
        None,
    )
    if existing_same_side:
        logger.warning(f"[{coin}] {sig.upper()} пропущен: позиция в том же направлении уже открыта")
        notify(f"[{coin}] ℹ️ {sig.upper()} пропущен: позиция в этом направлении уже есть")
        return

    # КРИТИЧЕСКАЯ ПРОВЕРКА: если есть противоположная позиция, сначала закрываем её
    opposite_side = "Sell" if side == "Buy" else "Buy"
    existing_opposite = next(
        (p for p in positions_list
         if p.get("symbol") == pair
         and p.get("side") == opposite_side
         and float(p.get("size", 0)) > 0),
        None
    )

    if existing_opposite:
        logger.warning(
            f"[{coin}] Обнаружена противоположная позиция {opposite_side}! "
            f"Сначала закрываем её перед открытием {side}"
        )

        # Закрываем противоположную позицию
        try:
            existing_qty = float(existing_opposite.get("size", 0))
            existing_idx = int(existing_opposite.get("positionIdx", 0))

            logger.info(f"[{coin}] Закрываю {opposite_side} позицию qty={existing_qty} перед открытием {side}")

            close_result = bybit.close_position_market(
                symbol=pair,
                side="Buy" if opposite_side == "Sell" else "Sell",
                position_idx=existing_idx
            )

            logger.info(f"[{coin}] ✅ Противоположная позиция закрыта: {close_result}")
            notify(
                f"[{coin}] 🔄 Разворот позиции\n"
                f"❌ Закрыта {opposite_side} позиция\n"
                f"Открываю {side}..."
            )

            # Обновляем список позиций после закрытия
            import time
            time.sleep(0.5)  # Небольшая задержка чтобы биржа обработала закрытие

            positions_data = bybit.get_positions(symbol=pair)
            positions_list = positions_data.get("result", {}).get("list", [])

        except Exception as e:
            logger.error(f"[{coin}] ❌ Ошибка закрытия противоположной позиции: {e}")
            notify(f"[{coin}] ❌ Не удалось закрыть {opposite_side} позицию: {e}")
            return  # Прерываем открытие новой позиции

    # Определяем position_idx на основе существующих позиций
    # Если у нас есть позиции с positionIdx > 0, значит hedge mode
    position_idx = 0  # По умолчанию one-way mode
    if positions_list:
        # Проверяем максимальный positionIdx
        max_idx = max(int(p.get("positionIdx", 0)) for p in positions_list)
        if max_idx > 0:
            # Hedge mode активен
            # 1 = Buy, 2 = Sell
            position_idx = 1 if side == "Buy" else 2
            logger.info(f"[{coin}] Hedge mode обнаружен, использую position_idx={position_idx} для {side}")

    # Рассчитываем суммы
    position_size_usd = qty * last_price
    margin_required = position_size_usd / leverage
    margin_with_buffer = margin_required * 1.10
    actual_risk_usd = abs(last_price - sl) * qty

    # Потенциальная прибыль и убыток
    if sig == "long":
        potential_profit_usd = (tp - last_price) * qty if tp else 0
        potential_loss_usd = (last_price - sl) * qty if sl else 0
    else:  # short
        potential_profit_usd = (last_price - tp) * qty if tp else 0
        potential_loss_usd = (sl - last_price) * qty if sl else 0

    risk_reward_ratio = (potential_profit_usd / potential_loss_usd) if potential_loss_usd > 0 else 0

    logger.info(
        f"[{coin}] {sig.upper()}: side={side}, qty={qty}, "
        f"TP={tp}, SL={sl}, leverage={leverage}, confidence={confidence:.2f}"
    )
    logger.info(
        f"[{coin}] Сумма позиции: ${position_size_usd:.2f}, "
        f"Маржа: ${margin_required:.2f}, R/R: {risk_reward_ratio:.2f}"
    )

    if DRY_RUN:
        logger.info(
            f"[DRY_RUN] {coin} {sig.upper()}: открытие {side} "
            f"qty={qty} TP={tp} SL={sl} leverage={leverage}"
        )
        notify(
            f"[{coin}] 🟢 {sig.upper()} (DRY): {side}\n"
            f"💰 Размер: {qty} @ ${last_price:.2f}\n"
            f"📊 Сумма: ${position_size_usd:.2f} (маржа: ${margin_required:.2f})\n"
            f"⚡ Плечо: {leverage}x\n"
            f"🎯 TP: ${tp} (+${potential_profit_usd:.2f})\n"
            f"🛑 SL: ${sl} (-${potential_loss_usd:.2f})\n"
            f"📈 R/R: {risk_reward_ratio:.2f} | Уверенность: {confidence:.0%}"
        )
        return {"risk_usd": actual_risk_usd, "margin_with_buffer": margin_with_buffer}
    else:
        # Устанавливаем leverage
        try:
            bybit.set_leverage(symbol=pair, buy_leverage=leverage, sell_leverage=leverage)
            logger.info(f"[{coin}] Leverage установлен: {leverage}x")
        except BybitAPIError as e:
            # Ошибка 110043 означает что leverage уже установлен
            if e.code == 110043:
                logger.info(f"[{coin}] Leverage уже установлен на {leverage}x")
            else:
                logger.warning(f"[{coin}] Не удалось установить leverage: {e}")
        except Exception as e:
            logger.warning(f"[{coin}] Не удалось установить leverage: {e}")

        # Создаем ордер с TP/SL
        try:
            resp = bybit.create_order(
                symbol=pair,
                side=side,
                order_type="Market",
                qty=qty,
                take_profit=tp,
                stop_loss=sl,
                position_idx=position_idx
            )
            logger.info(f"[{coin}] Позиция открыта: {resp}")
            notify(
                f"[{coin}] 🟢 {sig.upper()}: {side}\n"
                f"💰 Размер: {qty} @ ${last_price:.2f}\n"
                f"📊 Сумма: ${position_size_usd:.2f} (маржа: ${margin_required:.2f})\n"
                f"⚡ Плечо: {leverage}x\n"
                f"🎯 TP: ${tp} (+${potential_profit_usd:.2f})\n"
                f"🛑 SL: ${sl} (-${potential_loss_usd:.2f})\n"
                f"📈 R/R: {risk_reward_ratio:.2f} | Уверенность: {confidence:.0%}"
            )
            return {"risk_usd": actual_risk_usd, "margin_with_buffer": margin_with_buffer}
        except BybitAPIError as e:
            # Специальная обработка ошибки недостатка средств
            if e.code == 110007:
                logger.error(
                    f"[{coin}] Недостаточно средств! "
                    f"Требуется маржа: ${margin_required:.2f}, "
                    f"доступно: ${available_balance:.2f}"
                )
                notify(
                    f"[{coin}] ❌ Недостаточно средств\n"
                    f"Требуется: ${margin_required:.2f}\n"
                    f"Доступно: ${available_balance:.2f}"
                )
            else:
                raise  # Пробрасываем другие ошибки выше

def main_loop():
    """Основной цикл бота"""
    logger.info("="*60)
    logger.info("🚀 Запуск торгового бота")
    logger.info(f"DRY_RUN режим: {DRY_RUN}")
    logger.info(f"Токены: {', '.join(TRADABLE_TOKENS)}")
    logger.info(f"Интервал опроса: {POLL_INTERVAL}с")
    logger.info("="*60)

    notify(f"🤖 Бот запущен (DRY_RUN: {DRY_RUN})")

    # Инициализация API
    bybit = BybitAPI()
    ds = DeepSeekAPI()

    # Генерируем промпт для DeepSeek на основе текущей конфигурации
    prompt = build_deepseek_prompt()
    prompt_summary = get_prompt_summary()
    logger.info(f"📝 Промпт сгенерирован для {prompt_summary['tokens_count']} токенов: {', '.join(prompt_summary['tokens'])}")
    logger.info(f"⚙️ Параметры: Leverage {prompt_summary['max_leverage']}x, Risk {prompt_summary['risk_per_trade']}, Min ${prompt_summary['min_order_size']}")
    logger.debug(f"📊 Таймфреймы: {', '.join(prompt_summary['timeframes'])}")

    iteration = 0

    # Функция для проверки нужно ли продолжать работу
    def should_continue():
        """Проверка флага остановки из Telegram бота"""
        try:
            from telegram_bot.handlers.auto_mode import AUTO_MODE_STATE
            # A CLI launch has no registered worker thread and must not inherit
            # the Telegram UI's initial ``False`` flag.
            return AUTO_MODE_STATE.get("task") is None or AUTO_MODE_STATE.get("is_running", False)
        except ImportError:
            return True

    def wait_until_next_cycle() -> bool:
        """Sleep in short intervals so the Telegram Stop button reacts promptly."""
        for _ in range(max(1, POLL_INTERVAL)):
            if not should_continue():
                return False
            time.sleep(1)
        return True

    while should_continue():
        iteration += 1
        logger.info(f"\n{'='*60}")
        logger.info(f"📊 Итерация #{iteration}")
        logger.info(f"{'='*60}")

        try:
            # 1. Получаем позиции
            logger.info("Получаю позиции...")
            pos_resp = bybit.get_positions()
            result = pos_resp.get("result", {})

            positions_list = []
            if isinstance(result, dict) and "list" in result:
                positions_list = result["list"]
            elif isinstance(result, list):
                positions_list = result

            # Фильтруем только открытые позиции
            open_positions = [p for p in positions_list if float(p.get("size", 0)) > 0]
            logger.info(f"Открытых позиций: {len(open_positions)}")

            # 2. Получаем цены
            logger.info("Получаю цены...")
            tickers_map = {}
            for token in TRADABLE_TOKENS:
                pair = symbol_to_pair(token)
                try:
                    t = bybit.get_tickers(pair)
                    last = t["result"]["list"][0]["lastPrice"]
                    tickers_map[pair] = {"lastPrice": float(last), "raw": t}
                    logger.debug(f"{pair}: ${float(last):.2f}")
                except Exception as e:
                    logger.error(f"Ошибка получения тикера {pair}: {e}")
                    tickers_map[pair] = {"lastPrice": None, "raw": None}

            if not tickers_map or all(v["lastPrice"] is None for v in tickers_map.values()):
                logger.warning("⚠️ Нет данных по ценам — пропускаем итерацию")
                if not wait_until_next_cycle():
                    break
                continue

            # 3. Получаем баланс
            logger.info("Получаю баланс...")
            try:
                balance_resp = bybit.get_wallet_balance()
                logger.debug(f"Balance response: {balance_resp}")

                account_info = parse_account_overview(balance_resp, max_leverage=MAX_LEVERAGE)
                logger.info(
                    f"💰 Баланс: ${account_info['balance_usd']:.2f}, "
                    f"Equity: ${account_info['equity_usd']:.2f}, "
                    f"В позициях: ${account_info['position_margin_usd']:.2f}, "
                    f"В ордерах: ${account_info['order_margin_usd']:.2f}, "
                    f"Доступно: ${account_info['available_usd']:.2f}"
                )
            except Exception as e:
                logger.error(f"Ошибка получения баланса: {e}")
                logger.debug(traceback.format_exc())
                account_info = {
                    "balance_usd": 0,
                    "available_usd": 0,
                    "max_leverage": MAX_LEVERAGE
                }

            # 4. Формируем контекст
            context = build_context(positions_list, tickers_map, account_info)

            # 5. Обогащаем контекст техническими индикаторами (3м, 5м, 1ч, 4ч)
            logger.info("📈 Получаю технические индикаторы (3м, 5м, 1ч, 4ч)...")
            try:
                context = enrich_context_with_market_data(bybit, context, TRADABLE_TOKENS)
                logger.info(f"✅ Индикаторы получены для {len(context.get('market_analysis', {}))} токенов")
            except Exception as e:
                logger.warning(f"⚠️ Ошибка получения индикаторов: {e}")
                # Продолжаем без индикаторов

            # 6. Отправляем в DeepSeek
            logger.info("🧠 Отправляю контекст в DeepSeek...")
            try:
                raw = ds.analyze(prompt, context, temperature=0.0)
                logger.info(f"✅ Получен ответ от DeepSeek ({len(raw)} символов)")
                logger.debug(f"Ответ DeepSeek (полный):\n{raw}")
            except Exception as e:
                error_str = str(e)
                logger.error(f"❌ Ошибка DeepSeek API: {e}")

                # Специальная обработка ошибки недостатка баланса
                if "402" in error_str or "Insufficient Balance" in error_str:
                    logger.error("💳 У вас недостаточно средств на балансе DeepSeek!")
                    logger.error("   Пополните баланс: https://platform.deepseek.com/")
                    logger.error("   DeepSeek Reasoner стоит ~$0.50-1.00 за 1M токенов")
                    notify("💳 Ошибка: недостаточно средств на DeepSeek. Пополните баланс на platform.deepseek.com")
                else:
                    notify(f"❌ Ошибка DeepSeek: {e}")

                if not wait_until_next_cycle():
                    break
                continue

            # 7. Валидация JSON
            try:
                decision = validate_deepseek_json(raw, expected_tokens=TRADABLE_TOKENS)
                logger.info(f"✅ Получены решения для {len(decision)} токенов")
            except Exception as e:
                logger.error(f"❌ Невалидный JSON от DeepSeek: {e}")
                logger.debug(f"Raw response: {raw}")
                notify(f"❌ Невалидный JSON от DeepSeek")
                if not wait_until_next_cycle():
                    break
                continue

            # 8. Исполнение решений
            logger.info("⚙️ Исполняю решения...")
            execute_decision(bybit, decision, account_info)

            logger.info(f"✅ Итерация #{iteration} завершена")

        except KeyboardInterrupt:
            logger.info("⛔ Получен сигнал остановки")
            notify("⛔ Бот остановлен вручную")
            break
        except Exception as e:
            logger.error(f"❌ Ошибка в главном цикле: {e}")
            logger.debug(traceback.format_exc())
            notify(f"❌ Критическая ошибка: {e}")

        # Проверка флага остановки перед сном
        if not should_continue():
            logger.info("⏹️ Авто-режим остановлен через Telegram бот")
            notify("⏹️ Авто-режим остановлен")
            break

        logger.info(f"💤 Сплю {POLL_INTERVAL}с...\n")

        if not wait_until_next_cycle():
            logger.info("⏹️ Авто-режим остановлен через Telegram бот")
            notify("⏹️ Авто-режим остановлен")
            return

if __name__ == "__main__":
    try:
        main_loop()
    except Exception as e:
        logger.critical(f"💥 Критическая ошибка при запуске: {e}")
        logger.debug(traceback.format_exc())
        notify(f"💥 Критическая ошибка: {e}")
