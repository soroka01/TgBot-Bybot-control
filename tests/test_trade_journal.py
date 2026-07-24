import sqlite3
from contextlib import closing
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from api.bybit_api import BybitAPI, BybitAPIError
from core.trade_journal import (
    _CLOSED_PNL_SYNC_LOCK,
    DAY_MS,
    MAX_WINDOW_MS,
    TradeJournal,
    _iter_windows_newest_first,
    normalize_closed_pnl,
)
from storage.database import SQLiteStore


def closed_row(
    order_id: str | None,
    *,
    size: str = "1",
    pnl: str = "9.8",
    created_ms: int = 1_000,
    updated_ms: int = 2_000,
    fill_count: str = "1",
) -> dict:
    return {
        "orderId": order_id or "",
        "symbol": "BTCUSDT",
        "side": "Sell",
        "qty": "2",
        "closedSize": size,
        "orderPrice": "110",
        "avgEntryPrice": "100",
        "avgExitPrice": "110",
        "cumEntryValue": "100",
        "cumExitValue": "110",
        "closedPnl": pnl,
        "openFee": "0.1",
        "closeFee": "0.1",
        "leverage": "2",
        "fillCount": fill_count,
        "orderType": "Market",
        "execType": "Trade",
        "createdTime": str(created_ms),
        "updatedTime": str(updated_ms),
    }


class FakeBybit:
    base = "https://api.bybit.test"

    def __init__(self, uid_results, *, api_key="super-secret-api-key"):
        self.api_key = api_key
        self.uid_results = list(uid_results)
        self.uid_calls = 0

    def get_account_user_id(self):
        self.uid_calls += 1
        if not self.uid_results:
            raise RuntimeError("UID endpoint unavailable")
        result = self.uid_results.pop(0)
        if isinstance(result, Exception):
            raise result
        return result


class SyncBybit(FakeBybit):
    def __init__(self, row):
        super().__init__(["123456"])
        self.row = row
        self.closed_calls = []
        self.fail_on_call = 2

    def get_closed_pnl(self, **kwargs):
        self.closed_calls.append(dict(kwargs))
        if self.fail_on_call == len(self.closed_calls):
            raise RuntimeError("temporary history outage")
        return {"retCode": 0, "result": {"list": [dict(self.row)]}}


class TradeJournalSchemaTests(unittest.TestCase):
    def test_clean_schema_and_reinitialization_are_idempotent(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "journal.sqlite3"
            SQLiteStore(path)
            SQLiteStore(path)
            with closing(sqlite3.connect(path)) as conn:
                tables = {
                    row[0]
                    for row in conn.execute(
                        "SELECT name FROM sqlite_master WHERE type = 'table'"
                    )
                }
            self.assertTrue(
                {
                    "trade_account_aliases",
                    "trade_setups",
                    "closed_trade_records",
                    "trade_sync_state",
                    "equity_snapshots",
                }.issubset(tables)
            )

    def test_old_database_is_upgraded_without_losing_existing_data(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "journal.sqlite3"
            store = SQLiteStore(path)
            user = SimpleNamespace(
                id=7,
                username="owner",
                first_name="Owner",
                last_name="",
            )
            store.ensure_user(user, 7, is_admin=True)
            with store._connection() as conn:
                conn.execute("DROP TABLE closed_trade_records")
                conn.execute("DROP TABLE trade_setups")
                conn.execute("DROP TABLE trade_sync_state")
                conn.execute("DROP TABLE equity_snapshots")
                conn.execute("DROP TABLE trade_account_aliases")
                conn.execute(
                    """
                    CREATE TABLE trade_account_aliases (
                        api_fingerprint TEXT PRIMARY KEY,
                        account_scope TEXT NOT NULL
                    )
                    """
                )
                conn.execute(
                    "INSERT INTO trade_account_aliases VALUES (?, ?)",
                    ("legacy-fingerprint", "legacy-scope"),
                )

            upgraded = SQLiteStore(path)
            self.assertEqual(upgraded.get_user(7)["username"], "owner")
            with upgraded._connection() as conn:
                tables = {
                    row["name"]
                    for row in conn.execute(
                        "SELECT name FROM sqlite_master WHERE type = 'table'"
                    )
                }
            self.assertIn("closed_trade_records", tables)
            self.assertIn("trade_account_aliases", tables)
            with upgraded._connection() as conn:
                aliases = conn.execute(
                    "SELECT COUNT(*) FROM trade_account_aliases"
                ).fetchone()[0]
            self.assertEqual(aliases, 0)


class ClosedTradePersistenceTests(unittest.TestCase):
    def setUp(self):
        self.temporary = TemporaryDirectory()
        self.path = Path(self.temporary.name) / "journal.sqlite3"
        self.store = SQLiteStore(self.path)
        self.scope = "uid-scope"

    def tearDown(self):
        self.temporary.cleanup()

    def _setup(self, candidate_id="candidate-1"):
        self.store.upsert_trade_setup(
            account_scope=self.scope,
            candidate_id=candidate_id,
            snapshot_id="snapshot-1",
            symbol="BTCUSDT",
            side="Buy",
            status="open",
            dry_run=False,
            plan={
                "quantity": "2",
                "entry_price": "100",
                "take_profit": "110",
                "stop_loss": "95",
            },
        )
        self.store.update_trade_setup(
            self.scope,
            candidate_id,
            actual_entry_qty="2",
            actual_entry_price="100",
            opened_at_ms=500,
        )

    def _status(self, candidate_id="candidate-1"):
        with self.store._connection() as conn:
            row = conn.execute(
                """
                SELECT status, closed_at_ms
                FROM trade_setups
                WHERE account_scope = ? AND candidate_id = ?
                """,
                (self.scope, candidate_id),
            ).fetchone()
        return dict(row)

    def test_fallback_record_id_ignores_mutable_close_progress(self):
        first = closed_row(None, size="0.5", fill_count="1")
        later = dict(first, closedSize="1.5", fillCount="4", updatedTime="3000")
        self.assertEqual(
            normalize_closed_pnl(first)["record_id"],
            normalize_closed_pnl(later)["record_id"],
        )

    def test_normalizer_rejects_semantically_corrupt_exchange_rows(self):
        with self.assertRaisesRegex(ValueError, "сторону"):
            normalize_closed_pnl(dict(closed_row("bad-side"), side="Unknown"))
        with self.assertRaisesRegex(ValueError, "closedSize"):
            normalize_closed_pnl(dict(closed_row("bad-size"), closedSize="0"))
        with self.assertRaisesRegex(ValueError, "updatedTime"):
            normalize_closed_pnl(
                closed_row(
                    "bad-time",
                    created_ms=3_000,
                    updated_ms=2_000,
                )
            )

    def test_upsert_is_idempotent_stale_safe_and_partial_close_aware(self):
        self._setup()
        first = normalize_closed_pnl(
            closed_row("close-1", size="1", pnl="9", updated_ms=2_000)
        )
        self.assertTrue(self.store.upsert_closed_trade_record(self.scope, first))
        self.assertEqual(self._status()["status"], "open")

        newer = normalize_closed_pnl(
            closed_row("close-1", size="1", pnl="11", updated_ms=2_500)
        )
        self.assertFalse(self.store.upsert_closed_trade_record(self.scope, newer))

        stale = normalize_closed_pnl(
            closed_row("close-1", size="1", pnl="-99", updated_ms=2_200)
        )
        self.assertFalse(self.store.upsert_closed_trade_record(self.scope, stale))
        records = self.store.list_closed_trade_records(
            self.scope,
            since_ms=0,
        )
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["closed_pnl"], "11")
        self.assertEqual(records[0]["updated_time_ms"], 2_500)

        # Same-price external activity larger than the remaining bot quantity
        # must not complete or contaminate the bot lifecycle.
        external = normalize_closed_pnl(
            closed_row("external", size="2", pnl="20", updated_ms=2_700)
        )
        self.store.upsert_closed_trade_record(self.scope, external)
        records = self.store.list_closed_trade_records(
            self.scope,
            since_ms=0,
        )
        external_record = next(
            item for item in records if item["record_id"] == "order:external"
        )
        self.assertIsNone(external_record["candidate_id"])
        self.assertEqual(self._status()["status"], "open")

        final = normalize_closed_pnl(
            closed_row("close-2", size="1", pnl="8", updated_ms=3_000)
        )
        self.store.upsert_closed_trade_record(self.scope, final)
        self.assertEqual(self._status()["status"], "closed")
        self.assertEqual(self._status()["closed_at_ms"], 3_000)

    def test_ambiguous_price_match_is_not_attributed(self):
        self._setup("candidate-1")
        with self.store._connection() as conn:
            conn.execute(
                """
                INSERT INTO trade_setups (
                    account_scope, candidate_id, strategy_version, symbol,
                    side, status, dry_run, planned_qty, planned_entry_price,
                    actual_entry_qty, actual_entry_price, opened_at_ms,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    self.scope,
                    "candidate-2",
                    "trend_atr.v1",
                    "BTCUSDT",
                    "Buy",
                    "open",
                    0,
                    "2",
                    "100",
                    "2",
                    "100",
                    600,
                    "2026-01-01T00:00:00+00:00",
                    "2026-01-01T00:00:00+00:00",
                ),
            )
        record = normalize_closed_pnl(closed_row("ambiguous", size="1"))
        self.store.upsert_closed_trade_record(self.scope, record)
        rows = self.store.list_closed_trade_records(self.scope, since_ms=0)
        self.assertIsNone(rows[0]["candidate_id"])

    def test_unconfirmed_entry_submission_is_not_guessed_as_a_trade(self):
        self.store.upsert_trade_setup(
            account_scope=self.scope,
            candidate_id="prepared-only",
            snapshot_id="snapshot",
            symbol="BTCUSDT",
            side="Buy",
            status="entry_submitted",
            dry_run=False,
            plan={"quantity": "1", "entry_price": "100"},
        )
        record = normalize_closed_pnl(closed_row("external-after-crash", size="1"))
        self.store.upsert_closed_trade_record(self.scope, record)
        rows = self.store.list_closed_trade_records(self.scope, since_ms=0)
        self.assertIsNone(rows[0]["candidate_id"])

    def test_equity_snapshot_rejects_bad_values_and_keeps_newest_in_bucket(self):
        with self.assertRaisesRegex(ValueError, "positive"):
            self.store.record_equity_snapshot(
                self.scope,
                captured_at_ms=2_000,
                equity_usd="0",
            )
        self.store.record_equity_snapshot(
            self.scope,
            captured_at_ms=2_000,
            equity_usd="100",
        )
        self.store.record_equity_snapshot(
            self.scope,
            captured_at_ms=1_000,
            equity_usd="90",
        )
        snapshots = self.store.list_equity_snapshots(
            self.scope,
            since_ms=0,
        )
        self.assertEqual(len(snapshots), 1)
        self.assertEqual(snapshots[0]["captured_at_ms"], 2_000)
        self.assertEqual(snapshots[0]["equity_usd"], "100")


class AccountScopeTests(unittest.TestCase):
    def test_only_verified_uid_alias_is_cached_and_reused_offline(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "journal.sqlite3"
            store = SQLiteStore(path)
            bybit = FakeBybit([RuntimeError("outage"), "777001"])
            journal = TradeJournal(bybit, store)

            fallback_scope = journal.account_scope
            store.upsert_trade_setup(
                account_scope=fallback_scope,
                candidate_id="dry-candidate",
                snapshot_id=None,
                symbol="BTCUSDT",
                side="Buy",
                status="previewed",
                dry_run=True,
                plan={"quantity": "1", "entry_price": "100"},
            )
            store.upsert_closed_trade_record(
                fallback_scope,
                normalize_closed_pnl(closed_row("history-row")),
            )
            store.update_trade_sync_state(
                fallback_scope,
                coverage_start_ms=100,
                coverage_end_ms=2_000,
                last_success_ms=2_000,
            )
            store.record_equity_snapshot(
                fallback_scope,
                captured_at_ms=2_000,
                equity_usd="100",
            )

            journal._next_uid_retry_at = 0
            uid_scope = journal.account_scope
            self.assertNotEqual(uid_scope, fallback_scope)
            self.assertEqual(bybit.uid_calls, 2)

            with store._connection() as conn:
                for table in (
                    "trade_setups",
                    "closed_trade_records",
                    "trade_sync_state",
                    "equity_snapshots",
                ):
                    source_count = conn.execute(
                        f"SELECT COUNT(*) FROM {table} WHERE account_scope = ?",
                        (fallback_scope,),
                    ).fetchone()[0]
                    target_count = conn.execute(
                        f"SELECT COUNT(*) FROM {table} WHERE account_scope = ?",
                        (uid_scope,),
                    ).fetchone()[0]
                    self.assertEqual(source_count, 0, table)
                    self.assertEqual(target_count, 1, table)
                alias = conn.execute(
                    """
                    SELECT account_scope, verified_uid
                    FROM trade_account_aliases
                    WHERE account_scope = ?
                    """,
                    (uid_scope,),
                ).fetchone()
            self.assertEqual(alias["account_scope"], uid_scope)
            self.assertEqual(alias["verified_uid"], 1)

            reopened_store = SQLiteStore(path)
            offline_bybit = FakeBybit([], api_key=bybit.api_key)
            offline_journal = TradeJournal(offline_bybit, reopened_store)
            self.assertEqual(offline_journal.account_scope, uid_scope)
            self.assertEqual(offline_bybit.uid_calls, 0)

            rotated_bybit = FakeBybit(["777001"], api_key="rotated-key")
            rotated_journal = TradeJournal(rotated_bybit, store)
            self.assertEqual(rotated_journal.account_scope, uid_scope)
            self.assertEqual(rotated_bybit.uid_calls, 1)
            with store._connection() as conn:
                alias_count = conn.execute(
                    "SELECT COUNT(*) FROM trade_account_aliases"
                ).fetchone()[0]
            self.assertEqual(alias_count, 2)

            secret = bybit.api_key.encode()
            for database_file in path.parent.glob(f"{path.name}*"):
                self.assertNotIn(secret, database_file.read_bytes())

    def test_live_prepare_requires_uid_but_dry_run_can_use_fallback(self):
        with TemporaryDirectory() as directory:
            store = SQLiteStore(Path(directory) / "journal.sqlite3")
            bybit = FakeBybit([])
            journal = TradeJournal(bybit, store)
            plan = SimpleNamespace(
                quantity="1",
                entry_price="100",
                take_profit="110",
                stop_loss="95",
                leverage="2",
                risk_usd="5",
                reward_usd="10",
                estimated_cost_usd="0.2",
                net_risk_reward="2",
            )
            common = {
                "plan": plan,
                "cycle": {"snapshot": {}, "account": {}},
                "decision": None,
                "sizing_context": {},
            }
            with self.assertRaisesRegex(RuntimeError, "UID"):
                journal.prepare_entry(
                    candidate={
                        "id": "live",
                        "symbol": "BTCUSDT",
                        "side": "Buy",
                    },
                    order_link_id="live-order",
                    dry_run=False,
                    **common,
                )

            journal.prepare_entry(
                candidate={
                    "id": "dry",
                    "symbol": "BTCUSDT",
                    "side": "Buy",
                },
                order_link_id="dry-order",
                dry_run=True,
                **common,
            )
            with store._connection() as conn:
                rows = conn.execute(
                    "SELECT candidate_id, dry_run FROM trade_setups"
                ).fetchall()
                alias_count = conn.execute(
                    "SELECT COUNT(*) FROM trade_account_aliases"
                ).fetchone()[0]
            self.assertEqual(
                [(row["candidate_id"], row["dry_run"]) for row in rows],
                [("dry", 1)],
            )
            self.assertEqual(alias_count, 0)

    def test_equity_write_failure_marks_prepared_setup_terminal(self):
        with TemporaryDirectory() as directory:
            store = SQLiteStore(Path(directory) / "journal.sqlite3")
            journal = TradeJournal(FakeBybit(["123456"]), store)
            plan = SimpleNamespace(
                quantity="1",
                entry_price="100",
                take_profit="110",
                stop_loss="95",
                leverage="2",
                risk_usd="5",
                reward_usd="10",
                estimated_cost_usd="0.2",
                net_risk_reward="2",
            )
            with patch.object(
                store,
                "record_equity_snapshot",
                side_effect=sqlite3.OperationalError("disk unavailable"),
            ):
                with self.assertRaises(sqlite3.OperationalError):
                    journal.prepare_entry(
                        candidate={
                            "id": "candidate",
                            "symbol": "BTCUSDT",
                            "side": "Buy",
                        },
                        plan=plan,
                        cycle={
                            "snapshot": {},
                            "account": {"equity_usd": "100"},
                        },
                        decision=None,
                        order_link_id="open-candidate",
                        sizing_context={},
                        dry_run=False,
                    )
            with store._connection() as conn:
                setup = conn.execute(
                    "SELECT status, last_error FROM trade_setups"
                ).fetchone()
            self.assertEqual(setup["status"], "failed")
            self.assertIn("OperationalError", setup["last_error"])


class ClosedPnlSyncTests(unittest.TestCase):
    def test_windows_are_newest_first_complete_and_at_most_seven_days(self):
        start = 0
        end = 15 * DAY_MS
        windows = list(_iter_windows_newest_first(start, end))
        self.assertEqual(windows[0][1], end)
        self.assertEqual(windows[-1][0], start)
        self.assertTrue(
            all(window_end - window_start <= MAX_WINDOW_MS for window_start, window_end in windows)
        )
        ascending = sorted(windows)
        self.assertTrue(
            all(
                ascending[index][1] == ascending[index + 1][0]
                for index in range(len(ascending) - 1)
            )
        )

    def test_failed_backfill_keeps_watermark_and_retry_is_idempotent(self):
        with TemporaryDirectory() as directory:
            now = 20 * DAY_MS
            row = closed_row(
                "history",
                created_ms=18 * DAY_MS,
                updated_ms=19 * DAY_MS,
            )
            bybit = SyncBybit(row)
            store = SQLiteStore(Path(directory) / "journal.sqlite3")
            journal = TradeJournal(bybit, store)

            with self.assertRaisesRegex(RuntimeError, "history outage"):
                journal.sync_closed_pnl(
                    lookback_days=15,
                    now_ms=now,
                )
            self.assertEqual(
                store.get_trade_sync_state(journal.account_scope),
                {},
            )
            self.assertEqual(
                len(store.list_closed_trade_records(journal.account_scope, since_ms=0)),
                1,
            )

            bybit.fail_on_call = None
            summary = journal.sync_closed_pnl(
                lookback_days=15,
                now_ms=now,
            )
            self.assertEqual(summary.inserted, 0)
            self.assertEqual(
                len(store.list_closed_trade_records(journal.account_scope, since_ms=0)),
                1,
            )
            state = store.get_trade_sync_state(journal.account_scope)
            self.assertEqual(state["coverage_start_ms"], now - 15 * DAY_MS)
            self.assertEqual(state["coverage_end_ms"], now)
            self.assertEqual(state["last_success_ms"], now)
            self.assertTrue(
                all(
                    call["all_pages"]
                    and call["end_time"] - call["start_time"] <= MAX_WINDOW_MS
                    for call in bybit.closed_calls
                )
            )

    def test_concurrent_sync_uses_cache_instead_of_duplicate_api_backfill(self):
        with TemporaryDirectory() as directory:
            bybit = SyncBybit(closed_row("history"))
            bybit.fail_on_call = None
            store = SQLiteStore(Path(directory) / "journal.sqlite3")
            journal = TradeJournal(bybit, store)
            self.assertTrue(_CLOSED_PNL_SYNC_LOCK.acquire(blocking=False))
            try:
                summary = journal.sync_closed_pnl(
                    lookback_days=365,
                    now_ms=400 * DAY_MS,
                )
            finally:
                _CLOSED_PNL_SYNC_LOCK.release()
            self.assertTrue(summary.skipped_busy)
            self.assertFalse(summary.skipped_fresh)
            self.assertEqual(summary.windows, 0)
            self.assertEqual(bybit.closed_calls, [])


class BybitClosedPnlApiTests(unittest.TestCase):
    def test_closed_pnl_paginates_and_rejects_repeated_cursor(self):
        client = BybitAPI("key", "secret", dry_run=False)
        first = {
            "retCode": 0,
            "result": {
                "list": [{"orderId": "one"}],
                "nextPageCursor": "next",
            },
        }
        second = {
            "retCode": 0,
            "result": {
                "list": [{"orderId": "two"}],
                "nextPageCursor": "",
            },
        }
        with patch.object(
            client,
            "_private_request",
            side_effect=[first, second],
        ) as request:
            response = client.get_closed_pnl(
                start_time=0,
                end_time=DAY_MS,
                all_pages=True,
            )
        self.assertEqual(
            [row["orderId"] for row in response["result"]["list"]],
            ["one", "two"],
        )
        self.assertEqual(request.call_args_list[1].kwargs["params"]["cursor"], "next")

        repeated = dict(
            first,
            result={"list": [], "nextPageCursor": "next"},
        )
        with patch.object(
            client,
            "_private_request",
            side_effect=[repeated, repeated],
        ):
            with self.assertRaisesRegex(BybitAPIError, "pagination cursor"):
                client.get_closed_pnl(
                    start_time=0,
                    end_time=DAY_MS,
                    all_pages=True,
                )
        client.close()

    def test_closed_pnl_rejects_oversized_window_before_request(self):
        client = BybitAPI("key", "secret", dry_run=False)
        with patch.object(client, "_private_request") as request:
            with self.assertRaisesRegex(ValueError, "7"):
                client.get_closed_pnl(
                    start_time=0,
                    end_time=MAX_WINDOW_MS + 1,
                    all_pages=True,
                )
        request.assert_not_called()
        client.close()

    def test_account_user_id_exposes_only_valid_uid(self):
        client = BybitAPI("key", "secret", dry_run=False)
        with patch.object(
            client,
            "_private_request",
            return_value={
                "retCode": 0,
                "result": {
                    "userID": 123456,
                    "apiKey": "must-not-be-returned",
                },
            },
        ):
            self.assertEqual(client.get_account_user_id(), "123456")
        with patch.object(
            client,
            "_private_request",
            return_value={"retCode": 0, "result": {"userID": "invalid"}},
        ):
            with self.assertRaises(BybitAPIError):
                client.get_account_user_id()
        client.close()


if __name__ == "__main__":
    unittest.main()
