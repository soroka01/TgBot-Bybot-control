"""Walk-forward R-multiple backtest of the deterministic candidate generator.

Assumes the AI selector accepts every candidate (upper bound on trade count).
Ambiguous bars (SL and TP in the same candle) resolve to the stop.
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass, replace
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import core.market_data as md  # noqa: E402

md.calculate_macd = lambda *a, **k: (0.0, 0.0)  # unused by candidates; O(n^2)

INTERVAL = {"5": 300_000, "60": 3_600_000, "240": 14_400_000}
FEE = 0.00055  # realistic taker fee per side
SLIP = 0.0003  # realistic slippage per side


@dataclass(frozen=True)
class Params:
    rsi_lo: float = 28
    rsi_hi: float = 72
    stop_atr: float = 1.2
    rr: float = 2.0
    use_swing_target: bool = True
    min_net_rr: float = 1.5
    cost_mode: str = "prod"  # prod | real
    pullback: bool = False
    ema_align: bool = False
    vol_min: float = 0.0
    max_hold_bars: int = 864
    cooldown_bars: int = 0
    breakeven_r: float = 0.0
    min_atr_pct: float = 0.0
    trend_strength: float = 0.0
    tf: str = "5"  # timeframe for ATR/stop: 5 | 60
    stop_swing: bool = True


def load(symbol: str, root: Path):
    data = {}
    for interval in INTERVAL:
        rows = json.loads((root / f"{symbol}_{interval}.json").read_text())
        data[interval] = [
            {
                "timestamp": r[0],
                "closed_at": r[0] + INTERVAL[interval],
                "open": r[1],
                "high": r[2],
                "low": r[3],
                "close": r[4],
                "volume": r[5],
            }
            for r in rows
        ]
    return data


def prod_cost_rate(spread_bps: float) -> float:
    from config import BYBIT_MAX_SLIPPAGE_PERCENT as A, ESTIMATED_SLIPPAGE_PERCENT as B

    return 0.0006 * 2 + A / 100 + B / 100 + spread_bps / 10_000


def make_candidate(p: Params, regime: str, f5: dict, f1: dict, entry: float):
    fs = f1 if p.tf == "60" else f5
    atr = fs["atr14"]
    if atr <= 0 or entry <= 0:
        return None
    if f5["rsi14"] < p.rsi_lo or f5["rsi14"] > p.rsi_hi:
        return None
    if f5["volume_ratio"] < p.vol_min:
        return None
    if atr / entry < p.min_atr_pct:
        return None
    if p.trend_strength and abs(f1["ema20"] - f1["ema50"]) / entry < p.trend_strength:
        return None
    up = regime == "trend_up"
    if p.ema_align and (f5["ema20"] > f5["ema50"]) != up:
        return None
    if p.pullback:
        dist = (entry - f5["ema20"]) / atr
        if (up and dist > 0.5) or (not up and dist < -0.5):
            return None
    if up:
        stop = entry - atr * p.stop_atr
        if p.stop_swing:
            stop = min(fs["swing_low"] - atr * 0.10, stop)
        risk = entry - stop
        target = entry + risk * p.rr
        if p.use_swing_target:
            target = max(f1["swing_high"], target)
    else:
        stop = entry + atr * p.stop_atr
        if p.stop_swing:
            stop = max(fs["swing_high"] + atr * 0.10, stop)
        risk = stop - entry
        target = entry - risk * p.rr
        if p.use_swing_target:
            target = min(f1["swing_low"], target)
    if risk <= 0 or risk / entry > 0.05 or target <= 0:
        return None
    reward = abs(target - entry)
    cost_rate = prod_cost_rate(1.0) if p.cost_mode == "prod" else 2 * (FEE + SLIP)
    cost = entry * cost_rate
    if (reward - cost) / (risk + cost) < p.min_net_rr:
        return None
    return up, stop, target, risk


def simulate(symbol: str, data, p: Params, start_ms: int, end_ms: int):
    m5, h1, h4 = data["5"], data["60"], data["240"]
    trades = []
    i1 = i4 = 0
    f1c = f4c = None
    busy_until = -1
    last_exit_idx = -(10**9)
    for i in range(100, len(m5) - 1):
        bar = m5[i]
        t = bar["closed_at"]
        if t < start_ms or t > end_ms or i <= busy_until:
            continue
        if i - last_exit_idx < p.cooldown_bars:
            continue
        while i1 + 1 < len(h1) and h1[i1 + 1]["closed_at"] <= t:
            i1 += 1
            f1c = None
        while i4 + 1 < len(h4) and h4[i4 + 1]["closed_at"] <= t:
            i4 += 1
            f4c = None
        if i1 < 60 or i4 < 55:
            continue
        if f1c is None:
            f1c = md._timeframe_features(h1[i1 - 99 : i1 + 1], t)
        if f4c is None:
            f4c = md._timeframe_features(h4[max(0, i4 - 59) : i4 + 1], t)
        regime = md._regime({"timeframe_1h": f1c, "timeframe_4h": f4c})
        if regime == "range":
            continue
        f5 = md._timeframe_features(m5[i - 99 : i + 1], t)
        entry = m5[i + 1]["open"]
        cand = make_candidate(p, regime, f5, f1c, entry)
        if not cand:
            continue
        up, stop, target, risk = cand
        stop_now = stop
        exit_price = None
        exit_idx = None
        end_idx = min(len(m5) - 1, i + 1 + p.max_hold_bars)
        for j in range(i + 1, end_idx + 1):
            b = m5[j]
            hi, lo = b["high"], b["low"]
            if up:
                if lo <= stop_now:
                    exit_price, exit_idx = stop_now, j
                    break
                if hi >= target:
                    exit_price, exit_idx = target, j
                    break
                if p.breakeven_r and hi >= entry + risk * p.breakeven_r:
                    stop_now = max(stop_now, entry + entry * 0.0012)
            else:
                if hi >= stop_now:
                    exit_price, exit_idx = stop_now, j
                    break
                if lo <= target:
                    exit_price, exit_idx = target, j
                    break
                if p.breakeven_r and lo <= entry - risk * p.breakeven_r:
                    stop_now = min(stop_now, entry - entry * 0.0012)
        if exit_price is None:
            exit_idx = end_idx
            exit_price = m5[end_idx]["close"]
        gross = (exit_price - entry) if up else (entry - exit_price)
        cost = entry * 2 * (FEE + SLIP)
        hold_h = (exit_idx - i) * 5 / 60
        funding = entry * 0.0001 * (hold_h / 8) * (1 if up else -1)
        pnl = gross - cost - funding
        trades.append(
            {
                "symbol": symbol,
                "t": t,
                "r": pnl / risk,
                "cost_r": cost / risk,
                "hold_h": hold_h,
                "up": up,
            }
        )
        busy_until = exit_idx
        last_exit_idx = exit_idx
    return trades


def stats(trades):
    if not trades:
        return "n=0"
    rs = [t["r"] for t in trades]
    wins = [r for r in rs if r > 0]
    losses = [-r for r in rs if r <= 0]
    pf = sum(wins) / sum(losses) if losses else float("inf")
    eq = peak = dd = 0.0
    for r in sorted(trades, key=lambda x: x["t"]):
        eq += r["r"]
        peak = max(peak, eq)
        dd = max(dd, peak - eq)
    return (
        f"n={len(rs):4d} win={len(wins)/len(rs):5.1%} avgR={sum(rs)/len(rs):+.3f} "
        f"totR={sum(rs):+7.1f} PF={pf:4.2f} maxDD={dd:5.1f}R "
        f"cost={sum(t['cost_r'] for t in trades)/len(rs):.2f}R"
    )


def run(p: Params, datasets, start_ms, end_ms):
    out = []
    for symbol, data in datasets.items():
        out += simulate(symbol, data, p, start_ms, end_ms)
    return out


def parse_variant(spec: str):
    name, _, kv = spec.partition("=")
    kwargs = {}
    for pair in kv.split(","):
        if not pair:
            continue
        k, _, v = pair.partition(":")
        typ = Params.__dataclass_fields__[k].type
        if typ == "bool":
            kwargs[k] = v.lower() == "true"
        elif typ == "float":
            kwargs[k] = float(v)
        elif typ == "int":
            kwargs[k] = int(v)
        else:
            kwargs[k] = v
    return name, replace(Params(), **kwargs)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", required=True)
    ap.add_argument("--symbols", default="BTCUSDT,ETHUSDT,SOLUSDT,XRPUSDT,BNBUSDT,DOGEUSDT")
    ap.add_argument("--set", action="append", default=[], help="name=k:v,k:v")
    args = ap.parse_args()
    root = Path(args.root)
    datasets = {s: load(s, root) for s in args.symbols.split(",")}
    t0 = min(d["5"][100]["closed_at"] for d in datasets.values())
    t1 = max(d["5"][-1]["closed_at"] for d in datasets.values())
    mid = t0 + (t1 - t0) * 0.5
    print(f"window days={(t1 - t0) / 86_400_000:.0f}; split IS/OOS at 50%")
    variants = {"baseline": Params()}
    variants.update(parse_variant(s) for s in args.set)
    for name, p in variants.items():
        tr = run(p, datasets, t0, t1)
        a = [t for t in tr if t["t"] < mid]
        b = [t for t in tr if t["t"] >= mid]
        print(f"{name:22s} ALL {stats(tr)}", flush=True)
        print(f"{'':22s} IS  {stats(a)}")
        print(f"{'':22s} OOS {stats(b)}", flush=True)


if __name__ == "__main__":
    main()
