"""Pure Decimal analytics for the durable trade journal.

The module deliberately has no SQLite, Bybit, Telegram, or wall-clock I/O.
Callers pass rows returned by ``SQLiteStore.list_closed_trade_records`` and,
optionally, rows returned by ``list_equity_snapshots``.

Closed PnL is already net of trading fees and funding on Bybit.  Fees are
therefore reported as an informational breakdown and are never subtracted
again.  Bot partial closes are grouped by ``(account_scope, candidate_id)``;
unattributed account rows remain separate exchange close records.
"""

from __future__ import annotations

from decimal import Decimal, InvalidOperation
from typing import Any, Iterable, Mapping, Optional


ZERO = Decimal("0")
HUNDRED = Decimal("100")
INFINITY = Decimal("Infinity")


def _decimal(
    value: Any,
    *,
    field: str,
    default: Optional[Decimal] = None,
) -> Optional[Decimal]:
    if value is None or value == "":
        return default
    try:
        number = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as error:
        raise ValueError(f"{field} is not a valid decimal: {value!r}") from error
    if not number.is_finite():
        raise ValueError(f"{field} must be finite")
    return number


def _timestamp(value: Any, *, field: str) -> Optional[int]:
    if value is None or value == "":
        return None
    try:
        timestamp = int(value)
    except (TypeError, ValueError) as error:
        raise ValueError(f"{field} is not a valid timestamp: {value!r}") from error
    if timestamp < 0:
        raise ValueError(f"{field} cannot be negative")
    return timestamp


def _row_turnover(row: Mapping[str, Any]) -> tuple[Decimal, Decimal]:
    """Return entry and exit notionals, preferring Bybit cumulative values."""
    entry = _decimal(
        row.get("cum_entry_value"),
        field="cum_entry_value",
    )
    exit_value = _decimal(
        row.get("cum_exit_value"),
        field="cum_exit_value",
    )
    size = _decimal(
        row.get("closed_size") or row.get("qty"),
        field="closed_size",
    )
    if entry is None and size is not None:
        price = _decimal(
            row.get("avg_entry_price"),
            field="avg_entry_price",
        )
        if price is not None:
            entry = size * price
    if exit_value is None and size is not None:
        price = _decimal(
            row.get("avg_exit_price"),
            field="avg_exit_price",
        )
        if price is not None:
            exit_value = size * price
    return abs(entry or ZERO), abs(exit_value or ZERO)


def aggregate_trade_records(
    rows: Iterable[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    """Aggregate bot partial closes while keeping account rows independent.

    ``closed_at_ms`` is the latest update of all constituent close records.
    Holding time and R are available only for records enriched by a bot setup.
    """
    grouped: dict[tuple[str, str, str], dict[str, Any]] = {}
    for index, row in enumerate(rows):
        if not isinstance(row, Mapping):
            raise ValueError("Trade analytics rows must be mappings")
        account_scope = str(row.get("account_scope") or "")
        candidate_raw = row.get("candidate_id")
        candidate_id = (
            str(candidate_raw).strip()
            if candidate_raw is not None and str(candidate_raw).strip()
            else None
        )
        record_id = str(
            row.get("record_id")
            or row.get("order_id")
            or f"anonymous:{index}"
        )
        if candidate_id:
            key = ("candidate", account_scope, candidate_id)
            trade_id = candidate_id
        else:
            # A manual partial close cannot be safely reconstructed into a
            # logical position without a locally captured lifecycle.
            key = ("record", account_scope, f"{record_id}:{index}")
            trade_id = record_id

        pnl = _decimal(
            row.get("closed_pnl"),
            field="closed_pnl",
        )
        if pnl is None:
            raise ValueError("closed_pnl is required")
        symbol = str(row.get("symbol") or "").strip().upper()
        if not symbol:
            raise ValueError("symbol is required")
        side_raw = row.get("setup_side") or row.get("position_side")
        side = str(side_raw) if side_raw in {"Buy", "Sell"} else None
        opened_at = _timestamp(
            row.get("setup_opened_at_ms", row.get("opened_at_ms")),
            field="setup_opened_at_ms",
        )
        closed_at = _timestamp(
            row.get("updated_time_ms", row.get("created_time_ms")),
            field="updated_time_ms",
        )
        closed_at = closed_at or 0
        planned_risk = _decimal(
            row.get("planned_risk_usd"),
            field="planned_risk_usd",
        )
        if planned_risk is not None and planned_risk <= ZERO:
            planned_risk = None
        open_fee = _decimal(row.get("open_fee"), field="open_fee")
        close_fee = _decimal(row.get("close_fee"), field="close_fee")
        fee_complete = open_fee is not None and close_fee is not None
        entry_turnover, exit_turnover = _row_turnover(row)

        item = grouped.get(key)
        if item is None:
            item = {
                "trade_id": trade_id,
                "account_scope": account_scope or None,
                "candidate_id": candidate_id,
                "source": "bot" if candidate_id else "account",
                "record_ids": [],
                "symbol": symbol,
                "side": side,
                "parts": 0,
                "closed_pnl": ZERO,
                "fees_total": ZERO,
                "fee_complete_parts": 0,
                "entry_turnover": ZERO,
                "exit_turnover": ZERO,
                "turnover": ZERO,
                "planned_risk_usd": planned_risk,
                "opened_at_ms": opened_at,
                "closed_at_ms": closed_at,
            }
            grouped[key] = item
        else:
            if item["symbol"] != symbol:
                raise ValueError(
                    f"candidate_id {candidate_id!r} spans multiple symbols"
                )
            if side and item["side"] and item["side"] != side:
                raise ValueError(
                    f"candidate_id {candidate_id!r} spans multiple sides"
                )
            if item["side"] is None:
                item["side"] = side
            current_risk = item["planned_risk_usd"]
            if (
                current_risk is not None
                and planned_risk is not None
                and current_risk != planned_risk
            ):
                raise ValueError(
                    f"candidate_id {candidate_id!r} has inconsistent planned risk"
                )
            if current_risk is None:
                item["planned_risk_usd"] = planned_risk
            if opened_at is not None:
                item["opened_at_ms"] = (
                    opened_at
                    if item["opened_at_ms"] is None
                    else min(item["opened_at_ms"], opened_at)
                )
            item["closed_at_ms"] = max(item["closed_at_ms"], closed_at)

        item["record_ids"].append(record_id)
        item["parts"] += 1
        item["closed_pnl"] += pnl
        item["fees_total"] += (open_fee or ZERO) + (close_fee or ZERO)
        item["fee_complete_parts"] += int(fee_complete)
        item["entry_turnover"] += entry_turnover
        item["exit_turnover"] += exit_turnover
        item["turnover"] += entry_turnover + exit_turnover

    trades = list(grouped.values())
    for item in trades:
        opened_at = item["opened_at_ms"]
        closed_at = item["closed_at_ms"]
        item["hold_ms"] = (
            closed_at - opened_at
            if opened_at is not None and closed_at >= opened_at
            else None
        )
        risk = item["planned_risk_usd"]
        item["r_multiple"] = (
            item["closed_pnl"] / risk
            if risk is not None and risk > ZERO
            else None
        )
        item["fee_complete"] = item["fee_complete_parts"] == item["parts"]
    trades.sort(
        key=lambda item: (
            int(item["closed_at_ms"]),
            str(item["trade_id"]),
        )
    )
    return trades


def _median(values: list[Decimal]) -> Optional[Decimal]:
    if not values:
        return None
    ordered = sorted(values)
    middle = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[middle]
    return (ordered[middle - 1] + ordered[middle]) / Decimal("2")


def _ratio(
    numerator: Decimal,
    denominator: Decimal,
    *,
    infinite_when_positive: bool = False,
) -> Optional[Decimal]:
    if denominator > ZERO:
        return numerator / denominator
    if numerator > ZERO and infinite_when_positive:
        return INFINITY
    return None


def _max_streaks(trades: list[dict[str, Any]]) -> dict[str, Any]:
    max_win = max_loss = current_length = 0
    current_kind: Optional[str] = None
    for trade in trades:
        pnl = trade["closed_pnl"]
        kind = "win" if pnl > ZERO else "loss" if pnl < ZERO else None
        if kind is None:
            current_kind = None
            current_length = 0
            continue
        if kind == current_kind:
            current_length += 1
        else:
            current_kind = kind
            current_length = 1
        if kind == "win":
            max_win = max(max_win, current_length)
        else:
            max_loss = max(max_loss, current_length)
    return {
        "max_win_streak": max_win,
        "max_loss_streak": max_loss,
        "current_streak_type": current_kind,
        "current_streak": current_length,
    }


def _closed_pnl_curve(
    trades: list[dict[str, Any]],
) -> tuple[list[Decimal], Decimal]:
    cumulative = [ZERO]
    peak = ZERO
    max_drawdown = ZERO
    for trade in trades:
        current = cumulative[-1] + trade["closed_pnl"]
        cumulative.append(current)
        peak = max(peak, current)
        max_drawdown = max(max_drawdown, peak - current)
    return cumulative, max_drawdown


def _symbol_statistics(
    trades: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    grouped: dict[str, list[Decimal]] = {}
    for trade in trades:
        grouped.setdefault(trade["symbol"], []).append(trade["closed_pnl"])
    result: list[dict[str, Any]] = []
    for symbol in sorted(grouped):
        pnls = grouped[symbol]
        wins = sum(value > ZERO for value in pnls)
        losses = sum(value < ZERO for value in pnls)
        decisive = wins + losses
        result.append(
            {
                "symbol": symbol,
                "trade_count": len(pnls),
                "wins": wins,
                "losses": losses,
                "breakeven": len(pnls) - decisive,
                "net_pnl": sum(pnls, ZERO),
                "win_rate": (
                    Decimal(wins) / Decimal(decisive) * HUNDRED
                    if decisive
                    else None
                ),
            }
        )
    return result


def _side_statistics(trades: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    for name, side in (("long", "Buy"), ("short", "Sell")):
        pnls = [
            trade["closed_pnl"]
            for trade in trades
            if trade["side"] == side
        ]
        wins = sum(value > ZERO for value in pnls)
        losses = sum(value < ZERO for value in pnls)
        decisive = wins + losses
        result[name] = {
            "side": side,
            "trade_count": len(pnls),
            "wins": wins,
            "losses": losses,
            "breakeven": len(pnls) - decisive,
            "net_pnl": sum(pnls, ZERO),
            "win_rate": (
                Decimal(wins) / Decimal(decisive) * HUNDRED
                if decisive
                else None
            ),
        }
    return result


def _sqn(r_values: list[Decimal]) -> Optional[Decimal]:
    """Return Van Tharp's SQN only for a meaningful R-multiple sample."""
    count = len(r_values)
    if count < 30:
        return None
    mean = sum(r_values, ZERO) / Decimal(count)
    variance = sum(
        ((value - mean) ** 2 for value in r_values),
        ZERO,
    ) / Decimal(count - 1)
    if variance <= ZERO:
        return None
    return Decimal(count).sqrt() * mean / variance.sqrt()


def _equity_statistics(
    snapshots: Optional[Iterable[Mapping[str, Any]]],
) -> tuple[Optional[Decimal], Optional[Decimal], int]:
    if snapshots is None:
        return None, None, 0
    by_timestamp: dict[int, Decimal] = {}
    for index, snapshot in enumerate(snapshots):
        if not isinstance(snapshot, Mapping):
            raise ValueError("Equity snapshots must be mappings")
        equity = _decimal(
            snapshot.get("equity_usd"),
            field="equity_usd",
        )
        if equity is None:
            continue
        if equity <= ZERO:
            raise ValueError("equity_usd must be positive")
        timestamp = _timestamp(
            snapshot.get("captured_at_ms"),
            field="captured_at_ms",
        )
        by_timestamp[timestamp if timestamp is not None else index] = equity
    values = [equity for _, equity in sorted(by_timestamp.items())]
    if len(values) < 2:
        return None, None, len(values)
    first, last = values[0], values[-1]
    equity_return = (last - first) / first * HUNDRED
    peak = first
    max_drawdown_percent = ZERO
    for equity in values[1:]:
        peak = max(peak, equity)
        drawdown = (peak - equity) / peak * HUNDRED
        max_drawdown_percent = max(max_drawdown_percent, drawdown)
    return equity_return, max_drawdown_percent, len(values)


def build_trade_analytics(
    rows: Iterable[Mapping[str, Any]],
    equity_snapshots: Optional[Iterable[Mapping[str, Any]]] = None,
) -> dict[str, Any]:
    """Return complete journal metrics using only Decimal arithmetic.

    ``gross_loss`` and ``max_drawdown`` are positive magnitudes. ``avg_loss``
    retains its negative sign. Win rate and equity metrics are percentages in
    the 0..100 scale. ``cumulative_pnl`` starts with a zero baseline. Equity
    values describe raw account-level equity change and drawdown; deposits and
    withdrawals are not adjusted, so they are not strategy-return metrics.
    """
    trades = aggregate_trade_records(rows)
    pnls = [trade["closed_pnl"] for trade in trades]
    win_values = [value for value in pnls if value > ZERO]
    loss_values = [value for value in pnls if value < ZERO]
    wins = len(win_values)
    losses = len(loss_values)
    breakeven = len(pnls) - wins - losses
    decisive = wins + losses
    net_pnl = sum(pnls, ZERO)
    gross_profit = sum(win_values, ZERO)
    gross_loss = -sum(loss_values, ZERO)
    avg_win = (
        gross_profit / Decimal(wins)
        if wins
        else None
    )
    avg_loss = (
        -gross_loss / Decimal(losses)
        if losses
        else None
    )
    profit_factor = _ratio(
        gross_profit,
        gross_loss,
        infinite_when_positive=True,
    )
    payoff_ratio = _ratio(
        avg_win or ZERO,
        abs(avg_loss) if avg_loss is not None else ZERO,
        infinite_when_positive=True,
    )
    cumulative_pnl, max_drawdown = _closed_pnl_curve(trades)
    recovery_factor = (
        net_pnl / max_drawdown
        if max_drawdown > ZERO
        else None
    )
    hold_values = [
        Decimal(trade["hold_ms"])
        for trade in trades
        if trade["hold_ms"] is not None
    ]
    r_values = [
        trade["r_multiple"]
        for trade in trades
        if trade["r_multiple"] is not None
    ]
    symbol_stats = _symbol_statistics(trades)
    per_symbol = {
        item["symbol"]: item
        for item in symbol_stats
    }
    long_short = _side_statistics(trades)
    best_symbol = (
        sorted(
            symbol_stats,
            key=lambda item: (-item["net_pnl"], item["symbol"]),
        )[0]
        if symbol_stats
        else None
    )
    worst_symbol = (
        sorted(
            symbol_stats,
            key=lambda item: (item["net_pnl"], item["symbol"]),
        )[0]
        if symbol_stats
        else None
    )
    equity_return, equity_drawdown, equity_count = _equity_statistics(
        equity_snapshots
    )

    result = {
        "trades": trades,
        "trade_count": len(trades),
        "source_record_count": sum(trade["parts"] for trade in trades),
        "wins": wins,
        "losses": losses,
        "breakeven": breakeven,
        "net_pnl": net_pnl,
        "gross_profit": gross_profit,
        "gross_loss": gross_loss,
        "win_rate": (
            Decimal(wins) / Decimal(decisive) * HUNDRED
            if decisive
            else None
        ),
        "profit_factor": profit_factor,
        "expectancy": (
            net_pnl / Decimal(len(trades))
            if trades
            else None
        ),
        "median_pnl": _median(pnls),
        "avg_win": avg_win,
        "avg_loss": avg_loss,
        "payoff_ratio": payoff_ratio,
        "best_trade": (
            max(trades, key=lambda trade: trade["closed_pnl"])
            if trades
            else None
        ),
        "worst_trade": (
            min(trades, key=lambda trade: trade["closed_pnl"])
            if trades
            else None
        ),
        "max_drawdown": max_drawdown,
        "recovery_factor": recovery_factor,
        **_max_streaks(trades),
        "fees_total": sum(
            (trade["fees_total"] for trade in trades),
            ZERO,
        ),
        "fee_complete_count": sum(
            int(trade["fee_complete"])
            for trade in trades
        ),
        "fee_complete_record_count": sum(
            trade["fee_complete_parts"]
            for trade in trades
        ),
        "turnover": sum(
            (trade["turnover"] for trade in trades),
            ZERO,
        ),
        "avg_hold_ms": (
            sum(hold_values, ZERO) / Decimal(len(hold_values))
            if hold_values
            else None
        ),
        "median_hold_ms": _median(hold_values),
        "hold_count": len(hold_values),
        "avg_r": (
            sum(r_values, ZERO) / Decimal(len(r_values))
            if r_values
            else None
        ),
        "total_r": sum(r_values, ZERO),
        "r_count": len(r_values),
        "sqn": _sqn(r_values),
        "cumulative_pnl": cumulative_pnl,
        "symbol_stats": symbol_stats,
        "per_symbol": per_symbol,
        "best_symbol": best_symbol,
        "worst_symbol": worst_symbol,
        "long_short": long_short,
        "unknown_side_count": sum(
            trade["side"] not in {"Buy", "Sell"}
            for trade in trades
        ),
        "equity_return_percent": equity_return,
        "equity_max_drawdown_percent": equity_drawdown,
        "equity_snapshot_count": equity_count,
    }
    result["current_streak_count"] = result["current_streak"]
    return result


__all__ = [
    "aggregate_trade_records",
    "build_trade_analytics",
]
