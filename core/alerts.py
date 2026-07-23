"""Domain logic for durable price and RSI alerts."""

from __future__ import annotations

import math
from collections import defaultdict
from dataclasses import dataclass

from api.bybit_api import BybitAPI
from core.market_data import calculate_rsi, get_kline_data
from storage.database import SQLiteStore, get_store
from utils.helpers import format_price
from utils.logger_setup import logger


@dataclass(frozen=True)
class AlertEvent:
    outbox_id: int
    chat_id: int
    alert_id: int
    message: str


def _crossed(previous: float | None, current: float, direction: str, threshold: float) -> bool:
    """Trigger only on an actual crossing, never immediately after creation."""
    if previous is None:
        return False
    if direction == "above":
        return previous < threshold <= current
    return previous > threshold >= current


class AlertService:
    """Checks all users' alerts while sharing market requests per instrument."""

    def __init__(self, store: SQLiteStore | None = None, bybit: BybitAPI | None = None) -> None:
        self.store = store or get_store()
        self.bybit = bybit or BybitAPI()

    def _price(self, symbol: str) -> float:
        response = self.bybit.get_tickers(f"{symbol}USDT")
        tickers = response.get("result", {}).get("list", [])
        if not tickers:
            raise ValueError(f"Нет тикера для {symbol}USDT")
        price = float(tickers[0]["lastPrice"])
        if not math.isfinite(price) or price <= 0:
            raise ValueError(f"Некорректная цена {symbol}USDT")
        return price

    def close(self) -> None:
        self.bybit.close()

    def _rsi(self, symbol: str, timeframe: str) -> float:
        candles = get_kline_data(self.bybit, f"{symbol}USDT", interval=timeframe, limit=100)
        if len(candles) < 15:
            raise ValueError(f"Недостаточно свечей для RSI {symbol}/{timeframe}")
        value = round(calculate_rsi([item["close"] for item in candles]), 2)
        if not math.isfinite(value) or not 0 <= value <= 100:
            raise ValueError(f"Некорректный RSI {symbol}/{timeframe}")
        return value

    def check_all(self) -> list[AlertEvent]:
        """Read active alerts, persist observations and return crossed thresholds."""
        active = self.store.get_active_alerts()
        grouped: dict[tuple[str, str, str | None], list[dict]] = defaultdict(list)
        for alert in active:
            grouped[(alert["kind"], alert["symbol"], alert["timeframe"])].append(alert)

        values: dict[tuple[str, str, str | None], float] = {}
        for key in grouped:
            kind, symbol, timeframe = key
            try:
                values[key] = self._price(symbol) if kind == "price" else self._rsi(symbol, timeframe or "15")
            except Exception as error:
                logger.warning(f"Не удалось проверить алерты {kind}/{symbol}/{timeframe}: {error}")

        for key, alerts in grouped.items():
            if key not in values:
                continue
            current = values[key]
            for alert in alerts:
                enabled = bool(
                    alert["price_alerts_enabled"] if alert["kind"] == "price"
                    else alert["rsi_alerts_enabled"]
                ) and bool(alert["notifications_enabled"])
                should_trigger = enabled and _crossed(
                    alert["last_value"], current, alert["direction"], float(alert["threshold"])
                )
                comparator = "≥" if alert["direction"] == "above" else "≤"
                value_text = format_price(current) if alert["kind"] == "price" else f"{current:.2f}"
                unit = "USDT" if alert["kind"] == "price" else ""
                timeframe = f", {alert['timeframe']}" if alert["kind"] == "rsi" else ""
                message = (
                    f"{'💲 Цена' if alert['kind'] == 'price' else '📊 RSI'} {alert['symbol']}{timeframe}: "
                    f"{value_text} {unit} {comparator} {alert['threshold']}"
                ).strip()
                if not self.store.apply_alert_observation(
                    int(alert["id"]),
                    value=current,
                    should_trigger=should_trigger,
                    notification_message=message,
                ):
                    continue

                self.store.log_activity(
                    int(alert["chat_id"]),
                    "alert_triggered",
                    message,
                    severity="warning",
                    symbol=alert["symbol"],
                    payload={"alert_id": alert["id"], "value": current},
                )
        return [
            AlertEvent(
                int(item["id"]),
                int(item["chat_id"]),
                int(item["alert_id"] or 0),
                str(item["message"]),
            )
            for item in self.store.pending_notifications()
        ]
