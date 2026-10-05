"""Immutable data and rich rendering for the live Bybit asset monitor.

The module keeps exchange collection, presentation, and Matplotlib rendering
separate enough that every representation is built from the same snapshot.
Missing Bybit fields remain ``None`` instead of becoming plausible zeroes.
"""

from __future__ import annotations

import math
import re
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from html import escape
from typing import Any, Mapping, Optional, Sequence

from core.market_data import get_kline_data
from core.rich_charts import (
    DARK_THEME,
    FIGURE_PIXEL_SIZE,
    MATPLOTLIB_RENDER_LOCK,
    figure_to_png,
)
from utils.logger_setup import logger


MARKET_REPORT_MEDIA_ID = "market_rsi_history"
MAX_MARKET_ASSETS = 6
_MEDIA_ID_PATTERN = re.compile(r"[A-Za-z0-9_-]{1,64}")
_TOKEN_PATTERN = re.compile(r"[A-Z0-9]{2,15}")
_SYMBOL_PATTERN = re.compile(r"[A-Z0-9]{4,24}")


def _decimal_or_none(value: Any) -> Optional[Decimal]:
    if value is None or value == "" or isinstance(value, bool):
        return None
    try:
        number = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None
    if not number.is_finite():
        return None
    try:
        converted = float(number)
    except (OverflowError, ValueError):
        return None
    return number if math.isfinite(converted) else None


def _positive_decimal_or_none(value: Any) -> Optional[Decimal]:
    number = _decimal_or_none(value)
    return number if number is not None and number > 0 else None


def _validated_decimal(
    value: Optional[Decimal],
    *,
    field: str,
    positive: bool = False,
    non_negative: bool = False,
) -> None:
    if value is None:
        return
    if not isinstance(value, Decimal) or not value.is_finite():
        raise ValueError(f"{field} must be a finite Decimal or None")
    try:
        finite_float = math.isfinite(float(value))
    except (OverflowError, ValueError):
        finite_float = False
    if not finite_float:
        raise ValueError(f"{field} is outside the supported plotting range")
    if positive and value <= 0:
        raise ValueError(f"{field} must be positive")
    if non_negative and value < 0:
        raise ValueError(f"{field} must be non-negative")


@dataclass(frozen=True, slots=True)
class MarketAssetSnapshot:
    """One configured asset with exact nullable exchange values and RSI history."""

    token: str
    symbol: str
    last_price: Optional[Decimal]
    change_24h_percent: Optional[Decimal]
    rsi_1h: Optional[Decimal]
    error: Optional[str] = None
    rsi_timestamps_ms: tuple[int, ...] = ()
    rsi_series: tuple[Decimal, ...] = ()

    def __post_init__(self) -> None:
        if not _TOKEN_PATTERN.fullmatch(self.token):
            raise ValueError("token has an invalid value")
        if not _SYMBOL_PATTERN.fullmatch(self.symbol):
            raise ValueError("symbol has an invalid value")
        _validated_decimal(
            self.last_price,
            field="last_price",
            positive=True,
        )
        _validated_decimal(
            self.change_24h_percent,
            field="change_24h_percent",
        )
        _validated_decimal(
            self.rsi_1h,
            field="rsi_1h",
            non_negative=True,
        )
        if self.rsi_1h is not None and self.rsi_1h > 100:
            raise ValueError("rsi_1h must be in the 0..100 range")
        if not isinstance(self.rsi_timestamps_ms, tuple):
            raise ValueError("rsi_timestamps_ms must be an immutable tuple")
        if not isinstance(self.rsi_series, tuple):
            raise ValueError("rsi_series must be an immutable tuple")
        if len(self.rsi_timestamps_ms) != len(self.rsi_series):
            raise ValueError("RSI timestamps and values must have the same length")
        if len(self.rsi_series) > 1_000:
            raise ValueError("RSI history exceeds the safe size")
        previous_timestamp = 0
        for index, timestamp in enumerate(self.rsi_timestamps_ms):
            if (
                isinstance(timestamp, bool)
                or not isinstance(timestamp, int)
                or timestamp <= previous_timestamp
            ):
                raise ValueError("RSI timestamps must be positive and increasing")
            try:
                datetime.fromtimestamp(timestamp / 1_000, tz=timezone.utc)
            except (OSError, OverflowError, ValueError) as error:
                raise ValueError(
                    f"rsi_timestamps_ms[{index}] is outside the UTC range"
                ) from error
            previous_timestamp = timestamp
        for index, value in enumerate(self.rsi_series):
            _validated_decimal(
                value,
                field=f"rsi_series[{index}]",
                non_negative=True,
            )
            if value > 100:
                raise ValueError("RSI history values must be in the 0..100 range")
        if self.rsi_series:
            if self.rsi_1h is None:
                raise ValueError("rsi_1h is required when RSI history is present")
            if self.rsi_1h != self.rsi_series[-1]:
                raise ValueError("rsi_1h must equal the final RSI history value")
        if self.error is not None and (
            not self.error
            or len(self.error) > 160
            or any(ord(character) < 32 for character in self.error)
        ):
            raise ValueError("error has an invalid value")

    @property
    def is_chartable(self) -> bool:
        """Return whether the asset has enough honest data for an RSI line."""
        return self.rsi_1h is not None and len(self.rsi_series) >= 2


@dataclass(frozen=True, slots=True)
class MarketReportSnapshot:
    """A coherent, common-time report over at most six configured assets."""

    assets: tuple[MarketAssetSnapshot, ...]
    captured_at_ms: int
    requested_count: int
    error_count: int
    omitted_count: int = 0

    def __post_init__(self) -> None:
        if not isinstance(self.assets, tuple):
            raise ValueError("assets must be an immutable tuple")
        if len(self.assets) > MAX_MARKET_ASSETS:
            raise ValueError("market report contains too many assets")
        if isinstance(self.requested_count, bool) or not isinstance(
            self.requested_count,
            int,
        ):
            raise ValueError("requested_count must be an integer")
        if isinstance(self.error_count, bool) or not isinstance(
            self.error_count,
            int,
        ):
            raise ValueError("error_count must be an integer")
        if isinstance(self.omitted_count, bool) or not isinstance(
            self.omitted_count,
            int,
        ):
            raise ValueError("omitted_count must be an integer")
        if self.omitted_count < 0:
            raise ValueError("omitted_count must be non-negative")
        if self.requested_count != len(self.assets):
            raise ValueError("requested_count does not match assets")
        if self.error_count != sum(asset.error is not None for asset in self.assets):
            raise ValueError("error_count does not match asset errors")
        if (
            isinstance(self.captured_at_ms, bool)
            or not isinstance(self.captured_at_ms, int)
            or self.captured_at_ms <= 0
        ):
            raise ValueError("captured_at_ms must be positive")
        try:
            datetime.fromtimestamp(
                self.captured_at_ms / 1_000,
                tz=timezone.utc,
            )
        except (OSError, OverflowError, ValueError) as error:
            raise ValueError("captured_at_ms is outside the UTC range") from error

    @property
    def chart_assets(self) -> tuple[MarketAssetSnapshot, ...]:
        return tuple(asset for asset in self.assets if asset.is_chartable)

    @property
    def valid_asset_count(self) -> int:
        return len(self.chart_assets)

    @property
    def configured_count(self) -> int:
        return self.requested_count + self.omitted_count


def _ticker_row(response: Any, symbol: str) -> Mapping[str, Any]:
    if not isinstance(response, Mapping):
        raise ValueError("ticker response is not a mapping")
    result = response.get("result")
    rows = result.get("list") if isinstance(result, Mapping) else None
    if not isinstance(rows, list) or not rows:
        raise ValueError("ticker response has no rows")
    row = next(
        (
            item
            for item in rows
            if isinstance(item, Mapping)
            and str(item.get("symbol") or "").upper() == symbol
        ),
        None,
    )
    if row is None and len(rows) == 1 and isinstance(rows[0], Mapping):
        row = rows[0]
    if not isinstance(row, Mapping):
        raise ValueError(f"ticker response has no {symbol} row")
    return row


def _calculate_rsi_history(
    candles: Sequence[Mapping[str, Any]],
    period: int = 14,
) -> tuple[tuple[int, ...], tuple[Decimal, ...]]:
    """Calculate Wilder RSI once and align every point with its candle close."""
    if len(candles) < period + 1:
        return (), ()

    closes: list[float] = []
    closed_at_values: list[int] = []
    previous_closed_at = 0
    for candle in candles:
        close = float(candle["close"])
        closed_at = int(candle["closed_at"])
        if not math.isfinite(close) or close <= 0:
            raise ValueError("candle close must be finite and positive")
        if closed_at <= previous_closed_at:
            raise ValueError("candle close timestamps must be positive and increasing")
        closes.append(close)
        closed_at_values.append(closed_at)
        previous_closed_at = closed_at

    deltas = [
        later - earlier
        for earlier, later in zip(closes, closes[1:])
    ]
    gains = [max(delta, 0.0) for delta in deltas]
    losses = [max(-delta, 0.0) for delta in deltas]
    average_gain = sum(gains[:period]) / period
    average_loss = sum(losses[:period]) / period

    def current_rsi() -> Decimal:
        if average_loss == 0:
            value = 50.0 if average_gain == 0 else 100.0
        else:
            relative_strength = average_gain / average_loss
            value = 100.0 - (100.0 / (1.0 + relative_strength))
        result = _decimal_or_none(value)
        if result is None or not Decimal("0") <= result <= Decimal("100"):
            raise ValueError("calculated RSI is outside the 0..100 range")
        return result

    values = [current_rsi()]
    for index in range(period, len(deltas)):
        average_gain = (
            average_gain * (period - 1) + gains[index]
        ) / period
        average_loss = (
            average_loss * (period - 1) + losses[index]
        ) / period
        values.append(current_rsi())

    timestamps = tuple(closed_at_values[period:])
    series = tuple(values)
    if len(timestamps) != len(series):
        raise RuntimeError("RSI history alignment failed")
    return timestamps, series


def _collect_asset(bybit: Any, token: str) -> MarketAssetSnapshot:
    symbol = f"{token}USDT"
    issues: list[str] = []
    last_price: Optional[Decimal] = None
    change_24h_percent: Optional[Decimal] = None

    try:
        ticker = _ticker_row(bybit.get_tickers(symbol), symbol)
    except Exception as error:
        logger.warning(
            f"Не удалось получить market-report ticker {symbol} "
            f"({type(error).__name__})"
        )
        issues.append("цена и изменение за 24ч недоступны")
    else:
        last_price = _positive_decimal_or_none(ticker.get("lastPrice"))
        if last_price is None:
            issues.append("цена недоступна")

        raw_change = _decimal_or_none(ticker.get("price24hPcnt"))
        change_24h_percent = (
            raw_change * Decimal("100")
            if raw_change is not None
            else None
        )
        if change_24h_percent is None:
            issues.append("изменение за 24ч недоступно")

    rsi_1h: Optional[Decimal] = None
    rsi_timestamps_ms: tuple[int, ...] = ()
    rsi_series: tuple[Decimal, ...] = ()
    try:
        candles = get_kline_data(bybit, symbol, "60", 120)
        if len(candles) < 50:
            raise ValueError(f"получено только {len(candles)} закрытых свечей")
        rsi_timestamps_ms, rsi_series = _calculate_rsi_history(candles)
        if not rsi_series:
            raise ValueError("история RSI пуста")
        rsi_1h = rsi_series[-1]
    except Exception as error:
        logger.warning(
            f"Не удалось получить market-report RSI {symbol} "
            f"({type(error).__name__})"
        )
        issues.append("RSI 1ч недоступен")

    return MarketAssetSnapshot(
        token=token,
        symbol=symbol,
        last_price=last_price,
        change_24h_percent=change_24h_percent,
        rsi_1h=rsi_1h,
        rsi_timestamps_ms=rsi_timestamps_ms,
        rsi_series=rsi_series,
        error="; ".join(dict.fromkeys(issues)) or None,
    )


def collect_market_report(
    bybit: Any,
    tokens: Sequence[str],
    *,
    captured_at_ms: Optional[int] = None,
) -> MarketReportSnapshot:
    """Collect up to six assets without turning incomplete fields into zeroes."""
    configured: list[str] = []
    for raw_token in tokens:
        token = str(raw_token).strip().upper()
        if not token:
            continue
        configured.append(token)
    selected = configured[:MAX_MARKET_ASSETS]

    assets = tuple(_collect_asset(bybit, token) for token in selected)
    if captured_at_ms is not None and (
        isinstance(captured_at_ms, bool)
        or not isinstance(captured_at_ms, int)
    ):
        raise ValueError("captured_at_ms must be an integer")
    timestamp = int(
        time.time() * 1_000
        if captured_at_ms is None
        else captured_at_ms
    )
    return MarketReportSnapshot(
        assets=assets,
        captured_at_ms=timestamp,
        requested_count=len(assets),
        error_count=sum(asset.error is not None for asset in assets),
        omitted_count=max(0, len(configured) - len(selected)),
    )


def _format_price(value: Optional[Decimal]) -> str:
    if value is None:
        return "—"
    absolute = abs(value)
    decimals = 2 if absolute >= 1_000 else 4 if absolute >= 1 else 6
    return f"${value:,.{decimals}f}"


def _format_percent(
    value: Optional[Decimal],
    *,
    places: int = 2,
    signed: bool = False,
) -> str:
    if value is None:
        return "—"
    if abs(value) < Decimal("0.5") * (Decimal("10") ** -places):
        return f"{Decimal('0'):.{places}f}%"
    sign = "+" if signed else ""
    return f"{value:{sign}.{places}f}%"


def _format_rsi(value: Optional[Decimal]) -> str:
    return "—" if value is None else f"{value:.1f}"


def _rsi_state(value: Optional[Decimal]) -> str:
    """Describe the oscillator without inventing a signal for missing data."""
    if value is None:
        return "⚪ нет данных"
    if value <= 30:
        return "🔵 перепроданность"
    if value < 45:
        return "🔻 продавцы сильнее"
    if value <= 55:
        return "⚪ баланс"
    if value < 70:
        return "🔺 покупатели сильнее"
    if value >= 70:
        return "🟠 перекупленность"
    raise AssertionError("validated RSI must be in the 0..100 range")


def _rsi_summary(snapshot: MarketReportSnapshot) -> Optional[str]:
    values = [
        asset.rsi_1h
        for asset in snapshot.assets
        if asset.rsi_1h is not None
    ]
    if not values:
        return None
    above_midline = sum(value > 50 for value in values)
    extremes = sum(value <= 30 or value >= 70 for value in values)
    return (
        f"выше 50 — <code>{above_midline}/{len(values)}</code> · "
        f"в крайних зонах — <code>{extremes}</code>"
    )


def _captured_at(snapshot: MarketReportSnapshot) -> str:
    return datetime.fromtimestamp(
        snapshot.captured_at_ms / 1_000,
        timezone.utc,
    ).strftime("%d.%m.%Y %H:%M:%S")


def format_market_report_text(snapshot: MarketReportSnapshot) -> str:
    """Return a compact classic Telegram-HTML view of the same snapshot."""
    lines = [
        "📊 <b>Монитор активов</b>",
        "<i>Автообновление каждую минуту</i>",
    ]
    if snapshot.omitted_count:
        lines.extend(
            [
                "",
                (
                    f"ℹ️ <i>Показаны первые {snapshot.requested_count} из "
                    f"{snapshot.configured_count} настроенных активов.</i>"
                ),
            ]
        )
    if snapshot.error_count:
        problem_tokens = ", ".join(
            escape(asset.token)
            for asset in snapshot.assets
            if asset.error is not None
        )
        lines.extend(
            [
                "",
                f"⚠️ <i>Неполные данные: {problem_tokens or 'источник недоступен'}.</i>",
            ]
        )
    if not snapshot.assets:
        lines.extend(["", "⚠️ Настроенные активы отсутствуют."])

    summary = _rsi_summary(snapshot)
    if summary:
        lines.extend(["", f"<b>Сводка RSI:</b> {summary}"])

    for asset in snapshot.assets:
        lines.extend(
            [
                "",
                (
                    f"<b>{escape(asset.symbol)}</b>\n"
                    f"Цена <code>{_format_price(asset.last_price)}</code> · 24ч "
                    f"<code>{_format_percent(asset.change_24h_percent, signed=True)}</code>"
                ),
                (
                    f"RSI14 · 1ч <code>{_format_rsi(asset.rsi_1h)}</code> · "
                    f"{_rsi_state(asset.rsi_1h)}"
                ),
            ]
        )

    lines.extend(
        [
            "",
            (
                f"<i>Обновлено {_captured_at(snapshot)} UTC\n"
                "RSI14 рассчитан по закрытым часовым свечам</i>"
            ),
        ]
    )
    return "\n".join(lines)


def format_market_report_rich_html(
    snapshot: MarketReportSnapshot,
    *,
    media_id: str = MARKET_REPORT_MEDIA_ID,
) -> str:
    """Return structured Telegram Rich HTML for a confirmed PNG report."""
    if snapshot.valid_asset_count < 2:
        raise ValueError("At least two valid assets are required for rich market HTML")
    if not _MEDIA_ID_PATTERN.fullmatch(media_id):
        raise ValueError("media_id has an invalid value")

    overview_rows = []
    for asset in snapshot.assets:
        safe_symbol = escape(asset.symbol)
        overview_rows.append(
            "<tr>"
            f"<td><b>{safe_symbol}</b></td>"
            f'<td align="right"><code>{_format_price(asset.last_price)}</code></td>'
            f'<td align="right"><code>'
            f"{_format_percent(asset.change_24h_percent, signed=True)}"
            "</code></td>"
            f"<td><code>{_format_rsi(asset.rsi_1h)}</code> "
            f"{escape(_rsi_state(asset.rsi_1h))}</td>"
            "</tr>"
        )

    availability = ""
    if snapshot.error_count:
        availability = (
            f" · история доступна для {snapshot.valid_asset_count}/"
            f"{snapshot.requested_count} активов"
        )
    scope = ""
    if snapshot.omitted_count:
        scope = (
            f" · показаны первые {snapshot.requested_count} из "
            f"{snapshot.configured_count}"
        )
    summary = _rsi_summary(snapshot)
    summary_html = (
        f"<p><b>Сводка RSI:</b> {summary}</p>"
        if summary
        else ""
    )

    return (
        "<h3>📊 Монитор активов</h3>"
        f'<figure><img src="tg://photo?id={media_id}"/>'
        f"<figcaption>История RSI14 · закрытые часовые свечи"
        f"{availability}{scope}"
        "</figcaption></figure>"
        f"{summary_html}"
        "<table bordered striped>"
        "<tr><th>Актив</th><th>Цена</th><th>24ч</th><th>RSI14 · 1ч</th></tr>"
        f"{''.join(overview_rows)}"
        "</table>"
        f"<footer>Обновлено {_captured_at(snapshot)} UTC · автоматически раз в 60с · "
        "RSI14 по закрытым часовым свечам</footer>"
    )


def render_market_report_png(snapshot: MarketReportSnapshot) -> bytes:
    """Render exact 1280×720 RSI mini-panels for two to six assets."""
    assets = snapshot.chart_assets
    if len(assets) < 2:
        raise ValueError("At least two valid assets are required for a PNG report")

    try:
        from matplotlib.dates import DateFormatter, HourLocator
        from matplotlib.figure import Figure
    except ImportError as error:
        raise RuntimeError("Matplotlib is required for the market report") from error

    theme = DARK_THEME
    width, height = FIGURE_PIXEL_SIZE
    dpi = 100
    palette = (
        theme["cyan"],
        theme["amber"],
        theme["green"],
        theme["violet"],
        theme["blue"],
        theme["red"],
    )
    histories = []
    all_moments: list[datetime] = []
    for asset, color in zip(assets, palette):
        moments = tuple(
            datetime.fromtimestamp(timestamp / 1_000, tz=timezone.utc)
            for timestamp in asset.rsi_timestamps_ms
        )
        values = tuple(float(value) for value in asset.rsi_series)
        histories.append((asset, color, moments, values))
        all_moments.extend(moments)

    earliest = min(all_moments)
    latest = max(all_moments)
    if earliest >= latest:
        raise ValueError("RSI history must span more than one timestamp")
    time_padding = max(
        (latest - earliest) * 0.018,
        timedelta(minutes=20),
    )
    span_hours = (latest - earliest).total_seconds() / 3_600
    tick_interval_hours = 6 if span_hours <= 54 else 12

    with MATPLOTLIB_RENDER_LOCK:
        figure = Figure(
            figsize=(width / dpi, height / dpi),
            dpi=dpi,
            facecolor=theme["background"],
        )
        delegated_clear = False
        try:
            layout = figure.add_gridspec(
                len(histories),
                1,
                left=0.075,
                right=0.95,
                top=0.82,
                bottom=0.14,
                hspace=0.08,
            )
            axes = []
            for index, (asset, color, moments, values) in enumerate(histories):
                rsi_axis = figure.add_subplot(
                    layout[index, 0],
                    sharex=axes[0] if axes else None,
                )
                axes.append(rsi_axis)
                rsi_axis.set_facecolor(theme["panel"])
                rsi_axis.set_axisbelow(True)
                for spine in rsi_axis.spines.values():
                    spine.set_visible(False)
                rsi_axis.axhspan(
                    0,
                    30,
                    color=theme["cyan"],
                    alpha=0.07,
                    zorder=0,
                )
                rsi_axis.axhspan(
                    70,
                    100,
                    color=theme["amber"],
                    alpha=0.07,
                    zorder=0,
                )
                rsi_axis.grid(
                    True,
                    axis="x",
                    color=theme["grid"],
                    linewidth=0.65,
                    alpha=0.46,
                )
                for threshold, threshold_color in (
                    (30, theme["cyan"]),
                    (50, theme["muted"]),
                    (70, theme["amber"]),
                ):
                    rsi_axis.axhline(
                        threshold,
                        color=threshold_color,
                        linewidth=0.75,
                        linestyle="--" if threshold != 50 else ":",
                        alpha=0.55,
                        zorder=1,
                    )
                rsi_axis.plot(
                    moments,
                    values,
                    color=color,
                    linewidth=1.75,
                    alpha=0.94,
                    solid_capstyle="round",
                    zorder=3,
                )
                rsi_axis.scatter(
                    [moments[-1]],
                    [values[-1]],
                    s=49,
                    color=theme["panel"],
                    edgecolors=color,
                    linewidths=1.8,
                    clip_on=False,
                    zorder=5,
                )
                rsi_axis.scatter(
                    [moments[-1]],
                    [values[-1]],
                    s=18,
                    color=color,
                    clip_on=False,
                    zorder=6,
                )
                rsi_axis.text(
                    0.012,
                    0.80,
                    asset.token,
                    transform=rsi_axis.transAxes,
                    color=theme["foreground"],
                    fontsize=9.5,
                    fontweight="bold",
                    ha="left",
                    va="top",
                    bbox={
                        "boxstyle": "round,pad=0.22",
                        "facecolor": theme["background"],
                        "edgecolor": "none",
                        "alpha": 0.78,
                    },
                    zorder=7,
                )
                rsi_axis.text(
                    0.988,
                    0.80,
                    f"{values[-1]:.1f}",
                    transform=rsi_axis.transAxes,
                    color=color,
                    fontsize=10,
                    fontweight="bold",
                    ha="right",
                    va="top",
                    bbox={
                        "boxstyle": "round,pad=0.22",
                        "facecolor": theme["background"],
                        "edgecolor": color,
                        "linewidth": 0.65,
                        "alpha": 0.90,
                    },
                    zorder=7,
                )
                rsi_axis.set_xlim(
                    earliest - time_padding,
                    latest + time_padding,
                )
                rsi_axis.set_ylim(0, 100)
                rsi_axis.set_yticks([30, 50, 70])
                rsi_axis.tick_params(
                    axis="y",
                    colors=theme["muted"],
                    labelsize=7,
                    length=0,
                    pad=4,
                )
                if index < len(histories) - 1:
                    rsi_axis.tick_params(
                        axis="x",
                        labelbottom=False,
                        length=0,
                    )

            locator = HourLocator(
                byhour=range(0, 24, tick_interval_hours),
                tz=timezone.utc,
            )
            rsi_axis = axes[-1]
            rsi_axis.xaxis.set_major_locator(locator)
            rsi_axis.xaxis.set_major_formatter(
                DateFormatter("%d.%m\n%H:%M", tz=timezone.utc)
            )
            rsi_axis.tick_params(
                axis="x",
                colors=theme["muted"],
                labelsize=8.5,
                length=0,
                pad=5,
            )
            rsi_axis.set_xlabel(
                "Время закрытия свечи · UTC",
                color=theme["muted"],
                fontsize=8.5,
                labelpad=7,
            )

            figure.text(
                0.075,
                0.935,
                "МОНИТОР АКТИВОВ · ИСТОРИЯ RSI14",
                color=theme["foreground"],
                fontsize=18,
                fontweight="bold",
                ha="left",
                va="center",
            )
            status_notes = []
            if snapshot.error_count:
                status_notes.append(
                    "НЕПОЛНЫЕ ДАННЫЕ · "
                    f"ИСТОРИЯ {snapshot.valid_asset_count}/{snapshot.requested_count}"
                )
            if snapshot.omitted_count:
                status_notes.append(
                    f"ПОКАЗАНЫ {snapshot.requested_count} ИЗ "
                    f"{snapshot.configured_count} АКТИВОВ"
                )
            if status_notes:
                figure.text(
                    0.075,
                    0.895,
                    " · ".join(status_notes),
                    color=theme["amber"],
                    fontsize=9.5,
                    ha="left",
                    va="center",
                )
            figure.text(
                0.96,
                0.935,
                f"{_captured_at(snapshot)} UTC",
                color=theme["foreground"],
                fontsize=11,
                fontweight="bold",
                ha="right",
                va="center",
            )
            figure.text(
                0.96,
                0.895,
                f"1Ч · до {max(len(asset.rsi_series) for asset in assets)} точек на актив",
                color=theme["muted"],
                fontsize=9.5,
                ha="right",
                va="center",
            )
            figure.text(
                0.075,
                0.045,
                "30/70 — границы крайних зон · 50 — баланс · "
                "справа показан текущий RSI · только закрытые свечи",
                color=theme["muted"],
                fontsize=9,
                ha="left",
                va="center",
            )
            delegated_clear = True
            return figure_to_png(figure)
        finally:
            if not delegated_clear:
                figure.clear()


__all__ = [
    "MARKET_REPORT_MEDIA_ID",
    "MarketAssetSnapshot",
    "MarketReportSnapshot",
    "collect_market_report",
    "format_market_report_rich_html",
    "format_market_report_text",
    "render_market_report_png",
]
