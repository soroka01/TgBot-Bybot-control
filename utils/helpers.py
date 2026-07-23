"""Small shared parsing and presentation helpers."""

from __future__ import annotations

import math
from typing import Any, Iterable, Optional, Tuple

from config import FALLBACK_TAKER_FEE_RATE, MAX_LEVERAGE
from core.risk_engine import portfolio_risk_usd


def to_float(value: Any, default: float = 0.0) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return default
    return result if math.isfinite(result) else default


def format_price(value: Any) -> str:
    price = to_float(value)
    if price >= 1_000:
        decimals = 2
    elif price >= 1:
        decimals = 4
    else:
        decimals = 6
    return f"${price:,.{decimals}f}"


def calculate_position_roi(
    unrealized_pnl: float,
    quantity: float,
    entry_price: float,
    leverage: float,
) -> float:
    notional = to_float(quantity) * to_float(entry_price)
    margin = notional / to_float(leverage, 1.0) if leverage else 0.0
    return to_float(unrealized_pnl) / margin * 100 if margin > 0 else 0.0


def parse_account_overview(
    wallet_response: dict,
    max_leverage: int = MAX_LEVERAGE,
    *,
    strict: bool = False,
) -> dict:
    if not isinstance(wallet_response, dict):
        raise ValueError("Bybit вернул повреждённый ответ баланса аккаунта")
    result = wallet_response.get("result", {})
    result_list = result.get("list", []) if isinstance(result, dict) else []
    if not isinstance(result_list, list) or not result_list:
        raise ValueError("Bybit не вернул баланс аккаунта")
    account = result_list[0]
    if not isinstance(account, dict):
        raise ValueError("Bybit вернул повреждённый баланс аккаунта")
    coins = account.get("coin", [])
    if not isinstance(coins, list):
        raise ValueError("Bybit вернул повреждённый список активов аккаунта")
    usdt = next(
        (
            coin
            for coin in coins
            if isinstance(coin, dict) and coin.get("coin") == "USDT"
        ),
        {},
    )
    if strict:
        def required_number(field: str) -> float:
            raw = account.get(field)
            if raw is None or raw == "" or isinstance(raw, bool):
                raise ValueError(
                    f"Bybit не вернул обязательное числовое поле {field}"
                )
            try:
                value = float(raw)
            except (TypeError, ValueError) as error:
                raise ValueError(
                    f"Bybit вернул некорректное числовое поле {field}"
                ) from error
            if not math.isfinite(value):
                raise ValueError(
                    f"Bybit вернул некорректное числовое поле {field}"
                )
            return value

        equity = required_number("totalEquity")
        wallet_balance = required_number("totalWalletBalance")
        unrealized_pnl = required_number("totalPerpUPL")
        position_margin = required_number("totalInitialMargin")
        available_raw = required_number("totalAvailableBalance")
        if position_margin < 0:
            raise ValueError(
                "Bybit вернул отрицательную начальную маржу аккаунта"
            )
    else:
        equity = to_float(account.get("totalEquity"), to_float(usdt.get("equity")))
        wallet_balance = to_float(
            account.get("totalWalletBalance"),
            to_float(usdt.get("walletBalance")),
        )
        unrealized_pnl = to_float(
            account.get("totalPerpUPL"),
            to_float(usdt.get("unrealisedPnl")),
        )
        position_margin = to_float(
            account.get("totalInitialMargin"),
            to_float(usdt.get("totalPositionIM")),
        )
    order_margin = to_float(
        account.get("totalOrderIM"),
        to_float(usdt.get("totalOrderIM")),
    )
    if not strict:
        fallback_available = max(0.0, equity - position_margin - order_margin)
        available_raw = to_float(
            account.get("totalAvailableBalance"),
            fallback_available,
        )
    available = max(0.0, available_raw)
    return {
        "balance_usd": wallet_balance,
        "available_usd": available,
        "equity_usd": equity,
        "position_margin_usd": position_margin,
        "order_margin_usd": order_margin,
        "unrealized_pnl_usd": unrealized_pnl,
        "max_leverage": max_leverage,
    }


def validate_sl_vs_liquidation(
    side: str,
    stop_loss: float,
    liquidation_price: float,
    min_distance_percent: float = 5.0,
) -> Tuple[bool, Optional[str]]:
    stop = to_float(stop_loss)
    liquidation = to_float(liquidation_price)
    if liquidation <= 0 or stop <= 0:
        return True, None
    if side == "Buy":
        minimum = liquidation * (1 + min_distance_percent / 100)
        if stop < minimum:
            return False, f"SL должен быть выше ликвидации минимум на {min_distance_percent:g}%"
    elif side == "Sell":
        maximum = liquidation * (1 - min_distance_percent / 100)
        if stop > maximum:
            return False, f"SL должен быть ниже ликвидации минимум на {min_distance_percent:g}%"
    else:
        return False, f"Неизвестная сторона позиции: {side}"
    return True, None


def calculate_position_risk(positions: Iterable[dict]) -> float:
    risk, _ = portfolio_risk_usd(
        positions,
        taker_fee_rate=FALLBACK_TAKER_FEE_RATE,
    )
    return float(risk)


def find_unprotected_positions(positions: Iterable[dict]) -> list[str]:
    _, symbols = portfolio_risk_usd(
        positions,
        taker_fee_rate=FALLBACK_TAKER_FEE_RATE,
    )
    return symbols


def build_context(
    positions_result: list,
    tickers_map: dict,
    account_info: Optional[dict] = None,
) -> dict:
    """Compatibility helper that deliberately strips raw exchange responses."""
    positions = [
        {
            "symbol": position.get("symbol", ""),
            "side": position.get("side", ""),
            "quantity": abs(to_float(position.get("size"))),
            "entry_price": to_float(position.get("avgPrice") or position.get("entryPrice")),
            "mark_price": to_float(position.get("markPrice")),
            "stop_loss": to_float(position.get("stopLoss")),
            "take_profit": to_float(position.get("takeProfit")),
            "liquidation_price": to_float(position.get("liqPrice")) or None,
            "unrealized_pnl": to_float(position.get("unrealisedPnl")),
            "leverage": to_float(position.get("leverage"), 1),
        }
        for position in positions_result
        if to_float(position.get("size")) > 0
    ]
    prices = {
        symbol: {
            key: value
            for key, value in ticker.items()
            if key in {
                "lastPrice",
                "markPrice",
                "bid1Price",
                "ask1Price",
                "fundingRate",
                "nextFundingTime",
            }
        }
        for symbol, ticker in tickers_map.items()
    }
    context = {"positions": positions, "prices": prices}
    if account_info is not None:
        context["account"] = account_info
    return context
