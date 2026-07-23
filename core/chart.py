"""Accessible text chart that preserves Telegram's one-message invariant."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Sequence

from api.bybit_api import BybitAPI
from core.market_data import (
    calculate_atr,
    calculate_ema,
    calculate_rsi,
    get_kline_data,
)
from utils.helpers import format_price, to_float


SPARK_LEVELS = "▁▂▃▄▅▆▇█"


def downsample(values: Sequence[float], width: int = 32) -> list[float]:
    if width <= 0 or not values:
        return []
    if len(values) <= width:
        return [float(value) for value in values]
    result: list[float] = []
    for index in range(width):
        start = index * len(values) // width
        end = max(start + 1, (index + 1) * len(values) // width)
        bucket = values[start:end]
        result.append(sum(float(value) for value in bucket) / len(bucket))
    return result


def sparkline(values: Sequence[float], width: int = 32) -> str:
    points = downsample(values, width)
    if not points:
        return "—"
    low, high = min(points), max(points)
    if high == low:
        return SPARK_LEVELS[len(SPARK_LEVELS) // 2] * len(points)
    return "".join(
        SPARK_LEVELS[
            min(
                len(SPARK_LEVELS) - 1,
                int((value - low) / (high - low) * (len(SPARK_LEVELS) - 1)),
            )
        ]
        for value in points
    )


def build_chart_text(bybit: BybitAPI, symbol: str, interval: str) -> str:
    candles = get_kline_data(bybit, symbol, interval, 80)
    if len(candles) < 50:
        raise ValueError(f"Недостаточно закрытых свечей {symbol}/{interval}")
    ticker = bybit.get_tickers(symbol).get("result", {}).get("list", [None])[0]
    if not ticker:
        raise ValueError(f"Нет ticker {symbol}")
    closes = [float(candle["close"]) for candle in candles]
    current = to_float(ticker.get("lastPrice"))
    first = closes[-32]
    change = (current - first) / first * 100 if first else 0.0
    high = max(candle["high"] for candle in candles[-32:])
    low = min(candle["low"] for candle in candles[-32:])
    ema20 = calculate_ema(closes, 20)
    ema50 = calculate_ema(closes, 50)
    rsi = calculate_rsi(closes)
    atr = calculate_atr(candles)
    bid = to_float(ticker.get("bid1Price"))
    ask = to_float(ticker.get("ask1Price"))
    spread = (ask - bid) / ((ask + bid) / 2) * 100 if bid > 0 and ask > 0 else 0.0
    closed_at = datetime.fromtimestamp(
        candles[-1]["closed_at"] / 1_000,
        timezone.utc,
    ).strftime("%H:%M:%S")
    updated = datetime.now(timezone.utc).strftime("%H:%M:%S")
    interval_label = {
        "5": "5м",
        "15": "15м",
        "60": "1ч",
        "240": "4ч",
    }.get(str(interval), str(interval))
    direction = "🟢" if change >= 0 else "🔴"
    trend = "выше EMA" if current >= ema20 >= ema50 else (
        "ниже EMA" if current <= ema20 <= ema50 else "смешанный"
    )
    return (
        f"📈 <b>{symbol} · {interval_label}</b>\n"
        f"{direction} <code>{format_price(current)}</code> · "
        f"<code>{change:+.2f}%</code> за 32 свечи\n\n"
        f"<pre>{sparkline(closes[-32:])}</pre>\n"
        f"L <code>{format_price(low)}</code> · "
        f"H <code>{format_price(high)}</code>\n"
        f"EMA20 <code>{format_price(ema20)}</code> · "
        f"EMA50 <code>{format_price(ema50)}</code>\n"
        f"RSI14 <code>{rsi:.1f}</code> · ATR14 <code>{format_price(atr)}</code>\n"
        f"Тренд: <b>{trend}</b> · spread <code>{spread:.3f}%</code>\n\n"
        f"<i>Последняя закрытая свеча {closed_at} UTC · цена {updated} UTC</i>"
    )
