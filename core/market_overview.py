"""Cached public market overview built from CoinGecko data."""

from __future__ import annotations

from copy import deepcopy
import math
import threading
import time
from typing import Any

import requests
from loguru import logger

MARKET_OVERVIEW_REFRESH_SECONDS = 90
_CACHE_TTL_SECONDS = MARKET_OVERVIEW_REFRESH_SECONDS
_cached_at = 0.0
_cached_overview: dict[str, Any] | None = None
_cache_lock = threading.Lock()


def _get_json(url: str) -> dict[str, Any]:
    response = requests.get(
        url,
        timeout=(3.05, 10),
        headers={"User-Agent": "soroka01-crypto-bot/2"},
    )
    try:
        response.raise_for_status()
        payload = response.json()
    finally:
        response.close()
    if not isinstance(payload, dict):
        raise ValueError("CoinGecko вернул JSON неожиданного типа")
    return payload


def _number_or_none(
    value: Any,
    *,
    non_negative: bool = False,
) -> float | None:
    if value is None or value == "" or isinstance(value, bool):
        return None
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(result) or (non_negative and result < 0):
        return None
    return result


def _integer_or_none(value: Any) -> int | None:
    if value is None or value == "" or isinstance(value, bool):
        return None
    try:
        result = int(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return result if result >= 0 else None


def _cached_copy(*, stale: bool) -> dict[str, Any] | None:
    if _cached_overview is None:
        return None
    result = deepcopy(_cached_overview)
    result["stale"] = stale
    return result


def get_market_overview() -> dict[str, Any]:
    global _cached_at, _cached_overview
    now = time.monotonic()
    if _cached_overview and now - _cached_at < _CACHE_TTL_SECONDS:
        return _cached_copy(stale=False) or {}

    with _cache_lock:
        now = time.monotonic()
        if _cached_overview and now - _cached_at < _CACHE_TTL_SECONDS:
            return _cached_copy(stale=False) or {}
        try:
            global_payload = _get_json(
                "https://api.coingecko.com/api/v3/global"
            )
            trending_payload = _get_json(
                "https://api.coingecko.com/api/v3/search/trending"
            )
        except Exception as error:
            stale = _cached_copy(stale=True)
            if stale is None:
                raise
            logger.warning(
                "CoinGecko overview временно недоступен; "
                f"показываю последний успешный snapshot ({type(error).__name__})"
            )
            return stale

        global_data = global_payload.get("data")
        global_data = global_data if isinstance(global_data, dict) else {}
        trending = trending_payload.get("coins")
        trending = trending if isinstance(trending, list) else []
        normalized_trending: list[dict[str, Any]] = []
        for row in trending[:5]:
            item = row.get("item", {}) if isinstance(row, dict) else {}
            item = item if isinstance(item, dict) else {}
            data = item.get("data")
            data = data if isinstance(data, dict) else {}
            changes = data.get("price_change_percentage_24h")
            changes = changes if isinstance(changes, dict) else {}
            try:
                rank = int(item["market_cap_rank"])
            except (KeyError, TypeError, ValueError):
                rank = None
            if rank is not None and rank <= 0:
                rank = None
            normalized_trending.append(
                {
                    "name": str(item.get("name") or "—")[:80],
                    "symbol": str(item.get("symbol") or "—").upper()[:20],
                    "rank": rank,
                    "price_usd": _number_or_none(
                        data.get("price"),
                        non_negative=True,
                    ),
                    "change_24h_percent": _number_or_none(
                        changes.get("usd")
                    ),
                }
            )
        market_caps = global_data.get("total_market_cap")
        volumes = global_data.get("total_volume")
        percentages = global_data.get("market_cap_percentage")
        percentages = percentages if isinstance(percentages, dict) else {}
        updated_at = _integer_or_none(global_data.get("updated_at"))
        captured_at_ms = int(time.time() * 1_000)
        result = {
            "market_cap": _number_or_none(
                market_caps.get("usd")
                if isinstance(market_caps, dict)
                else None,
                non_negative=True,
            ),
            "market_cap_change_24h_percent": _number_or_none(
                global_data.get("market_cap_change_percentage_24h_usd")
            ),
            "volume": _number_or_none(
                volumes.get("usd")
                if isinstance(volumes, dict)
                else None,
                non_negative=True,
            ),
            "volume_change_24h_percent": _number_or_none(
                global_data.get("volume_change_percentage_24h_usd")
            ),
            "btc_dominance": _number_or_none(
                percentages.get("btc"),
                non_negative=True,
            ),
            "eth_dominance": _number_or_none(
                percentages.get("eth"),
                non_negative=True,
            ),
            "active_cryptocurrencies": _integer_or_none(
                global_data.get("active_cryptocurrencies")
            ),
            "markets": _integer_or_none(global_data.get("markets")),
            "trending": normalized_trending,
            "source_updated_at_ms": (
                updated_at * 1_000
                if updated_at is not None and updated_at > 0
                else None
            ),
            "captured_at_ms": captured_at_ms,
            "stale": False,
        }
        _cached_at, _cached_overview = now, result
        return _cached_copy(stale=False) or {}


__all__ = [
    "MARKET_OVERVIEW_REFRESH_SECONDS",
    "get_market_overview",
]
