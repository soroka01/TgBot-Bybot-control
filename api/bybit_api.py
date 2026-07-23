"""Small, defensive Bybit V5 REST client.

The client separates retryable reads from state-changing writes, uses stable
``orderLinkId`` values, reads live instrument rules, and reconciles the
asynchronous order acknowledgement before callers report success.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import random
import threading
import time
import uuid
from dataclasses import dataclass
from decimal import Decimal, ROUND_DOWN, ROUND_HALF_UP, ROUND_UP
from typing import Any, Callable, Optional
from urllib.parse import urlencode

import requests
from loguru import logger

from config import (
    BYBIT_API_KEY,
    BYBIT_API_SECRET,
    BYBIT_BASE_URL,
    BYBIT_CATEGORY,
    BYBIT_HTTP_TIMEOUT_SECONDS,
    BYBIT_MAX_SLIPPAGE_PERCENT,
    BYBIT_RECV_WINDOW_MS,
    DRY_RUN,
)


READ_ATTEMPTS = 3
INSTRUMENT_CACHE_SECONDS = 3_600
TERMINAL_ORDER_STATUSES = {
    "Filled",
    "Cancelled",
    "Rejected",
    "Deactivated",
    "PartiallyFilledCanceled",
    "PartiallyFilledCancelled",
}


class BybitAPIError(Exception):
    """A transport or business error returned by Bybit."""

    def __init__(self, message: str, code: Optional[int] = None, response: Any = None):
        super().__init__(message)
        self.code = code
        self.response = response


class BybitAmbiguousWriteError(BybitAPIError):
    """A write may have reached Bybit, so it must be reconciled, not retried."""

    def __init__(
        self,
        message: str,
        *,
        endpoint: str,
        order_link_id: Optional[str] = None,
        response: Any = None,
    ) -> None:
        super().__init__(message, response=response)
        self.endpoint = endpoint
        self.order_link_id = order_link_id


class BybitOrderNotFilledError(BybitAPIError):
    """The acknowledged order reached a non-filled terminal state."""

    def __init__(self, order: dict[str, Any]):
        status = order.get("orderStatus", "unknown")
        super().__init__(f"Ордер не исполнен полностью: {status}", response=order)
        self.order = order


class BybitOrderConfirmationError(BybitAPIError):
    """An acknowledged order did not reach a safely known terminal state."""

    def __init__(
        self,
        message: str,
        *,
        order_link_id: str,
        order: Optional[dict[str, Any]] = None,
    ) -> None:
        super().__init__(message, response=order)
        self.order_link_id = order_link_id
        self.order = order or {}


def _decimal(value: Any) -> Decimal:
    try:
        result = Decimal(str(value))
    except Exception as error:
        raise BybitAPIError(f"Некорректное числовое значение Bybit: {value!r}") from error
    if not result.is_finite():
        raise BybitAPIError(f"Некорректное числовое значение Bybit: {value!r}")
    return result


def _decimal_text(value: Decimal) -> str:
    text = format(value, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text or "0"


def _object_rows(response: dict[str, Any], endpoint: str) -> list[dict[str, Any]]:
    result = response.get("result")
    rows = result.get("list") if isinstance(result, dict) else None
    if not isinstance(rows, list) or any(not isinstance(item, dict) for item in rows):
        raise BybitAPIError(
            f"Bybit {endpoint} не вернул корректный массив result.list",
            response=response,
        )
    return rows


def _next_cursor(response: dict[str, Any], endpoint: str) -> str:
    result = response.get("result")
    raw = result.get("nextPageCursor") if isinstance(result, dict) else None
    if raw is None or raw == "":
        return ""
    if not isinstance(raw, str):
        raise BybitAPIError(
            f"Bybit {endpoint} вернул некорректный nextPageCursor",
            response=response,
        )
    return raw


@dataclass(frozen=True)
class InstrumentRules:
    symbol: str
    status: str
    tick_size: Decimal
    min_qty: Decimal
    qty_step: Decimal
    min_notional: Decimal
    max_market_qty: Decimal
    max_leverage: Decimal
    leverage_step: Decimal

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> "InstrumentRules":
        lot = payload.get("lotSizeFilter", {})
        price = payload.get("priceFilter", {})
        leverage = payload.get("leverageFilter", {})
        rules = cls(
            symbol=str(payload.get("symbol", "")),
            status=str(payload.get("status", "")),
            tick_size=_decimal(price.get("tickSize", "0")),
            min_qty=_decimal(lot.get("minOrderQty", "0")),
            qty_step=_decimal(lot.get("qtyStep", "0")),
            min_notional=_decimal(lot.get("minNotionalValue", "0")),
            max_market_qty=_decimal(
                lot.get("maxMktOrderQty") or lot.get("maxMarketOrderQty") or "0"
            ),
            max_leverage=_decimal(leverage.get("maxLeverage", "1")),
            leverage_step=_decimal(leverage.get("leverageStep", "1")),
        )
        if (
            not rules.symbol
            or rules.tick_size <= 0
            or rules.qty_step <= 0
            or rules.min_qty <= 0
            or rules.max_leverage < 1
            or rules.leverage_step <= 0
        ):
            raise BybitAPIError(f"Bybit вернул неполные правила инструмента {rules.symbol!r}")
        return rules

    def quantity(self, requested: Any) -> Decimal:
        value = _decimal(requested)
        if value <= 0:
            return Decimal("0")
        return (value / self.qty_step).to_integral_value(rounding=ROUND_DOWN) * self.qty_step

    def price(self, requested: Any, rounding: str = ROUND_HALF_UP) -> Decimal:
        value = _decimal(requested)
        if value <= 0:
            return Decimal("0")
        return (value / self.tick_size).to_integral_value(rounding=rounding) * self.tick_size

    def validate_quantity(self, quantity: Decimal, reference_price: Decimal) -> None:
        if self.status != "Trading":
            raise BybitAPIError(f"{self.symbol} недоступен для торговли: status={self.status}")
        if quantity < self.min_qty:
            raise BybitAPIError(
                f"{self.symbol}: количество {_decimal_text(quantity)} ниже "
                f"минимума {_decimal_text(self.min_qty)}"
            )
        if self.max_market_qty > 0 and quantity > self.max_market_qty:
            raise BybitAPIError(
                f"{self.symbol}: количество выше maxMktOrderQty "
                f"{_decimal_text(self.max_market_qty)}"
            )
        notional = quantity * reference_price
        if self.min_notional > 0 and notional < self.min_notional:
            raise BybitAPIError(
                f"{self.symbol}: номинал {_decimal_text(notional)} ниже "
                f"minNotionalValue {_decimal_text(self.min_notional)}"
            )


class BybitAPI:
    _instrument_cache: dict[tuple[str, str], tuple[float, InstrumentRules]] = {}
    _instrument_lock = threading.Lock()
    _time_cache: dict[str, tuple[float, int]] = {}
    _time_lock = threading.Lock()

    def __init__(
        self,
        api_key: str = BYBIT_API_KEY,
        api_secret: str = BYBIT_API_SECRET,
        base: str = BYBIT_BASE_URL,
        *,
        dry_run: bool = DRY_RUN,
        timeout: float = BYBIT_HTTP_TIMEOUT_SECONDS,
        session: Optional[requests.Session] = None,
    ) -> None:
        self.api_key = api_key
        self.api_secret = api_secret
        self.base = base.rstrip("/")
        self.dry_run = dry_run
        self.timeout = timeout
        self.recv_window = str(BYBIT_RECV_WINDOW_MS)
        self.session = session or requests.Session()
        self.session.headers.update(
            {
                "Content-Type": "application/json",
                "User-Agent": "soroka01-crypto-bot/2",
            }
        )
        self._server_offset_ms = 0
        self._last_time_sync = 0.0
        with self._time_lock:
            cached_time = self._time_cache.get(self.base)
        if cached_time and time.monotonic() - cached_time[0] < 300:
            self._last_time_sync, self._server_offset_ms = cached_time
        self.last_rate_limit: dict[str, str] = {}

    def close(self) -> None:
        self.session.close()

    def _now_ms(self) -> str:
        return str(int(time.time() * 1_000) + self._server_offset_ms)

    def _sign_v5(self, timestamp: str, params_str: str = "") -> str:
        """HMAC-SHA256(timestamp + api_key + recv_window + payload)."""
        signed = f"{timestamp}{self.api_key}{self.recv_window}{params_str}"
        return hmac.new(
            self.api_secret.encode("utf-8"),
            signed.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()

    def _capture_rate_headers(self, response: requests.Response) -> None:
        self.last_rate_limit = {
            key: response.headers.get(key, "")
            for key in (
                "X-Bapi-Limit",
                "X-Bapi-Limit-Status",
                "X-Bapi-Limit-Reset-Timestamp",
            )
            if response.headers.get(key)
        }

    @staticmethod
    def _retry_delay(attempt: int, response: Optional[requests.Response] = None) -> float:
        if response is not None:
            reset = response.headers.get("X-Bapi-Limit-Reset-Timestamp")
            if reset and reset.isdigit():
                wait = int(reset) / 1_000 - time.time()
                if 0 < wait <= 10:
                    return wait + random.uniform(0.05, 0.20)
            retry_after = response.headers.get("Retry-After")
            if retry_after:
                try:
                    return min(10.0, max(0.1, float(retry_after)))
                except ValueError:
                    pass
        return min(4.0, (2 ** (attempt - 1)) + random.uniform(0.05, 0.30))

    @staticmethod
    def _decode(response: requests.Response) -> dict[str, Any]:
        try:
            data = response.json()
        except ValueError as error:
            raise BybitAPIError(
                f"Bybit вернул не-JSON ответ HTTP {response.status_code}",
                response=response,
            ) from error
        if not isinstance(data, dict):
            raise BybitAPIError("Bybit вернул JSON неожиданного типа", response=data)
        return data

    def _public_get(self, endpoint: str, params: Optional[dict[str, Any]] = None) -> dict:
        url = f"{self.base}{endpoint}"
        last_error: Optional[Exception] = None
        for attempt in range(1, READ_ATTEMPTS + 1):
            response: Optional[requests.Response] = None
            try:
                response = self.session.get(url, params=params or {}, timeout=self.timeout)
                self._capture_rate_headers(response)
                if response.status_code == 403:
                    raise BybitAPIError("Bybit отклонил запрос (HTTP 403)", response=response)
                if response.status_code == 429 or response.status_code >= 500:
                    raise requests.HTTPError(
                        f"HTTP {response.status_code}", response=response
                    )
                response.raise_for_status()
                data = self._decode(response)
                code = data.get("retCode")
                if code == 0:
                    if not isinstance(data.get("result"), dict):
                        raise BybitAPIError(
                            f"Bybit public GET {endpoint} вернул повреждённый result",
                            response=data,
                        )
                    return data
                if code == 10006:
                    raise requests.HTTPError("Bybit rate limit", response=response)
                raise BybitAPIError(
                    str(data.get("retMsg", "Unknown public API error")),
                    code=code,
                    response=data,
                )
            except BybitAPIError:
                raise
            except (requests.RequestException, ValueError) as error:
                last_error = error
                if attempt >= READ_ATTEMPTS:
                    break
                time.sleep(self._retry_delay(attempt, response))
        raise BybitAPIError(f"Ошибка публичного запроса Bybit: {last_error}") from last_error

    def sync_server_time(self) -> int:
        before = int(time.time() * 1_000)
        data = self._public_get("/v5/market/time")
        after = int(time.time() * 1_000)
        result = data.get("result", {})
        server_ms = int(data.get("time") or int(result.get("timeNano", "0")) // 1_000_000)
        if server_ms <= 0:
            server_ms = int(result.get("timeSecond", "0")) * 1_000
        if server_ms <= 0:
            raise BybitAPIError("Bybit не вернул серверное время")
        self._server_offset_ms = server_ms - ((before + after) // 2)
        self._last_time_sync = time.monotonic()
        with self._time_lock:
            self._time_cache[self.base] = (
                self._last_time_sync,
                self._server_offset_ms,
            )
        return self._server_offset_ms

    def _ensure_time_sync(self) -> None:
        if time.monotonic() - self._last_time_sync < 300:
            return
        with self._time_lock:
            cached = self._time_cache.get(self.base)
        if cached and time.monotonic() - cached[0] < 300:
            self._last_time_sync, self._server_offset_ms = cached
            return
        try:
            self.sync_server_time()
        except Exception as error:
            logger.warning(f"Не удалось синхронизировать время Bybit, использую системное: {error}")

    def _private_request(
        self,
        method: str,
        endpoint: str,
        params: Optional[dict[str, Any]] = None,
    ) -> dict:
        method = method.upper()
        payload = dict(params or {})
        if method != "GET" and self.dry_run:
            logger.info(f"[DRY] Заблокирован {method} {endpoint}")
            return {
                "retCode": 0,
                "retMsg": "DRY preview: запрос не отправлен",
                "result": {
                    "simulated": True,
                    "orderLinkId": payload.get("orderLinkId", ""),
                },
            }
        if not self.api_key or not self.api_secret:
            raise BybitAPIError("Не заданы BYBIT_API_KEY или BYBIT_API_SECRET")

        self._ensure_time_sync()
        url = f"{self.base}{endpoint}"
        max_attempts = READ_ATTEMPTS if method == "GET" else 2
        resynced = False

        for attempt in range(1, max_attempts + 1):
            timestamp = self._now_ms()
            query_string = urlencode(sorted(payload.items())) if method == "GET" else ""
            body = (
                ""
                if method == "GET"
                else json.dumps(payload, separators=(",", ":"), ensure_ascii=False)
            )
            signed_payload = query_string if method == "GET" else body
            headers = {
                "X-BAPI-API-KEY": self.api_key,
                "X-BAPI-SIGN": self._sign_v5(timestamp, signed_payload),
                "X-BAPI-TIMESTAMP": timestamp,
                "X-BAPI-RECV-WINDOW": self.recv_window,
                "cdn-request-id": uuid.uuid4().hex,
            }
            request_url = f"{url}?{query_string}" if query_string else url
            response: Optional[requests.Response] = None
            try:
                if method == "GET":
                    response = self.session.get(
                        request_url, headers=headers, timeout=self.timeout
                    )
                else:
                    response = self.session.post(
                        request_url, headers=headers, data=body, timeout=self.timeout
                    )
                self._capture_rate_headers(response)
                if response.status_code == 403:
                    raise BybitAPIError("Bybit отклонил запрос (HTTP 403)", response=response)
                if response.status_code == 429 or response.status_code >= 500:
                    raise requests.HTTPError(
                        f"HTTP {response.status_code}", response=response
                    )
                response.raise_for_status()
                try:
                    data = self._decode(response)
                except BybitAPIError as error:
                    if method != "GET":
                        raise BybitAmbiguousWriteError(
                            f"Bybit принял {endpoint}, но ответ невозможно разобрать",
                            endpoint=endpoint,
                            order_link_id=str(payload.get("orderLinkId") or "") or None,
                            response=response,
                        ) from error
                    raise
            except BybitAPIError:
                raise
            except requests.RequestException as error:
                if method != "GET":
                    raise BybitAmbiguousWriteError(
                        f"Неопределённый результат {endpoint}: {error}",
                        endpoint=endpoint,
                        order_link_id=str(payload.get("orderLinkId") or "") or None,
                        response=response,
                    ) from error
                if attempt >= max_attempts:
                    raise BybitAPIError(f"Ошибка приватного GET Bybit: {error}") from error
                time.sleep(self._retry_delay(attempt, response))
                continue

            code = data.get("retCode")
            if not isinstance(code, int):
                if method != "GET":
                    raise BybitAmbiguousWriteError(
                        f"Bybit вернул неопределённый ответ на {endpoint}",
                        endpoint=endpoint,
                        order_link_id=str(payload.get("orderLinkId") or "") or None,
                        response=data,
                    )
                raise BybitAPIError(
                    f"Bybit GET {endpoint} не содержит корректный retCode",
                    response=data,
                )
            if code == 0:
                if not isinstance(data.get("result"), dict):
                    if method != "GET":
                        raise BybitAmbiguousWriteError(
                            f"Bybit вернул повреждённый result на {endpoint}",
                            endpoint=endpoint,
                            order_link_id=str(payload.get("orderLinkId") or "") or None,
                            response=data,
                        )
                    raise BybitAPIError(
                        f"Bybit GET {endpoint} вернул повреждённый result",
                        response=data,
                    )
                return data
            message = str(data.get("retMsg", "Unknown error"))
            if code == 10002 and not resynced:
                self.sync_server_time()
                resynced = True
                continue
            if code == 10006 and attempt < max_attempts:
                time.sleep(self._retry_delay(attempt, response))
                continue
            raise BybitAPIError(message, code=code, response=data)

        raise BybitAPIError(f"Bybit не выполнил запрос {endpoint}")

    # ---- Public market data -------------------------------------------------
    def get_tickers(self, symbol: str) -> dict:
        return self._public_get(
            "/v5/market/tickers",
            params={"category": BYBIT_CATEGORY, "symbol": symbol.upper()},
        )

    def get_kline(self, symbol: str, interval: str, limit: int = 200) -> dict:
        return self._public_get(
            "/v5/market/kline",
            params={
                "category": BYBIT_CATEGORY,
                "symbol": symbol.upper(),
                "interval": str(interval),
                "limit": max(1, min(int(limit), 1_000)),
            },
        )

    def get_instrument_rules(self, symbol: str, *, refresh: bool = False) -> InstrumentRules:
        symbol = symbol.upper()
        cache_key = (self.base, symbol)
        now = time.monotonic()
        with self._instrument_lock:
            cached = self._instrument_cache.get(cache_key)
            if not refresh and cached and now - cached[0] < INSTRUMENT_CACHE_SECONDS:
                return cached[1]
        response = self._public_get(
            "/v5/market/instruments-info",
            params={"category": BYBIT_CATEGORY, "symbol": symbol},
        )
        rows = _object_rows(response, "/v5/market/instruments-info")
        payload = next((row for row in rows if row.get("symbol") == symbol), None)
        if not payload:
            raise BybitAPIError(f"Bybit не вернул правила инструмента {symbol}")
        rules = InstrumentRules.from_payload(payload)
        with self._instrument_lock:
            self._instrument_cache[cache_key] = (now, rules)
        return rules

    # ---- Account and positions ---------------------------------------------
    def get_positions(
        self,
        symbol: Optional[str] = None,
        settle_coin: Optional[str] = "USDT",
        *,
        category: str = BYBIT_CATEGORY,
    ) -> dict:
        base_params: dict[str, Any] = {"category": category, "limit": 200}
        if symbol:
            base_params["symbol"] = symbol.upper()
        elif settle_coin:
            base_params["settleCoin"] = settle_coin
        rows: list[dict[str, Any]] = []
        cursor = ""
        seen_cursors: set[str] = set()
        first_response: Optional[dict] = None
        while True:
            params = dict(base_params)
            if cursor:
                params["cursor"] = cursor
            response = self._private_request("GET", "/v5/position/list", params=params)
            first_response = first_response or response
            rows.extend(_object_rows(response, "/v5/position/list"))
            next_cursor = _next_cursor(response, "/v5/position/list")
            if not next_cursor:
                break
            if next_cursor in seen_cursors:
                raise BybitAPIError(
                    "Bybit /v5/position/list повторил pagination cursor",
                    response=response,
                )
            seen_cursors.add(next_cursor)
            cursor = next_cursor
        merged = dict(first_response or {"retCode": 0, "retMsg": "OK", "result": {}})
        merged["result"] = {**merged.get("result", {}), "list": rows, "nextPageCursor": ""}
        return merged

    def get_wallet_balance(self, account_type: str = "UNIFIED") -> dict:
        return self._private_request(
            "GET",
            "/v5/account/wallet-balance",
            params={"accountType": account_type},
        )

    def get_account_info(self) -> dict:
        return self._private_request("GET", "/v5/account/info", params={})

    def get_fee_rate(self, symbol: str) -> Decimal:
        response = self._private_request(
            "GET",
            "/v5/account/fee-rate",
            params={"category": BYBIT_CATEGORY, "symbol": symbol.upper()},
        )
        rows = _object_rows(response, "/v5/account/fee-rate")
        if not rows:
            raise BybitAPIError(f"Bybit не вернул комиссию для {symbol}")
        return _decimal(rows[0].get("takerFeeRate", "0"))

    def get_open_orders(
        self,
        symbol: Optional[str] = None,
        *,
        category: str = BYBIT_CATEGORY,
        settle_coin: Optional[str] = "USDT",
    ) -> dict:
        base_params: dict[str, Any] = {"category": category, "limit": 50}
        if symbol:
            base_params["symbol"] = symbol.upper()
        elif settle_coin:
            base_params["settleCoin"] = settle_coin
        rows: list[dict[str, Any]] = []
        cursor = ""
        seen_cursors: set[str] = set()
        first: Optional[dict] = None
        while True:
            params = dict(base_params)
            if cursor:
                params["cursor"] = cursor
            response = self._private_request("GET", "/v5/order/realtime", params=params)
            first = first or response
            rows.extend(_object_rows(response, "/v5/order/realtime"))
            next_cursor = _next_cursor(response, "/v5/order/realtime")
            if not next_cursor:
                break
            if next_cursor in seen_cursors:
                raise BybitAPIError(
                    "Bybit /v5/order/realtime повторил pagination cursor",
                    response=response,
                )
            seen_cursors.add(next_cursor)
            cursor = next_cursor
        merged = dict(first or {"retCode": 0, "retMsg": "OK", "result": {}})
        merged["result"] = {**merged.get("result", {}), "list": rows, "nextPageCursor": ""}
        return merged

    def set_leverage(self, symbol: str, buy_leverage: Any, sell_leverage: Any) -> dict:
        rules = self.get_instrument_rules(symbol)
        buy = _decimal(buy_leverage)
        sell = _decimal(sell_leverage)
        if buy < 1 or sell < 1 or buy > rules.max_leverage or sell > rules.max_leverage:
            raise BybitAPIError(
                f"{symbol}: плечо вне диапазона 1–{_decimal_text(rules.max_leverage)}"
            )
        return self._private_request(
            "POST",
            "/v5/position/set-leverage",
            params={
                "category": BYBIT_CATEGORY,
                "symbol": symbol.upper(),
                "buyLeverage": _decimal_text(buy),
                "sellLeverage": _decimal_text(sell),
            },
        )

    # ---- Orders and reconciliation -----------------------------------------
    @staticmethod
    def new_order_link_id(prefix: str = "cb") -> str:
        return f"{prefix}-{int(time.time() * 1000):x}-{uuid.uuid4().hex[:10]}"[:36]

    def prepare_quantity(self, symbol: str, quantity: Any, reference_price: Any) -> Decimal:
        rules = self.get_instrument_rules(symbol)
        prepared = rules.quantity(quantity)
        rules.validate_quantity(prepared, _decimal(reference_price))
        return prepared

    def prepare_protective_prices(
        self,
        symbol: str,
        side: str,
        take_profit: Any,
        stop_loss: Any,
    ) -> tuple[Decimal, Decimal]:
        rules = self.get_instrument_rules(symbol)
        if side == "Buy":
            tp = rules.price(take_profit, ROUND_DOWN)
            sl = rules.price(stop_loss, ROUND_UP)
        elif side == "Sell":
            tp = rules.price(take_profit, ROUND_UP)
            sl = rules.price(stop_loss, ROUND_DOWN)
        else:
            raise BybitAPIError(f"Неизвестная сторона: {side}")
        return tp, sl

    def create_order(
        self,
        symbol: str,
        side: str,
        order_type: str,
        qty: Any,
        *,
        price: Any = None,
        time_in_force: Optional[str] = None,
        take_profit: Any = None,
        stop_loss: Any = None,
        reduce_only: bool = False,
        position_idx: int = 0,
        order_link_id: Optional[str] = None,
    ) -> dict:
        link_id = order_link_id or self.new_order_link_id("cb")
        params: dict[str, Any] = {
            "category": BYBIT_CATEGORY,
            "symbol": symbol.upper(),
            "side": side,
            "orderType": order_type,
            "qty": _decimal_text(_decimal(qty)),
            "positionIdx": int(position_idx),
            "orderLinkId": link_id,
        }
        if price is not None:
            params["price"] = _decimal_text(_decimal(price))
        if order_type == "Market":
            if take_profit is not None or stop_loss is not None:
                raise BybitAPIError(
                    "Market entry с slippageTolerance нельзя совмещать с TP/SL; "
                    "установите защиту подтверждённым вторым запросом"
                )
            params["slippageToleranceType"] = "Percent"
            params["slippageTolerance"] = f"{BYBIT_MAX_SLIPPAGE_PERCENT:.2f}"
        else:
            params["timeInForce"] = time_in_force or "GTC"
        if take_profit is not None:
            params.update(
                {
                    "takeProfit": _decimal_text(_decimal(take_profit)),
                    "tpTriggerBy": "MarkPrice",
                    "tpOrderType": "Market",
                }
            )
        if stop_loss is not None:
            params.update(
                {
                    "stopLoss": _decimal_text(_decimal(stop_loss)),
                    "slTriggerBy": "MarkPrice",
                    "slOrderType": "Market",
                }
            )
        if take_profit is not None or stop_loss is not None:
            params["tpslMode"] = "Full"
        if reduce_only:
            params["reduceOnly"] = True
        return self._private_request("POST", "/v5/order/create", params=params)

    def get_order(
        self,
        *,
        symbol: str,
        order_id: Optional[str] = None,
        order_link_id: Optional[str] = None,
    ) -> Optional[dict[str, Any]]:
        if not order_id and not order_link_id:
            raise ValueError("Нужен order_id или order_link_id")
        params: dict[str, Any] = {
            "category": BYBIT_CATEGORY,
            "symbol": symbol.upper(),
        }
        if order_id:
            params["orderId"] = order_id
        else:
            params["orderLinkId"] = order_link_id
        realtime = self._private_request("GET", "/v5/order/realtime", params=params)
        rows = _object_rows(realtime, "/v5/order/realtime")
        if rows:
            return rows[0]
        history = self._private_request("GET", "/v5/order/history", params=params)
        history_rows = _object_rows(history, "/v5/order/history")
        return history_rows[0] if history_rows else None

    def wait_for_order(
        self,
        *,
        symbol: str,
        order_id: Optional[str] = None,
        order_link_id: Optional[str] = None,
        timeout_seconds: float = 10.0,
    ) -> dict[str, Any]:
        deadline = time.monotonic() + timeout_seconds
        last_order: Optional[dict[str, Any]] = None
        while time.monotonic() < deadline:
            last_order = self.get_order(
                symbol=symbol,
                order_id=order_id,
                order_link_id=order_link_id,
            )
            if last_order and last_order.get("orderStatus") in TERMINAL_ORDER_STATUSES:
                return last_order
            time.sleep(0.35)
        if last_order:
            raise BybitAPIError(
                f"Статус ордера не подтверждён: {last_order.get('orderStatus')}",
                response=last_order,
            )
        raise BybitAPIError("Bybit не вернул ордер для подтверждения")

    def place_order_and_confirm(self, **order: Any) -> dict[str, Any]:
        link_id = str(order.get("order_link_id") or self.new_order_link_id("cb"))
        order["order_link_id"] = link_id
        try:
            acknowledgement = self.create_order(**order)
        except BybitAmbiguousWriteError as error:
            if not error.order_link_id:
                raise
            logger.warning(
                f"Ответ create-order потерян; сверяю стабильный orderLinkId={error.order_link_id}"
            )
            acknowledgement = {"result": {"orderLinkId": error.order_link_id}}

        raw_result = acknowledgement.get("result", {})
        if not isinstance(raw_result, dict):
            logger.warning(
                "Bybit create-order ACK имеет повреждённый result; "
                f"сверяю стабильный orderLinkId={link_id}"
            )
            result: dict[str, Any] = {}
        else:
            result = raw_result
        if result.get("simulated"):
            return {
                "orderStatus": "DryPreview",
                "orderLinkId": link_id,
                "simulated": True,
                "cumExecQty": "0",
            }
        try:
            final = self.wait_for_order(
                symbol=str(order["symbol"]),
                order_id=result.get("orderId"),
                order_link_id=link_id,
            )
        except BybitAPIError as error:
            last = error.response if isinstance(error.response, dict) else None
            try:
                reconciled = self.get_order(
                    symbol=str(order["symbol"]),
                    order_link_id=link_id,
                )
            except BybitAPIError:
                reconciled = last
            if reconciled and reconciled.get("orderStatus") in TERMINAL_ORDER_STATUSES:
                if reconciled.get("orderStatus") == "Filled":
                    return reconciled
                raise BybitOrderNotFilledError(reconciled) from error
            raise BybitOrderConfirmationError(
                f"Не удалось подтвердить итог ордера {link_id}",
                order_link_id=link_id,
                order=reconciled or last,
            ) from error
        if final.get("orderStatus") != "Filled":
            raise BybitOrderNotFilledError(final)
        return final

    def cancel_order_and_confirm(
        self,
        *,
        symbol: str,
        order_link_id: str,
        timeout_seconds: float = 10.0,
    ) -> dict[str, Any]:
        """Cancel an uncertain order and require a terminal reconciliation."""
        params = {
            "category": BYBIT_CATEGORY,
            "symbol": symbol.upper(),
            "orderLinkId": order_link_id,
        }
        cancel_error: Optional[BybitAPIError] = None
        try:
            self._private_request("POST", "/v5/order/cancel", params=params)
        except BybitAPIError as error:
            # A known "too late/not found" response and a lost response both
            # race with IOC completion.  Reconcile the stable ID before
            # deciding whether this is fatal.
            cancel_error = error
            logger.warning(
                f"Cancel {order_link_id} ответил ошибкой; сверяю итог: {error}"
            )
        try:
            final = self.wait_for_order(
                symbol=symbol,
                order_link_id=order_link_id,
                timeout_seconds=timeout_seconds,
            )
        except BybitAPIError as error:
            raise BybitOrderConfirmationError(
                f"Не удалось подтвердить отмену ордера {order_link_id}",
                order_link_id=order_link_id,
                order=error.response if isinstance(error.response, dict) else None,
            ) from (cancel_error or error)
        if final.get("orderStatus") not in TERMINAL_ORDER_STATUSES:
            raise BybitOrderConfirmationError(
                f"Отмена ордера {order_link_id} не подтверждена",
                order_link_id=order_link_id,
                order=final,
            )
        return final

    def wait_for_position(
        self,
        symbol: str,
        position_idx: int,
        predicate: Callable[[dict[str, Any]], bool],
        *,
        timeout_seconds: float = 8.0,
    ) -> dict[str, Any]:
        deadline = time.monotonic() + timeout_seconds
        last: dict[str, Any] = {}
        while time.monotonic() < deadline:
            rows = self.get_positions(symbol=symbol).get("result", {}).get("list", [])
            last = next(
                (
                    item
                    for item in rows
                    if int(item.get("positionIdx", 0)) == int(position_idx)
                ),
                {},
            )
            if predicate(last):
                return last
            time.sleep(0.35)
        raise BybitAPIError(f"Позиция {symbol}/{position_idx} не подтвердила новое состояние")

    def set_trading_stop(
        self,
        symbol: str,
        position_idx: int = 0,
        *,
        take_profit: Any,
        stop_loss: Any,
        tp_trigger_by: str = "MarkPrice",
        sl_trigger_by: str = "MarkPrice",
    ) -> dict:
        if take_profit is None or stop_loss is None:
            raise BybitAPIError("TP и SL должны обновляться парой")
        return self._private_request(
            "POST",
            "/v5/position/trading-stop",
            params={
                "category": BYBIT_CATEGORY,
                "symbol": symbol.upper(),
                "positionIdx": int(position_idx),
                "tpslMode": "Full",
                "takeProfit": _decimal_text(_decimal(take_profit)),
                "stopLoss": _decimal_text(_decimal(stop_loss)),
                "tpTriggerBy": tp_trigger_by,
                "slTriggerBy": sl_trigger_by,
                "tpOrderType": "Market",
                "slOrderType": "Market",
            },
        )

    def set_trading_stop_and_verify(
        self,
        symbol: str,
        position_idx: int,
        *,
        take_profit: Any,
        stop_loss: Any,
    ) -> dict[str, Any]:
        response = self.set_trading_stop(
            symbol,
            position_idx,
            take_profit=take_profit,
            stop_loss=stop_loss,
        )
        if response.get("result", {}).get("simulated"):
            return {
                "symbol": symbol,
                "positionIdx": position_idx,
                "takeProfit": str(take_profit),
                "stopLoss": str(stop_loss),
                "simulated": True,
            }
        expected_tp = _decimal(take_profit)
        expected_sl = _decimal(stop_loss)

        def protected(position: dict[str, Any]) -> bool:
            return (
                _decimal(position.get("takeProfit") or "0") == expected_tp
                and _decimal(position.get("stopLoss") or "0") == expected_sl
            )

        return self.wait_for_position(symbol, position_idx, protected)

    def close_position_market(
        self,
        symbol: str,
        side: str,
        position_idx: int = 0,
        *,
        order_link_id: Optional[str] = None,
    ) -> dict:
        rows = self.get_positions(symbol=symbol).get("result", {}).get("list", [])
        position = next(
            (
                item
                for item in rows
                if int(item.get("positionIdx", 0)) == int(position_idx)
                and _decimal(item.get("size", "0")) > 0
            ),
            None,
        )
        if not position:
            raise BybitAPIError(f"Нет позиции {symbol}/{position_idx} для закрытия")
        result = self.place_order_and_confirm(
            symbol=symbol,
            side=side,
            order_type="Market",
            qty=position["size"],
            reduce_only=True,
            position_idx=position_idx,
            order_link_id=order_link_id or self.new_order_link_id("close"),
        )
        if result.get("simulated"):
            return result
        self.wait_for_position(
            symbol,
            position_idx,
            lambda item: _decimal(item.get("size", "0")) == 0,
        )
        return result

    def get_closed_pnl(
        self,
        symbol: Optional[str] = None,
        limit: int = 100,
        *,
        start_time: Optional[int] = None,
        end_time: Optional[int] = None,
        all_pages: bool = False,
    ) -> dict:
        base_params: dict[str, Any] = {
            "category": BYBIT_CATEGORY,
            "limit": max(1, min(limit, 100)),
        }
        if symbol:
            base_params["symbol"] = symbol.upper()
        if start_time is not None:
            base_params["startTime"] = int(start_time)
        if end_time is not None:
            base_params["endTime"] = int(end_time)
        if not all_pages:
            response = self._private_request(
                "GET",
                "/v5/position/closed-pnl",
                params=base_params,
            )
            _object_rows(response, "/v5/position/closed-pnl")
            return response
        rows: list[dict[str, Any]] = []
        cursor = ""
        seen: set[str] = set()
        first: Optional[dict] = None
        while True:
            params = dict(base_params)
            if cursor:
                params["cursor"] = cursor
            response = self._private_request(
                "GET",
                "/v5/position/closed-pnl",
                params=params,
            )
            first = first or response
            rows.extend(_object_rows(response, "/v5/position/closed-pnl"))
            next_cursor = _next_cursor(response, "/v5/position/closed-pnl")
            if not next_cursor:
                break
            if next_cursor in seen:
                raise BybitAPIError(
                    "Bybit /v5/position/closed-pnl повторил pagination cursor",
                    response=response,
                )
            seen.add(next_cursor)
            cursor = next_cursor
        merged = dict(first or {"retCode": 0, "retMsg": "OK", "result": {}})
        merged["result"] = {**merged.get("result", {}), "list": rows, "nextPageCursor": ""}
        return merged
