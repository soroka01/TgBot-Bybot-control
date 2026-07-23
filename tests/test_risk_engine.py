from decimal import Decimal
import unittest

from api.bybit_api import InstrumentRules
from core.risk_engine import (
    build_trade_plan,
    execution_price_and_spread,
    portfolio_risk_usd,
)
from config import BYBIT_MAX_SLIPPAGE_PERCENT, MAX_RISK_PER_TRADE_PERCENT


def rules(**overrides):
    values = {
        "symbol": "BTCUSDT",
        "status": "Trading",
        "tick_size": Decimal("0.1"),
        "min_qty": Decimal("0.001"),
        "qty_step": Decimal("0.001"),
        "min_notional": Decimal("5"),
        "max_market_qty": Decimal("100"),
        "max_leverage": Decimal("10"),
        "leverage_step": Decimal("0.01"),
    }
    values.update(overrides)
    return InstrumentRules(**values)


class RiskEngineTests(unittest.TestCase):
    def test_quantity_is_deterministic_and_rounded_down(self):
        candidate = {
            "id": "abc",
            "symbol": "BTCUSDT",
            "side": "Buy",
            "entry_ref": 100,
            "stop": 98,
            "target": 105,
        }
        ticker = {"bid1Price": "99.9", "ask1Price": "100.0"}
        plan = build_trade_plan(
            candidate,
            rules=rules(),
            ticker=ticker,
            equity_usd=1_000,
            available_usd=1_000,
            current_portfolio_risk_usd=0,
            taker_fee_rate="0.0006",
        )
        self.assertGreater(plan.quantity, 0)
        self.assertEqual(plan.quantity % Decimal("0.001"), 0)
        self.assertLessEqual(
            plan.risk_usd,
            Decimal("1000") * Decimal(str(MAX_RISK_PER_TRADE_PERCENT)) / 100,
        )
        self.assertGreaterEqual(plan.net_risk_reward, Decimal("1.5"))
        worst_entry_slippage = (
            plan.entry_price
            * Decimal(str(BYBIT_MAX_SLIPPAGE_PERCENT))
            / 100
            * plan.quantity
        )
        self.assertGreaterEqual(plan.estimated_cost_usd, worst_entry_slippage)

    def test_spread_gate_fails_closed(self):
        with self.assertRaisesRegex(ValueError, "Спред"):
            execution_price_and_spread(
                "Buy",
                {"bid1Price": "99", "ask1Price": "101"},
            )

    def test_dynamic_minimum_notional_is_enforced(self):
        strict = rules(min_notional=Decimal("5000"))
        with self.assertRaisesRegex(Exception, "minNotionalValue"):
            strict.validate_quantity(Decimal("0.01"), Decimal("100"))

    def test_portfolio_risk_uses_mark_not_entry(self):
        risk, unprotected = portfolio_risk_usd(
            [
                {
                    "symbol": "BTCUSDT",
                    "side": "Buy",
                    "size": "1",
                    "entryPrice": "100",
                    "markPrice": "120",
                    "stopLoss": "90",
                }
            ],
            taker_fee_rate="0",
        )
        # Mark-to-stop drawdown is 30, plus configured adverse exit slippage.
        self.assertGreater(risk, Decimal("30"))
        self.assertEqual(unprotected, [])

    def test_missing_stop_blocks_portfolio(self):
        _, unprotected = portfolio_risk_usd(
            [{"symbol": "ETHUSDT", "side": "Buy", "size": "1", "markPrice": "10"}],
            taker_fee_rate="0.0006",
        )
        self.assertEqual(unprotected, ["ETHUSDT"])

    def test_crossed_stop_is_unprotected(self):
        _, unprotected = portfolio_risk_usd(
            [
                {
                    "symbol": "BTCUSDT",
                    "side": "Buy",
                    "size": "1",
                    "markPrice": "100",
                    "stopLoss": "105",
                },
                {
                    "symbol": "ETHUSDT",
                    "side": "Sell",
                    "size": "1",
                    "markPrice": "100",
                    "stopLoss": "95",
                },
            ],
            taker_fee_rate="0.0006",
        )
        self.assertEqual(unprotected, ["BTCUSDT", "ETHUSDT"])

    def test_stop_too_close_to_liquidation_is_unprotected(self):
        _, unprotected = portfolio_risk_usd(
            [
                {
                    "symbol": "BTCUSDT",
                    "side": "Buy",
                    "size": "1",
                    "markPrice": "100",
                    "stopLoss": "82",
                    "liqPrice": "80",
                }
            ],
            taker_fee_rate="0.0006",
        )
        self.assertEqual(unprotected, ["BTCUSDT"])


if __name__ == "__main__":
    unittest.main()
