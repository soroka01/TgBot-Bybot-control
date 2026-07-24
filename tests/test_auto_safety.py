from decimal import Decimal
from datetime import datetime, timedelta, timezone
import threading
import unittest
from unittest.mock import Mock, patch

from api.bybit_api import (
    BybitAPIError,
    BybitOrderConfirmationError,
    BybitOrderNotFilledError,
    InstrumentRules,
)
from core.auto_trading import (
    ExecutionStopped,
    FatalExecutionError,
    MAX_SAFETY_CLOSE_ATTEMPTS,
    _entry_block_reason,
    _execute_candidate,
    _fee_rates,
    _manage_protection,
    main_loop,
    manage_existing_protection,
)
from utils.helpers import parse_account_overview


class ProtectionBybit:
    def __init__(self):
        self.calls = []
        self.closes = []

    def prepare_protective_prices(self, symbol, side, take_profit, stop_loss):
        del symbol, side
        return Decimal(str(take_profit)), Decimal(str(stop_loss))

    def set_trading_stop_and_verify(
        self,
        symbol,
        position_idx,
        *,
        take_profit,
        stop_loss,
    ):
        self.calls.append((symbol, position_idx, take_profit, stop_loss))
        return {
            "symbol": symbol,
            "positionIdx": position_idx,
            "takeProfit": str(take_profit),
            "stopLoss": str(stop_loss),
            "simulated": True,
        }

    def new_order_link_id(self, prefix):
        return f"{prefix}-id"

    def close_position_market(
        self,
        symbol,
        side,
        position_idx,
        *,
        order_link_id,
    ):
        self.closes.append((symbol, side, position_idx, order_link_id))
        return {"simulated": True}


class LiveProtectionBybit(ProtectionBybit):
    def __init__(
        self,
        *,
        close_on_attempt=None,
        unknown_close=False,
        fail_protection=False,
    ):
        super().__init__()
        self.close_on_attempt = close_on_attempt
        self.unknown_close = unknown_close
        self.fail_protection = fail_protection
        self.position_open = True

    def set_trading_stop_and_verify(
        self,
        symbol,
        position_idx,
        *,
        take_profit,
        stop_loss,
    ):
        if self.fail_protection:
            self.calls.append((symbol, position_idx, take_profit, stop_loss))
            raise BybitAPIError("protection rejected")
        return super().set_trading_stop_and_verify(
            symbol,
            position_idx,
            take_profit=take_profit,
            stop_loss=stop_loss,
        )

    def new_order_link_id(self, prefix):
        return f"{prefix}-{len(self.closes) + 1}"

    def close_position_market(
        self,
        symbol,
        side,
        position_idx,
        *,
        order_link_id,
    ):
        self.closes.append((symbol, side, position_idx, order_link_id))
        if self.unknown_close:
            raise BybitOrderConfirmationError(
                "unknown close",
                order_link_id=order_link_id,
            )
        if self.close_on_attempt is None or len(self.closes) < self.close_on_attempt:
            raise BybitOrderNotFilledError(
                {
                    "orderStatus": "PartiallyFilledCanceled",
                    "cumExecQty": "0.5",
                }
            )
        self.position_open = False
        return {"orderStatus": "Filled", "cumExecQty": "1"}

    def get_positions(self, symbol=None):
        rows = []
        if self.position_open:
            rows = [
                {
                    "symbol": symbol or "BTCUSDT",
                    "side": "Buy",
                    "positionIdx": 0,
                    "size": "0.5",
                }
            ]
        return {"result": {"list": rows}}


def analysis(swing_low):
    return {
        "complete": True,
        "current_price": 120,
        "timeframe_5m": {
            "atr14": 2,
            "swing_low": swing_low,
            "swing_high": 125,
        },
    }


class AutoProtectionTests(unittest.TestCase):
    def test_long_stop_can_only_tighten(self):
        bybit = ProtectionBybit()
        position = {
            "symbol": "BTCUSDT",
            "side": "Buy",
            "positionIdx": 0,
            "markPrice": "120",
            "avgPrice": "100",
            "stopLoss": "100",
            "takeProfit": "140",
            "liqPrice": "50",
        }
        self.assertTrue(_manage_protection(bybit, position, analysis(115)))
        self.assertGreaterEqual(bybit.calls[0][3], Decimal("100"))

    def test_long_stop_is_never_widened(self):
        bybit = ProtectionBybit()
        position = {
            "symbol": "BTCUSDT",
            "side": "Buy",
            "positionIdx": 0,
            "markPrice": "120",
            "avgPrice": "100",
            "stopLoss": "100",
            "takeProfit": "140",
            "liqPrice": "50",
        }
        self.assertFalse(_manage_protection(bybit, position, analysis(80)))
        self.assertEqual(bybit.calls, [])

    def test_missing_target_is_repaired_even_without_stop_change(self):
        bybit = ProtectionBybit()
        position = {
            "symbol": "BTCUSDT",
            "side": "Buy",
            "positionIdx": 0,
            "markPrice": "120",
            "avgPrice": "100",
            "stopLoss": "100",
            "takeProfit": "0",
            "liqPrice": "50",
        }
        self.assertEqual(
            _manage_protection(bybit, position, analysis(80)),
            "protected",
        )
        self.assertEqual(len(bybit.calls), 1)

    def test_crossed_stop_is_closed_even_without_analysis(self):
        bybit = ProtectionBybit()
        position = {
            "symbol": "BTCUSDT",
            "side": "Buy",
            "positionIdx": 0,
            "markPrice": "95",
            "avgPrice": "100",
            "stopLoss": "100",
            "takeProfit": "130",
            "liqPrice": "50",
        }
        self.assertEqual(_manage_protection(bybit, position, {}), "closed")
        self.assertEqual(len(bybit.closes), 1)

    def test_partial_guard_exit_retries_with_new_ids_until_flat(self):
        bybit = LiveProtectionBybit(close_on_attempt=2)
        position = {
            "symbol": "BTCUSDT",
            "side": "Buy",
            "positionIdx": 0,
            "markPrice": "95",
            "avgPrice": "100",
            "stopLoss": "100",
            "takeProfit": "130",
            "liqPrice": "50",
        }
        with patch("core.auto_trading.notify"):
            self.assertEqual(_manage_protection(bybit, position, {}), "closed")
        self.assertEqual(len(bybit.closes), 2)
        self.assertEqual(len({item[3] for item in bybit.closes}), 2)

    def test_persistent_guard_remainder_is_fatal_without_poll_delay(self):
        bybit = LiveProtectionBybit()
        position = {
            "symbol": "BTCUSDT",
            "side": "Buy",
            "positionIdx": 0,
            "markPrice": "95",
            "avgPrice": "100",
            "stopLoss": "100",
            "takeProfit": "130",
            "liqPrice": "50",
        }
        with self.assertRaises(FatalExecutionError):
            _manage_protection(bybit, position, {})
        self.assertEqual(len(bybit.closes), MAX_SAFETY_CLOSE_ATTEMPTS)

    def test_unknown_guard_close_is_immediately_fatal(self):
        bybit = LiveProtectionBybit(unknown_close=True)
        position = {
            "symbol": "BTCUSDT",
            "side": "Buy",
            "positionIdx": 0,
            "markPrice": "95",
            "avgPrice": "100",
            "stopLoss": "100",
            "takeProfit": "130",
            "liqPrice": "50",
        }
        with self.assertRaises(FatalExecutionError):
            _manage_protection(bybit, position, {})
        self.assertEqual(len(bybit.closes), 1)

    def test_missing_stop_failure_triggers_confirmed_emergency_flatten(self):
        bybit = LiveProtectionBybit(
            close_on_attempt=1,
            fail_protection=True,
        )
        position = {
            "symbol": "BTCUSDT",
            "side": "Buy",
            "positionIdx": 0,
            "markPrice": "120",
            "avgPrice": "100",
            "stopLoss": "0",
            "takeProfit": "140",
            "liqPrice": "50",
        }
        with patch("core.auto_trading.notify"):
            self.assertEqual(
                _manage_protection(bybit, position, analysis(115)),
                "closed",
            )
        self.assertEqual(len(bybit.calls), 1)
        self.assertEqual(len(bybit.closes), 1)
        self.assertFalse(bybit.position_open)

    def test_missing_stop_with_unconfirmed_flatten_is_fatal(self):
        bybit = LiveProtectionBybit(fail_protection=True)
        position = {
            "symbol": "BTCUSDT",
            "side": "Buy",
            "positionIdx": 0,
            "markPrice": "120",
            "avgPrice": "100",
            "stopLoss": "0",
            "takeProfit": "140",
            "liqPrice": "50",
        }
        with self.assertRaises(FatalExecutionError):
            _manage_protection(bybit, position, analysis(115))
        self.assertEqual(len(bybit.closes), MAX_SAFETY_CLOSE_ATTEMPTS)

    def test_safety_management_does_not_depend_on_ai_decision(self):
        bybit = ProtectionBybit()
        position = {
            "symbol": "BTCUSDT",
            "side": "Buy",
            "positionIdx": 0,
            "markPrice": "120",
            "avgPrice": "100",
            "stopLoss": "100",
            "takeProfit": "0",
            "liqPrice": "50",
        }
        cycle = {
            "positions": [position],
            "analyses": {"BTC": analysis(80)},
        }
        actions = manage_existing_protection(bybit, cycle, threading.Event())
        self.assertEqual(actions, ["protected:BTCUSDT"])


class EntryGateBybit:
    base = "https://api-testnet.bybit.com"
    api_key = "key"

    def __init__(
        self,
        *,
        usdc_position=False,
        usdt_order=False,
        reduce_only_value=False,
        margin_mode="REGULAR_MARGIN",
        unified_margin_status=5,
    ):
        self.usdc_position = usdc_position
        self.usdt_order = usdt_order
        self.reduce_only_value = reduce_only_value
        self.margin_mode = margin_mode
        self.unified_margin_status = unified_margin_status

    def get_account_info(self):
        return {
            "result": {
                "marginMode": self.margin_mode,
                "unifiedMarginStatus": self.unified_margin_status,
            }
        }

    def get_positions(self, symbol=None, settle_coin="USDT", *, category="linear"):
        del symbol
        rows = []
        if category == "linear" and settle_coin == "USDC" and self.usdc_position:
            rows = [{"symbol": "BTCPERP", "size": "1"}]
        return {"result": {"list": rows}}

    def get_open_orders(self, symbol=None, *, category="linear", settle_coin="USDT"):
        del symbol
        rows = []
        if category == "linear" and settle_coin == "USDT" and self.usdt_order:
            rows = [
                {
                    "orderStatus": "Untriggered",
                    "reduceOnly": self.reduce_only_value,
                }
            ]
        return {"result": {"list": rows}}

    def get_closed_pnl(self, **kwargs):
        del kwargs
        return {"result": {"list": []}}


class EntryGateTests(unittest.TestCase):
    def _reason(self, bybit):
        store = type(
            "Store",
            (),
            {
                "update_daily_equity_guard": lambda self, equity, scope: {
                    "high_water_equity": equity,
                    "drawdown": 0,
                }
            },
        )()
        with patch("core.auto_trading.get_store", return_value=store):
            return _entry_block_reason(
                bybit,
                [],
                {"equity_usd": 1_000},
                [],
            )

    def test_untriggered_entry_order_blocks_new_risk(self):
        self.assertIn(
            "активные",
            self._reason(EntryGateBybit(usdt_order=True)).lower(),
        )

    def test_usdc_position_blocks_usdt_auto_entry(self):
        self.assertIn(
            "неподдерживаемая",
            self._reason(EntryGateBybit(usdc_position=True)).lower(),
        )

    def test_malformed_reduce_only_flag_cannot_hide_exposure(self):
        self.assertIn(
            "активные",
            self._reason(
                EntryGateBybit(
                    usdt_order=True,
                    reduce_only_value="false",
                )
            ).lower(),
        )

    def test_unknown_margin_mode_is_blocked(self):
        self.assertIn(
            "regular_margin",
            self._reason(
                EntryGateBybit(margin_mode="FUTURE_MARGIN_MODE")
            ).lower(),
        )

    def test_malformed_unified_margin_status_is_blocked(self):
        self.assertIn(
            "unified",
            self._reason(
                EntryGateBybit(unified_margin_status="5")
            ).lower(),
        )


class PreviewEntryBybit:
    def __init__(self):
        self.order = None
        self.entry_error = None
        self.cancel_result = {
            "orderStatus": "Cancelled",
            "cumExecQty": "0",
            "orderId": "cancelled-entry",
        }
        self.calls = []
        self.leverage_calls = 0
        self.stop_event = None
        self.stop_during_position_read = False
        self.stop_during_leverage = False
        self.global_positions = []
        self.open_orders = []
        self.wallet_account = {
            "totalEquity": "1000",
            "totalWalletBalance": "1000",
            "totalPerpUPL": "0",
            "totalInitialMargin": "0",
            "totalAvailableBalance": "1000",
            "coin": [],
        }
        self.rules = InstrumentRules(
            symbol="BTCUSDT",
            status="Trading",
            tick_size=Decimal("0.1"),
            min_qty=Decimal("0.001"),
            qty_step=Decimal("0.001"),
            min_notional=Decimal("5"),
            max_market_qty=Decimal("100"),
            max_leverage=Decimal("10"),
            leverage_step=Decimal("0.01"),
        )

    def get_tickers(self, symbol):
        del symbol
        return {
            "result": {
                "list": [{"bid1Price": "99.9", "ask1Price": "100.0"}]
            }
        }

    def get_instrument_rules(self, symbol, refresh=False):
        del symbol, refresh
        return self.rules

    def get_positions(self, symbol=None):
        self.calls.append(
            f"positions:{symbol}" if symbol else "positions:global"
        )
        if symbol and self.stop_during_position_read and self.stop_event:
            self.stop_event.set()
        return {
            "result": {
                "list": [] if symbol else list(self.global_positions)
            }
        }

    def get_open_orders(self):
        self.calls.append("open_orders")
        return {"result": {"list": list(self.open_orders)}}

    def get_wallet_balance(self):
        self.calls.append("wallet")
        return {"result": {"list": [dict(self.wallet_account)]}}

    def set_leverage(self, symbol, buy, sell):
        del symbol, buy, sell
        self.calls.append("set_leverage")
        self.leverage_calls += 1
        if self.stop_during_leverage and self.stop_event:
            self.stop_event.set()
        return {"result": {"simulated": True}}

    def place_order_and_confirm(self, **order):
        self.calls.append("create_order")
        self.order = order
        if self.entry_error is not None:
            raise self.entry_error
        return {"simulated": True}

    def cancel_order_and_confirm(self, **order):
        self.calls.append("cancel_order")
        self.cancel_order = order
        return dict(self.cancel_result)


class RecordingTradeJournal:
    def __init__(self, calls, *, fail_prepare=False):
        self.calls = calls
        self.fail_prepare = fail_prepare
        self.prepared = None
        self.updates = []

    def prepare_entry(self, **payload):
        self.calls.append("journal_prepare")
        if self.fail_prepare:
            raise RuntimeError("database unavailable")
        self.prepared = payload

    def update_setup(self, candidate_id, **changes):
        self.calls.append("journal_update")
        self.updates.append((candidate_id, changes))


def preview_entry_case():
    now = datetime.now(timezone.utc)
    cycle = {
        "snapshot": {
            "valid_until": (now + timedelta(minutes=2))
            .isoformat()
            .replace("+00:00", "Z")
        },
        "account": {"equity_usd": 1_000, "available_usd": 1_000},
        "portfolio_risk": Decimal("0"),
    }
    fresh = {
        "positions": [],
        "account": {"equity_usd": 1_000, "available_usd": 1_000},
        "portfolio_risk": Decimal("0"),
        "entry_block_reason": None,
    }
    candidate = {
        "id": "candidate",
        "symbol": "BTCUSDT",
        "side": "Buy",
        "entry_ref": 100,
        "stop": 98,
        "target": 105,
    }
    return cycle, fresh, candidate


class EntryExecutionTests(unittest.TestCase):
    def setUp(self):
        self.daily_guard = patch(
            "core.auto_trading._daily_drawdown_block_reason",
            return_value=None,
        )
        self.daily_guard_mock = self.daily_guard.start()
        self.addCleanup(self.daily_guard.stop)

    def test_entry_uses_bounded_ioc_limit_with_attached_protection(self):
        bybit = PreviewEntryBybit()
        cycle, fresh, candidate = preview_entry_case()
        with (
            patch("core.auto_trading._fresh_entry_state", return_value=fresh),
            patch("core.auto_trading.notify"),
        ):
            _execute_candidate(
                bybit,
                candidate,
                cycle,
                {"BTCUSDT": Decimal("0.0006")},
                threading.Event(),
            )
        self.assertEqual(bybit.order["order_type"], "Limit")
        self.assertEqual(bybit.order["time_in_force"], "IOC")
        self.assertEqual(bybit.order["take_profit"], Decimal("105.0"))
        self.assertEqual(bybit.order["stop_loss"], Decimal("98.0"))
        self.assertLessEqual(bybit.order["price"], Decimal("100.3"))
        final_start = bybit.calls.index("set_leverage")
        self.assertEqual(
            bybit.calls[final_start:],
            [
                "set_leverage",
                "positions:global",
                "open_orders",
                "wallet",
                "create_order",
            ],
        )

    def test_trade_plan_is_durable_before_exchange_entry(self):
        bybit = PreviewEntryBybit()
        journal = RecordingTradeJournal(bybit.calls)
        cycle, fresh, candidate = preview_entry_case()
        with (
            patch("core.auto_trading._fresh_entry_state", return_value=fresh),
            patch("core.auto_trading.notify"),
        ):
            _execute_candidate(
                bybit,
                candidate,
                cycle,
                {"BTCUSDT": Decimal("0.0006")},
                threading.Event(),
                journal=journal,
                decision_item={"reason_code": "BEST_RR"},
            )
        self.assertLess(
            bybit.calls.index("journal_prepare"),
            bybit.calls.index("create_order"),
        )
        self.assertEqual(journal.prepared["order_link_id"], "open-candidate")
        self.assertEqual(journal.prepared["decision"]["reason_code"], "BEST_RR")
        self.assertEqual(journal.updates[-1][1]["status"], "previewed")

    def test_journal_prepare_failure_prevents_live_write(self):
        bybit = PreviewEntryBybit()
        journal = RecordingTradeJournal(bybit.calls, fail_prepare=True)
        cycle, fresh, candidate = preview_entry_case()
        with patch("core.auto_trading._fresh_entry_state", return_value=fresh):
            with self.assertRaisesRegex(RuntimeError, "database unavailable"):
                _execute_candidate(
                    bybit,
                    candidate,
                    cycle,
                    {"BTCUSDT": Decimal("0.0006")},
                    threading.Event(),
                    journal=journal,
                )
        self.assertIn("journal_prepare", bybit.calls)
        self.assertNotIn("create_order", bybit.calls)
        self.assertIsNone(bybit.order)

    def test_stop_during_fresh_reads_prevents_leverage_and_order(self):
        bybit = PreviewEntryBybit()
        event = threading.Event()
        bybit.stop_event = event
        bybit.stop_during_position_read = True
        cycle, fresh, candidate = preview_entry_case()
        with patch("core.auto_trading._fresh_entry_state", return_value=fresh):
            with self.assertRaises(ExecutionStopped):
                _execute_candidate(
                    bybit,
                    candidate,
                    cycle,
                    {"BTCUSDT": Decimal("0.0006")},
                    event,
                )
        self.assertEqual(bybit.leverage_calls, 0)
        self.assertIsNone(bybit.order)

    def test_stop_during_leverage_prevents_entry_order(self):
        bybit = PreviewEntryBybit()
        event = threading.Event()
        bybit.stop_event = event
        bybit.stop_during_leverage = True
        cycle, fresh, candidate = preview_entry_case()
        with patch("core.auto_trading._fresh_entry_state", return_value=fresh):
            with self.assertRaises(ExecutionStopped):
                _execute_candidate(
                    bybit,
                    candidate,
                    cycle,
                    {"BTCUSDT": Decimal("0.0006")},
                    event,
                )
        self.assertEqual(bybit.leverage_calls, 1)
        self.assertIsNone(bybit.order)

    def test_expired_after_fresh_reads_prevents_leverage_and_order(self):
        bybit = PreviewEntryBybit()
        cycle, fresh, candidate = preview_entry_case()
        started = datetime.now(timezone.utc)
        cycle["snapshot"]["valid_until"] = (
            started + timedelta(minutes=1)
        ).isoformat().replace("+00:00", "Z")

        class ExpiringDateTime(datetime):
            moments = [started, started + timedelta(minutes=2)]

            @classmethod
            def now(cls, tz=None):
                del tz
                return cls.moments.pop(0) if cls.moments else started + timedelta(minutes=2)

        with (
            patch("core.auto_trading._fresh_entry_state", return_value=fresh),
            patch("core.auto_trading.datetime", ExpiringDateTime),
        ):
            with self.assertRaisesRegex(ValueError, "leverage"):
                _execute_candidate(
                    bybit,
                    candidate,
                    cycle,
                    {"BTCUSDT": Decimal("0.0006")},
                    threading.Event(),
                )
        self.assertEqual(bybit.leverage_calls, 0)
        self.assertIsNone(bybit.order)

    def test_dry_preview_uses_synthetic_cycle_risk_reservation(self):
        bybit = PreviewEntryBybit()
        cycle, fresh, candidate = preview_entry_case()
        cycle["portfolio_risk"] = Decimal("1000000")
        with (
            patch("core.auto_trading.DRY_RUN", True),
            patch("core.auto_trading._fresh_entry_state", return_value=fresh),
        ):
            with self.assertRaises(ValueError):
                _execute_candidate(
                    bybit,
                    candidate,
                    cycle,
                    {"BTCUSDT": Decimal("0.0006")},
                    threading.Event(),
                )
        self.assertEqual(bybit.leverage_calls, 0)
        self.assertIsNone(bybit.order)

    def test_cross_symbol_change_in_final_state_blocks_order(self):
        bybit = PreviewEntryBybit()
        bybit.global_positions = [
            {
                "symbol": "ETHUSDT",
                "side": "Buy",
                "positionIdx": 0,
                "size": "1",
                "markPrice": "100",
                "stopLoss": "40",
                "liqPrice": "0",
                "positionStatus": "Normal",
            }
        ]
        cycle, fresh, candidate = preview_entry_case()
        with patch(
            "core.auto_trading._fresh_entry_state",
            return_value=fresh,
        ):
            with self.assertRaisesRegex(ValueError, "общего риска"):
                _execute_candidate(
                    bybit,
                    candidate,
                    cycle,
                    {"BTCUSDT": Decimal("0.0006")},
                    threading.Event(),
                )
        self.assertEqual(bybit.leverage_calls, 1)
        self.assertIsNone(bybit.order)

    def test_final_equity_drawdown_guard_blocks_order_after_leverage(self):
        bybit = PreviewEntryBybit()
        cycle, fresh, candidate = preview_entry_case()
        self.daily_guard_mock.return_value = (
            "Дневной equity drawdown достиг лимита"
        )
        with patch(
            "core.auto_trading._fresh_entry_state",
            return_value=fresh,
        ):
            with self.assertRaisesRegex(ValueError, "drawdown"):
                _execute_candidate(
                    bybit,
                    candidate,
                    cycle,
                    {"BTCUSDT": Decimal("0.0006")},
                    threading.Event(),
                )
        self.assertEqual(bybit.leverage_calls, 1)
        self.assertIsNone(bybit.order)

    def test_missing_terminal_cum_exec_qty_is_fatal(self):
        bybit = PreviewEntryBybit()
        bybit.entry_error = BybitOrderNotFilledError(
            {"orderStatus": "Cancelled"}
        )
        cycle, fresh, candidate = preview_entry_case()
        with (
            patch(
                "core.auto_trading._fresh_entry_state",
                return_value=fresh,
            ),
            patch(
                "core.auto_trading._emergency_flatten_entry",
                return_value=False,
            ) as flatten,
        ):
            with self.assertRaises(FatalExecutionError):
                _execute_candidate(
                    bybit,
                    candidate,
                    cycle,
                    {"BTCUSDT": Decimal("0.0006")},
                    threading.Event(),
                )
        self.assertFalse(flatten.call_args.kwargs["require_position"])

    def test_partial_terminal_status_is_flattened_even_with_zero_qty(self):
        bybit = PreviewEntryBybit()
        bybit.entry_error = BybitOrderNotFilledError(
            {
                "orderStatus": "PartiallyFilledCanceled",
                "cumExecQty": "0",
            }
        )
        cycle, fresh, candidate = preview_entry_case()
        with (
            patch(
                "core.auto_trading._fresh_entry_state",
                return_value=fresh,
            ),
            patch(
                "core.auto_trading._emergency_flatten_entry",
                return_value=True,
            ) as flatten,
        ):
            with self.assertRaises(BybitOrderNotFilledError):
                _execute_candidate(
                    bybit,
                    candidate,
                    cycle,
                    {"BTCUSDT": Decimal("0.0006")},
                    threading.Event(),
                )
        self.assertTrue(flatten.call_args.kwargs["require_position"])

    def test_confirmed_unfilled_cancellation_is_terminal_in_journal(self):
        bybit = PreviewEntryBybit()
        bybit.entry_error = BybitOrderConfirmationError(
            "confirmation timed out",
            order_link_id="open-candidate",
        )
        journal = RecordingTradeJournal(bybit.calls)
        cycle, fresh, candidate = preview_entry_case()
        with (
            patch("core.auto_trading._fresh_entry_state", return_value=fresh),
            patch(
                "core.auto_trading._emergency_flatten_entry",
                return_value=False,
            ),
        ):
            with self.assertRaises(BybitOrderConfirmationError):
                _execute_candidate(
                    bybit,
                    candidate,
                    cycle,
                    {"BTCUSDT": Decimal("0.0006")},
                    threading.Event(),
                    journal=journal,
                )
        self.assertIn("cancel_order", bybit.calls)
        self.assertEqual(journal.updates[-1][1]["status"], "not_filled")


class StrictAccountParsingTests(unittest.TestCase):
    def test_malformed_available_balance_blocks_auto_account_parse(self):
        response = {
            "result": {
                "list": [
                    {
                        "totalEquity": "1000",
                        "totalWalletBalance": "1000",
                        "totalPerpUPL": "0",
                        "totalInitialMargin": "0",
                        "totalAvailableBalance": "not-a-number",
                        "coin": [],
                    }
                ]
            }
        }
        with self.assertRaisesRegex(
            ValueError,
            "totalAvailableBalance",
        ):
            parse_account_overview(response, strict=True)


class FeeAndLifecycleTests(unittest.TestCase):
    def test_fee_refresh_retains_last_known_rate_on_read_failure(self):
        bybit = type(
            "FeeBybit",
            (),
            {"get_fee_rate": lambda self, symbol: (_ for _ in ()).throw(
                BybitAPIError(f"fee unavailable for {symbol}")
            )},
        )()
        previous = {"BTCUSDT": Decimal("0.0002")}
        with patch("core.auto_trading.TRADABLE_TOKENS", ["BTC"]):
            self.assertEqual(_fee_rates(bybit, previous=previous), previous)

    def test_main_loop_reraises_fatal_after_cleanup(self):
        class BybitDouble:
            def __init__(self):
                self.closed = False

            def close(self):
                self.closed = True

        class DeepSeekDouble:
            def __init__(self):
                self.closed = False

            def validate_model(self):
                return None

            def close(self):
                self.closed = True

        bybit = BybitDouble()
        deepseek = DeepSeekDouble()
        event = threading.Event()
        fatal = FatalExecutionError("unsafe remainder")
        with (
            patch("core.auto_trading.validate_config", return_value=[]),
            patch("core.auto_trading.BybitAPI", return_value=bybit),
            patch("core.auto_trading.DeepSeekAPI", return_value=deepseek),
            patch(
                "core.auto_trading._urgent_protection_preflight",
                return_value=[],
            ),
            patch("core.auto_trading._fee_rates", return_value={}),
            patch("core.auto_trading.collect_cycle", side_effect=fatal),
            patch("core.auto_trading.notify"),
        ):
            with self.assertRaises(FatalExecutionError) as raised:
                main_loop(event, once=True)
        self.assertIs(raised.exception, fatal)
        self.assertTrue(event.is_set())
        self.assertTrue(bybit.closed)
        self.assertTrue(deepseek.closed)

    def test_post_init_preflight_closes_crossed_stop_before_collect(self):
        class BybitDouble:
            def __init__(self):
                self.crossed = False
                self.closes = []

            def get_positions(self):
                return {
                    "result": {
                        "list": [
                            {
                                "symbol": "BTCUSDT",
                                "side": "Buy",
                                "positionIdx": 0,
                                "size": "1",
                                "markPrice": "95" if self.crossed else "120",
                                "avgPrice": "100",
                                "stopLoss": "100",
                                "takeProfit": "140",
                                "liqPrice": "50",
                            }
                        ]
                    }
                }

            def new_order_link_id(self, prefix):
                return f"{prefix}-1"

            def close_position_market(
                self,
                symbol,
                side,
                position_idx,
                *,
                order_link_id,
            ):
                self.closes.append((symbol, side, position_idx, order_link_id))
                return {"simulated": True}

            def close(self):
                return None

        bybit = BybitDouble()

        class DeepSeekDouble:
            def validate_model(self):
                bybit.crossed = True

            def close(self):
                return None

        collect = Mock()
        with (
            patch("core.auto_trading.validate_config", return_value=[]),
            patch("core.auto_trading.BybitAPI", return_value=bybit),
            patch(
                "core.auto_trading.DeepSeekAPI",
                return_value=DeepSeekDouble(),
            ),
            patch("core.auto_trading._fee_rates", return_value={}),
            patch("core.auto_trading.collect_cycle", collect),
            patch("core.auto_trading.notify"),
        ):
            main_loop(threading.Event(), once=True)
        self.assertEqual(len(bybit.closes), 1)
        collect.assert_not_called()


if __name__ == "__main__":
    unittest.main()
