"""Accessible market chart payloads for Telegram's single-message UI.

The PNG renderer is deliberately isolated from Telegram and Bybit transport:
it receives already validated, confirmed candles and returns in-memory bytes.
The existing text chart remains the compatibility and accessibility fallback.
"""

from __future__ import annotations

import io
import math
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from html import escape
from typing import Mapping, Optional, Sequence

from api.bybit_api import BybitAPI
from core.market_data import (
    calculate_atr,
    calculate_ema,
    calculate_rsi,
    get_kline_data,
)
from utils.helpers import format_price, to_float
from utils.logger_setup import logger


SPARK_LEVELS = "▁▂▃▄▅▆▇█"
CHART_HISTORY_CANDLES = 250
CHART_VISIBLE_CANDLES = 120
DAILY_LOW_CANDLES = 14
RICH_MEDIA_ID = "market_chart"
DAILY_LOW_CACHE_SECONDS = 15 * 60
DAILY_LOW_RETRY_SECONDS = 60
_MATPLOTLIB_LOCK = threading.Lock()
_DAILY_LOW_CACHE_LOCK = threading.Lock()
_DAILY_LOW_CACHE: dict[tuple[str, str], tuple[float, Optional[float]]] = {}
_DAILY_LOW_FETCH_LOCKS: dict[tuple[str, str], threading.Lock] = {}


@dataclass(frozen=True, slots=True)
class ChartPayload:
    """One immutable chart render and its accessible text alternatives."""

    text: str
    fallback_text: str
    rich_html: str
    png: Optional[bytes]


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
    """Validate OHLCV invariants and chronological ordering."""
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
        raise ValueError(f"Недостаточно закрытых свечей: {len(validated)} < {minimum}")
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


def _closed_daily_low(candles: Sequence[Mapping[str, object]]) -> Optional[float]:
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
    """Return a cached confirmed level without duplicate concurrent requests."""
    key = (str(getattr(bybit, "base", "")).rstrip("/"), symbol)
    now = time.monotonic()
    with _DAILY_LOW_CACHE_LOCK:
        cached = _DAILY_LOW_CACHE.get(key)
        if cached and cached[0] > now:
            return cached[1]
        fetch_lock = _DAILY_LOW_FETCH_LOCKS.setdefault(key, threading.Lock())

    # A per-market single-flight prevents several Telegram chats from requesting
    # the same daily candles at once.  Re-check after waiting for the leader.
    with fetch_lock:
        now = time.monotonic()
        with _DAILY_LOW_CACHE_LOCK:
            cached = _DAILY_LOW_CACHE.get(key)
            if cached and cached[0] > now:
                return cached[1]
            previous_value = cached[1] if cached else None

        try:
            value = _closed_daily_low(
                get_kline_data(
                    bybit,
                    symbol,
                    "D",
                    DAILY_LOW_CANDLES,
                )
            )
        except Exception as error:
            logger.warning(f"Не удалось обновить 14D low {symbol}: {error}")
            value = None

        # Keep a last-known-good level through a temporary Bybit failure.  The
        # shorter expiry schedules a prompt retry without flattening the chart.
        stored_value = value if value is not None else previous_value
        ttl = (
            DAILY_LOW_CACHE_SECONDS
            if value is not None
            else DAILY_LOW_RETRY_SECONDS
        )
        completed_at = time.monotonic()
        with _DAILY_LOW_CACHE_LOCK:
            _DAILY_LOW_CACHE[key] = (completed_at + ttl, stored_value)
        return stored_value


def _interval_label(interval: str) -> str:
    return {
        "5": "5м",
        "15": "15м",
        "60": "1ч",
        "240": "4ч",
    }.get(str(interval), str(interval))


def _build_chart_text_from_data(
    candles: Sequence[Mapping[str, object]],
    ticker: Mapping[str, object],
    symbol: str,
    interval: str,
    *,
    daily_low: Optional[float] = None,
    updated_ms: Optional[int] = None,
) -> str:
    if len(candles) < 50:
        raise ValueError(f"Недостаточно закрытых свечей {symbol}/{interval}")
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
        int(candles[-1]["closed_at"]) / 1_000,
        timezone.utc,
    ).strftime("%H:%M:%S")
    updated = datetime.fromtimestamp(
        (updated_ms or int(time.time() * 1_000)) / 1_000,
        timezone.utc,
    ).strftime("%H:%M:%S")
    interval_label = _interval_label(interval)
    direction = "🟢" if change >= 0 else "🔴"
    trend = "выше EMA" if current >= ema20 >= ema50 else (
        "ниже EMA" if current <= ema20 <= ema50 else "смешанный"
    )
    daily_line = ""
    if daily_low is not None:
        distance = (current / daily_low - 1) * 100
        daily_line = (
            f"14D low <code>{format_price(daily_low)}</code> · "
            f"цена <code>{distance:+.2f}%</code>\n"
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
        f"{daily_line}"
        f"RSI14 <code>{rsi:.1f}</code> · ATR14 <code>{format_price(atr)}</code>\n"
        f"Тренд: <b>{trend}</b> · spread <code>{spread:.3f}%</code>\n\n"
        f"<i>Последняя закрытая свеча {closed_at} UTC · цена {updated} UTC</i>"
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
    candles: Sequence[Mapping[str, object]],
    *,
    symbol: str,
    interval: str,
    current_price: float,
    daily_low: Optional[float],
    updated_ms: int,
) -> bytes:
    """Render a Telegram-friendly PNG using Matplotlib's headless Agg canvas."""
    try:
        from matplotlib.backends.backend_agg import FigureCanvasAgg
        from matplotlib.figure import Figure
        from matplotlib.patches import Rectangle
        from matplotlib.ticker import FuncFormatter, MaxNLocator
    except ImportError as error:
        raise RuntimeError("Для PNG-графика не установлен matplotlib") from error

    closes = [float(candle["close"]) for candle in candles]
    ema20 = ema_series(closes, 20)
    ema50 = ema_series(closes, 50)
    visible_count = min(CHART_VISIBLE_CANDLES, len(candles))
    visible = list(candles[-visible_count:])
    visible_ema20 = ema20[-visible_count:]
    visible_ema50 = ema50[-visible_count:]
    x_values = list(range(visible_count))

    background = "#08111F"
    panel = "#0D1728"
    grid = "#243247"
    foreground = "#E5ECF5"
    muted = "#8EA0B8"
    green = "#21C784"
    red = "#F05A67"
    cyan = "#32C7E6"
    amber = "#F4B942"
    blue = "#6EA8FE"
    violet = "#B58BFA"

    with _MATPLOTLIB_LOCK:
        figure = Figure(figsize=(12.8, 7.2), dpi=100, facecolor=background)
        canvas = FigureCanvasAgg(figure)
        grid_spec = figure.add_gridspec(
            5,
            1,
            height_ratios=(1, 1, 1, 1, 0.92),
            hspace=0.03,
            left=0.065,
            right=0.91,
            top=0.875,
            bottom=0.105,
        )
        price_axis = figure.add_subplot(grid_spec[:4, 0])
        volume_axis = figure.add_subplot(grid_spec[4, 0], sharex=price_axis)
        for axis in (price_axis, volume_axis):
            axis.set_facecolor(panel)
            axis.grid(True, color=grid, linewidth=0.65, alpha=0.58)
            axis.tick_params(colors=muted, labelsize=9, length=0)
            for spine in axis.spines.values():
                spine.set_visible(False)

        visible_low = min(
            min(float(candle["low"]) for candle in visible),
            current_price,
        )
        visible_high = max(
            max(float(candle["high"]) for candle in visible),
            current_price,
        )
        price_span = max(
            visible_high - visible_low,
            abs(current_price) * 0.002,
            1e-9,
        )
        lower_bound = max(0.0, visible_low - price_span * 0.09)
        upper_bound = visible_high + price_span * 0.11
        body_floor = price_span * 0.0012

        candle_colors: list[str] = []
        for index, candle in enumerate(visible):
            open_price = float(candle["open"])
            high = float(candle["high"])
            low = float(candle["low"])
            close = float(candle["close"])
            color = green if close >= open_price else red
            candle_colors.append(color)
            price_axis.vlines(
                index,
                low,
                high,
                color=color,
                linewidth=1.05,
                alpha=0.95,
                zorder=2,
            )
            body_height = max(abs(close - open_price), body_floor)
            body_bottom = (
                min(open_price, close)
                if abs(close - open_price) >= body_floor
                else ((open_price + close) / 2 - body_height / 2)
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
            color=amber,
            linewidth=1.55,
            label="EMA20",
            zorder=4,
        )
        price_axis.plot(
            x_values,
            visible_ema50,
            color=blue,
            linewidth=1.55,
            label="EMA50",
            zorder=4,
        )
        price_axis.axhline(
            current_price,
            color=cyan,
            linewidth=1.0,
            linestyle=(0, (2, 3)),
            alpha=0.95,
            zorder=1,
        )
        price_axis.text(
            visible_count - 0.4,
            current_price,
            f" LAST {format_price(current_price)} ",
            ha="right",
            va="bottom",
            color=background,
            fontsize=8.5,
            fontweight="bold",
            bbox={"facecolor": cyan, "edgecolor": "none", "pad": 2.0},
            zorder=6,
        )

        bottom_low_note: Optional[str]
        if daily_low is None:
            bottom_low_note = "14D LOW · временно недоступен"
        else:
            distance = (current_price / daily_low - 1) * 100
            low_note = (
                f"14D LOW {format_price(daily_low)} · "
                f"цена {distance:+.2f}%"
            )
            if lower_bound <= daily_low <= upper_bound:
                price_axis.axhline(
                    daily_low,
                    color=violet,
                    linewidth=1.15,
                    linestyle=(0, (6, 4)),
                    alpha=0.9,
                    zorder=1,
                )
                price_axis.text(
                    0.8,
                    daily_low,
                    f" {low_note} ",
                    ha="left",
                    va="bottom",
                    color=violet,
                    fontsize=8.5,
                    bbox={
                        "facecolor": panel,
                        "edgecolor": violet,
                        "alpha": 0.9,
                        "pad": 2.0,
                    },
                    zorder=5,
                )
                bottom_low_note = None
            else:
                direction = "↓ вне масштаба" if daily_low < lower_bound else "↑ вне масштаба"
                bottom_low_note = f"{low_note} · {direction}"
        if bottom_low_note:
            price_axis.text(
                0.012,
                0.022,
                bottom_low_note,
                transform=price_axis.transAxes,
                ha="left",
                va="bottom",
                color=violet if daily_low is not None else muted,
                fontsize=8.8,
                bbox={
                    "facecolor": background,
                    "edgecolor": "none",
                    "alpha": 0.82,
                    "pad": 3.0,
                },
                zorder=7,
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
            ncol=2,
            fontsize=9,
            handlelength=2.5,
        )
        for label in legend.get_texts():
            label.set_color(foreground)

        volumes = [float(candle["volume"]) for candle in visible]
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
            color=muted,
            fontsize=8,
            fontweight="bold",
        )

        tick_count = min(7, visible_count)
        tick_indices = sorted(
            {
                round(index * (visible_count - 1) / max(1, tick_count - 1))
                for index in range(tick_count)
            }
        )
        span_ms = int(visible[-1]["timestamp"]) - int(visible[0]["timestamp"])
        time_format = "%H:%M" if span_ms < 2 * 86_400_000 else "%d %b\n%H:%M"
        tick_labels = [
            datetime.fromtimestamp(
                int(visible[index]["timestamp"]) / 1_000,
                timezone.utc,
            ).strftime(time_format)
            for index in tick_indices
        ]
        volume_axis.set_xticks(tick_indices, tick_labels)

        first_visible = float(visible[0]["close"])
        change = (
            (current_price / first_visible - 1) * 100
            if first_visible > 0
            else 0.0
        )
        interval_label = _interval_label(interval)
        figure.text(
            0.065,
            0.945,
            f"{symbol}  ·  {interval_label}  ·  ЗАКРЫТЫЕ СВЕЧИ",
            color=foreground,
            fontsize=16,
            fontweight="bold",
            ha="left",
            va="center",
        )
        figure.text(
            0.965,
            0.95,
            format_price(current_price),
            color=green if change >= 0 else red,
            fontsize=16,
            fontweight="bold",
            ha="right",
            va="center",
        )
        figure.text(
            0.965,
            0.915,
            f"{change:+.2f}% за {visible_count} свечей",
            color=green if change >= 0 else red,
            fontsize=9.5,
            ha="right",
            va="center",
        )
        updated = datetime.fromtimestamp(
            updated_ms / 1_000,
            timezone.utc,
        ).strftime("%d.%m.%Y %H:%M:%S")
        figure.text(
            0.065,
            0.035,
            "UTC · EMA по закрытиям · 14D Low по 14 закрытым дневным свечам",
            color=muted,
            fontsize=8.5,
            ha="left",
            va="center",
        )
        figure.text(
            0.965,
            0.035,
            f"Обновлено {updated} UTC",
            color=muted,
            fontsize=8.5,
            ha="right",
            va="center",
        )

        output = io.BytesIO()
        canvas.print_png(output, metadata={"Software": "Crypto trading bot"})
        png = output.getvalue()
        figure.clear()
    if not png.startswith(b"\x89PNG\r\n\x1a\n"):
        raise RuntimeError("Matplotlib не создал корректный PNG")
    if len(png) > 8 * 1024 * 1024:
        raise RuntimeError("PNG-график превышает безопасный размер Telegram")
    return png


def _summary_text(
    candles: Sequence[Mapping[str, object]],
    *,
    symbol: str,
    interval: str,
    current: float,
    daily_low: Optional[float],
    updated_ms: int,
) -> str:
    closes = [float(candle["close"]) for candle in candles]
    ema20 = ema_series(closes, 20)[-1]
    ema50 = ema_series(closes, 50)[-1]
    if ema20 is None or ema50 is None:
        raise ValueError("Недостаточно свечей для EMA20/EMA50")
    first = float(candles[-min(CHART_VISIBLE_CANDLES, len(candles))]["close"])
    change = (current / first - 1) * 100 if first else 0.0
    daily_text = "временно недоступен"
    if daily_low is not None:
        distance = (current / daily_low - 1) * 100
        daily_text = f"{format_price(daily_low)} · цена {distance:+.2f}%"
    updated = datetime.fromtimestamp(
        updated_ms / 1_000,
        timezone.utc,
    ).strftime("%H:%M:%S")
    return (
        f"📈 <b>{escape(symbol)} · {_interval_label(interval)}</b>\n"
        f"Цена <code>{format_price(current)}</code> · "
        f"<code>{change:+.2f}%</code>\n"
        f"EMA20 <code>{format_price(ema20)}</code> · "
        f"EMA50 <code>{format_price(ema50)}</code>\n"
        f"14D low <code>{daily_text}</code>\n"
        f"<i>Обновлено {updated} UTC · индикаторы по закрытым свечам</i>"
    )


def build_chart_payload(
    bybit: BybitAPI,
    symbol: str,
    interval: str,
) -> ChartPayload:
    """Fetch one coherent market snapshot and render PNG plus text fallback."""
    symbol = symbol.upper()
    candles = _validated_candles(
        get_kline_data(
            bybit,
            symbol,
            interval,
            CHART_HISTORY_CANDLES,
        ),
        minimum=50,
    )
    ticker_response = bybit.get_tickers(symbol)
    ticker = _ticker(ticker_response, symbol)
    current = to_float(ticker.get("lastPrice"))
    updated_ms = int(ticker_response.get("time") or time.time() * 1_000)
    if updated_ms <= 0:
        updated_ms = int(time.time() * 1_000)

    # The daily level is useful context, but never mandatory for the chart.
    # get_kline_data returns only confirmed candles, so this cannot repaint
    # during the current UTC day.
    daily_low = _cached_daily_low(bybit, symbol)
    text = _summary_text(
        candles,
        symbol=symbol,
        interval=interval,
        current=current,
        daily_low=daily_low,
        updated_ms=updated_ms,
    )
    fallback_text = _build_chart_text_from_data(
        candles[-80:],
        ticker,
        symbol,
        interval,
        daily_low=daily_low,
        updated_ms=updated_ms,
    )
    png: Optional[bytes]
    try:
        png = _render_chart_png(
            candles,
            symbol=symbol,
            interval=interval,
            current_price=current,
            daily_low=daily_low,
            updated_ms=updated_ms,
        )
    except Exception as error:
        # Text data is already complete and remains useful when Matplotlib is
        # unavailable or Telegram's image limit cannot be met.
        logger.warning(
            f"PNG-график {symbol}/{interval} недоступен; "
            f"используется текстовый fallback: {type(error).__name__}"
        )
        png = None
    safe_symbol = escape(symbol)
    safe_interval = escape(_interval_label(interval))
    rich_html = (
        f'<figure><img src="tg://photo?id={RICH_MEDIA_ID}"/>'
        f"<figcaption><b>{safe_symbol} · {safe_interval}</b> · "
        "точные закрытые свечи</figcaption></figure>"
        f"<p>{text.replace(chr(10), '<br>')}</p>"
    )
    return ChartPayload(
        text=text,
        fallback_text=fallback_text,
        rich_html=rich_html,
        png=png,
    )


def build_chart_text(bybit: BybitAPI, symbol: str, interval: str) -> str:
    """Build the original lightweight text-only chart."""
    candles = get_kline_data(bybit, symbol, interval, 80)
    ticker_response = bybit.get_tickers(symbol)
    ticker = _ticker(ticker_response, symbol)
    return _build_chart_text_from_data(
        candles,
        ticker,
        symbol,
        interval,
        updated_ms=int(ticker_response.get("time") or time.time() * 1_000),
    )
