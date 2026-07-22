# core/market_data.py
"""
Модуль для получения рыночных данных и расчета технических индикаторов
"""
from typing import List, Sequence, Tuple
import time
import numpy as np
from loguru import logger
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

from api.bybit_api import BybitAPI
from config import BYBIT_CATEGORY


ANALYSIS_CACHE_TTL_SECONDS = 60
_analysis_cache: dict[str, tuple[float, dict]] = {}


def calculate_ema(prices: Sequence[float], period: int) -> float:
    """Рассчитывает EMA (Exponential Moving Average)"""
    if len(prices) < period:
        return sum(prices) / len(prices) if prices else 0

    prices_array = np.asarray(prices, dtype=float)
    ema = prices_array[:period].mean()  # Начальное значение - SMA
    multiplier = 2 / (period + 1)

    for price in prices_array[period:]:
        ema = (price - ema) * multiplier + ema

    return float(ema)


def calculate_rsi(prices: Sequence[float], period: int = 14) -> float:
    """Рассчитывает RSI (Relative Strength Index)"""
    if len(prices) < period + 1:
        return 50.0

    deltas = np.diff(prices)
    gains = np.where(deltas > 0, deltas, 0)
    losses = np.where(deltas < 0, -deltas, 0)

    avg_gain = gains[:period].mean()
    avg_loss = losses[:period].mean()

    for i in range(period, len(deltas)):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period

    if avg_loss == 0:
        # A completely flat series is neutral, not overbought.
        return 50.0 if avg_gain == 0 else 100.0

    rs = avg_gain / avg_loss
    rsi = 100 - (100 / (1 + rs))

    return float(rsi)


def calculate_macd(
    prices: Sequence[float], fast: int = 12, slow: int = 26, signal: int = 9
) -> Tuple[float, float]:
    """Calculate the MACD and its signal EMA from the MACD history.

    The old code set the signal line equal to MACD, making the histogram always
    zero and removing the indicator's crossover information.
    """
    if len(prices) < slow + signal - 1:
        return 0.0, 0.0

    macd_history = [
        calculate_ema(prices[:index], fast) - calculate_ema(prices[:index], slow)
        for index in range(slow, len(prices) + 1)
    ]
    return float(macd_history[-1]), float(calculate_ema(macd_history, signal))


def calculate_atr(klines: Sequence[dict], period: int = 14) -> float:
    """Calculate Wilder's ATR from OHLC candles."""
    if len(klines) < period + 1:
        return 0.0

    true_ranges = []
    for index in range(1, len(klines)):
        high = klines[index]["high"]
        low = klines[index]["low"]
        previous_close = klines[index - 1]["close"]
        true_ranges.append(max(high - low, abs(high - previous_close), abs(low - previous_close)))

    atr = float(np.mean(true_ranges[:period]))
    for true_range in true_ranges[period:]:
        atr = (atr * (period - 1) + true_range) / period
    return atr


def get_kline_data(bybit: BybitAPI, symbol: str, interval: str = "1", limit: int = 200) -> List[dict]:
    """
    Получает свечные данные (kline) от Bybit
    interval: "1" (1 min), "5" (5 min), "15", "60" (1h), "240" (4h), "D" (1d)
    """
    try:
        params = {
            "category": BYBIT_CATEGORY,
            "symbol": symbol,
            "interval": interval,
            "limit": limit
        }

        response = bybit._public_get("/v5/market/kline", params=params)
        klines = response.get("result", {}).get("list", [])

        # Bybit возвращает в обратном порядке (новые первые)
        klines.reverse()

        parsed = []
        for k in klines:
            parsed.append({
                "timestamp": int(k[0]),
                "open": float(k[1]),
                "high": float(k[2]),
                "low": float(k[3]),
                "close": float(k[4]),
                "volume": float(k[5]),
            })

        return parsed

    except Exception as e:
        logger.error(f"Ошибка получения kline для {symbol}: {e}")
        return []


def get_market_analysis(bybit: BybitAPI, symbol: str, current_price: float) -> dict:
    """
    Получает полный анализ рынка с техническими индикаторами
    Анализирует несколько таймфреймов: 3м, 5м, 1ч, 4ч
    """
    cached = _analysis_cache.get(symbol)
    now = time.monotonic()
    if cached and now - cached[0] < ANALYSIS_CACHE_TTL_SECONDS:
        # Price changes every live refresh; indicator candles do not need to be
        # downloaded every two seconds.
        return {**cached[1], "current_price": current_price}

    try:
        # Получаем данные разных таймфреймов
        klines_3m = get_kline_data(bybit, symbol, interval="3", limit=100)   # 3 минуты (текущий момент)
        klines_5m = get_kline_data(bybit, symbol, interval="5", limit=100)   # 5 минут
        klines_1h = get_kline_data(bybit, symbol, interval="60", limit=100)  # 1 час
        klines_4h = get_kline_data(bybit, symbol, interval="240", limit=50)  # 4 часа

        if not klines_3m or len(klines_3m) < 30:
            logger.warning(f"Недостаточно данных для {symbol}")
            return {"error": "insufficient_data"}

        # === АНАЛИЗ 3-МИНУТНОГО ТАЙМФРЕЙМА (текущий момент) ===
        closes_3m = [k["close"] for k in klines_3m] if klines_3m else []
        volumes_3m = [k["volume"] for k in klines_3m] if klines_3m else []

        ema20_3m = calculate_ema(closes_3m, 20) if len(closes_3m) >= 20 else 0
        ema50_3m = calculate_ema(closes_3m, 50) if len(closes_3m) >= 50 else 0
        rsi14_3m = calculate_rsi(closes_3m, 14) if len(closes_3m) >= 15 else 50
        macd_3m, macd_signal_3m = calculate_macd(closes_3m)

        avg_volume_3m = np.mean(volumes_3m[-20:]) if len(volumes_3m) >= 20 else 0
        current_volume_3m = volumes_3m[-1] if volumes_3m else 0

        price_series_3m = [round(p, 8) for p in closes_3m[-10:]] if len(closes_3m) >= 10 else []

        # === АНАЛИЗ 5-МИНУТНОГО ТАЙМФРЕЙМА (краткосрочный тренд) ===
        closes_5m = [k["close"] for k in klines_5m] if klines_5m else []
        ema20_5m = calculate_ema(closes_5m, 20) if len(closes_5m) >= 20 else 0
        ema50_5m = calculate_ema(closes_5m, 50) if len(closes_5m) >= 50 else 0
        rsi14_5m = calculate_rsi(closes_5m, 14) if len(closes_5m) >= 15 else 50
        macd_5m, macd_signal_5m = calculate_macd(closes_5m)

        price_series_5m = [round(p, 8) for p in closes_5m[-10:]] if len(closes_5m) >= 10 else []

        # === АНАЛИЗ 1-ЧАСОВОГО ТАЙМФРЕЙМА (среднесрочный тренд) ===
        closes_1h = [k["close"] for k in klines_1h] if klines_1h else []
        volumes_1h = [k["volume"] for k in klines_1h] if klines_1h else []

        ema20_1h = calculate_ema(closes_1h, 20) if len(closes_1h) >= 20 else 0
        ema50_1h = calculate_ema(closes_1h, 50) if len(closes_1h) >= 50 else 0
        rsi14_1h = calculate_rsi(closes_1h, 14) if len(closes_1h) >= 15 else 50
        macd_1h, macd_signal_1h = calculate_macd(closes_1h)

        # ATR with Wilder smoothing, consistent with the RSI implementation.
        atr14_1h = calculate_atr(klines_1h)

        avg_volume_1h = np.mean(volumes_1h[-20:]) if len(volumes_1h) >= 20 else 0

        price_series_1h = [round(p, 8) for p in closes_1h[-10:]] if len(closes_1h) >= 10 else []

        # === АНАЛИЗ 4-ЧАСОВОГО ТАЙМФРЕЙМА (долгосрочный контекст) ===
        closes_4h = [k["close"] for k in klines_4h] if klines_4h else []

        ema20_4h = calculate_ema(closes_4h, 20) if len(closes_4h) >= 20 else 0
        ema50_4h = calculate_ema(closes_4h, 50) if len(closes_4h) >= 50 else 0
        rsi14_4h = calculate_rsi(closes_4h, 14) if len(closes_4h) >= 15 else 50
        macd_4h, macd_signal_4h = calculate_macd(closes_4h)

        # MACD серия для 4h (последние 10 точек)
        macd_series_4h = []
        for i in range(max(0, len(closes_4h) - 10), len(closes_4h)):
            subset = closes_4h[:i+1]
            if len(subset) >= 34:
                macd_val, _ = calculate_macd(subset)
                macd_series_4h.append(round(macd_val, 3))

        analysis = {
            "current_price": current_price,

            # 3-МИНУТНЫЙ ТАЙМФРЕЙМ (текущий момент)
            "timeframe_3m": {
                "ema20": round(ema20_3m, 2),
                "ema50": round(ema50_3m, 2),
                "macd": round(macd_3m, 3),
                "macd_signal": round(macd_signal_3m, 3),
                "macd_histogram": round(macd_3m - macd_signal_3m, 3),
                "rsi14": round(rsi14_3m, 3),
                "avg_volume": round(avg_volume_3m, 3),
                "current_volume": round(current_volume_3m, 3),
                "price_series": price_series_3m,
            },

            # 5-МИНУТНЫЙ ТАЙМФРЕЙМ (краткосрочный)
            "timeframe_5m": {
                "ema20": round(ema20_5m, 2),
                "ema50": round(ema50_5m, 2),
                "macd": round(macd_5m, 3),
                "macd_signal": round(macd_signal_5m, 3),
                "macd_histogram": round(macd_5m - macd_signal_5m, 3),
                "rsi14": round(rsi14_5m, 3),
                "price_series": price_series_5m,
            },

            # 1-ЧАСОВОЙ ТАЙМФРЕЙМ (среднесрочный)
            "timeframe_1h": {
                "ema20": round(ema20_1h, 2),
                "ema50": round(ema50_1h, 2),
                "macd": round(macd_1h, 3),
                "macd_signal": round(macd_signal_1h, 3),
                "macd_histogram": round(macd_1h - macd_signal_1h, 3),
                "rsi14": round(rsi14_1h, 3),
                "atr14": round(atr14_1h, 2),
                "avg_volume": round(avg_volume_1h, 3),
                "current_volume": round(volumes_1h[-1], 3) if volumes_1h else 0,
                "price_series": price_series_1h,
            },

            # 4-ЧАСОВОЙ ТАЙМФРЕЙМ (долгосрочный контекст)
            "timeframe_4h": {
                "ema20": round(ema20_4h, 2),
                "ema50": round(ema50_4h, 2),
                "macd": round(macd_4h, 3),
                "macd_signal": round(macd_signal_4h, 3),
                "macd_histogram": round(macd_4h - macd_signal_4h, 3),
                "rsi14": round(rsi14_4h, 3),
                "macd_series": macd_series_4h,
            },
        }

        _analysis_cache[symbol] = (now, analysis)
        return analysis

    except Exception as e:
        logger.error(f"Ошибка анализа рынка для {symbol}: {e}")
        return {"error": str(e)}


def enrich_context_with_market_data(bybit: BybitAPI, context: dict, tokens: List[str]) -> dict:
    """
    Обогащает контекст техническими индикаторами для каждого токена
    """
    market_analysis = {}

    for token in tokens:
        symbol = f"{token}USDT"

        # Получаем текущую цену из контекста
        current_price = context.get("prices", {}).get(symbol, {}).get("lastPrice")

        if not current_price:
            logger.warning(f"Нет цены для {symbol}, пропускаем анализ")
            continue

        logger.debug(f"Получаю рыночный анализ для {token}...")
        analysis = get_market_analysis(bybit, symbol, current_price)

        if "error" not in analysis:
            market_analysis[token] = analysis
        else:
            logger.warning(f"Ошибка анализа {token}: {analysis['error']}")

    # Добавляем в контекст
    context["market_analysis"] = market_analysis

    return context
