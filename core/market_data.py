"""Closed-candle market data and deterministic technical features."""

from __future__ import annotations

import time
from typing import List, Sequence, Tuple

import numpy as np
from loguru import logger

from api.bybit_api import BybitAPI


ANALYSIS_CACHE_TTL_SECONDS = 60
_analysis_cache: dict[str, tuple[float, dict]] = {}


def calculate_ema(prices: Sequence[float], period: int) -> float:
    if not prices:
        return 0.0
    if len(prices) < period:
        return float(sum(prices) / len(prices))
    values = np.asarray(prices, dtype=float)
    ema = float(values[:period].mean())
    multiplier = 2 / (period + 1)
    for price in values[period:]:
        ema = (float(price) - ema) * multiplier + ema
    return float(ema)


def calculate_rsi(prices: Sequence[float], period: int = 14) -> float:
    if len(prices) < period + 1:
        return 50.0
    deltas = np.diff(prices)
    gains = np.where(deltas > 0, deltas, 0)
    losses = np.where(deltas < 0, -deltas, 0)
    average_gain = float(gains[:period].mean())
    average_loss = float(losses[:period].mean())
    for index in range(period, len(deltas)):
        average_gain = (average_gain * (period - 1) + float(gains[index])) / period
        average_loss = (average_loss * (period - 1) + float(losses[index])) / period
    if average_loss == 0:
        return 50.0 if average_gain == 0 else 100.0
    relative_strength = average_gain / average_loss
    return float(100 - (100 / (1 + relative_strength)))


def calculate_macd(
    prices: Sequence[float], fast: int = 12, slow: int = 26, signal: int = 9
) -> Tuple[float, float]:
    if len(prices) < slow + signal - 1:
        return 0.0, 0.0
    history = [
        calculate_ema(prices[:index], fast) - calculate_ema(prices[:index], slow)
        for index in range(slow, len(prices) + 1)
    ]
    return float(history[-1]), float(calculate_ema(history, signal))


def calculate_atr(klines: Sequence[dict], period: int = 14) -> float:
    if len(klines) < period + 1:
        return 0.0
    true_ranges = []
    for index in range(1, len(klines)):
        high = float(klines[index]["high"])
        low = float(klines[index]["low"])
        previous_close = float(klines[index - 1]["close"])
        true_ranges.append(max(high - low, abs(high - previous_close), abs(low - previous_close)))
    atr = float(np.mean(true_ranges[:period]))
    for value in true_ranges[period:]:
        atr = (atr * (period - 1) + value) / period
    return atr


def _interval_ms(interval: str) -> int:
    if str(interval).isdigit():
        return int(interval) * 60_000
    mapping = {"D": 86_400_000, "W": 604_800_000, "M": 2_592_000_000}
    if interval not in mapping:
        raise ValueError(f"Неподдерживаемый интервал: {interval}")
    return mapping[interval]


def get_kline_data(
    bybit: BybitAPI,
    symbol: str,
    interval: str = "1",
    limit: int = 200,
) -> List[dict]:
    """Return only confirmed closed candles in chronological order.

    Bybit explicitly documents that the newest open candle's ``closePrice`` is
    merely the latest trade.  Excluding it prevents repainting AI signals.
    """
    try:
        response = bybit.get_kline(symbol, interval, limit=min(1_000, limit + 1))
        server_ms = int(response.get("time") or time.time() * 1_000)
        duration_ms = _interval_ms(str(interval))
        rows = list(response.get("result", {}).get("list", []))
        rows.reverse()
        parsed = [
            {
                "timestamp": int(row[0]),
                "closed_at": int(row[0]) + duration_ms,
                "open": float(row[1]),
                "high": float(row[2]),
                "low": float(row[3]),
                "close": float(row[4]),
                "volume": float(row[5]),
            }
            for row in rows
            if int(row[0]) + duration_ms <= server_ms
        ]
        return parsed[-limit:]
    except Exception as error:
        logger.error(f"Ошибка получения закрытых свечей {symbol}/{interval}: {error}")
        return []


def _timeframe_features(klines: Sequence[dict], now_ms: int) -> dict:
    closes = [float(candle["close"]) for candle in klines]
    volumes = [float(candle["volume"]) for candle in klines]
    ema20 = calculate_ema(closes, 20)
    ema50 = calculate_ema(closes, 50)
    previous_ema20 = calculate_ema(closes[:-3], 20) if len(closes) > 23 else ema20
    macd, macd_signal = calculate_macd(closes)
    window = list(klines[-20:])
    average_volume = float(np.mean(volumes[-20:])) if volumes else 0.0
    volume_ratio = volumes[-1] / average_volume if average_volume > 0 else 0.0
    last_closed_at = int(klines[-1]["closed_at"])
    return {
        "ema20": round(ema20, 8),
        "ema50": round(ema50, 8),
        "ema20_slope": round(ema20 - previous_ema20, 8),
        "macd": round(macd, 8),
        "macd_signal": round(macd_signal, 8),
        "macd_histogram": round(macd - macd_signal, 8),
        "rsi14": round(calculate_rsi(closes), 3),
        "atr14": round(calculate_atr(klines), 8),
        "volume_ratio": round(volume_ratio, 3),
        "swing_high": round(max(candle["high"] for candle in window), 8),
        "swing_low": round(min(candle["low"] for candle in window), 8),
        "price_series": [round(price, 8) for price in closes[-32:]],
        "last_closed_candle_at": last_closed_at,
        "age_ms": max(0, now_ms - last_closed_at),
    }


def _regime(frames: dict[str, dict]) -> str:
    one_hour = frames["timeframe_1h"]
    four_hour = frames["timeframe_4h"]
    bullish = (
        one_hour["ema20"] > one_hour["ema50"]
        and four_hour["ema20"] > four_hour["ema50"]
        and one_hour["ema20_slope"] > 0
    )
    bearish = (
        one_hour["ema20"] < one_hour["ema50"]
        and four_hour["ema20"] < four_hour["ema50"]
        and one_hour["ema20_slope"] < 0
    )
    if bullish:
        return "trend_up"
    if bearish:
        return "trend_down"
    return "range"


def get_market_analysis(bybit: BybitAPI, symbol: str, current_price: float) -> dict:
    cached = _analysis_cache.get(symbol)
    now = time.monotonic()
    if cached and now - cached[0] < ANALYSIS_CACHE_TTL_SECONDS:
        return {**cached[1], "current_price": current_price}

    try:
        datasets = {
            "timeframe_3m": get_kline_data(bybit, symbol, "3", 100),
            "timeframe_5m": get_kline_data(bybit, symbol, "5", 100),
            "timeframe_1h": get_kline_data(bybit, symbol, "60", 100),
            "timeframe_4h": get_kline_data(bybit, symbol, "240", 60),
        }
        incomplete = [name for name, candles in datasets.items() if len(candles) < 50]
        if incomplete:
            return {"error": f"incomplete_timeframes:{','.join(incomplete)}", "complete": False}
        now_ms = int(time.time() * 1_000)
        frames = {
            name: _timeframe_features(candles, now_ms)
            for name, candles in datasets.items()
        }
        analysis = {
            "current_price": float(current_price),
            "complete": True,
            "as_of_ms": now_ms,
            "regime": _regime(frames),
            **frames,
        }
        _analysis_cache[symbol] = (now, analysis)
        return analysis
    except Exception as error:
        logger.error(f"Ошибка анализа рынка для {symbol}: {error}")
        return {"error": str(error), "complete": False}


def enrich_context_with_market_data(
    bybit: BybitAPI,
    context: dict,
    tokens: List[str],
) -> dict:
    market_analysis: dict[str, dict] = {}
    for token in tokens:
        symbol = f"{token}USDT"
        current_price = context.get("prices", {}).get(symbol, {}).get("lastPrice")
        if not current_price:
            logger.warning(f"Нет цены для {symbol}, анализ пропущен")
            continue
        analysis = get_market_analysis(bybit, symbol, float(current_price))
        if analysis.get("complete"):
            market_analysis[token] = analysis
        else:
            logger.warning(f"Неполный анализ {token}: {analysis.get('error', 'unknown')}")
    context["market_analysis"] = market_analysis
    return context
