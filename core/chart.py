"""Coherent market charts for Telegram's single-message interface.

Every representation is built from one immutable snapshot.  Fetching and
indicator calculation happen once; PNG, rich HTML and the classic HTML
fallback only format the already validated values.
"""

from __future__ import annotations

import math
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from html import escape
from typing import Mapping, Optional, Sequence

from api.bybit_api import BybitAPI
from core.market_data import calculate_atr, calculate_rsi, get_kline_data
from core.rich_charts import (
    DARK_THEME,
    FIGURE_DPI,
    FIGURE_SIZE_INCHES,
    MATPLOTLIB_RENDER_LOCK,
    figure_to_png,
)
from utils.helpers import format_price, to_float
from utils.logger_setup import logger


SPARK_LEVELS = "▁▂▃▄▅▆▇█"
# EMA200 needs a meaningful warm-up before the 120 displayed candles.
CHART_HISTORY_CANDLES = 400
CHART_VISIBLE_CANDLES = 120
DAILY_LOW_CANDLES = 14
RICH_MEDIA_ID = "market_chart"
CHART_REFRESH_SECONDS = 30
DAILY_LOW_CACHE_SECONDS = 15 * 60
DAILY_LOW_RETRY_SECONDS = 60

# Kept as a compatibility alias for code which used the old private name.
_MATPLOTLIB_LOCK = MATPLOTLIB_RENDER_LOCK
_DAILY_LOW_CACHE_LOCK = threading.Lock()
_DAILY_LOW_CACHE: dict[tuple[str, str], tuple[float, Optional[float]]] = {}
_DAILY_LOW_FETCH_LOCKS: dict[tuple[str, str], threading.Lock] = {}


@dataclass(frozen=True, slots=True)
class MarketCandle:
    """One deeply immutable, validated closed OHLCV candle."""

    timestamp: int
    closed_at: int
    open: float
    high: float
    low: float
    close: float
    volume: float


@dataclass(frozen=True, slots=True)
class MarketChartSnapshot:
    """The sole source of truth for all market-chart representations."""

    symbol: str
    interval: str
    interval_label: str
    candles: tuple[MarketCandle, ...]
    window: tuple[MarketCandle, ...]
    ticker_timestamp_ms: int
    current_price: float
    ema20_series: tuple[Optional[float], ...]
    ema50_series: tuple[Optional[float], ...]
    ema200_series: tuple[Optional[float], ...]
    ema20: float
    ema50: float
    ema200: float
    ema20_distance_percent: float
    ema50_distance_percent: float
    ema200_distance_percent: float
    change_percent: float
    window_low: float
    window_high: float
    rsi14: float
    atr14: float
    atr14_percent: float
    regime: str
    regime_label: str
    daily_low: Optional[float]
    daily_low_distance_percent: Optional[float]

    @property
    def last_closed_at_ms(self) -> int:
        return self.window[-1].closed_at

    @property
    def window_size(self) -> int:
        return len(self.window)


@dataclass(frozen=True, slots=True)
class ChartPayload:
    """One chart image and both supported textual representations."""

    text: str
    fallback_text: str
    rich_html: str
    png: Optional[bytes]
    snapshot: Optional[MarketChartSnapshot] = None


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


def ema_series(
    prices: Sequence[float],
    period: int,
) -> list[Optional[float]]:
    """Return a deterministic SMA-seeded EMA aligned with ``prices``."""
    if period <= 0:
        raise ValueError("EMA period должен быть положительным")
    values = [float(price) for price in prices]
    if any(not math.isfinite(value) for value in values):
        raise ValueError("EMA содержит нечисловую цену")
    result: list[Optional[float]] = [None] * len(values)
    if len(values) < period:
        return result
    ema = sum(values[:period]) / period
    result[period - 1] = ema
    multiplier = 2.0 / (period + 1)
    for index in range(period, len(values)):
        ema = (values[index] - ema) * multiplier + ema
        result[index] = ema
    return result


def _validated_candles(
    candles: Sequence[Mapping[str, object]],
    *,
    minimum: int,
) -> list[dict]:
    """Validate OHLCV invariants and strict chronological ordering."""
    validated: list[dict] = []
    previous_timestamp = -1
    for raw in candles:
        try:
            timestamp = int(raw["timestamp"])
            closed_at = int(raw["closed_at"])
            open_price = float(raw["open"])
            high = float(raw["high"])
            low = float(raw["low"])
            close = float(raw["close"])
            volume = float(raw["volume"])
        except (KeyError, TypeError, ValueError) as error:
            raise ValueError("Bybit вернул повреждённую свечу") from error
        values = (open_price, high, low, close, volume)
        if (
            timestamp <= previous_timestamp
            or closed_at <= timestamp
            or any(not math.isfinite(value) for value in values)
            or min(open_price, close) < low
            or max(open_price, close) > high
            or high < low
            or low <= 0
            or volume < 0
        ):
            raise ValueError("Bybit вернул некорректную OHLCV-свечу")
        validated.append(
            {
                "timestamp": timestamp,
                "closed_at": closed_at,
                "open": open_price,
                "high": high,
                "low": low,
                "close": close,
                "volume": volume,
            }
        )
        previous_timestamp = timestamp
    if len(validated) < minimum:
        raise ValueError(
            f"Недостаточно закрытых свечей: {len(validated)} < {minimum}"
        )
    return validated


def _ticker(response: Mapping[str, object], symbol: str) -> dict:
    result = response.get("result")
    rows = result.get("list") if isinstance(result, Mapping) else None
    ticker = rows[0] if isinstance(rows, list) and rows else None
    if not isinstance(ticker, dict):
        raise ValueError(f"Нет ticker {symbol}")
    current = to_float(ticker.get("lastPrice"))
    if not math.isfinite(current) or current <= 0:
        raise ValueError(f"Некорректная текущая цена {symbol}")
    return ticker


def _closed_daily_low(
    candles: Sequence[Mapping[str, object]],
) -> Optional[float]:
    """Return the low of exactly the latest 14 confirmed daily candles."""
    if len(candles) < DAILY_LOW_CANDLES:
        return None
    try:
        validated = _validated_candles(
            candles[-DAILY_LOW_CANDLES:],
            minimum=DAILY_LOW_CANDLES,
        )
    except ValueError:
        return None
    return min(float(candle["low"]) for candle in validated)


def _cached_daily_low(bybit: BybitAPI, symbol: str) -> Optional[float]:
    """Cache the confirmed daily level and collapse concurrent requests."""
    key = (str(getattr(bybit, "base", "")).rstrip("/"), symbol)
    now = time.monotonic()
    with _DAILY_LOW_CACHE_LOCK:
        cached = _DAILY_LOW_CACHE.get(key)
        if cached and cached[0] > now:
            return cached[1]
        fetch_lock = _DAILY_LOW_FETCH_LOCKS.setdefault(key, threading.Lock())

    with fetch_lock:
        now = time.monotonic()
        with _DAILY_LOW_CACHE_LOCK:
            cached = _DAILY_LOW_CACHE.get(key)
            if cached and cached[0] > now:
                return cached[1]
            previous_value = cached[1] if cached else None

        try:
            value = _closed_daily_low(
                get_kline_data(bybit, symbol, "D", DAILY_LOW_CANDLES)
            )
        except Exception as error:
            logger.warning(
                f"Не удалось обновить 14D low {symbol}: {type(error).__name__}"
            )
            value = None

        stored_value = value if value is not None else previous_value
        ttl = (
            DAILY_LOW_CACHE_SECONDS
            if value is not None
            else DAILY_LOW_RETRY_SECONDS
        )
        with _DAILY_LOW_CACHE_LOCK:
            _DAILY_LOW_CACHE[key] = (
                time.monotonic() + ttl,
                stored_value,
            )
        return stored_value


def _interval_label(interval: str) -> str:
    return {
        "5": "5м",
        "15": "15м",
        "60": "1ч",
        "240": "4ч",
        "D": "1д",
    }.get(str(interval), str(interval))


def _safe_timestamp_ms(value: object) -> int:
    fallback = int(time.time() * 1_000)
    if isinstance(value, bool):
        return fallback
    try:
        timestamp = int(value)
        if timestamp <= 0:
            return fallback
        datetime.fromtimestamp(timestamp / 1_000, timezone.utc)
    except (TypeError, ValueError, OSError, OverflowError):
        return fallback
    return timestamp


def _utc_time(timestamp_ms: int) -> str:
    return datetime.fromtimestamp(
        timestamp_ms / 1_000,
        timezone.utc,
    ).strftime("%d.%m.%Y %H:%M:%S")


def build_market_chart_snapshot(
    bybit: BybitAPI,
    symbol: str,
    interval: str,
) -> MarketChartSnapshot:
    """Fetch and calculate one exact closed-candle market snapshot."""
    safe_symbol = str(symbol).strip().upper()
    safe_interval = str(interval).strip()
    if not safe_symbol or not safe_interval:
        raise ValueError("Нужны symbol и interval")

    validated = _validated_candles(
        get_kline_data(
            bybit,
            safe_symbol,
            safe_interval,
            CHART_HISTORY_CANDLES,
        )[-CHART_HISTORY_CANDLES:],
        minimum=CHART_HISTORY_CANDLES,
    )
    response = bybit.get_tickers(safe_symbol)
    if not isinstance(response, Mapping):
        raise ValueError(f"Некорректный ticker response {safe_symbol}")
    ticker = _ticker(response, safe_symbol)
    current = float(ticker["lastPrice"])

    closes = [float(candle["close"]) for candle in validated]
    ema20_values = tuple(ema_series(closes, 20))
    ema50_values = tuple(ema_series(closes, 50))
    ema200_values = tuple(ema_series(closes, 200))
    ema20 = ema20_values[-1]
    ema50 = ema50_values[-1]
    ema200 = ema200_values[-1]
    if ema20 is None or ema50 is None or ema200 is None:
        raise ValueError("Недостаточно закрытых свечей для EMA20/EMA50/EMA200")

    rsi14 = float(calculate_rsi(closes, 14))
    atr14 = float(calculate_atr(validated, 14))
    if (
        not math.isfinite(rsi14)
        or not 0 <= rsi14 <= 100
        or not math.isfinite(atr14)
        or atr14 < 0
    ):
        raise ValueError("Некорректное значение RSI14/ATR14")

    candles = tuple(MarketCandle(**candle) for candle in validated)
    window = candles[-CHART_VISIBLE_CANDLES:]
    window_low = min(candle.low for candle in window)
    window_high = max(candle.high for candle in window)
    change = (current / window[0].close - 1) * 100

    if current >= ema20 >= ema50 >= ema200:
        regime = "bullish_alignment"
        regime_label = "восходящий тренд: цена и EMA выстроены вверх"
    elif current <= ema20 <= ema50 <= ema200:
        regime = "bearish_alignment"
        regime_label = "нисходящий тренд: цена и EMA выстроены вниз"
    elif current >= max(ema20, ema50, ema200):
        regime = "above_all_ema"
        regime_label = "цена выше всех EMA, но линии ещё не выстроены"
    elif current <= min(ema20, ema50, ema200):
        regime = "below_all_ema"
        regime_label = "цена ниже всех EMA, но линии ещё не выстроены"
    else:
        regime = "mixed"
        regime_label = "цена между EMA — направление пока неоднозначно"

    daily_low = (
        _closed_daily_low(validated)
        if safe_interval == "D"
        else _cached_daily_low(bybit, safe_symbol)
    )
    if daily_low is not None and (
        not math.isfinite(daily_low) or daily_low <= 0
    ):
        daily_low = None
    daily_distance = (
        (current / daily_low - 1) * 100
        if daily_low is not None
        else None
    )
    return MarketChartSnapshot(
        symbol=safe_symbol,
        interval=safe_interval,
        interval_label=_interval_label(safe_interval),
        candles=candles,
        window=window,
        ticker_timestamp_ms=_safe_timestamp_ms(response.get("time")),
        current_price=current,
        ema20_series=ema20_values,
        ema50_series=ema50_values,
        ema200_series=ema200_values,
        ema20=ema20,
        ema50=ema50,
        ema200=ema200,
        ema20_distance_percent=(current / ema20 - 1) * 100,
        ema50_distance_percent=(current / ema50 - 1) * 100,
        ema200_distance_percent=(current / ema200 - 1) * 100,
        change_percent=change,
        window_low=window_low,
        window_high=window_high,
        rsi14=rsi14,
        atr14=atr14,
        atr14_percent=atr14 / current * 100,
        regime=regime,
        regime_label=regime_label,
        daily_low=daily_low,
        daily_low_distance_percent=daily_distance,
    )


def _html(value: object) -> str:
    return escape(str(value), quote=True)


def _price(value: float) -> str:
    return _html(format_price(value))


def _percent(value: float, digits: int = 2) -> str:
    if abs(value) < 0.5 * 10 ** (-digits):
        return _html(f"{0:.{digits}f}%")
    return _html(f"{value:+.{digits}f}%")


def _level_relation(distance_percent: float) -> str:
    if abs(distance_percent) < 0.005:
        return "на уровне"
    direction = "выше" if distance_percent > 0 else "ниже"
    return f"{direction} на {abs(distance_percent):.2f}%"


def _trend_icon(snapshot: MarketChartSnapshot) -> str:
    if snapshot.regime in {"bullish_alignment", "above_all_ema"}:
        return "🟢"
    if snapshot.regime in {"bearish_alignment", "below_all_ema"}:
        return "🔴"
    return "🟡"


def _rsi_description(value: float) -> tuple[str, str]:
    if value >= 70:
        return "🟠", "перекупленность — возможен перегрев"
    if value > 55:
        return "🟢", "покупатели сильнее"
    if value >= 45:
        return "⚪", "баланс покупателей и продавцов"
    if value > 30:
        return "🔴", "продавцы сильнее"
    return "🔵", "перепроданность — возможен отскок"


def _summary_text(snapshot: MarketChartSnapshot) -> str:
    direction = "🟢" if snapshot.change_percent >= 0 else "🔴"
    return (
        f"📈 <b>{_html(snapshot.symbol)} · "
        f"{_html(snapshot.interval_label)}</b>\n"
        f"{direction} <b>Текущая цена</b> "
        f"<code>{_price(snapshot.current_price)}</code>\n"
        "От первой свечи на графике: "
        f"<code>{_percent(snapshot.change_percent)}</code>\n"
        f"{_trend_icon(snapshot)} {_html(snapshot.regime_label)}\n"
        f"EMA20 <code>{_price(snapshot.ema20)}</code> · "
        f"EMA50 <code>{_price(snapshot.ema50)}</code> · "
        f"EMA200 <code>{_price(snapshot.ema200)}</code>\n"
        f"<i>Автообновление каждые {CHART_REFRESH_SECONDS} с</i>"
    )


def build_chart_rich_html(
    snapshot: MarketChartSnapshot,
    *,
    media_id: str = RICH_MEDIA_ID,
) -> str:
    """Build Telegram's structured rich caption from a snapshot only."""
    direction = "🟢" if snapshot.change_percent >= 0 else "🔴"
    rsi_icon, rsi_text = _rsi_description(snapshot.rsi14)
    daily_price = (
        "н/д" if snapshot.daily_low is None else _price(snapshot.daily_low)
    )
    daily_distance = (
        "н/д"
        if snapshot.daily_low_distance_percent is None
        else _html(_level_relation(snapshot.daily_low_distance_percent))
    )
    return (
        f'<figure><img src="tg://photo?id={_html(media_id)}"/>'
        f"<figcaption><b>{_html(snapshot.symbol)} · "
        f"{_html(snapshot.interval_label)}</b> · "
        f"{snapshot.window_size} показанных закрытых свечей · "
        "EMA 20 / 50 / 200</figcaption></figure>"
        f"<p>{direction} <b>Текущая цена</b> "
        f"<code>{_price(snapshot.current_price)}</code>"
        "<br/>От первой свечи на графике: "
        f"<code>{_percent(snapshot.change_percent)}</code></p>"
        f"<blockquote>{_trend_icon(snapshot)} <b>Тренд по EMA</b>"
        f"<br/>{_html(snapshot.regime_label)}</blockquote>"
        "<h3>Уровни тренда</h3>"
        "<table bordered striped>"
        "<tr><th>Уровень</th><th align=\"right\">Цена</th>"
        "<th align=\"right\">Текущая цена</th></tr>"
        f"<tr><td>EMA20</td><td align=\"right\"><code>{_price(snapshot.ema20)}</code></td>"
        f"<td align=\"right\">{_html(_level_relation(snapshot.ema20_distance_percent))}</td></tr>"
        f"<tr><td>EMA50</td><td align=\"right\"><code>{_price(snapshot.ema50)}</code></td>"
        f"<td align=\"right\">{_html(_level_relation(snapshot.ema50_distance_percent))}</td></tr>"
        f"<tr><td>EMA200</td><td align=\"right\"><code>{_price(snapshot.ema200)}</code></td>"
        f"<td align=\"right\">{_html(_level_relation(snapshot.ema200_distance_percent))}</td></tr>"
        f"<tr><td>Минимум 14 дней</td><td align=\"right\"><code>{daily_price}</code></td>"
        f"<td align=\"right\">{daily_distance}</td></tr>"
        "</table>"
        "<h3>Импульс и волатильность</h3>"
        "<table bordered>"
        "<tr><th>Индикатор</th><th align=\"right\">Значение</th>"
        "<th>Как читать</th></tr>"
        f"<tr><td>RSI14</td><td align=\"right\"><code>{snapshot.rsi14:.1f}</code></td>"
        f"<td>{rsi_icon} {_html(rsi_text)}</td></tr>"
        f"<tr><td>ATR14</td><td align=\"right\"><code>{_price(snapshot.atr14)}</code></td>"
        f"<td>средний диапазон свечи · <code>{snapshot.atr14_percent:.2f}%</code> цены</td></tr>"
        "</table>"
        f"<p><b>Диапазон показанных свечей</b><br/>"
        f"Минимум <code>{_price(snapshot.window_low)}</code> · "
        f"максимум <code>{_price(snapshot.window_high)}</code></p>"
        f"<footer>Автообновление каждые {CHART_REFRESH_SECONDS} с · "
        f"текущая цена: {_html(_utc_time(snapshot.ticker_timestamp_ms))} UTC · "
        f"последняя закрытая свеча: {_html(_utc_time(snapshot.last_closed_at_ms))} UTC"
        "<br/>EMA, RSI и ATR рассчитаны только по закрытым свечам</footer>"
    )


def build_chart_fallback_text(snapshot: MarketChartSnapshot) -> str:
    """Build a complete classic Telegram HTML fallback from the same window."""
    direction = "🟢" if snapshot.change_percent >= 0 else "🔴"
    rsi_icon, rsi_text = _rsi_description(snapshot.rsi14)
    daily = "н/д"
    if (
        snapshot.daily_low is not None
        and snapshot.daily_low_distance_percent is not None
    ):
        daily = (
            f"{format_price(snapshot.daily_low)} · "
            f"цена {_level_relation(snapshot.daily_low_distance_percent)}"
        )
    closes = [candle.close for candle in snapshot.window]
    return (
        f"📈 <b>{_html(snapshot.symbol)} · "
        f"{_html(snapshot.interval_label)}</b>\n"
        f"{direction} <b>Текущая цена</b> "
        f"<code>{_price(snapshot.current_price)}</code>\n"
        "От первой свечи на графике: "
        f"<code>{_percent(snapshot.change_percent)}</code>\n\n"
        f"<pre>{sparkline(closes)}</pre>\n"
        f"{_trend_icon(snapshot)} <b>Тренд по EMA</b>\n"
        f"{_html(snapshot.regime_label)}\n\n"
        "<b>Уровни тренда</b>\n"
        f"EMA20  <code>{_price(snapshot.ema20)}</code> · "
        f"{_html(_level_relation(snapshot.ema20_distance_percent))}\n"
        f"EMA50  <code>{_price(snapshot.ema50)}</code> · "
        f"{_html(_level_relation(snapshot.ema50_distance_percent))}\n"
        f"EMA200 <code>{_price(snapshot.ema200)}</code> · "
        f"{_html(_level_relation(snapshot.ema200_distance_percent))}\n"
        f"Минимум 14 дней <code>{_html(daily)}</code>\n\n"
        "<b>Импульс и волатильность</b>\n"
        f"{rsi_icon} RSI14 <code>{snapshot.rsi14:.1f}</code> · "
        f"{_html(rsi_text)}\n"
        f"🌊 ATR14 <code>{_price(snapshot.atr14)}</code> · "
        f"средний диапазон свечи · <code>{snapshot.atr14_percent:.2f}%</code> цены\n\n"
        "<b>Диапазон показанных свечей</b>\n"
        f"Минимум <code>{_price(snapshot.window_low)}</code> · "
        f"максимум <code>{_price(snapshot.window_high)}</code>\n\n"
        f"<i>Автообновление каждые {CHART_REFRESH_SECONDS} с\n"
        f"Текущая цена: {_html(_utc_time(snapshot.ticker_timestamp_ms))} UTC\n"
        f"Последняя закрытая свеча: {_html(_utc_time(snapshot.last_closed_at_ms))} UTC\n"
        "EMA, RSI и ATR рассчитаны по закрытым свечам</i>"
    )


def _compact_number(value: float) -> str:
    absolute = abs(value)
    if absolute >= 1_000_000_000:
        return f"{value / 1_000_000_000:.1f}B"
    if absolute >= 1_000_000:
        return f"{value / 1_000_000:.1f}M"
    if absolute >= 1_000:
        return f"{value / 1_000:.1f}K"
    return f"{value:.2f}".rstrip("0").rstrip(".")


def _render_chart_png(
    snapshot: MarketChartSnapshot,
) -> bytes:
    """Render a self-contained 1280×720 PNG from a snapshot only."""
    try:
        from matplotlib.figure import Figure
        from matplotlib.patches import Rectangle
        from matplotlib.ticker import FuncFormatter, MaxNLocator
    except ImportError as error:
        raise RuntimeError(
            "Для PNG-графика не установлен matplotlib"
        ) from error

    theme = DARK_THEME
    visible = snapshot.window
    visible_count = snapshot.window_size
    series_offset = len(snapshot.candles) - visible_count
    visible_ema20 = snapshot.ema20_series[series_offset:]
    visible_ema50 = snapshot.ema50_series[series_offset:]
    visible_ema200 = snapshot.ema200_series[series_offset:]
    x_values = list(range(visible_count))

    with MATPLOTLIB_RENDER_LOCK:
        figure = Figure(
            figsize=FIGURE_SIZE_INCHES,
            dpi=FIGURE_DPI,
            facecolor=theme["background"],
        )
        delegated_clear = False
        try:
            layout = figure.add_gridspec(
                5,
                1,
                height_ratios=(1, 1, 1, 1, 0.92),
                hspace=0.03,
                left=0.065,
                right=0.91,
                top=0.875,
                bottom=0.105,
            )
            price_axis = figure.add_subplot(layout[:4, 0])
            volume_axis = figure.add_subplot(layout[4, 0], sharex=price_axis)
            for axis in (price_axis, volume_axis):
                axis.set_facecolor(theme["panel"])
                axis.grid(
                    True,
                    color=theme["grid"],
                    linewidth=0.65,
                    alpha=0.58,
                )
                axis.tick_params(
                    colors=theme["muted"],
                    labelsize=9,
                    length=0,
                )
                for spine in axis.spines.values():
                    spine.set_visible(False)

            visible_low = snapshot.window_low
            visible_high = snapshot.window_high
            price_span = max(
                visible_high - visible_low,
                abs(snapshot.current_price) * 0.002,
                1e-9,
            )
            lower_bound = max(0.0, visible_low - price_span * 0.09)
            upper_bound = visible_high + price_span * 0.11
            body_floor = price_span * 0.0012

            candle_colors: list[str] = []
            for index, candle in enumerate(visible):
                color = (
                    theme["green"]
                    if candle.close >= candle.open
                    else theme["red"]
                )
                candle_colors.append(color)
                price_axis.vlines(
                    index,
                    candle.low,
                    candle.high,
                    color=color,
                    linewidth=1.05,
                    alpha=0.95,
                    zorder=2,
                )
                body_height = max(
                    abs(candle.close - candle.open),
                    body_floor,
                )
                body_bottom = (
                    min(candle.open, candle.close)
                    if abs(candle.close - candle.open) >= body_floor
                    else (candle.open + candle.close) / 2 - body_height / 2
                )
                price_axis.add_patch(
                    Rectangle(
                        (index - 0.31, body_bottom),
                        0.62,
                        body_height,
                        facecolor=color,
                        edgecolor=color,
                        linewidth=0.6,
                        zorder=3,
                    )
                )

            price_axis.plot(
                x_values,
                visible_ema20,
                color=theme["amber"],
                linewidth=1.55,
                label="EMA20",
                zorder=4,
            )
            price_axis.plot(
                x_values,
                visible_ema50,
                color=theme["blue"],
                linewidth=1.55,
                label="EMA50",
                zorder=4,
            )
            price_axis.plot(
                x_values,
                visible_ema200,
                color=theme["cyan"],
                linewidth=1.6,
                label="EMA200",
                zorder=4,
            )

            _draw_daily_low(
                price_axis,
                snapshot,
                lower_bound=lower_bound,
                upper_bound=upper_bound,
                visible_count=visible_count,
            )

            price_axis.set_xlim(-1.1, visible_count + 0.5)
            price_axis.set_ylim(lower_bound, upper_bound)
            price_axis.yaxis.set_major_locator(MaxNLocator(nbins=7))
            price_axis.yaxis.set_major_formatter(
                FuncFormatter(lambda value, _: format_price(float(value)))
            )
            price_axis.yaxis.tick_right()
            price_axis.tick_params(axis="x", labelbottom=False)
            legend = price_axis.legend(
                loc="upper left",
                frameon=False,
                ncol=3,
                fontsize=9,
                handlelength=2.5,
            )
            for label in legend.get_texts():
                label.set_color(theme["foreground"])

            volumes = [candle.volume for candle in visible]
            volume_axis.bar(
                x_values,
                volumes,
                width=0.62,
                color=candle_colors,
                alpha=0.55,
                linewidth=0,
            )
            volume_axis.yaxis.set_major_locator(MaxNLocator(nbins=3))
            volume_axis.yaxis.set_major_formatter(
                FuncFormatter(lambda value, _: _compact_number(float(value)))
            )
            volume_axis.yaxis.tick_right()
            volume_axis.text(
                0.012,
                0.82,
                "VOLUME",
                transform=volume_axis.transAxes,
                color=theme["muted"],
                fontsize=8,
                fontweight="bold",
            )

            tick_count = min(7, visible_count)
            tick_indices = sorted(
                {
                    round(
                        index
                        * (visible_count - 1)
                        / max(1, tick_count - 1)
                    )
                    for index in range(tick_count)
                }
            )
            time_format = {
                "5": "%H:%M",
                "15": "%d %b\n%H:%M",
                "60": "%d %b\n%H:%M",
                "240": "%d %b",
                "D": "%d %b\n%Y",
            }.get(snapshot.interval, "%d %b\n%H:%M")
            volume_axis.set_xticks(
                tick_indices,
                [
                    datetime.fromtimestamp(
                        visible[index].timestamp / 1_000,
                        timezone.utc,
                    ).strftime(time_format)
                    for index in tick_indices
                ],
            )

            figure.text(
                0.065,
                0.945,
                f"{snapshot.symbol}  ·  {snapshot.interval_label}"
                "  ·  ЗАКРЫТЫЕ СВЕЧИ",
                color=theme["foreground"],
                fontsize=16,
                fontweight="bold",
                ha="left",
                va="center",
            )
            change_color = (
                theme["green"]
                if snapshot.change_percent >= 0
                else theme["red"]
            )
            figure.text(
                0.965,
                0.95,
                format_price(snapshot.current_price),
                color=change_color,
                fontsize=16,
                fontweight="bold",
                ha="right",
                va="center",
            )
            figure.text(
                0.965,
                0.915,
                f"{snapshot.change_percent:+.2f}% "
                "от первой свечи на графике",
                color=change_color,
                fontsize=9.5,
                ha="right",
                va="center",
            )
            figure.text(
                0.065,
                0.035,
                "UTC · EMA 20 / 50 / 200 · минимум по 14 закрытым дням",
                color=theme["muted"],
                fontsize=8.5,
                ha="left",
                va="center",
            )
            figure.text(
                0.965,
                0.035,
                f"Цена {_utc_time(snapshot.ticker_timestamp_ms)} UTC · "
                f"свеча закрыта {_utc_time(snapshot.last_closed_at_ms)} UTC",
                color=theme["muted"],
                fontsize=8.2,
                ha="right",
                va="center",
            )
            delegated_clear = True
            return figure_to_png(figure)
        finally:
            if not delegated_clear:
                figure.clear()


def _draw_daily_low(
    axis: object,
    snapshot: MarketChartSnapshot,
    *,
    lower_bound: float,
    upper_bound: float,
    visible_count: int,
) -> None:
    theme = DARK_THEME
    if snapshot.daily_low is None:
        note = "МИНИМУМ 14 ДНЕЙ · Н/Д"
        color = theme["muted"]
    else:
        distance = snapshot.daily_low_distance_percent
        note = (
            f"МИНИМУМ 14 ДНЕЙ {format_price(snapshot.daily_low)} · "
            f"ЦЕНА {_level_relation(distance).upper()}"
        )
        color = theme["violet"]
        if lower_bound <= snapshot.daily_low <= upper_bound:
            axis.axhline(
                snapshot.daily_low,
                color=color,
                linewidth=1.15,
                linestyle=(0, (6, 4)),
                alpha=0.9,
                zorder=1,
            )
            axis.text(
                0.8,
                snapshot.daily_low,
                f" {note} ",
                ha="left",
                va="bottom",
                color=color,
                fontsize=8.5,
                bbox={
                    "facecolor": theme["panel"],
                    "edgecolor": color,
                    "alpha": 0.9,
                    "pad": 2.0,
                },
                zorder=6,
            )
            return
        direction = (
            "↓ вне масштаба"
            if snapshot.daily_low < lower_bound
            else "↑ вне масштаба"
        )
        note = f"{note} · {direction}"
    axis.text(
        0.012,
        0.022,
        note,
        transform=axis.transAxes,
        ha="left",
        va="bottom",
        color=color,
        fontsize=8.8,
        bbox={
            "facecolor": theme["background"],
            "edgecolor": "none",
            "alpha": 0.82,
            "pad": 3.0,
        },
        zorder=7,
    )


def build_chart_payload_from_snapshot(
    snapshot: MarketChartSnapshot,
) -> ChartPayload:
    """Render all outputs independently from one immutable snapshot."""
    text = _summary_text(snapshot)
    fallback_text = build_chart_fallback_text(snapshot)
    rich_html = build_chart_rich_html(snapshot)
    try:
        png = _render_chart_png(snapshot)
    except Exception as error:
        logger.warning(
            f"PNG-график {snapshot.symbol}/{snapshot.interval} недоступен; "
            "используется текстовый fallback: "
            f"{type(error).__name__}"
        )
        png = None
    return ChartPayload(
        text=text,
        fallback_text=fallback_text,
        rich_html=rich_html,
        png=png,
        snapshot=snapshot,
    )


def build_chart_payload(
    bybit: BybitAPI,
    symbol: str,
    interval: str,
) -> ChartPayload:
    """Fetch once, then render PNG and text from the resulting snapshot."""
    return build_chart_payload_from_snapshot(
        build_market_chart_snapshot(bybit, symbol, interval)
    )


def build_chart_text(
    bybit: BybitAPI,
    symbol: str,
    interval: str,
) -> str:
    """Build the complete classic text chart from the canonical snapshot."""
    return build_chart_fallback_text(
        build_market_chart_snapshot(bybit, symbol, interval)
    )


__all__ = [
    "CHART_HISTORY_CANDLES",
    "CHART_REFRESH_SECONDS",
    "CHART_VISIBLE_CANDLES",
    "ChartPayload",
    "MarketCandle",
    "MarketChartSnapshot",
    "RICH_MEDIA_ID",
    "build_chart_fallback_text",
    "build_chart_payload",
    "build_chart_payload_from_snapshot",
    "build_chart_rich_html",
    "build_chart_text",
    "build_market_chart_snapshot",
    "downsample",
    "ema_series",
    "sparkline",
]
