from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest

from storage.database import SQLiteStore


class StorageSecurityTests(unittest.TestCase):
    def test_admin_flag_is_not_sticky(self):
        with TemporaryDirectory() as directory:
            store = SQLiteStore(Path(directory) / "test.sqlite3")
            user = SimpleNamespace(
                id=42,
                username="owner",
                first_name="A",
                last_name="",
            )
            store.ensure_user(user, 42, is_admin=True)
            self.assertEqual(store.get_user(42)["is_admin"], 1)
            store.ensure_user(user, 42, is_admin=False)
            self.assertEqual(store.get_user(42)["is_admin"], 0)

    def test_screen_restores_even_when_notifications_are_off(self):
        with TemporaryDirectory() as directory:
            store = SQLiteStore(Path(directory) / "test.sqlite3")
            user = SimpleNamespace(
                id=42,
                username="owner",
                first_name="A",
                last_name="",
            )
            store.ensure_user(user, 42)
            store.update_user_settings(42, notifications_enabled=False)
            store.save_screen(42, 77, 9)
            self.assertEqual(store.screen_targets(), [(42, 77, 9)])

    def test_daily_equity_high_water_survives_updates(self):
        with TemporaryDirectory() as directory:
            store = SQLiteStore(Path(directory) / "test.sqlite3")
            first = store.update_daily_equity_guard(1_000, utc_day="2026-07-24")
            peak = store.update_daily_equity_guard(1_100, utc_day="2026-07-24")
            loss = store.update_daily_equity_guard(1_020, utc_day="2026-07-24")
            self.assertEqual(first["drawdown"], 0)
            self.assertEqual(peak["high_water_equity"], 1_100)
            self.assertEqual(loss["high_water_equity"], 1_100)
            self.assertEqual(loss["drawdown"], 80)

    def test_daily_guard_is_scoped_per_exchange_account(self):
        with TemporaryDirectory() as directory:
            store = SQLiteStore(Path(directory) / "test.sqlite3")
            store.update_daily_equity_guard(
                1_000,
                utc_day="2026-07-24",
                scope="account-a",
            )
            second = store.update_daily_equity_guard(
                500,
                utc_day="2026-07-24",
                scope="account-b",
            )
            self.assertEqual(second["high_water_equity"], 500)
            self.assertEqual(second["drawdown"], 0)

    def test_legacy_group_screen_is_not_restored(self):
        with TemporaryDirectory() as directory:
            store = SQLiteStore(Path(directory) / "test.sqlite3")
            user = SimpleNamespace(
                id=42,
                username="owner",
                first_name="A",
                last_name="",
            )
            store.ensure_user(user, -100_123)
            store.save_screen(-100_123, 77, 1)
            self.assertEqual(store.screen_targets(), [])

    def test_permanently_unreachable_chat_is_not_restored(self):
        with TemporaryDirectory() as directory:
            store = SQLiteStore(Path(directory) / "test.sqlite3")
            user = SimpleNamespace(
                id=42,
                username="owner",
                first_name="Owner",
                last_name="",
            )
            store.ensure_user(user, 42, is_admin=True)
            store.save_screen(42, 77, 3)
            store.deactivate_chat(42)
            self.assertEqual(store.screen_targets(), [])

    def test_notification_backoff_does_not_starve_new_alert(self):
        with TemporaryDirectory() as directory:
            store = SQLiteStore(Path(directory) / "test.sqlite3")
            user = SimpleNamespace(
                id=42,
                username="owner",
                first_name="A",
                last_name="",
            )
            store.ensure_user(user, 42)
            first_alert = store.create_alert(
                42,
                kind="price",
                symbol="BTC",
                direction="above",
                threshold=100,
            )
            store.apply_alert_observation(
                first_alert,
                value=101,
                should_trigger=True,
                notification_message="first",
            )
            first_outbox = store.pending_notifications()[0]["id"]
            store.mark_notification_attempt(first_outbox, "temporary_failure")
            self.assertEqual(store.pending_notifications(), [])

            second_alert = store.create_alert(
                42,
                kind="price",
                symbol="ETH",
                direction="above",
                threshold=100,
            )
            store.apply_alert_observation(
                second_alert,
                value=101,
                should_trigger=True,
                notification_message="second",
            )
            pending = store.pending_notifications()
            self.assertEqual([item["message"] for item in pending], ["second"])

    def test_alert_threshold_rejects_non_finite_values(self):
        with TemporaryDirectory() as directory:
            store = SQLiteStore(Path(directory) / "test.sqlite3")
            user = SimpleNamespace(
                id=42,
                username="owner",
                first_name="A",
                last_name="",
            )
            store.ensure_user(user, 42)
            with self.assertRaises(ValueError):
                store.create_alert(
                    42,
                    kind="price",
                    symbol="BTC",
                    direction="above",
                    threshold=float("nan"),
                )


if __name__ == "__main__":
    unittest.main()
