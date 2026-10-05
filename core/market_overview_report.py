"""Immutable presentation model for the global CoinGecko market overview.

The existing ``core.market_overview`` module owns HTTP and caching.  This
module deliberately receives one already collected mapping and builds the
text, rich HTML, and local PNG from the same immutable snapshot.
"""

from __future__ import annotations

import math
import re
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from html import escape
from typing import Any, Mapping, Optional

from core.market_overview import MARKET_OVERVIEW_REFRESH_SECONDS
from core.rich_charts import (
    DARK_THEME,
    FIGURE_DPI,
    FIGURE_SIZE_INCHES,
    MATPLOTLIB_RENDER_LOCK,
    figure_to_png,
)


OVERVIEW_REPORT_MEDIA_ID = "global_market_overview"
OVERVIEW_REFRESH_SECONDS = MARKET_OVERVIEW_REFRESH_SECONDS
MAX_TRENDING_ASSETS = 5
_MEDIA_ID_PATTERN = re.compile(r"[A-Za-z0-9_-]{1,64}")
_SYMBOL_PATTERN = re.compile(r"[A-Z0-9._-]{1,20}")


def _decimal_or_none(value: Any) -> Optional[Decimal]:
    if value is None or value == "" or isinstance(value, bool):
        return None
    try:
        result = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None
    if not result.is_finite():
        return None
    try:
        as_float = float(result)
    except (OverflowError, ValueError):
        return None
    return result if math.isfinite(as_float) else None


def _positive_decimal_or_none(value: Any) -> Optional[Decimal]:
    result = _decimal_or_none(value)
    return result if result is not None and result > 0 else None


def _percent_or_none(value: Any) -> Optional[Decimal]:
    result = _decimal_or_none(value)
    return (
        result
        if result is not None and Decimal("0") <= result <= Decimal("100")
        else None
    )


def _positive_int_or_none(value: Any) -> Optional[int]:
    if value is None or value == "" or isinstance(value, bool):
        return None
    try:
        result = int(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return result if result > 0 else None


def _clean_text(value: Any, *, fallback: str, limit: int) -> str:
    text = " ".join(str(value or "").split()).strip()
    if not text or text == "—":
        return fallback
    text = "".join(character for character in text if ord(character) >= 32)
    return text[:limit] or fallback


def _timestamp_ms_or_none(value: Any) -> Optional[int]:
    number = _positive_decimal_or_none(value)
    if number is None:
        return None
    # CoinGecko's ``updated_at`` is seconds, while the presentation layer uses
    # milliseconds consistently with the rest of the bot.
    timestamp = int(number)
    if timestamp < 100_000_000_000:
        timestamp *= 1_000
    try:
        datetime.fromtimestamp(timestamp / 1_000, tz=timezone.utc)
    except (OSError, OverflowError, ValueError):
        return None
    return timestamp


def _validate_decimal(
    value: Optional[Decimal],
    *,
    field: str,
    positive: bool = False,
    percent: bool = False,
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
    if percent and not Decimal("0") <= value <= Decimal("100"):
        raise ValueError(f"{field} must be in the 0..100 range")


@dataclass(frozen=True, slots=True)
class TrendingAsset:
    """One CoinGecko search-trending asset."""

    name: str
    symbol: str
    market_cap_rank: Optional[int]
    price_usd: Optional[Decimal] = None
    change_24h_percent: Optional[Decimal] = None

    def __post_init__(self) -> None:
        if (
            not self.name
            or len(self.name) > 80
            or any(ord(character) < 32 for character in self.name)
        ):
            raise ValueError("trending name has an invalid value")
        if not _SYMBOL_PATTERN.fullmatch(self.symbol):
            raise ValueError("trending symbol has an invalid value")
        if self.market_cap_rank is not None and self.market_cap_rank <= 0:
            raise ValueError("market_cap_rank must be positive")
        _validate_decimal(
            self.price_usd,
            field="price_usd",
            positive=True,
        )
        _validate_decimal(
            self.change_24h_percent,
            field="change_24h_percent",
        )


@dataclass(frozen=True, slots=True)
class GlobalMarketSnapshot:
    """One coherent global-market response, ready for every UI format."""

    market_cap_usd: Optional[Decimal]
    volume_24h_usd: Optional[Decimal]
    btc_dominance_percent: Optional[Decimal]
    trending: tuple[TrendingAsset, ...]
    captured_at_ms: int
    market_cap_change_24h_percent: Optional[Decimal] = None
    volume_change_24h_percent: Optional[Decimal] = None
    eth_dominance_percent: Optional[Decimal] = None
    active_cryptocurrencies: Optional[int] = None
    tracked_markets: Optional[int] = None
    source_updated_at_ms: Optional[int] = None
    stale: bool = False

    def __post_init__(self) -> None:
        _validate_decimal(
            self.market_cap_usd,
            field="market_cap_usd",
            positive=True,
        )
        _validate_decimal(
            self.volume_24h_usd,
            field="volume_24h_usd",
            positive=True,
        )
        _validate_decimal(
            self.btc_dominance_percent,
            field="btc_dominance_percent",
            percent=True,
        )
        _validate_decimal(
            self.eth_dominance_percent,
            field="eth_dominance_percent",
            percent=True,
        )
        _validate_decimal(
            self.market_cap_change_24h_percent,
            field="market_cap_change_24h_percent",
        )
        _validate_decimal(
            self.volume_change_24h_percent,
            field="volume_change_24h_percent",
        )
        if (
            self.btc_dominance_percent is not None
            and self.eth_dominance_percent is not None
            and self.btc_dominance_percent + self.eth_dominance_percent > 100
        ):
            raise ValueError("BTC and ETH dominance cannot exceed 100%")
        if not isinstance(self.trending, tuple):
            raise ValueError("trending must be an immutable tuple")
        if len(self.trending) > MAX_TRENDING_ASSETS:
            raise ValueError("too many trending assets")
        for field_name in ("active_cryptocurrencies", "tracked_markets"):
            value = getattr(self, field_name)
            if value is not None and (
                isinstance(value, bool)
                or not isinstance(value, int)
                or value <= 0
            ):
                raise ValueError(f"{field_name} must be a positive integer or None")
        if not isinstance(self.stale, bool):
            raise ValueError("stale must be a boolean")
        for field_name in ("captured_at_ms", "source_updated_at_ms"):
            value = getattr(self, field_name)
            if value is None and field_name == "source_updated_at_ms":
                continue
            if (
                isinstance(value, bool)
                or not isinstance(value, int)
                or value <= 0
            ):
                raise ValueError(f"{field_name} must be a positive timestamp")
            try:
                datetime.fromtimestamp(value / 1_000, tz=timezone.utc)
            except (OSError, OverflowError, ValueError) as error:
                raise ValueError(f"{field_name} is outside the UTC range") from error

    @property
    def turnover_percent(self) -> Optional[Decimal]:
        """24h volume as a share of global capitalization."""
        if self.market_cap_usd is None or self.volume_24h_usd is None:
            return None
        return self.volume_24h_usd / self.market_cap_usd * Decimal("100")

    @property
    def has_visual_data(self) -> bool:
        return any(
            value is not None
            for value in (
                self.market_cap_usd,
                self.volume_24h_usd,
                self.btc_dominance_percent,
            )
        ) or bool(self.trending)


def build_global_market_snapshot(
    overview: Mapping[str, Any],
    *,
    captured_at_ms: Optional[int] = None,
) -> GlobalMarketSnapshot:
    """Normalize one cached CoinGecko mapping without inventing zero values."""
    if not isinstance(overview, Mapping):
        raise ValueError("market overview must be a mapping")

    raw_trending = overview.get("trending")
    if not isinstance(raw_trending, list):
        raw_trending = []
    trending: list[TrendingAsset] = []
    for raw_item in raw_trending[:MAX_TRENDING_ASSETS]:
        if not isinstance(raw_item, Mapping):
            continue
        symbol = _clean_text(
            raw_item.get("symbol"),
            fallback="N/A",
            limit=20,
        ).upper()
        if not _SYMBOL_PATTERN.fullmatch(symbol):
            symbol = "N/A"
        trending.append(
            TrendingAsset(
                name=_clean_text(
                    raw_item.get("name"),
                    fallback="Неизвестный актив",
                    limit=80,
                ),
                symbol=symbol,
                market_cap_rank=_positive_int_or_none(
                    raw_item.get("rank")
                    if "rank" in raw_item
                    else raw_item.get("market_cap_rank")
                ),
                price_usd=_positive_decimal_or_none(
                    raw_item.get("price_usd")
                ),
                change_24h_percent=_decimal_or_none(
                    raw_item.get("change_24h_percent")
                    if "change_24h_percent" in raw_item
                    else raw_item.get("price_change_percentage_24h")
                ),
            )
        )

    timestamp = (
        _timestamp_ms_or_none(overview.get("captured_at_ms"))
        if captured_at_ms is None
        else captured_at_ms
    )
    if timestamp is None:
        timestamp = int(time.time() * 1_000)
    if isinstance(timestamp, bool) or not isinstance(timestamp, int):
        raise ValueError("captured_at_ms must be an integer")

    source_timestamp = _timestamp_ms_or_none(
        overview.get("source_updated_at_ms")
    )
    return GlobalMarketSnapshot(
        market_cap_usd=_positive_decimal_or_none(overview.get("market_cap")),
        volume_24h_usd=_positive_decimal_or_none(overview.get("volume")),
        btc_dominance_percent=_percent_or_none(
            overview.get("btc_dominance")
        ),
        trending=tuple(trending),
        captured_at_ms=timestamp,
        market_cap_change_24h_percent=_decimal_or_none(
            overview.get("market_cap_change_24h_percent")
            if "market_cap_change_24h_percent" in overview
            else overview.get("market_cap_change_percentage_24h_usd")
        ),
        volume_change_24h_percent=_decimal_or_none(
            overview.get("volume_change_24h_percent")
            if "volume_change_24h_percent" in overview
            else overview.get("volume_change_percentage_24h_usd")
        ),
        eth_dominance_percent=_percent_or_none(
            overview.get("eth_dominance")
        ),
        active_cryptocurrencies=_positive_int_or_none(
            overview.get("active_cryptocurrencies")
        ),
        tracked_markets=_positive_int_or_none(
            overview.get("markets")
            if "markets" in overview
            else overview.get("tracked_markets")
        ),
        source_updated_at_ms=source_timestamp,
        stale=overview.get("stale") is True,
    )


def _format_money(value: Optional[Decimal], *, compact: bool = False) -> str:
    if value is None:
        return "—"
    absolute = abs(value)
    if compact and absolute >= Decimal("1000000000000"):
        return f"${value / Decimal('1000000000000'):.2f} трлн"
    if compact and absolute >= Decimal("1000000000"):
        return f"${value / Decimal('1000000000'):.1f} млрд"
    if compact and absolute >= Decimal("1000000"):
        return f"${value / Decimal('1000000'):.1f} млн"
    return f"${value:,.0f}"


def _format_asset_price(value: Optional[Decimal]) -> str:
    if value is None:
        return "—"
    absolute = abs(value)
    if absolute >= Decimal("1000"):
        places = 2
    elif absolute >= Decimal("1"):
        places = 4
    elif absolute >= Decimal("0.01"):
        places = 6
    else:
        places = 8
    return f"${value:,.{places}f}"


def _format_percent(
    value: Optional[Decimal],
    *,
    places: int = 1,
    signed: bool = False,
) -> str:
    if value is None:
        return "—"
    if abs(value) < Decimal("0.5") * (Decimal("10") ** -places):
        value = Decimal("0")
    prefix = "+" if signed and value > 0 else ""
    return f"{prefix}{value:.{places}f}%"


def _change_text(value: Optional[Decimal]) -> str:
    return (
        "нет данных за 24ч"
        if value is None
        else f"{_format_percent(value, places=2, signed=True)} за 24ч"
    )


def _trend_change(value: Optional[Decimal]) -> str:
    return _format_percent(value, places=2, signed=True)


def _display_timestamp(snapshot: GlobalMarketSnapshot) -> str:
    timestamp = snapshot.source_updated_at_ms or snapshot.captured_at_ms
    return datetime.fromtimestamp(
        timestamp / 1_000,
        tz=timezone.utc,
    ).strftime("%d.%m.%Y %H:%M:%S")


def format_global_market_text(snapshot: GlobalMarketSnapshot) -> str:
    """Classic Telegram HTML fallback for clients without rich messages."""
    lines = [
        "🌍 <b>Крипторынок</b>",
        "<i>Глобальные показатели CoinGecko</i>",
    ]
    if snapshot.stale:
        lines.extend(
            [
                "",
                "⚠️ <i>CoinGecko временно недоступен — показан последний "
                "успешный срез.</i>",
            ]
        )
    lines.extend(
        [
            "",
            "<b>Размер рынка</b>",
        (
            "Капитализация "
            f"<code>{_format_money(snapshot.market_cap_usd, compact=True)}</code>"
            f" · {_change_text(snapshot.market_cap_change_24h_percent)}"
        ),
        (
            "Объём торгов за 24ч "
            f"<code>{_format_money(snapshot.volume_24h_usd, compact=True)}</code>"
            f" · {_change_text(snapshot.volume_change_24h_percent)}"
        ),
        (
            "Объём / капитализация "
            f"<code>{_format_percent(snapshot.turnover_percent, places=2)}</code>"
        ),
        "",
        "<b>Распределение рынка</b>",
        (
            "Доля BTC "
            f"<code>{_format_percent(snapshot.btc_dominance_percent)}</code>"
            " · ETH "
            f"<code>{_format_percent(snapshot.eth_dominance_percent)}</code>"
        ),
        ]
    )
    if (
        snapshot.active_cryptocurrencies is not None
        or snapshot.tracked_markets is not None
    ):
        active = (
            f"{snapshot.active_cryptocurrencies:,}".replace(",", " ")
            if snapshot.active_cryptocurrencies is not None
            else "—"
        )
        markets = (
            f"{snapshot.tracked_markets:,}".replace(",", " ")
            if snapshot.tracked_markets is not None
            else "—"
        )
        lines.append(f"Активных монет <code>{active}</code> · рынков <code>{markets}</code>")

    lines.extend(["", "🔥 <b>Популярно в CoinGecko</b>"])
    if not snapshot.trending:
        lines.append("Данные трендов временно недоступны.")
    for position, asset in enumerate(snapshot.trending, 1):
        market_rank = (
            "место по капитализации —"
            if asset.market_cap_rank is None
            else f"капитализация #{asset.market_cap_rank}"
        )
        change = (
            ""
            if asset.change_24h_percent is None
            else f" · 24ч <code>{_trend_change(asset.change_24h_percent)}</code>"
        )
        lines.append(
            f"{position}. <b>{escape(asset.symbol)}</b> · "
            f"{escape(asset.name)} · "
            f"<code>{_format_asset_price(asset.price_usd)}</code> · "
            f"{market_rank}{change}"
        )

    lines.extend(
        [
            "",
            (
                "<i>Популярность отражает интерес поиска, а не направление цены "
                "или торговый сигнал.\n"
                f"Данные {_display_timestamp(snapshot)} UTC · "
                f"автообновление раз в {OVERVIEW_REFRESH_SECONDS}с</i>"
            ),
        ]
    )
    return "\n".join(lines)


def format_global_market_rich_html(
    snapshot: GlobalMarketSnapshot,
    *,
    media_id: str = OVERVIEW_REPORT_MEDIA_ID,
) -> str:
    """Structured Telegram Rich HTML tied to this exact snapshot."""
    if not snapshot.has_visual_data:
        raise ValueError("global market snapshot has no visual data")
    if not _MEDIA_ID_PATTERN.fullmatch(media_id):
        raise ValueError("media_id has an invalid value")

    detail_rows = [
        (
            "Капитализация",
            _format_money(snapshot.market_cap_usd, compact=True),
            _change_text(snapshot.market_cap_change_24h_percent),
        ),
        (
            "Объём торгов",
            _format_money(snapshot.volume_24h_usd, compact=True),
            _change_text(snapshot.volume_change_24h_percent),
        ),
        (
            "Объём / капитализация",
            _format_percent(snapshot.turnover_percent, places=2),
            "оборот рынка за 24ч",
        ),
        (
            "Доминация",
            f"BTC {_format_percent(snapshot.btc_dominance_percent)}",
            f"ETH {_format_percent(snapshot.eth_dominance_percent)}",
        ),
    ]
    if (
        snapshot.active_cryptocurrencies is not None
        or snapshot.tracked_markets is not None
    ):
        active = (
            f"{snapshot.active_cryptocurrencies:,}".replace(",", " ")
            if snapshot.active_cryptocurrencies is not None
            else "—"
        )
        markets = (
            f"{snapshot.tracked_markets:,}".replace(",", " ")
            if snapshot.tracked_markets is not None
            else "—"
        )
        detail_rows.append(("Охват CoinGecko", f"{active} монет", f"{markets} рынков"))

    rows_html = "".join(
        "<tr>"
        f"<td>{escape(label)}</td>"
        f'<td align="right"><code>{escape(value)}</code></td>'
        f"<td>{escape(note)}</td>"
        "</tr>"
        for label, value, note in detail_rows
    )

    trending_rows = ""
    for position, asset in enumerate(snapshot.trending, 1):
        rank = "—" if asset.market_cap_rank is None else f"#{asset.market_cap_rank}"
        trending_rows += (
            "<tr>"
            f'<td align="right">{position}</td>'
            f"<td><b>{escape(asset.symbol)}</b> · {escape(asset.name)}</td>"
            f'<td align="right"><code>{_format_asset_price(asset.price_usd)}</code></td>'
            f'<td align="right"><code>{rank}</code></td>'
            f'<td align="right"><code>{_trend_change(asset.change_24h_percent)}</code></td>'
            "</tr>"
        )
    if not trending_rows:
        trending_rows = (
            "<tr><td>—</td><td>Данные временно недоступны</td>"
            "<td>—</td><td>—</td><td>—</td></tr>"
        )

    stale_notice = (
        "<blockquote>⚠️ CoinGecko временно недоступен — показан последний "
        "успешный срез.</blockquote>"
        if snapshot.stale
        else ""
    )

    return (
        "<h3>🌍 Крипторынок</h3>"
        "<p>Глобальный размер рынка, его распределение и интерес пользователей CoinGecko.</p>"
        f"{stale_notice}"
        f'<figure><img src="tg://photo?id={media_id}"/>'
        "<figcaption>Текущий глобальный срез · данные без ценовой истории</figcaption>"
        "</figure>"
        "<table bordered striped>"
        "<tr><th>Показатель</th><th>Значение</th><th>Контекст</th></tr>"
        f"{rows_html}"
        "</table>"
        "<h4>🔥 Популярно в CoinGecko</h4>"
        "<table bordered striped>"
        "<tr><th>№</th><th>Актив</th><th>Цена</th><th>Кап. место</th><th>24ч</th></tr>"
        f"{trending_rows}"
        "</table>"
        "<blockquote>Популярность отражает интерес поиска, а не направление цены "
        "или торговый сигнал.</blockquote>"
        f"<footer>Данные {_display_timestamp(snapshot)} UTC · "
        f"автоматически раз в {OVERVIEW_REFRESH_SECONDS}с</footer>"
    )


def _card(
    figure: Any,
    *,
    x: float,
    width: float,
    label: str,
    value: str,
    note: str,
    note_color: str,
) -> None:
    from matplotlib.patches import FancyBboxPatch

    theme = DARK_THEME
    figure.patches.append(
        FancyBboxPatch(
            (x, 0.705),
            width,
            0.145,
            boxstyle="round,pad=0.012,rounding_size=0.018",
            transform=figure.transFigure,
            facecolor=theme["panel"],
            edgecolor=theme["grid"],
            linewidth=1.2,
        )
    )
    figure.text(
        x + 0.02,
        0.815,
        label,
        color=theme["muted"],
        fontsize=10,
        weight="bold",
    )
    figure.text(
        x + 0.02,
        0.756,
        value,
        color=theme["foreground"],
        fontsize=20,
        weight="bold",
    )
    figure.text(
        x + 0.02,
        0.720,
        note,
        color=note_color,
        fontsize=9.5,
    )


def _change_color(value: Optional[Decimal]) -> str:
    if value is None or value == 0:
        return DARK_THEME["muted"]
    return DARK_THEME["green"] if value > 0 else DARK_THEME["red"]


def render_global_market_png(snapshot: GlobalMarketSnapshot) -> bytes:
    """Render an exact 1280×720 global dashboard without remote image assets."""
    if not snapshot.has_visual_data:
        raise ValueError("global market snapshot has no visual data")
    try:
        from matplotlib.figure import Figure
        from matplotlib.patches import Arc, Circle, FancyBboxPatch
    except ImportError as error:
        raise RuntimeError("Matplotlib is required for the global overview") from error

    theme = DARK_THEME
    with MATPLOTLIB_RENDER_LOCK:
        figure = Figure(
            figsize=FIGURE_SIZE_INCHES,
            dpi=FIGURE_DPI,
            facecolor=theme["background"],
        )
        # A small vector globe keeps the requested title readable even on
        # Matplotlib installations without a colour-emoji font.
        icon_axis = figure.add_axes([0.055, 0.895, 0.035, 0.055], zorder=20)
        icon_axis.set_xlim(0, 1)
        icon_axis.set_ylim(0, 1)
        icon_axis.add_patch(
            Circle(
                (0.5, 0.5),
                0.39,
                facecolor="none",
                edgecolor=theme["cyan"],
                linewidth=1.8,
            )
        )
        icon_axis.add_patch(
            Arc(
                (0.5, 0.5),
                0.36,
                0.76,
                edgecolor=theme["cyan"],
                linewidth=1.0,
            )
        )
        icon_axis.add_patch(
            Arc(
                (0.5, 0.5),
                0.72,
                0.34,
                edgecolor=theme["cyan"],
                linewidth=1.0,
            )
        )
        icon_axis.plot(
            [0.12, 0.88],
            [0.5, 0.5],
            color=theme["cyan"],
            linewidth=1.0,
        )
        icon_axis.axis("off")
        figure.text(
            0.097,
            0.928,
            "Крипторынок",
            color=theme["foreground"],
            fontsize=24,
            weight="bold",
        )
        figure.text(
            0.055,
            0.887,
            "CoinGecko · текущий глобальный срез",
            color=theme["muted"],
            fontsize=11,
        )
        figure.text(
            0.945,
            0.916 if snapshot.stale else 0.902,
            (
                "ПОСЛЕДНИЙ УСПЕШНЫЙ СРЕЗ\n"
                f"{_display_timestamp(snapshot)} UTC"
                if snapshot.stale
                else f"{_display_timestamp(snapshot)} UTC"
            ),
            color=theme["amber"] if snapshot.stale else theme["muted"],
            fontsize=9.5,
            horizontalalignment="right",
            verticalalignment="center",
        )

        card_gap = 0.018
        card_width = (0.89 - card_gap * 2) / 3
        _card(
            figure,
            x=0.055,
            width=card_width,
            label="КАПИТАЛИЗАЦИЯ",
            value=_format_money(snapshot.market_cap_usd, compact=True),
            note=_change_text(snapshot.market_cap_change_24h_percent),
            note_color=_change_color(snapshot.market_cap_change_24h_percent),
        )
        _card(
            figure,
            x=0.055 + card_width + card_gap,
            width=card_width,
            label="ОБЪЁМ ТОРГОВ · 24Ч",
            value=_format_money(snapshot.volume_24h_usd, compact=True),
            note=_change_text(snapshot.volume_change_24h_percent),
            note_color=_change_color(snapshot.volume_change_24h_percent),
        )
        _card(
            figure,
            x=0.055 + (card_width + card_gap) * 2,
            width=card_width,
            label="ОБОРОТ РЫНКА · 24Ч",
            value=_format_percent(snapshot.turnover_percent, places=2),
            note="объём / капитализация",
            note_color=theme["cyan"],
        )

        # Dominance panel.
        figure.patches.append(
            FancyBboxPatch(
                (0.055, 0.075),
                0.33,
                0.575,
                boxstyle="round,pad=0.012,rounding_size=0.018",
                transform=figure.transFigure,
                facecolor=theme["panel"],
                edgecolor=theme["grid"],
                linewidth=1.2,
                zorder=-10,
            )
        )
        figure.text(
            0.078,
            0.604,
            "РАСПРЕДЕЛЕНИЕ РЫНКА",
            color=theme["foreground"],
            fontsize=12,
            weight="bold",
        )

        dominance_axis = figure.add_axes([0.095, 0.185, 0.245, 0.34])
        dominance_axis.set_zorder(5)
        dominance_axis.patch.set_alpha(0)
        btc = (
            float(snapshot.btc_dominance_percent)
            if snapshot.btc_dominance_percent is not None
            else None
        )
        eth = (
            float(snapshot.eth_dominance_percent)
            if snapshot.eth_dominance_percent is not None
            else None
        )
        if btc is not None:
            pieces = [btc]
            colors = [theme["amber"]]
            if eth is not None:
                pieces.append(eth)
                colors.append(theme["violet"])
            pieces.append(max(0.0, 100.0 - sum(pieces)))
            colors.append(theme["grid"])
            dominance_axis.pie(
                pieces,
                colors=colors,
                startangle=90,
                counterclock=False,
                wedgeprops={
                    "width": 0.26,
                    "edgecolor": theme["panel"],
                    "linewidth": 2,
                },
            )
            dominance_axis.text(
                0,
                0.16,
                "BTC",
                ha="center",
                va="center",
                color=theme["muted"],
                fontsize=11,
                weight="bold",
            )
            dominance_axis.text(
                0,
                -0.12,
                _format_percent(snapshot.btc_dominance_percent),
                ha="center",
                va="center",
                color=theme["foreground"],
                fontsize=22,
                weight="bold",
            )
        else:
            dominance_axis.text(
                0.5,
                0.5,
                "Доминация\nнедоступна",
                transform=dominance_axis.transAxes,
                ha="center",
                va="center",
                color=theme["muted"],
                fontsize=13,
            )
        dominance_axis.axis("equal")
        dominance_axis.axis("off")

        eth_text = _format_percent(snapshot.eth_dominance_percent)
        figure.text(
            0.088,
            0.135,
            f"BTC  {_format_percent(snapshot.btc_dominance_percent)}",
            color=theme["amber"],
            fontsize=10.5,
            weight="bold",
        )
        figure.text(
            0.235,
            0.135,
            f"ETH  {eth_text}",
            color=theme["violet"] if snapshot.eth_dominance_percent is not None else theme["muted"],
            fontsize=10.5,
            weight="bold",
        )
        if (
            snapshot.active_cryptocurrencies is not None
            or snapshot.tracked_markets is not None
        ):
            active = (
                f"{snapshot.active_cryptocurrencies:,}".replace(",", " ")
                if snapshot.active_cryptocurrencies is not None
                else "—"
            )
            markets = (
                f"{snapshot.tracked_markets:,}".replace(",", " ")
                if snapshot.tracked_markets is not None
                else "—"
            )
            figure.text(
                0.078,
                0.093,
                f"{active} активных монет · {markets} рынков",
                color=theme["muted"],
                fontsize=9,
            )

        # Popularity panel: row length does not imply performance or rank.
        figure.patches.append(
            FancyBboxPatch(
                (0.41, 0.075),
                0.535,
                0.575,
                boxstyle="round,pad=0.012,rounding_size=0.018",
                transform=figure.transFigure,
                facecolor=theme["panel"],
                edgecolor=theme["grid"],
                linewidth=1.2,
            )
        )
        figure.text(
            0.435,
            0.604,
            "ПОПУЛЯРНО В COINGECKO",
            color=theme["foreground"],
            fontsize=12,
            weight="bold",
        )
        figure.text(
            0.918,
            0.604,
            "цена            кап. место     24ч",
            color=theme["muted"],
            fontsize=9,
            horizontalalignment="right",
        )

        if snapshot.trending:
            row_top = 0.544
            row_height = 0.088
            for position, asset in enumerate(snapshot.trending):
                y = row_top - position * row_height
                figure.patches.append(
                    FancyBboxPatch(
                        (0.432, y - 0.053),
                        0.49,
                        0.067,
                        boxstyle="round,pad=0.008,rounding_size=0.012",
                        transform=figure.transFigure,
                        facecolor=theme["background"],
                        edgecolor=theme["grid"],
                        linewidth=0.7,
                    )
                )
                figure.text(
                    0.449,
                    y - 0.02,
                    str(position + 1),
                    color=theme["cyan"],
                    fontsize=12,
                    weight="bold",
                    va="center",
                )
                figure.text(
                    0.478,
                    y - 0.011,
                    asset.symbol,
                    color=theme["foreground"],
                    fontsize=11.5,
                    weight="bold",
                    va="center",
                )
                name = asset.name if len(asset.name) <= 25 else asset.name[:24] + "…"
                figure.text(
                    0.478,
                    y - 0.038,
                    name,
                    color=theme["muted"],
                    fontsize=8.5,
                    va="center",
                )
                rank = (
                    "—"
                    if asset.market_cap_rank is None
                    else f"#{asset.market_cap_rank}"
                )
                figure.text(
                    0.748,
                    y - 0.02,
                    _format_asset_price(asset.price_usd),
                    color=theme["foreground"],
                    fontsize=9.5,
                    family="monospace",
                    ha="right",
                    va="center",
                )
                figure.text(
                    0.828,
                    y - 0.02,
                    rank,
                    color=theme["foreground"],
                    fontsize=10.5,
                    family="monospace",
                    ha="right",
                    va="center",
                )
                figure.text(
                    0.905,
                    y - 0.02,
                    _trend_change(asset.change_24h_percent),
                    color=_change_color(asset.change_24h_percent),
                    fontsize=10.5,
                    family="monospace",
                    weight="bold",
                    ha="right",
                    va="center",
                )
        else:
            figure.text(
                0.677,
                0.36,
                "Данные временно недоступны",
                color=theme["muted"],
                fontsize=13,
                horizontalalignment="center",
            )

        return figure_to_png(figure)


__all__ = [
    "GlobalMarketSnapshot",
    "MAX_TRENDING_ASSETS",
    "OVERVIEW_REFRESH_SECONDS",
    "OVERVIEW_REPORT_MEDIA_ID",
    "TrendingAsset",
    "build_global_market_snapshot",
    "format_global_market_rich_html",
    "format_global_market_text",
    "render_global_market_png",
]
