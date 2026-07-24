from decimal import Decimal
import unittest

from core.trade_analytics import aggregate_trade_records, build_trade_analytics


D = Decimal


def row(
    record_id: str,
    pnl: str,
    *,
    candidate_id: str | None = None,
    symbol: str = "BTCUSDT",
    closed_at: int = 10_000,
    opened_at: int | None = None,
    risk: str | None = None,
    open_fee: str | None = "0.1",
    close_fee: str | None = "0.1",
    entry_value: str | None = "100",
    exit_value: str | None = "100",
) -> dict:
    return {
        "account_scope": "account-a",
        "record_id": record_id,
        "candidate_id": candidate_id,
        "symbol": symbol,
        "setup_side": "Buy",
        "closed_pnl": pnl,
        "open_fee": open_fee,
        "close_fee": close_fee,
        "cum_entry_value": entry_value,
        "cum_exit_value": exit_value,
        "planned_risk_usd": risk,
        "setup_opened_at_ms": opened_at,
        "created_time_ms": closed_at,
        "updated_time_ms": closed_at,
    }


class TradeAggregationTests(unittest.TestCase):
    def test_partial_bot_closes_aggregate_but_account_rows_stay_separate(self):
        rows = [
            row(
                "part-1",
                "10",
                candidate_id="candidate-1",
                closed_at=3_000,
                opened_at=1_000,
                risk="5",
                open_fee="0.2",
                close_fee="0.3",
                entry_value="100",
                exit_value="110",
            ),
            row(
                "part-2",
                "-4",
                candidate_id="candidate-1",
                closed_at=4_000,
                opened_at=1_000,
                risk="5",
                open_fee="0.1",
                close_fee="0.2",
                entry_value="50",
                exit_value="45",
            ),
            row("manual-1", "3", closed_at=5_000),
            row("manual-2", "-2", closed_at=6_000),
        ]

        trades = aggregate_trade_records(rows)

        self.assertEqual(len(trades), 3)
        bot = trades[0]
        self.assertEqual(bot["trade_id"], "candidate-1")
        self.assertEqual(bot["parts"], 2)
        self.assertEqual(bot["record_ids"], ["part-1", "part-2"])
        self.assertEqual(bot["closed_pnl"], D("6"))
        self.assertEqual(bot["fees_total"], D("0.8"))
        self.assertEqual(bot["turnover"], D("305"))
        self.assertEqual(bot["hold_ms"], 3_000)
        self.assertEqual(bot["r_multiple"], D("1.2"))
        self.assertTrue(bot["fee_complete"])
        self.assertEqual(
            [trade["trade_id"] for trade in trades[1:]],
            ["manual-1", "manual-2"],
        )

        stats = build_trade_analytics(rows)
        self.assertEqual(stats["fee_complete_count"], 3)
        self.assertEqual(stats["fee_complete_record_count"], 4)

    def test_turnover_falls_back_to_size_times_prices(self):
        value = row(
            "fallback",
            "1",
            entry_value=None,
            exit_value=None,
        )
        value.update(
            {
                "closed_size": "2",
                "avg_entry_price": "10",
                "avg_exit_price": "12",
            }
        )

        trade = aggregate_trade_records([value])[0]

        self.assertEqual(trade["entry_turnover"], D("20"))
        self.assertEqual(trade["exit_turnover"], D("24"))
        self.assertEqual(trade["turnover"], D("44"))

    def test_inconsistent_candidate_identity_is_rejected(self):
        rows = [
            row("one", "1", candidate_id="same", symbol="BTCUSDT"),
            row("two", "1", candidate_id="same", symbol="ETHUSDT"),
        ]

        with self.assertRaisesRegex(ValueError, "multiple symbols"):
            aggregate_trade_records(rows)


class TradeAnalyticsTests(unittest.TestCase):
    def setUp(self):
        self.rows = [
            row(
                "a",
                "10",
                candidate_id="a",
                symbol="BTCUSDT",
                closed_at=2_000,
                opened_at=1_000,
                risk="5",
            ),
            row(
                "b",
                "-4",
                candidate_id="b",
                symbol="BTCUSDT",
                closed_at=5_000,
                opened_at=3_000,
                risk="4",
            ),
            row(
                "c",
                "-6",
                candidate_id="c",
                symbol="ETHUSDT",
                closed_at=9_000,
                opened_at=6_000,
                risk="3",
            ),
            row(
                "d",
                "8",
                candidate_id="d",
                symbol="ETHUSDT",
                closed_at=14_000,
                opened_at=10_000,
                risk="4",
            ),
            row(
                "e",
                "0",
                candidate_id="e",
                symbol="ETHUSDT",
                closed_at=20_000,
                opened_at=None,
                risk=None,
            ),
        ]

    def test_core_profit_and_drawdown_metrics(self):
        stats = build_trade_analytics(self.rows)

        self.assertEqual(stats["trade_count"], 5)
        self.assertEqual((stats["wins"], stats["losses"], stats["breakeven"]), (2, 2, 1))
        self.assertEqual(stats["net_pnl"], D("8"))
        self.assertEqual(stats["gross_profit"], D("18"))
        self.assertEqual(stats["gross_loss"], D("10"))
        self.assertEqual(stats["win_rate"], D("50"))
        self.assertEqual(stats["profit_factor"], D("1.8"))
        self.assertEqual(stats["expectancy"], D("1.6"))
        self.assertEqual(stats["median_pnl"], D("0"))
        self.assertEqual(stats["avg_win"], D("9"))
        self.assertEqual(stats["avg_loss"], D("-5"))
        self.assertEqual(stats["payoff_ratio"], D("1.8"))
        self.assertEqual(stats["best_trade"]["trade_id"], "a")
        self.assertEqual(stats["worst_trade"]["trade_id"], "c")
        self.assertEqual(
            stats["cumulative_pnl"],
            [D("0"), D("10"), D("6"), D("0"), D("8"), D("8")],
        )
        self.assertEqual(stats["max_drawdown"], D("10"))
        self.assertEqual(stats["recovery_factor"], D("0.8"))

    def test_streak_hold_r_fee_and_turnover_metrics(self):
        stats = build_trade_analytics(self.rows)

        self.assertEqual(stats["max_win_streak"], 1)
        self.assertEqual(stats["max_loss_streak"], 2)
        self.assertIsNone(stats["current_streak_type"])
        self.assertEqual(stats["current_streak"], 0)
        self.assertEqual(stats["current_streak_count"], 0)
        self.assertEqual(stats["fees_total"], D("1.0"))
        self.assertEqual(stats["fee_complete_count"], 5)
        self.assertEqual(stats["turnover"], D("1000"))
        self.assertEqual(stats["hold_count"], 4)
        self.assertEqual(stats["avg_hold_ms"], D("2500"))
        self.assertEqual(stats["median_hold_ms"], D("2500"))
        self.assertEqual(stats["r_count"], 4)
        self.assertEqual(stats["total_r"], D("1"))
        self.assertEqual(stats["avg_r"], D("0.25"))
        self.assertIsNone(stats["sqn"])

    def test_symbol_metrics_are_based_on_logical_trades(self):
        stats = build_trade_analytics(self.rows)

        self.assertEqual(
            stats["best_symbol"],
            {
                "symbol": "BTCUSDT",
                "trade_count": 2,
                "wins": 1,
                "losses": 1,
                "breakeven": 0,
                "net_pnl": D("6"),
                "win_rate": D("50"),
            },
        )
        self.assertEqual(stats["worst_symbol"]["symbol"], "ETHUSDT")
        self.assertEqual(stats["worst_symbol"]["net_pnl"], D("2"))
        self.assertIs(stats["per_symbol"]["BTCUSDT"], stats["best_symbol"])

    def test_long_short_breakdown_keeps_unknown_side_explicit(self):
        short = row(
            "short",
            "-2",
            candidate_id="short",
            closed_at=30_000,
        )
        short["setup_side"] = "Sell"
        unknown = row("unknown", "1", closed_at=40_000)
        unknown["setup_side"] = None
        unknown["position_side"] = None

        stats = build_trade_analytics(self.rows + [short, unknown])

        self.assertEqual(stats["long_short"]["long"]["trade_count"], 5)
        self.assertEqual(stats["long_short"]["long"]["net_pnl"], D("8"))
        self.assertEqual(stats["long_short"]["short"]["trade_count"], 1)
        self.assertEqual(stats["long_short"]["short"]["net_pnl"], D("-2"))
        self.assertEqual(stats["unknown_side_count"], 1)

    def test_sqn_requires_thirty_r_trades_and_sample_variation(self):
        records = [
            row(
                f"r-{index}",
                "2" if index % 2 == 0 else "-1",
                candidate_id=f"r-{index}",
                closed_at=10_000 + index,
                risk="1",
            )
            for index in range(30)
        ]

        stats = build_trade_analytics(records)

        mean = D("0.5")
        variance = (
            sum(
                (
                    (D("2") if index % 2 == 0 else D("-1")) - mean
                ) ** 2
                for index in range(30)
            )
            / D("29")
        )
        expected = D("30").sqrt() * mean / variance.sqrt()
        self.assertEqual(stats["r_count"], 30)
        self.assertEqual(stats["sqn"], expected)

    def test_no_losses_produces_infinite_profit_and_payoff_factors(self):
        stats = build_trade_analytics(
            [
                row("one", "2", candidate_id="one"),
                row("two", "3", candidate_id="two", closed_at=20_000),
            ]
        )

        self.assertEqual(stats["profit_factor"], D("Infinity"))
        self.assertEqual(stats["payoff_ratio"], D("Infinity"))
        self.assertIsNone(stats["recovery_factor"])

    def test_empty_history_has_explicit_nullable_metrics(self):
        stats = build_trade_analytics([])

        self.assertEqual(stats["trade_count"], 0)
        self.assertEqual(stats["net_pnl"], D("0"))
        self.assertEqual(stats["gross_profit"], D("0"))
        self.assertEqual(stats["gross_loss"], D("0"))
        self.assertIsNone(stats["win_rate"])
        self.assertIsNone(stats["profit_factor"])
        self.assertIsNone(stats["expectancy"])
        self.assertIsNone(stats["median_pnl"])
        self.assertIsNone(stats["avg_win"])
        self.assertIsNone(stats["avg_loss"])
        self.assertIsNone(stats["payoff_ratio"])
        self.assertIsNone(stats["best_trade"])
        self.assertIsNone(stats["worst_trade"])
        self.assertEqual(stats["max_drawdown"], D("0"))
        self.assertIsNone(stats["recovery_factor"])
        self.assertEqual(stats["cumulative_pnl"], [D("0")])
        self.assertIsNone(stats["best_symbol"])
        self.assertIsNone(stats["worst_symbol"])
        self.assertIsNone(stats["equity_return_percent"])
        self.assertIsNone(stats["equity_max_drawdown_percent"])

    def test_fee_total_keeps_known_values_and_reports_completeness(self):
        records = [
            row("complete", "1", open_fee="0.2", close_fee="0.3"),
            row("missing", "1", open_fee="0.4", close_fee=None, closed_at=20_000),
        ]

        stats = build_trade_analytics(records)

        self.assertEqual(stats["fees_total"], D("0.9"))
        self.assertEqual(stats["fee_complete_count"], 1)

    def test_non_finite_money_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "finite"):
            build_trade_analytics([row("bad", "NaN")])


class EquityAnalyticsTests(unittest.TestCase):
    def test_equity_return_and_peak_drawdown_need_two_snapshots(self):
        snapshots = [
            {"captured_at_ms": 1, "equity_usd": "100"},
            {"captured_at_ms": 2, "equity_usd": "120"},
            {"captured_at_ms": 3, "equity_usd": "90"},
            {"captured_at_ms": 4, "equity_usd": "110"},
        ]

        stats = build_trade_analytics([], snapshots)

        self.assertEqual(stats["equity_snapshot_count"], 4)
        self.assertEqual(stats["equity_return_percent"], D("10.0"))
        self.assertEqual(stats["equity_max_drawdown_percent"], D("25.00"))

    def test_one_snapshot_does_not_claim_return_or_drawdown(self):
        stats = build_trade_analytics(
            [],
            [{"captured_at_ms": 1, "equity_usd": "100"}],
        )

        self.assertEqual(stats["equity_snapshot_count"], 1)
        self.assertIsNone(stats["equity_return_percent"])
        self.assertIsNone(stats["equity_max_drawdown_percent"])

    def test_non_positive_equity_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "positive"):
            build_trade_analytics(
                [],
                [
                    {"captured_at_ms": 1, "equity_usd": "100"},
                    {"captured_at_ms": 2, "equity_usd": "0"},
                ],
            )


if __name__ == "__main__":
    unittest.main()
