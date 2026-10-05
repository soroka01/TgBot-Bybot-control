"""Shared, headless Matplotlib renderers for Telegram rich reports.

The module accepts only already aggregated application data.  It never reads
Bybit, SQLite, or Telegram state and never writes an image to disk.
"""

from __future__ import annotations

import io
import math
import threading
from collections.abc import Mapping, Sequence
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from types import MappingProxyType
from typing import Any, Final


FIGURE_PIXEL_SIZE: Final = (1_280, 720)
FIGURE_DPI: Final = 100
FIGURE_SIZE_INCHES: Final = (
    FIGURE_PIXEL_SIZE[0] / FIGURE_DPI,
    FIGURE_PIXEL_SIZE[1] / FIGURE_DPI,
)
MAX_PNG_BYTES: Final = 8 * 1024 * 1024
PNG_SIGNATURE: Final = b"\x89PNG\r\n\x1a\n"

DARK_THEME: Final = MappingProxyType(
    {
        "background": "#08111F",
        "panel": "#0D1728",
        "grid": "#243247",
        "foreground": "#E5ECF5",
        "muted": "#8EA0B8",
        "green": "#21C784",
        "red": "#F05A67",
        "cyan": "#32C7E6",
        "amber": "#F4B942",
        "blue": "#6EA8FE",
        "violet": "#B58BFA",
    }
)

# Matplotlib has process-global caches even when the object-oriented API is
# used.  A re-entrant lock lets renderers hold it across figure construction
# while ``figure_to_png`` independently remains safe for direct callers.
MATPLOTLIB_RENDER_LOCK = threading.RLock()


def _finite_decimal(value: Any, *, field: str) -> Decimal:
    if value is None or value == "" or isinstance(value, bool):
        raise ValueError(f"{field} must be a finite decimal")
    try:
        number = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as error:
        raise ValueError(f"{field} must be a finite decimal") from error
    if not number.is_finite():
        raise ValueError(f"{field} must be a finite decimal")
    return number


def _positive_timestamp_ms(value: Any, *, field: str) -> int:
    number = _finite_decimal(value, field=field)
    if number != number.to_integral_value() or number <= 0:
        raise ValueError(f"{field} must be a positive millisecond timestamp")
    timestamp = int(number)
    try:
        datetime.fromtimestamp(timestamp / 1_000, tz=timezone.utc)
    except (OSError, OverflowError, ValueError) as error:
        raise ValueError(f"{field} is outside the supported UTC range") from error
    return timestamp


def _validated_trades(
    analytics: Mapping[str, Any],
) -> list[tuple[int, str, Decimal]]:
    raw_trades = analytics.get("trades")
    if (
        not isinstance(raw_trades, Sequence)
        or isinstance(raw_trades, (str, bytes, bytearray))
    ):
        raise ValueError("analytics.trades must be a sequence")
    if len(raw_trades) < 2:
        raise ValueError("At least two aggregated trades are required")
    if len(raw_trades) > 100_000:
        raise ValueError("Trade report exceeds the safe render size")

    validated: list[tuple[int, str, Decimal, int]] = []
    for index, trade in enumerate(raw_trades):
        if not isinstance(trade, Mapping):
            raise ValueError(f"analytics.trades[{index}] must be a mapping")
        closed_at = _positive_timestamp_ms(
            trade.get("closed_at_ms"),
            field=f"analytics.trades[{index}].closed_at_ms",
        )
        pnl = _finite_decimal(
            trade.get("closed_pnl"),
            field=f"analytics.trades[{index}].closed_pnl",
        )
        # Reject Decimal values which overflow when converted for Matplotlib.
        if not math.isfinite(float(pnl)):
            raise ValueError(
                f"analytics.trades[{index}].closed_pnl is outside plot range"
            )
        trade_id = str(trade.get("trade_id") or f"trade:{index}")
        validated.append((closed_at, trade_id, pnl, index))

    declared_count = analytics.get("trade_count")
    if declared_count is not None:
        count = _finite_decimal(declared_count, field="analytics.trade_count")
        if count != count.to_integral_value() or int(count) != len(validated):
            raise ValueError("analytics.trade_count does not match analytics.trades")

    validated.sort(key=lambda item: (item[0], item[1], item[3]))
    return [
        (closed_at, trade_id, pnl)
        for closed_at, trade_id, pnl, _ in validated
    ]


def figure_to_png(figure: Any) -> bytes:
    """Encode an exact 1280×720 Figure and clear it on every exit path."""
    try:
        from matplotlib.figure import Figure
    except ImportError as error:
        raise RuntimeError("Matplotlib is required for rich PNG reports") from error

    if not isinstance(figure, Figure):
        raise TypeError("figure_to_png requires a matplotlib.figure.Figure")

    with MATPLOTLIB_RENDER_LOCK:
        try:
            from matplotlib.backends.backend_agg import FigureCanvasAgg

            canvas = FigureCanvasAgg(figure)
            pixels = tuple(int(value) for value in canvas.get_width_height())
            if pixels != FIGURE_PIXEL_SIZE:
                raise ValueError(
                    "Rich report Figure must be exactly "
                    f"{FIGURE_PIXEL_SIZE[0]}x{FIGURE_PIXEL_SIZE[1]} pixels"
                )

            output = io.BytesIO()
            canvas.print_png(
                output,
                metadata={"Software": "Crypto trading bot"},
            )
            png = output.getvalue()
            if not png.startswith(PNG_SIGNATURE):
                raise RuntimeError("Matplotlib did not create a valid PNG")
            if len(png) > MAX_PNG_BYTES:
                raise RuntimeError("Rich report PNG exceeds Telegram's safe limit")
            return png
        finally:
            figure.clear()


def render_trade_performance_png(
    analytics: Mapping[str, Any],
    days: int,
    scope_label: str,
) -> bytes:
    """Render cumulative net Closed PnL and drawdown from aggregated trades."""
    if not isinstance(analytics, Mapping):
        raise ValueError("analytics must be a mapping")
    if isinstance(days, bool) or not isinstance(days, int) or not 1 <= days <= 365:
        raise ValueError("days must be an integer from 1 to 365")
    scope = str(scope_label).strip()
    if (
        not scope
        or len(scope) > 80
        or any(ord(character) < 32 for character in scope)
    ):
        raise ValueError("scope_label has an invalid value")

    trades = _validated_trades(analytics)
    cumulative_decimals = [Decimal("0")]
    drawdown_decimals = [Decimal("0")]
    peak = Decimal("0")
    for _, _, pnl in trades:
        current = cumulative_decimals[-1] + pnl
        if not math.isfinite(float(current)):
            raise ValueError("Cumulative Closed PnL is outside plot range")
        cumulative_decimals.append(current)
        peak = max(peak, current)
        drawdown_decimals.append(current - peak)

    moments = [
        datetime.fromtimestamp(closed_at / 1_000, tz=timezone.utc)
        for closed_at, _, _ in trades
    ]
    positive_gaps = [
        later - earlier
        for earlier, later in zip(moments, moments[1:])
        if later > earlier
    ]
    baseline_gap = (
        min(positive_gaps)
        if positive_gaps
        else timedelta(hours=1)
    )
    baseline_gap = min(baseline_gap, timedelta(days=1))
    baseline_gap = max(baseline_gap, timedelta(milliseconds=1))
    x_values = [moments[0] - baseline_gap, *moments]
    cumulative = [float(value) for value in cumulative_decimals]
    drawdowns = [float(value) for value in drawdown_decimals]
    trade_pnls = [float(pnl) for _, _, pnl in trades]

    try:
        from matplotlib.dates import AutoDateLocator, ConciseDateFormatter
        from matplotlib.figure import Figure
        from matplotlib.ticker import FuncFormatter, MaxNLocator
    except ImportError as error:
        raise RuntimeError("Matplotlib is required for rich PNG reports") from error

    theme = DARK_THEME
    with MATPLOTLIB_RENDER_LOCK:
        figure = Figure(
            figsize=FIGURE_SIZE_INCHES,
            dpi=FIGURE_DPI,
            facecolor=theme["background"],
        )
        delegated_clear = False
        try:
            layout = figure.add_gridspec(
                3,
                1,
                height_ratios=(2.15, 0.04, 0.85),
                hspace=0.04,
                left=0.075,
                right=0.95,
                top=0.85,
                bottom=0.13,
            )
            pnl_axis = figure.add_subplot(layout[0, 0])
            drawdown_axis = figure.add_subplot(
                layout[2, 0],
                sharex=pnl_axis,
            )
            for axis in (pnl_axis, drawdown_axis):
                axis.set_facecolor(theme["panel"])
                axis.grid(
                    True,
                    color=theme["grid"],
                    linewidth=0.7,
                    alpha=0.58,
                )
                axis.tick_params(
                    colors=theme["muted"],
                    labelsize=9,
                    length=0,
                )
                axis.yaxis.set_major_locator(MaxNLocator(nbins=6))
                axis.yaxis.set_major_formatter(
                    FuncFormatter(lambda value, _: f"{value:,.2f}")
                )
                for spine in axis.spines.values():
                    spine.set_visible(False)

            pnl_axis.axhline(
                0,
                color=theme["muted"],
                linewidth=0.9,
                alpha=0.8,
                zorder=1,
            )
            pnl_axis.step(
                x_values,
                cumulative,
                where="post",
                color=theme["cyan"],
                linewidth=2.2,
                zorder=3,
            )
            pnl_axis.fill_between(
                x_values,
                cumulative,
                0,
                where=[value >= 0 for value in cumulative],
                color=theme["green"],
                alpha=0.12,
                interpolate=True,
                step="post",
                zorder=1,
            )
            pnl_axis.fill_between(
                x_values,
                cumulative,
                0,
                where=[value < 0 for value in cumulative],
                color=theme["red"],
                alpha=0.14,
                interpolate=True,
                step="post",
                zorder=1,
            )
            pnl_axis.scatter(
                moments,
                cumulative[1:],
                c=[
                    theme["green"]
                    if pnl > 0
                    else theme["red"]
                    if pnl < 0
                    else theme["muted"]
                    for pnl in trade_pnls
                ],
                s=34,
                edgecolors=theme["panel"],
                linewidths=0.8,
                zorder=4,
            )
            pnl_axis.text(
                0.012,
                0.94,
                "CUMULATIVE CLOSED PNL · USDT",
                transform=pnl_axis.transAxes,
                color=theme["foreground"],
                fontsize=9,
                fontweight="bold",
                ha="left",
                va="top",
            )
            pnl_axis.tick_params(axis="x", labelbottom=False)

            drawdown_axis.axhline(
                0,
                color=theme["muted"],
                linewidth=0.9,
                alpha=0.8,
                zorder=2,
            )
            drawdown_axis.step(
                x_values,
                drawdowns,
                where="post",
                color=theme["red"],
                linewidth=1.45,
                zorder=3,
            )
            drawdown_axis.fill_between(
                x_values,
                drawdowns,
                0,
                color=theme["red"],
                alpha=0.28,
                step="post",
                zorder=1,
            )
            drawdown_axis.text(
                0.012,
                0.86,
                "DRAWDOWN · USDT",
                transform=drawdown_axis.transAxes,
                color=theme["foreground"],
                fontsize=8.5,
                fontweight="bold",
                ha="left",
                va="top",
            )
            drawdown_axis.set_ylim(
                min(min(drawdowns) * 1.18, -1e-9),
                max(abs(min(drawdowns)) * 0.08, 1e-9),
            )

            locator = AutoDateLocator(
                minticks=4,
                maxticks=7,
                tz=timezone.utc,
            )
            formatter = ConciseDateFormatter(
                locator,
                tz=timezone.utc,
                show_offset=False,
            )
            drawdown_axis.xaxis.set_major_locator(locator)
            drawdown_axis.xaxis.set_major_formatter(formatter)
            drawdown_axis.set_xlabel(
                "Время закрытия сделок · UTC",
                color=theme["muted"],
                fontsize=9,
                labelpad=8,
            )

            net_pnl = cumulative_decimals[-1]
            max_drawdown = -min(drawdown_decimals)
            net_color = (
                theme["green"]
                if net_pnl > 0
                else theme["red"]
                if net_pnl < 0
                else theme["muted"]
            )
            figure.text(
                0.075,
                0.94,
                f"ИСТОРИЯ СДЕЛОК · {days} ДН. · {scope.upper()}",
                color=theme["foreground"],
                fontsize=16,
                fontweight="bold",
                ha="left",
                va="center",
            )
            figure.text(
                0.95,
                0.945,
                f"NET {net_pnl:+,.2f} USDT",
                color=net_color,
                fontsize=15,
                fontweight="bold",
                ha="right",
                va="center",
            )
            figure.text(
                0.95,
                0.907,
                f"{len(trades)} сделок · MAX DD {max_drawdown:,.2f} USDT",
                color=theme["muted"],
                fontsize=9.5,
                ha="right",
                va="center",
            )
            figure.text(
                0.075,
                0.035,
                "Closed PnL — net после комиссий и funding · USDT · UTC",
                color=theme["muted"],
                fontsize=8.5,
                ha="left",
                va="center",
            )
            delegated_clear = True
            return figure_to_png(figure)
        finally:
            # Once delegated, figure_to_png owns the single unconditional
            # clear.  Earlier construction failures are cleared here.
            if not delegated_clear:
                figure.clear()


__all__ = [
    "DARK_THEME",
    "FIGURE_DPI",
    "FIGURE_PIXEL_SIZE",
    "FIGURE_SIZE_INCHES",
    "MATPLOTLIB_RENDER_LOCK",
    "figure_to_png",
    "render_trade_performance_png",
]
