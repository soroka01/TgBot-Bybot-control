from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

from aiogram.types import CallbackQuery, Chat, Message, User

import telegram_bot.handlers.history as history
from telegram_bot.keyboards.history_menu import (
    DEFAULT_HISTORY_SCOPE,
    HISTORY_PERIODS,
    get_history_menu,
)


def _callback(data: str) -> CallbackQuery:
    user = User(id=42, is_bot=False, first_name="Tester")
    return CallbackQuery(
        id="history-callback",
        from_user=user,
        chat_instance="history-instance",
        data=data,
        message=Message(
            message_id=77,
            date=datetime.now(timezone.utc),
            chat=Chat(id=42, type="private"),
            from_user=user,
            text="screen",
        ),
    )


def _analytics(trade_count: int) -> dict:
    return {
        "trade_count": trade_count,
        "wins": 4,
        "losses": 2,
        "breakeven": 1,
        "net_pnl": Decimal("31.25"),
        "win_rate": Decimal("57.1428"),
        "profit_factor": Decimal("1.75"),
        "expectancy": Decimal("4.4642"),
        "avg_win": Decimal("10.5"),
        "avg_loss": Decimal("-5.25"),
        "payoff_ratio": Decimal("2"),
        "max_drawdown": Decimal("12.5"),
        "avg_r": Decimal("0.42"),
        "r_count": max(0, trade_count - 2),
        "fees_total": Decimal("3.125"),
        "fee_complete_count": trade_count,
        "fee_complete_record_count": trade_count,
        "source_record_count": trade_count,
        "turnover": Decimal("12345.67"),
        "avg_hold_ms": 7_200_000,
        "hold_count": trade_count,
        "cumulative_pnl": [
            Decimal("4"),
            Decimal("-2"),
            Decimal("6"),
            Decimal("5"),
            Decimal("14"),
            Decimal("22"),
            Decimal("31.25"),
        ],
        "equity_return_percent": Decimal("3.5"),
        "equity_max_drawdown_percent": Decimal("1.25"),
    }


class HistoryKeyboardTests(unittest.TestCase):
    def test_menu_contains_every_period_scope_refresh_and_back(self):
        self.assertEqual(DEFAULT_HISTORY_SCOPE, "all")
        markup = get_history_menu(30, "bot")
        buttons = [
            button
            for row in markup.inline_keyboard
            for button in row
        ]
        callbacks = {button.callback_data for button in buttons}
        labels = {button.text.lstrip("✓ ") for button in buttons}

        for period in HISTORY_PERIODS:
            self.assertIn(f"history:view:{period}:bot", callbacks)
        self.assertTrue(
            {"1Д", "7Д", "14Д", "1М", "3М", "6М", "1ГОД"}.issubset(labels)
        )
        self.assertIn("history:view:30:all", callbacks)
        self.assertIn("history:refresh:30:bot", callbacks)
        self.assertIn("menu:main", callbacks)
        self.assertTrue(
            all(
                len((button.callback_data or "").encode("utf-8")) <= 64
                for button in buttons
            )
        )


class HistoryFormattingTests(unittest.TestCase):
    def test_compact_screen_escapes_exchange_data_and_shows_only_five_latest(self):
        rows = []
        for index in range(7):
            symbol = "<BTC&USDT>" if index == 6 else f"R{index}USDT"
            rows.append(
                {
                    "record_id": f"record-{index}",
                    "symbol": symbol,
                    "position_side": "Buy" if index % 2 else "Sell",
                    "candidate_id": f"candidate-{index}" if index % 2 else None,
                    "closed_pnl": str(index - 2),
                    "created_time_ms": 1_700_000_000_000 + index,
                    "updated_time_ms": 1_700_000_000_000 + index,
                }
            )

        text = history.format_history_screen(
            rows,
            _analytics(len(rows)),
            days=30,
            scope="all",
        )

        self.assertLess(len(text), 2_500)
        self.assertIn("&lt;BTC&amp;USDT&gt;", text)
        self.assertNotIn("<BTC&USDT>", text)
        self.assertIn("R2USDT", text)
        self.assertNotIn("R1USDT", text)
        self.assertNotIn("R0USDT", text)
        self.assertIn("Cumulative PnL", text)
        self.assertIn("Avg W / Avg L", text)
        self.assertIn("Payoff", text)
        self.assertIn("0.42R (5)", text)
        self.assertTrue(any(block in text for block in "▁▂▃▄▅▆▇█"))
        self.assertIn("уже учтены в Net PnL", text)

    def test_empty_bot_scope_explains_filter_without_oversized_text(self):
        text = history.format_history_screen(
            [],
            {"trade_count": 0},
            days=7,
            scope="bot",
            cache_warning=True,
        )
        self.assertIn("локальный кэш", text)
        self.assertIn("только сделки", text)
        self.assertLess(len(text), 2_500)

    def test_busy_sync_reports_current_cache_without_claiming_outage(self):
        text = history.format_history_screen(
            [],
            {"trade_count": 0},
            days=365,
            scope="all",
            sync_busy=True,
        )
        self.assertIn("Синхронизация уже идёт", text)
        self.assertNotIn("Bybit временно недоступен", text)

    def test_recent_list_prefers_analytics_aggregates_over_partial_rows(self):
        analytics = _analytics(1)
        analytics["trades"] = [
            {
                "record_id": "aggregate",
                "symbol": "AGGUSDT",
                "position_side": "Buy",
                "closed_pnl": "31.25",
                "updated_time_ms": 1_700_000_000_100,
            }
        ]
        text = history.format_history_screen(
            [
                {
                    "record_id": "partial",
                    "symbol": "PARTIALUSDT",
                    "position_side": "Buy",
                    "closed_pnl": "10",
                    "updated_time_ms": 1_700_000_000_000,
                }
            ],
            analytics,
            days=30,
            scope="all",
        )
        self.assertIn("AGGUSDT", text)
        self.assertNotIn("PARTIALUSDT", text)

    def test_real_analytics_contract_renders_one_logical_partial_close(self):
        from core.trade_analytics import build_trade_analytics

        rows = [
            {
                "account_scope": "account",
                "record_id": f"part-{index}",
                "candidate_id": "candidate",
                "symbol": "BOTUSDT",
                "setup_side": "Buy",
                "closed_pnl": pnl,
                "open_fee": "0.10",
                "close_fee": "0.10",
                "cum_entry_value": "100",
                "cum_exit_value": "101",
                "planned_risk_usd": "5",
                "setup_opened_at_ms": 1_000,
                "created_time_ms": closed_at,
                "updated_time_ms": closed_at,
            }
            for index, (pnl, closed_at) in enumerate(
                (("2", 2_000), ("3", 3_000)),
                start=1,
            )
        ]
        analytics = build_trade_analytics(rows)

        text = history.format_history_screen(
            rows,
            analytics,
            days=30,
            scope="bot",
        )

        self.assertIn("<b>1</b> сделка · 2 закрытия", text)
        self.assertIn("<b>BOTUSDT</b> L", text)
        self.assertEqual(text.count("<b>BOTUSDT</b>"), 1)
        self.assertIn("(2/2, уже учтены в Net PnL)", text)

    def test_sparkline_is_bounded_and_handles_flat_series(self):
        chart = history.cumulative_pnl_sparkline(
            [Decimal("0")] * 100,
            width=12,
        )
        self.assertEqual(len(chart), 12)
        self.assertEqual(len(set(chart)), 1)


class HistoryBuilderTests(unittest.TestCase):
    def test_builder_uses_journal_filters_and_closes_bybit(self):
        bybit = Mock()
        journal = Mock()
        journal.sync_closed_pnl.return_value = SimpleNamespace(skipped_fresh=True)
        journal.closed_records.return_value = []
        journal.equity_snapshots.return_value = []

        with (
            patch.object(history, "BybitAPI", return_value=bybit),
            patch.object(history, "TradeJournal", return_value=journal),
            patch.object(
                history,
                "_build_analytics",
                return_value={"trade_count": 0},
            ),
        ):
            text, markup = history.build_history_view(14, "bot", force=True)

        journal.sync_closed_pnl.assert_called_once_with(
            lookback_days=14,
            force=True,
        )
        journal.closed_records.assert_called_once_with(
            lookback_days=14,
            bot_only=True,
        )
        self.assertEqual(journal.equity_snapshots.call_count, 2)
        journal.equity_snapshots.assert_called_with(lookback_days=14)
        journal.record_current_equity.assert_called_once_with()
        bybit.close.assert_called_once_with()
        self.assertIn("14 дн.", text)
        self.assertIsNotNone(markup)

    def test_sync_failure_uses_cache_without_exposing_api_error(self):
        bybit = Mock()
        journal = Mock()
        journal.sync_closed_pnl.side_effect = RuntimeError(
            "sensitive exchange response"
        )
        journal.closed_records.return_value = []
        journal.equity_snapshots.return_value = []

        with (
            patch.object(history, "BybitAPI", return_value=bybit),
            patch.object(history, "TradeJournal", return_value=journal),
            patch.object(
                history,
                "_build_analytics",
                return_value={"trade_count": 0},
            ),
        ):
            text, _ = history.build_history_view(30, "all")

        self.assertIn("показан локальный кэш", text)
        self.assertNotIn("sensitive exchange response", text)
        journal.record_current_equity.assert_not_called()
        bybit.close.assert_called_once_with()


class HistoryHandlerTests(unittest.IsolatedAsyncioTestCase):
    async def test_main_history_opens_thirty_day_account_scope(self):
        callback = _callback("menu:history")
        with (
            patch.object(CallbackQuery, "answer", new=AsyncMock()) as answer,
            patch.object(
                history,
                "_render_history",
                new=AsyncMock(),
            ) as render,
        ):
            await history.show_history(callback)

        answer.assert_awaited_once_with("Загружаю историю…")
        render.assert_awaited_once_with(
            callback,
            days=30,
            scope="all",
            force=False,
        )

    async def test_refresh_edits_the_same_callback_message(self):
        callback = _callback("history:refresh:90:all")
        rendered_markup = get_history_menu(90, "all")
        token = (42, 77, 9)

        with (
            patch.object(CallbackQuery, "answer", new=AsyncMock()) as answer,
            patch.object(
                history,
                "build_history_view",
                return_value=("history", rendered_markup),
            ) as build,
            patch.object(
                history,
                "render_callback_screen",
                new=AsyncMock(return_value=callback.message),
            ) as render_loading,
            patch.object(
                history,
                "current_screen_token",
                return_value=token,
            ),
            patch.object(
                history,
                "render_if_current",
                new=AsyncMock(),
            ) as render_result,
        ):
            await history.change_history(callback)

        answer.assert_awaited_once_with("Обновляю Bybit…")
        build.assert_called_once_with(90, "all", force=True)
        render_loading.assert_awaited_once()
        self.assertIs(render_loading.await_args.args[0], callback.message)
        render_result.assert_awaited_once_with(
            token,
            callback.message,
            "history",
            rendered_markup,
        )


if __name__ == "__main__":
    unittest.main()
