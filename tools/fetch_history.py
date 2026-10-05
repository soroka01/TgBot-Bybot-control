"""Download public Bybit linear klines for offline backtests (no API keys)."""

from __future__ import annotations

import argparse
import json
import time
from pathlib import Path

import requests

URL = "https://api.bybit.com/v5/market/kline"
INTERVAL_MS = {"5": 300_000, "60": 3_600_000, "240": 14_400_000}


def fetch(symbol: str, interval: str, days: int, session: requests.Session) -> list[list[float]]:
    end = int(time.time() * 1000)
    start = end - days * 86_400_000
    rows: dict[int, list[float]] = {}
    cursor = end
    while cursor > start:
        for attempt in range(5):
            try:
                response = session.get(
                    URL,
                    params={
                        "category": "linear",
                        "symbol": symbol,
                        "interval": interval,
                        "end": cursor,
                        "limit": 1000,
                    },
                    timeout=20,
                )
                payload = response.json()
                if payload.get("retCode") == 0:
                    break
            except requests.RequestException:
                pass
            time.sleep(1 + attempt)
        else:
            raise RuntimeError(f"Bybit kline failed: {symbol} {interval}")
        batch = payload["result"]["list"]
        if not batch:
            break
        for row in batch:
            rows[int(row[0])] = [int(row[0])] + [float(value) for value in row[1:6]]
        oldest = min(int(row[0]) for row in batch)
        if oldest >= cursor:
            break
        cursor = oldest - 1
        time.sleep(0.1)
    return [rows[key] for key in sorted(rows) if key >= start]


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--symbols", default="BTCUSDT,ETHUSDT,SOLUSDT,XRPUSDT,BNBUSDT,DOGEUSDT")
    parser.add_argument("--days", type=int, default=150)
    parser.add_argument("--out", default="data/history")
    args = parser.parse_args()
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    session = requests.Session()
    for symbol in args.symbols.split(","):
        for interval in INTERVAL_MS:
            # 1h/4h need extra warm-up history before the 5m window starts.
            days = args.days + (10 if interval != "5" else 0)
            rows = fetch(symbol, interval, days, session)
            (out / f"{symbol}_{interval}.json").write_text(json.dumps(rows))
            print(symbol, interval, len(rows))


if __name__ == "__main__":
    main()
