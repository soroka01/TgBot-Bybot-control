"""Cached public market overview built from CoinGecko data."""

from __future__ import annotations

import time
from typing import Any

import requests

_CACHE_TTL_SECONDS = 90
_cached_at = 0.0
_cached_overview: dict[str, Any] | None = None


def get_market_overview() -> dict[str, Any]:
    global _cached_at, _cached_overview
    now = time.monotonic()
    if _cached_overview and now - _cached_at < _CACHE_TTL_SECONDS:
        return _cached_overview

    timeout = 10
    global_data = requests.get(
        "https://api.coingecko.com/api/v3/global", timeout=timeout
    ).json().get("data", {})
    trending = requests.get(
        "https://api.coingecko.com/api/v3/search/trending", timeout=timeout
    ).json().get("coins", [])
    result = {
        "market_cap": float(global_data.get("total_market_cap", {}).get("usd", 0)),
        "volume": float(global_data.get("total_volume", {}).get("usd", 0)),
        "btc_dominance": float(global_data.get("market_cap_percentage", {}).get("btc", 0)),
        "trending": [
            {
                "name": item.get("item", {}).get("name", "—"),
                "symbol": item.get("item", {}).get("symbol", "—").upper(),
                "rank": item.get("item", {}).get("market_cap_rank"),
            }
            for item in trending[:5]
        ],
    }
    _cached_at, _cached_overview = now, result
    return result
