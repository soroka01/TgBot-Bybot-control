from datetime import datetime, timedelta, timezone
import json
import unittest

from core.decision_engine import build_trade_snapshot, validate_trade_decision


def frame(age=1_000):
    return {
        "ema20": 101,
        "ema50": 99,
        "ema20_slope": 1,
        "rsi14": 55,
        "macd_histogram": 0.5,
        "atr14": 1,
        "volume_ratio": 1.2,
        "swing_high": 105,
        "swing_low": 98,
        "last_closed_candle_at": 1_700_000_000_000,
        "age_ms": age,
    }


def snapshot(now=None, *, bid="99.95", ask="100.00"):
    analysis = {
        "complete": True,
        "regime": "trend_up",
        "timeframe_3m": frame(),
        "timeframe_5m": frame(),
        "timeframe_1h": frame(),
        "timeframe_4h": frame(),
    }
    ticker = {
        "lastPrice": "100",
        "markPrice": "100",
        "bid1Price": bid,
        "ask1Price": ask,
        "fundingRate": "0.0001",
        "nextFundingTime": "1700003600000",
    }
    return build_trade_snapshot(
        tokens=["BTC"],
        positions=[],
        tickers={"BTCUSDT": ticker},
        analyses={"BTC": analysis},
        fee_rates={"BTCUSDT": "0.0006"},
        now=now,
    )


class DecisionContractTests(unittest.TestCase):
    def test_candidate_id_is_stable_within_closed_candle(self):
        now = datetime.now(timezone.utc)
        first = snapshot(now, bid="99.95", ask="100.00")
        moved = snapshot(now, bid="100.05", ask="100.10")
        first_id = first["symbols"]["BTCUSDT"]["candidates"][0]["id"]
        moved_id = moved["symbols"]["BTCUSDT"]["candidates"][0]["id"]
        self.assertEqual(first_id, moved_id)

    def test_snapshot_does_not_expose_wallet_amounts(self):
        snap = snapshot()
        self.assertNotIn("account", snap)
        self.assertEqual(
            set(snap["entry_policy"]),
            {"entry_allowed", "entry_block_reason"},
        )

    def test_valid_exact_candidate_selection(self):
        snap = snapshot()
        candidate_id = snap["symbols"]["BTCUSDT"]["candidates"][0]["id"]
        raw = json.dumps(
            {
                "schema_version": "trade_decision.v1",
                "snapshot_id": snap["snapshot_id"],
                "decisions": [
                    {
                        "symbol": "BTCUSDT",
                        "action": "select_candidate",
                        "candidate_id": candidate_id,
                        "reason_code": "candidate_selected",
                    }
                ],
            }
        )
        result = validate_trade_decision(raw, snap)
        self.assertEqual(result["decisions"][0]["candidate_id"], candidate_id)

    def test_wrong_snapshot_fails_whole_batch(self):
        snap = snapshot()
        raw = json.dumps(
            {
                "schema_version": "trade_decision.v1",
                "snapshot_id": "stale",
                "decisions": [],
            }
        )
        with self.assertRaisesRegex(ValueError, "не на текущий"):
            validate_trade_decision(raw, snap)

    def test_invented_candidate_id_is_rejected(self):
        snap = snapshot()
        raw = json.dumps(
            {
                "schema_version": "trade_decision.v1",
                "snapshot_id": snap["snapshot_id"],
                "decisions": [
                    {
                        "symbol": "BTCUSDT",
                        "action": "select_candidate",
                        "candidate_id": "invented",
                        "reason_code": "candidate_selected",
                    }
                ],
            }
        )
        with self.assertRaisesRegex(ValueError, "выбор кандидата"):
            validate_trade_decision(raw, snap)

    def test_ai_can_never_close_a_position(self):
        snap = snapshot()
        raw = json.dumps(
            {
                "schema_version": "trade_decision.v1",
                "snapshot_id": snap["snapshot_id"],
                "decisions": [
                    {
                        "symbol": "BTCUSDT",
                        "action": "close",
                        "candidate_id": None,
                        "reason_code": "exit_signal",
                    }
                ],
            }
        )
        with self.assertRaisesRegex(ValueError, "Недопустимое"):
            validate_trade_decision(raw, snap)

    def test_duplicate_symbol_is_rejected(self):
        snap = snapshot()
        item = {
            "symbol": "BTCUSDT",
            "action": "hold",
            "candidate_id": None,
            "reason_code": "no_edge",
        }
        raw = json.dumps(
            {
                "schema_version": "trade_decision.v1",
                "snapshot_id": snap["snapshot_id"],
                "decisions": [item, item],
            }
        )
        with self.assertRaisesRegex(ValueError, "повторный"):
            validate_trade_decision(raw, snap)

    def test_expired_snapshot_is_rejected(self):
        snap = snapshot(datetime.now(timezone.utc) - timedelta(hours=1))
        raw = json.dumps(
            {
                "schema_version": "trade_decision.v1",
                "snapshot_id": snap["snapshot_id"],
                "decisions": [
                    {
                        "symbol": "BTCUSDT",
                        "action": "hold",
                        "candidate_id": None,
                        "reason_code": "no_edge",
                    }
                ],
            }
        )
        with self.assertRaisesRegex(ValueError, "устарело"):
            validate_trade_decision(raw, snap)

    def test_extra_fields_are_rejected(self):
        snap = snapshot()
        raw = json.dumps(
            {
                "schema_version": "trade_decision.v1",
                "snapshot_id": snap["snapshot_id"],
                "decisions": [],
                "quantity": 999,
            }
        )
        with self.assertRaisesRegex(ValueError, "лишние"):
            validate_trade_decision(raw, snap)


if __name__ == "__main__":
    unittest.main()
