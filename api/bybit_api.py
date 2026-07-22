# api/bybit_api.py
import time
import hmac
import hashlib
import requests
from urllib.parse import urlencode
import json
from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception_type
from loguru import logger

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

from config import BYBIT_API_KEY, BYBIT_API_SECRET, BYBIT_CATEGORY, DRY_RUN

# Документация Bybit V5 API: https://bybit-exchange.github.io/docs/v5/intro
BASE = "https://api.bybit.com"
REQUEST_TIMEOUT_SECONDS = 15


class BybitAPIError(Exception):
    """Исключение для ошибок Bybit API"""
    def __init__(self, message, code=None, response=None):
        super().__init__(message)
        self.code = code
        self.response = response


class BybitAPI:
    def __init__(self, api_key=BYBIT_API_KEY, api_secret=BYBIT_API_SECRET, base=BASE):
        self.api_key = api_key
        self.api_secret = api_secret
        self.base = base
        self.session = requests.Session()
        self.session.headers.update({"Content-Type": "application/json"})

    def _now_ms(self):
        return str(int(time.time() * 1000))

    def _sign_v5(self, timestamp: str, params_str: str = "") -> str:
        """
        Bybit V5 API подпись: HMAC-SHA256(api_key + timestamp + recv_window + params_str)
        """
        recv_window = "5000"
        param_str = f"{timestamp}{self.api_key}{recv_window}{params_str}"
        return hmac.new(
            self.api_secret.encode('utf-8'),
            param_str.encode('utf-8'),
            hashlib.sha256
        ).hexdigest()

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=2, max=10),
        retry=retry_if_exception_type(requests.exceptions.RequestException)
    )
    def _private_request(self, method: str, endpoint: str, params: dict = None):
        """Выполняет приватный запрос к Bybit V5 API с retry логикой"""
        if params is None:
            params = {}
        if not self.api_key or not self.api_secret:
            raise BybitAPIError("Не заданы BYBIT_API_KEY или BYBIT_API_SECRET")

        timestamp = self._now_ms()
        recv_window = "5000"

        # Для V5 параметры идут в query string для GET или body для POST
        if method.upper() == "GET":
            query_string = urlencode(sorted(params.items())) if params else ""
            sign = self._sign_v5(timestamp, query_string)
            url = f"{self.base}{endpoint}"
            if query_string:
                url = f"{url}?{query_string}"

            headers = {
                "X-BAPI-API-KEY": self.api_key,
                "X-BAPI-SIGN": sign,
                "X-BAPI-TIMESTAMP": timestamp,
                "X-BAPI-RECV-WINDOW": recv_window
            }

            # Dry-run only prevents state-changing requests.  Read-only data is
            # still needed for a useful dashboard and correct risk calculation.
            r = self.session.get(url, headers=headers, timeout=REQUEST_TIMEOUT_SECONDS)
        else:
            # POST
            url = f"{self.base}{endpoint}"
            params_str = json.dumps(params, separators=(",", ":")) if params else ""
            sign = self._sign_v5(timestamp, params_str)

            headers = {
                "X-BAPI-API-KEY": self.api_key,
                "X-BAPI-SIGN": sign,
                "X-BAPI-TIMESTAMP": timestamp,
                "X-BAPI-RECV-WINDOW": recv_window,
                "Content-Type": "application/json"
            }

            if DRY_RUN:
                logger.info(f"[DRY_RUN] POST {url} with body: {params_str}")
                return {"retCode": 0, "retMsg": "OK (dry-run)", "result": {}}

            r = self.session.post(
                url, headers=headers, data=params_str, timeout=REQUEST_TIMEOUT_SECONDS
            )

        # Обработка ответа
        # Keep transport errors as ``RequestException`` so the retry decorator
        # can retry transient network/5xx failures.  A malformed successful
        # response is a business error and should be reported once.
        r.raise_for_status()
        try:
            data = r.json()
        except ValueError as error:
            raise BybitAPIError(
                f"Invalid JSON response: {r.status_code} {r.text}", response=r
            ) from error

        # Проверка retCode
        if data.get("retCode") != 0:
            error_msg = data.get("retMsg", "Unknown error")
            logger.error(f"Bybit API error: {data.get('retCode')} - {error_msg}")
            raise BybitAPIError(error_msg, code=data.get("retCode"), response=data)

        return data

    def _public_get(self, endpoint: str, params: dict = None):
        url = f"{self.base}{endpoint}"
        try:
            response = self.session.get(url, params=params, timeout=REQUEST_TIMEOUT_SECONDS)
            response.raise_for_status()
            data = response.json()
        except (requests.exceptions.RequestException, ValueError) as error:
            raise BybitAPIError(f"Ошибка публичного запроса Bybit: {error}") from error
        if data.get("retCode") != 0:
            raise BybitAPIError(data.get("retMsg", "Unknown public API error"), code=data.get("retCode"), response=data)
        return data

    # === Public data ===
    def get_tickers(self, symbol: str):
        # /v5/market/tickers?category=linear&symbol=BTCUSDT
        params = {"category": BYBIT_CATEGORY, "symbol": symbol}
        return self._public_get("/v5/market/tickers", params=params)

    # === Account / positions ===
    def get_positions(self, symbol: str = None, settle_coin: str = "USDT"):
        """Получить список позиций"""
        params = {"category": BYBIT_CATEGORY}
        if symbol:
            params["symbol"] = symbol
        else:
            # Для получения всех позиций нужен settleCoin
            params["settleCoin"] = settle_coin
        return self._private_request("GET", "/v5/position/list", params=params)

    def get_wallet_balance(self, account_type: str = "UNIFIED"):
        """Получить баланс кошелька"""
        params = {"accountType": account_type}
        return self._private_request("GET", "/v5/account/wallet-balance", params=params)

    def set_leverage(self, symbol: str, buy_leverage: str, sell_leverage: str):
        """Установить leverage для символа"""
        params = {
            "category": BYBIT_CATEGORY,
            "symbol": symbol,
            "buyLeverage": str(buy_leverage),
            "sellLeverage": str(sell_leverage)
        }
        return self._private_request("POST", "/v5/position/set-leverage", params=params)

    # === Orders / trading ===
    def create_order(self, symbol: str, side: str, order_type: str, qty: float,
                     price: float = None, time_in_force: str = "GTC",
                     take_profit: float = None, stop_loss: float = None,
                     reduce_only: bool = False, position_idx: int = 0):
        """
        Создать ордер (market/limit) с опциональными TP/SL.
        order_type: 'Market' or 'Limit'
        side: 'Buy' or 'Sell'
        qty: размер в базовой валюте
        position_idx: 0 - one-way mode, 1 - hedge-mode Buy, 2 - hedge-mode Sell
        """
        params = {
            "category": BYBIT_CATEGORY,
            "symbol": symbol,
            "side": side,
            "orderType": order_type,
            "qty": str(qty),
            "timeInForce": time_in_force,
            "positionIdx": position_idx
        }

        if price is not None:
            params["price"] = str(price)
        if take_profit is not None:
            params["takeProfit"] = str(take_profit)
        if stop_loss is not None:
            params["stopLoss"] = str(stop_loss)
        if reduce_only:
            params["reduceOnly"] = True

        return self._private_request("POST", "/v5/order/create", params=params)

    def set_trading_stop(self, symbol: str, position_idx: int = 0,
                         take_profit: float = None, stop_loss: float = None,
                         tp_trigger_by: str = "LastPrice", sl_trigger_by: str = "LastPrice"):
        """
        Устанавливает/изменяет TP/SL для текущей позиции.
        position_idx: 0 - one-way mode, 1 - hedge-mode Buy, 2 - hedge-mode Sell
        """
        params = {
            "category": BYBIT_CATEGORY,
            "symbol": symbol,
            "positionIdx": position_idx
        }
        if take_profit is not None:
            params["takeProfit"] = str(take_profit)
            params["tpTriggerBy"] = tp_trigger_by
        if stop_loss is not None:
            params["stopLoss"] = str(stop_loss)
            params["slTriggerBy"] = sl_trigger_by

        return self._private_request("POST", "/v5/position/trading-stop", params=params)

    def close_position_market(self, symbol: str, side: str, position_idx: int = 0):
        """
        Закрыть позицию рыночным ордером (reduce-only).
        side: 'Buy' для закрытия short, 'Sell' для закрытия long
        position_idx: 0=one-way, 1=hedge-buy, 2=hedge-sell
        """
        # Получаем текущую позицию для определения qty
        pos_data = self.get_positions(symbol=symbol)
        positions = pos_data.get("result", {}).get("list", [])

        if not positions:
            logger.warning(f"No position found for {symbol}")
            return {"retCode": -1, "retMsg": "No position to close"}

        # Ищем позицию с нужным position_idx
        position = None
        for p in positions:
            if int(p.get("positionIdx", 0)) == position_idx:
                position = p
                break

        if not position:
            logger.warning(f"No position found for {symbol} with position_idx={position_idx}")
            return {"retCode": -1, "retMsg": f"No position with idx={position_idx}"}

        qty = float(position.get("size", 0))

        if qty == 0:
            logger.warning(f"Position size is 0 for {symbol}")
            return {"retCode": -1, "retMsg": "Position size is 0"}

        # Создаем reduce-only ордер с position_idx
        return self.create_order(
            symbol=symbol,
            side=side,
            order_type="Market",
            qty=qty,
            reduce_only=True,
            position_idx=position_idx
        )

    def get_closed_pnl(self, symbol: str = None, limit: int = 20):
        """
        Получить историю закрытых позиций (Closed P&L).
        https://bybit-exchange.github.io/docs/v5/position/close-pnl
        """
        params = {
            "category": BYBIT_CATEGORY,
            "limit": limit
        }
        if symbol:
            params["symbol"] = symbol

        return self._private_request("GET", "/v5/position/closed-pnl", params=params)
