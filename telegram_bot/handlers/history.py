"""Durable trade-history analytics for the single-message Telegram UI."""

from __future__ import annotations

import asyncio
import html
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any, Iterable, Optional

from aiogram import F, Router
from aiogram.types import CallbackQuery, InlineKeyboardMarkup

from api.bybit_api import BybitAPI
from core.trade_journal import TradeJournal
from telegram_bot.keyboards.history_menu import (
    DEFAULT_HISTORY_PERIOD,
    DEFAULT_HISTORY_SCOPE,
    HISTORY_PERIODS,
    HISTORY_SCOPES,
    get_history_menu,
)
from telegram_bot.ui import (
    callback_action,
    current_screen_token,
    render_callback_screen,
    render_if_current,
)
from utils.logger_setup import logger


router = Router()

_SPARK_BLOCKS = "▁▂▃▄▅▆▇█"
_MAX_SPARK_POINTS = 28
_MAX_RECENT_TRADES = 5
_MAX_SCREEN_LENGTH = 2_500


def _decimal(value: Any) -> Optional[Decimal]:
    if value is None or value == "":
        return None
    try:
        number = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None
    return number if number.is_finite() else None


def _money(value: Any, *, signed: bool = False) -> str:
    number = _decimal(value)
    if number is None:
        return "—"
    if number == 0:
        number = Decimal("0")
    sign = "+" if signed and number > 0 else ""
    return f"${sign}{number:,.2f}"


def _ratio(value: Any, *, suffix: str = "", signed: bool = False) -> str:
    if value is None or value == "":
        return "—"
    try:
        number = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return "—"
    if number.is_infinite():
        return "∞" if number > 0 else "—"
    if not number.is_finite():
        return "—"
    if number == 0:
        number = Decimal("0")
    sign = "+" if signed and number > 0 else ""
    return f"{sign}{number:.2f}{suffix}"


def _integer(value: Any) -> int:
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return 0


def _plural(count: int, one: str, few: str, many: str) -> str:
    value = abs(int(count)) % 100
    if 11 <= value <= 14:
        return many
    last = value % 10
    if last == 1:
        return one
    if 2 <= last <= 4:
        return few
    return many


def _series_value(item: Any) -> Optional[Decimal]:
    if isinstance(item, dict):
        for key in ("value", "pnl", "cumulative_pnl", "closed_pnl"):
            if key in item:
                return _decimal(item[key])
        return None
    if isinstance(item, (tuple, list)) and len(item) >= 2:
        return _decimal(item[-1])
    return _decimal(item)


def cumulative_pnl_sparkline(
    values: Iterable[Any],
    *,
    width: int = _MAX_SPARK_POINTS,
) -> str:
    """Render a deterministic, compact Unicode chart without Telegram media."""
    series = [number for item in values if (number := _series_value(item)) is not None]
    if not series:
        return "—"
    if series[0] != 0:
        series.insert(0, Decimal("0"))

    target = max(2, min(int(width), _MAX_SPARK_POINTS))
    if len(series) > target:
        last = len(series) - 1
        series = [
            series[round(index * last / (target - 1))]
            for index in range(target)
        ]

    low, high = min(series), max(series)
    if low == high:
        return _SPARK_BLOCKS[len(_SPARK_BLOCKS) // 2] * len(series)
    span = high - low
    top = len(_SPARK_BLOCKS) - 1
    return "".join(
        _SPARK_BLOCKS[
            min(top, max(0, int((number - low) * top / span)))
        ]
        for number in series
    )


def _recent_trade_line(record: dict[str, Any]) -> str:
    timestamp = (
        record.get("updated_time_ms")
        or record.get("closed_at_ms")
        or record.get("created_time_ms")
    )
    try:
        moment = datetime.fromtimestamp(int(timestamp) / 1_000, tz=timezone.utc)
        when = moment.strftime("%d.%m %H:%M")
    except (OSError, OverflowError, TypeError, ValueError):
        when = "—"

    raw_symbol = str(record.get("symbol") or "?").strip().upper()[:18]
    symbol = html.escape(raw_symbol)
    side = (
        record.get("position_side")
        or record.get("setup_side")
        or record.get("side")
    )
    direction = "L" if side == "Buy" else "S" if side == "Sell" else "·"
    pnl = _decimal(record.get("closed_pnl"))
    if pnl is None:
        pnl = _decimal(record.get("net_pnl"))
    if pnl is None:
        pnl = _decimal(record.get("pnl"))
    if pnl is None:
        pnl = Decimal("0")
    icon = "🟢" if pnl > 0 else "🔴" if pnl < 0 else "⚪"
    source = (
        "🤖"
        if record.get("candidate_id") or record.get("source") == "bot"
        else "👤"
    )
    return (
        f"{icon} <code>{when}</code> {source} "
        f"<b>{symbol}</b> {direction} <code>{_money(pnl, signed=True)}</code>"
    )


def _build_analytics(
    records: list[dict[str, Any]],
    equity_snapshots: list[dict[str, Any]],
) -> dict[str, Any]:
    # Imported lazily so the Telegram package remains importable while the
    # independently versioned analytics module is being deployed.
    from core.trade_analytics import build_trade_analytics

    return build_trade_analytics(records, equity_snapshots=equity_snapshots)


def format_history_screen(
    records: list[dict[str, Any]],
    analytics: dict[str, Any],
    *,
    days: int,
    scope: str,
    cache_warning: bool = False,
    sync_busy: bool = False,
) -> str:
    """Format only bounded, escaped content suitable for Telegram HTML."""
    scope_label = "🤖 только бот" if scope == "bot" else "🌐 весь аккаунт"
    lines = [
        "📜 <b>История сделок</b>",
        f"<code>{days} дн.</code> · {scope_label}",
    ]
    if cache_warning:
        lines.extend(
            [
                "",
                "⚠️ <i>Bybit временно недоступен — показан локальный кэш.</i>",
            ]
        )
    elif sync_busy:
        lines.extend(
            [
                "",
                "⏳ <i>Синхронизация уже идёт — показан текущий кэш.</i>",
            ]
        )

    trade_count = _integer(analytics.get("trade_count", len(records)))
    if not trade_count:
        lines.extend(
            [
                "",
                "За выбранный период сделок нет.",
            ]
        )
        if scope == "bot":
            lines.append(
                "<i>В этом режиме видны только сделки, сопоставленные "
                "с сетапами бота.</i>"
            )
        text = "\n".join(lines)
        if len(text) >= _MAX_SCREEN_LENGTH:
            raise ValueError("Экран истории превысил безопасный размер")
        return text

    net_pnl = analytics.get("net_pnl")
    wins = _integer(analytics.get("wins"))
    losses = _integer(analytics.get("losses"))
    breakeven = _integer(analytics.get("breakeven"))
    max_drawdown = abs(_decimal(analytics.get("max_drawdown")) or Decimal("0"))
    r_count = _integer(analytics.get("r_count"))
    avg_r = _ratio(analytics.get("avg_r"), suffix="R", signed=True)
    if r_count and r_count != trade_count:
        avg_r += f" ({r_count})"
    fee_count = _integer(
        analytics.get(
            "fee_complete_record_count",
            analytics.get("fee_complete_count"),
        )
    )
    source_record_count = (
        _integer(analytics.get("source_record_count", trade_count))
        or trade_count
    )
    trade_label = (
        f"<b>{trade_count}</b> "
        f"{_plural(trade_count, 'сделка', 'сделки', 'сделок')}"
    )
    if source_record_count != trade_count:
        trade_label += (
            f" · {source_record_count} "
            f"{_plural(source_record_count, 'закрытие', 'закрытия', 'закрытий')}"
        )
    lines.extend(
        [
            "",
            f"💰 <b>Net PnL:</b> <code>{_money(net_pnl, signed=True)}</code>",
            (
                f"🎯 {trade_label} · "
                f"<code>{wins}W/{losses}L/{breakeven}B</code> · "
                f"Win <code>{_ratio(analytics.get('win_rate'), suffix='%')}</code>"
            ),
            (
                "📈 PF "
                f"<code>{_ratio(analytics.get('profit_factor'))}</code> · "
                "Expectancy "
                f"<code>{_money(analytics.get('expectancy'), signed=True)}</code>"
            ),
            (
                "📉 Max DD "
                f"<code>{_money(-max_drawdown)}</code> · "
                "Avg R "
                f"<code>{avg_r}</code>"
            ),
            (
                "⚖️ Avg W / Avg L "
                f"<code>{_money(analytics.get('avg_win'), signed=True)}</code> / "
                f"<code>{_money(analytics.get('avg_loss'), signed=True)}</code> · "
                "Payoff "
                f"<code>{_ratio(analytics.get('payoff_ratio'))}</code>"
            ),
            (
                "💸 Комиссии "
                f"<code>{_money(analytics.get('fees_total'))}</code> "
                f"<i>({fee_count}/{source_record_count}, уже учтены в Net PnL)</i>"
            ),
            "",
            "<b>Cumulative PnL</b>",
            (
                "<code>"
                + cumulative_pnl_sparkline(analytics.get("cumulative_pnl") or [])
                + "</code>"
            ),
        ]
    )

    equity_return = _decimal(analytics.get("equity_return_percent"))
    equity_drawdown = _decimal(analytics.get("equity_max_drawdown_percent"))
    if equity_return is not None or equity_drawdown is not None:
        lines.append(
            "Equity Δ: "
            f"<code>{_ratio(equity_return, suffix='%', signed=True)}</code> · "
            "DD "
            f"<code>{_ratio(equity_drawdown, suffix='%')}</code>"
        )

    lines.extend(["", "<b>Последние сделки · UTC</b>"])
    aggregate_trades = analytics.get("trades")
    recent_source = (
        aggregate_trades
        if isinstance(aggregate_trades, list)
        else records
    )
    recent = sorted(
        recent_source,
        key=lambda item: (
            _integer(item.get("updated_time_ms")),
            _integer(item.get("closed_at_ms")),
            _integer(item.get("created_time_ms")),
            str(item.get("record_id") or item.get("trade_id") or ""),
        ),
        reverse=True,
    )[:_MAX_RECENT_TRADES]
    lines.extend(_recent_trade_line(record) for record in recent)

    text = "\n".join(lines)
    if len(text) >= _MAX_SCREEN_LENGTH:
        # The layout is intentionally fixed-size; fail closed if a future
        # metric accidentally reintroduces unbounded exchange text.
        raise ValueError("Экран истории превысил безопасный размер")
    return text


def build_history_view(
    days: int = DEFAULT_HISTORY_PERIOD,
    scope: str = DEFAULT_HISTORY_SCOPE,
    *,
    force: bool = False,
) -> tuple[str, InlineKeyboardMarkup]:
    """Synchronize the journal and render analytics from durable local rows."""
    if days not in HISTORY_PERIODS or scope not in HISTORY_SCOPES:
        raise ValueError("Некорректный фильтр истории")

    bybit = BybitAPI()
    cache_warning = False
    sync_busy = False
    try:
        journal = TradeJournal(bybit)
        try:
            summary = journal.sync_closed_pnl(
                lookback_days=days,
                force=force,
            )
            sync_busy = bool(getattr(summary, "skipped_busy", False))
        except Exception as error:
            cache_warning = True
            summary = None
            logger.warning(
                "Не удалось синхронизировать историю Bybit, показываю кэш "
                f"({type(error).__name__})"
            )

        records = journal.closed_records(
            lookback_days=days,
            bot_only=scope == "bot",
        )
        equity = journal.equity_snapshots(lookback_days=days)
        if summary is not None and (
            force or not summary.skipped_fresh or not equity
        ):
            try:
                journal.record_current_equity()
                equity = journal.equity_snapshots(lookback_days=days)
            except Exception as error:
                logger.warning(
                    "Не удалось обновить equity для истории "
                    f"({type(error).__name__})"
                )

        analytics = _build_analytics(records, equity)
        text = format_history_screen(
            records,
            analytics,
            days=days,
            scope=scope,
            cache_warning=cache_warning,
            sync_busy=sync_busy,
        )
        return text, get_history_menu(days, scope)
    finally:
        bybit.close()


def _parse_history_callback(data: Optional[str]) -> tuple[bool, int, str]:
    action = callback_action(data)
    parts = action.split(":")
    if len(parts) != 4 or parts[0] != "history":
        raise ValueError("Некорректная кнопка истории")
    refresh = parts[1] == "refresh"
    if parts[1] not in {"view", "refresh"}:
        raise ValueError("Некорректное действие истории")
    try:
        days = int(parts[2])
    except ValueError as error:
        raise ValueError("Некорректный период истории") from error
    scope = parts[3]
    if days not in HISTORY_PERIODS or scope not in HISTORY_SCOPES:
        raise ValueError("Некорректный фильтр истории")
    return refresh, days, scope


async def _render_history(
    callback: CallbackQuery,
    *,
    days: int,
    scope: str,
    force: bool,
) -> None:
    loading_markup = get_history_menu(days, scope)
    canonical = await render_callback_screen(
        callback.message,
        (
            "📜 <b>История сделок</b>\n\n"
            f"⏳ Синхронизирую {days} дн. и пересчитываю статистику…"
        ),
        loading_markup,
    )
    token = current_screen_token(canonical)
    try:
        text, markup = await asyncio.to_thread(
            build_history_view,
            days,
            scope,
            force=force,
        )
    except Exception as error:
        logger.error(f"Ошибка экрана истории ({type(error).__name__})")
        text = (
            "❌ <b>Не удалось обработать историю</b>\n\n"
            "Локальные данные не повреждены. Попробуйте обновить экран позже."
        )
        markup = get_history_menu(days, scope)
    await render_if_current(token, canonical, text, markup)


@router.callback_query(F.data == "menu:history")
async def show_history(callback: CallbackQuery) -> None:
    await callback.answer("Загружаю историю…")
    await _render_history(
        callback,
        days=DEFAULT_HISTORY_PERIOD,
        scope=DEFAULT_HISTORY_SCOPE,
        force=False,
    )


@router.callback_query(F.data.startswith("history:"))
async def change_history(callback: CallbackQuery) -> None:
    try:
        refresh, days, scope = _parse_history_callback(callback.data)
    except ValueError:
        await callback.answer("Кнопка истории устарела.", show_alert=False)
        return
    await callback.answer("Обновляю Bybit…" if refresh else "Применяю фильтр…")
    await _render_history(
        callback,
        days=days,
        scope=scope,
        force=refresh,
    )
