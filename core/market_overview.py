"""Cached public market overview built from CoinGecko data."""

from __future__ import annotations

import math
import threading
import time
from typing import Any

import requests

_CACHE_TTL_SECONDS = 90
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


def _number(value: Any) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return 0.0
    return result if math.isfinite(result) and result >= 0 else 0.0


def get_market_overview() -> dict[str, Any]:
    global _cached_at, _cached_overview
    now = time.monotonic()
    if _cached_overview and now - _cached_at < _CACHE_TTL_SECONDS:
        return _cached_overview

    with _cache_lock:
        now = time.monotonic()
        if _cached_overview and now - _cached_at < _CACHE_TTL_SECONDS:
            return _cached_overview
        global_payload = _get_json("https://api.coingecko.com/api/v3/global")
        trending_payload = _get_json(
            "https://api.coingecko.com/api/v3/search/trending"
        )
        global_data = global_payload.get("data")
        global_data = global_data if isinstance(global_data, dict) else {}
        trending = trending_payload.get("coins")
        trending = trending if isinstance(trending, list) else []
        normalized_trending: list[dict[str, Any]] = []
        for row in trending[:5]:
            item = row.get("item", {}) if isinstance(row, dict) else {}
            item = item if isinstance(item, dict) else {}
            try:
                rank = int(item["market_cap_rank"])
            except (KeyError, TypeError, ValueError):
                rank = None
            normalized_trending.append(
                {
                    "name": str(item.get("name") or "—")[:80],
                    "symbol": str(item.get("symbol") or "—").upper()[:20],
                    "rank": rank,
                }
            )
        market_caps = global_data.get("total_market_cap")
        volumes = global_data.get("total_volume")
        percentages = global_data.get("market_cap_percentage")
        result = {
            "market_cap": _number(
                market_caps.get("usd") if isinstance(market_caps, dict) else 0
            ),
            "volume": _number(
                volumes.get("usd") if isinstance(volumes, dict) else 0
            ),
            "btc_dominance": _number(
                percentages.get("btc") if isinstance(percentages, dict) else 0
            ),
            "trending": normalized_trending,
        }
        _cached_at, _cached_overview = now, result
        return result
