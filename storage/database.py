"""Transactional SQLite repository for multi-user bot data.

SQLite is the durable source of truth for one bot process. Its synchronous
methods are called through asyncio.to_thread from Telegram handlers, so I/O
does not block the event loop.
"""

from __future__ import annotations

import json
import math
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Iterable, Iterator, Optional

from config import ALERT_DEFAULT_COOLDOWN_SECONDS, DATABASE_PATH
from utils.logger_setup import logger


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _json_payload(value: Optional[dict[str, Any]]) -> Optional[str]:
    if value is None:
        return None
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )


def _hex_digest(value: str, length: int, label: str) -> str:
    normalized = str(value).strip().lower()
    if (
        len(normalized) != length
        or any(character not in "0123456789abcdef" for character in normalized)
    ):
        raise ValueError(f"{label} must be a {length}-character hex digest")
    return normalized


class SQLiteStore:
    """Repository for Telegram state, risk controls, and the trade journal."""

    def __init__(self, path: Path = DATABASE_PATH) -> None:
        self.path = Path(path)
        self._lock = threading.RLock()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    @contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(
            self.path,
            timeout=10,
            isolation_level=None,
            check_same_thread=False,
        )
        connection.row_factory = sqlite3.Row
        try:
            connection.execute("PRAGMA foreign_keys = ON")
            connection.execute("PRAGMA journal_mode = WAL")
            connection.execute("PRAGMA busy_timeout = 5000")
            yield connection
        finally:
            connection.close()

    def _initialize(self) -> None:
        with self._lock, self._connection() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS users (
                    chat_id INTEGER PRIMARY KEY,
                    telegram_user_id INTEGER,
                    username TEXT,
                    first_name TEXT,
                    last_name TEXT,
                    display_name TEXT,
                    is_admin INTEGER NOT NULL DEFAULT 0,
                    is_active INTEGER NOT NULL DEFAULT 1,
                    locale TEXT NOT NULL DEFAULT 'ru',
                    timezone TEXT NOT NULL DEFAULT 'Asia/Yekaterinburg',
                    notifications_enabled INTEGER NOT NULL DEFAULT 1,
                    price_alerts_enabled INTEGER NOT NULL DEFAULT 1,
                    rsi_alerts_enabled INTEGER NOT NULL DEFAULT 1,
                    default_symbol TEXT NOT NULL DEFAULT 'BTC',
                    default_interval TEXT NOT NULL DEFAULT '15',
                    risk_per_trade_percent REAL,
                    max_total_risk_percent REAL,
                    max_leverage INTEGER,
                    max_order_usdt REAL,
                    auto_mode_enabled INTEGER NOT NULL DEFAULT 0,
                    dry_run_override INTEGER,
                    screen_message_id INTEGER,
                    screen_revision INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    last_seen_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS alerts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    chat_id INTEGER NOT NULL REFERENCES users(chat_id) ON DELETE CASCADE,
                    kind TEXT NOT NULL CHECK(kind IN ('price', 'rsi')),
                    symbol TEXT NOT NULL,
                    timeframe TEXT,
                    direction TEXT NOT NULL CHECK(direction IN ('above', 'below')),
                    threshold REAL NOT NULL CHECK(threshold > 0),
                    repeat_mode TEXT NOT NULL DEFAULT 'once'
                        CHECK(repeat_mode IN ('once', 'repeat')),
                    cooldown_seconds INTEGER NOT NULL DEFAULT 60 CHECK(cooldown_seconds >= 0),
                    is_enabled INTEGER NOT NULL DEFAULT 1,
                    last_value REAL,
                    last_checked_at TEXT,
                    last_triggered_at TEXT,
                    trigger_count INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS activity_log (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    chat_id INTEGER REFERENCES users(chat_id) ON DELETE SET NULL,
                    event_type TEXT NOT NULL,
                    severity TEXT NOT NULL DEFAULT 'info',
                    symbol TEXT,
                    message TEXT NOT NULL,
                    payload_json TEXT,
                    created_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS execution_signals (
                    candidate_id TEXT PRIMARY KEY,
                    symbol TEXT NOT NULL,
                    status TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS daily_risk_state (
                    utc_day TEXT PRIMARY KEY,
                    start_equity REAL NOT NULL,
                    high_water_equity REAL NOT NULL,
                    last_equity REAL NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS notification_outbox (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    chat_id INTEGER NOT NULL,
                    alert_id INTEGER,
                    message TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'pending'
                        CHECK(status IN ('pending', 'delivered')),
                    attempts INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL,
                    delivered_at TEXT,
                    next_attempt_at TEXT,
                    last_attempt_at TEXT,
                    last_error TEXT,
                    abandoned_at TEXT
                );

                CREATE TABLE IF NOT EXISTS trade_account_aliases (
                    api_fingerprint TEXT PRIMARY KEY,
                    account_scope TEXT NOT NULL,
                    verified_uid INTEGER NOT NULL DEFAULT 1
                        CHECK(verified_uid = 1),
                    verified_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS trade_setups (
                    account_scope TEXT NOT NULL,
                    candidate_id TEXT NOT NULL,
                    snapshot_id TEXT,
                    strategy_version TEXT NOT NULL DEFAULT 'trend_atr.v1',
                    selector_reason TEXT,
                    symbol TEXT NOT NULL,
                    side TEXT NOT NULL CHECK(side IN ('Buy', 'Sell')),
                    status TEXT NOT NULL,
                    dry_run INTEGER NOT NULL DEFAULT 0,
                    entry_order_link_id TEXT,
                    entry_order_id TEXT,
                    planned_qty TEXT,
                    planned_entry_price TEXT,
                    planned_take_profit TEXT,
                    planned_stop_loss TEXT,
                    planned_leverage TEXT,
                    planned_risk_usd TEXT,
                    planned_reward_usd TEXT,
                    planned_cost_usd TEXT,
                    planned_net_rr TEXT,
                    actual_entry_qty TEXT,
                    actual_entry_price TEXT,
                    opened_at_ms INTEGER,
                    closed_at_ms INTEGER,
                    decision_json TEXT,
                    snapshot_json TEXT,
                    sizing_context_json TEXT,
                    last_error TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY(account_scope, candidate_id)
                );

                CREATE TABLE IF NOT EXISTS closed_trade_records (
                    account_scope TEXT NOT NULL,
                    record_id TEXT NOT NULL,
                    order_id TEXT,
                    candidate_id TEXT,
                    symbol TEXT NOT NULL,
                    close_side TEXT,
                    position_side TEXT,
                    order_type TEXT,
                    exec_type TEXT,
                    qty TEXT,
                    closed_size TEXT,
                    order_price TEXT,
                    avg_entry_price TEXT,
                    avg_exit_price TEXT,
                    cum_entry_value TEXT,
                    cum_exit_value TEXT,
                    closed_pnl TEXT NOT NULL,
                    open_fee TEXT,
                    close_fee TEXT,
                    fee_data_complete INTEGER NOT NULL DEFAULT 0,
                    leverage TEXT,
                    fill_count INTEGER,
                    created_time_ms INTEGER NOT NULL,
                    updated_time_ms INTEGER NOT NULL,
                    raw_json TEXT NOT NULL,
                    synced_at TEXT NOT NULL,
                    PRIMARY KEY(account_scope, record_id),
                    FOREIGN KEY(account_scope, candidate_id)
                        REFERENCES trade_setups(account_scope, candidate_id)
                        ON DELETE NO ACTION
                );

                CREATE TABLE IF NOT EXISTS trade_sync_state (
                    account_scope TEXT NOT NULL,
                    source TEXT NOT NULL,
                    coverage_start_ms INTEGER NOT NULL,
                    coverage_end_ms INTEGER NOT NULL,
                    last_success_ms INTEGER NOT NULL,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY(account_scope, source)
                );

                CREATE TABLE IF NOT EXISTS equity_snapshots (
                    account_scope TEXT NOT NULL,
                    bucket_time_ms INTEGER NOT NULL,
                    captured_at_ms INTEGER NOT NULL,
                    equity_usd TEXT NOT NULL,
                    wallet_balance_usd TEXT,
                    available_usd TEXT,
                    unrealized_pnl_usd TEXT,
                    source TEXT NOT NULL,
                    PRIMARY KEY(account_scope, bucket_time_ms)
                );

                CREATE INDEX IF NOT EXISTS idx_alerts_active
                    ON alerts(is_enabled, kind, symbol, timeframe);
                CREATE INDEX IF NOT EXISTS idx_alerts_chat ON alerts(chat_id, is_enabled);
                CREATE INDEX IF NOT EXISTS idx_activity_chat_time
                    ON activity_log(chat_id, created_at DESC);
                CREATE INDEX IF NOT EXISTS idx_execution_signals_time
                    ON execution_signals(created_at);
                CREATE INDEX IF NOT EXISTS idx_daily_risk_time
                    ON daily_risk_state(updated_at);
                CREATE INDEX IF NOT EXISTS idx_outbox_pending
                    ON notification_outbox(status, id);
                CREATE UNIQUE INDEX IF NOT EXISTS idx_trade_setup_entry_link
                    ON trade_setups(account_scope, entry_order_link_id)
                    WHERE entry_order_link_id IS NOT NULL;
                CREATE INDEX IF NOT EXISTS idx_trade_setup_open
                    ON trade_setups(account_scope, symbol, status, opened_at_ms DESC);
                CREATE INDEX IF NOT EXISTS idx_closed_trade_scope_time
                    ON closed_trade_records(account_scope, updated_time_ms DESC);
                CREATE INDEX IF NOT EXISTS idx_closed_trade_candidate
                    ON closed_trade_records(account_scope, candidate_id, updated_time_ms);
                CREATE INDEX IF NOT EXISTS idx_equity_scope_time
                    ON equity_snapshots(account_scope, captured_at_ms);
                """
            )
            # An early unreleased revision used the same table name for
            # unverified fallback aliases.  Only the canonical schema below can
            # be trusted as a UID-verified offline cache; incompatible rows are
            # securely discarded instead of being silently promoted.
            alias_columns = tuple(
                str(row["name"])
                for row in conn.execute(
                    "PRAGMA table_info(trade_account_aliases)"
                )
            )
            canonical_alias_columns = (
                "api_fingerprint",
                "account_scope",
                "verified_uid",
                "verified_at",
            )
            if alias_columns != canonical_alias_columns:
                conn.execute("PRAGMA secure_delete = ON")
                conn.execute("DROP TABLE trade_account_aliases")
                conn.execute(
                    """
                    CREATE TABLE trade_account_aliases (
                        api_fingerprint TEXT PRIMARY KEY,
                        account_scope TEXT NOT NULL,
                        verified_uid INTEGER NOT NULL DEFAULT 1
                            CHECK(verified_uid = 1),
                        verified_at TEXT NOT NULL
                    )
                    """
                )
            # Add retry metadata to databases created by older revisions.
            outbox_columns = {
                str(row["name"])
                for row in conn.execute("PRAGMA table_info(notification_outbox)")
            }
            for name, declaration in {
                "next_attempt_at": "TEXT",
                "last_attempt_at": "TEXT",
                "last_error": "TEXT",
                "abandoned_at": "TEXT",
            }.items():
                if name not in outbox_columns:
                    conn.execute(
                        f"ALTER TABLE notification_outbox "
                        f"ADD COLUMN {name} {declaration}"
                    )
            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_outbox_retry
                ON notification_outbox(
                    status, abandoned_at, next_attempt_at, attempts, id
                )
                """
            )
            # The bot now has a strict private-chat invariant.  Deactivate
            # legacy group rows so restart cannot edit or notify an old group.
            conn.execute(
                """
                UPDATE users
                SET is_active = 0, screen_message_id = NULL, updated_at = ?
                WHERE telegram_user_id IS NOT NULL
                  AND chat_id <> telegram_user_id
                """,
                (_utcnow(),),
            )

    def ensure_user(self, user: Any, chat_id: int, is_admin: bool = False) -> None:
        """Upsert Telegram profile fields without overwriting preferences."""
        now = _utcnow()
        first_name = getattr(user, "first_name", None) or ""
        last_name = getattr(user, "last_name", None) or ""
        with self._lock, self._connection() as conn:
            conn.execute(
                """
                INSERT INTO users (
                    chat_id, telegram_user_id, username, first_name, last_name,
                    display_name, is_admin, created_at, updated_at, last_seen_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(chat_id) DO UPDATE SET
                    telegram_user_id = excluded.telegram_user_id,
                    username = excluded.username,
                    first_name = excluded.first_name,
                    last_name = excluded.last_name,
                    display_name = COALESCE(NULLIF(users.display_name, ''), excluded.display_name),
                    is_admin = excluded.is_admin,
                    is_active = 1,
                    updated_at = excluded.updated_at,
                    last_seen_at = excluded.last_seen_at
                """,
                (
                    chat_id, getattr(user, "id", None), getattr(user, "username", None),
                    first_name, last_name, first_name, int(is_admin), now, now, now,
                ),
            )

    def get_user(self, chat_id: int) -> dict[str, Any]:
        with self._lock, self._connection() as conn:
            row = conn.execute("SELECT * FROM users WHERE chat_id = ?", (chat_id,)).fetchone()
        return dict(row) if row else {}

    def update_user_settings(self, chat_id: int, **settings: Any) -> None:
        allowed = {
            "display_name", "locale", "timezone", "notifications_enabled",
            "price_alerts_enabled", "rsi_alerts_enabled", "default_symbol",
            "default_interval", "risk_per_trade_percent", "max_total_risk_percent",
            "max_leverage", "max_order_usdt", "auto_mode_enabled", "dry_run_override",
        }
        selected = {key: value for key, value in settings.items() if key in allowed}
        if not selected:
            return
        selected["updated_at"] = _utcnow()
        assignments = ", ".join(f"{column} = ?" for column in selected)
        values = [int(value) if isinstance(value, bool) else value for value in selected.values()]
        with self._lock, self._connection() as conn:
            conn.execute(
                f"UPDATE users SET {assignments} WHERE chat_id = ?",
                (*values, chat_id),
            )

    def save_screen(self, chat_id: int, message_id: int, revision: int = 0) -> None:
        now = _utcnow()
        with self._lock, self._connection() as conn:
            conn.execute(
                """
                UPDATE users
                SET screen_message_id = ?, screen_revision = ?, updated_at = ?
                WHERE chat_id = ?
                """,
                (message_id, revision, now, chat_id),
            )

    def deactivate_chat(self, chat_id: int) -> None:
        """Drop a permanently unreachable Telegram target until it writes again."""
        with self._lock, self._connection() as conn:
            conn.execute(
                """
                UPDATE users
                SET is_active = 0, screen_message_id = NULL, updated_at = ?
                WHERE chat_id = ?
                """,
                (_utcnow(), chat_id),
            )

    def screen_targets(self) -> list[tuple[int, int, int]]:
        with self._lock, self._connection() as conn:
            rows = conn.execute(
                """
                SELECT chat_id, screen_message_id, screen_revision FROM users
                WHERE is_active = 1 AND screen_message_id IS NOT NULL
                  AND telegram_user_id = chat_id
                """
            ).fetchall()
        return [
            (
                int(row["chat_id"]),
                int(row["screen_message_id"]),
                int(row["screen_revision"]),
            )
            for row in rows
        ]

    def create_alert(
        self,
        chat_id: int,
        *,
        kind: str,
        symbol: str,
        direction: str,
        threshold: float,
        timeframe: Optional[str] = None,
        repeat_mode: str = "once",
        cooldown_seconds: int = ALERT_DEFAULT_COOLDOWN_SECONDS,
    ) -> int:
        if kind not in {"price", "rsi"}:
            raise ValueError("Недопустимый тип алерта")
        if direction not in {"above", "below"}:
            raise ValueError("Направление алерта должно быть above или below")
        if repeat_mode not in {"once", "repeat"}:
            raise ValueError("Недопустимый режим повтора")
        threshold = float(threshold)
        if (
            not math.isfinite(threshold)
            or threshold <= 0
            or (kind == "rsi" and threshold > 100)
        ):
            raise ValueError("Порог алерта вне допустимого диапазона")
        now = _utcnow()
        with self._lock, self._connection() as conn:
            cursor = conn.execute(
                """
                INSERT INTO alerts (
                    chat_id, kind, symbol, timeframe, direction, threshold,
                    repeat_mode, cooldown_seconds, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    chat_id, kind, symbol.upper(), timeframe, direction, threshold,
                    repeat_mode, cooldown_seconds, now, now,
                ),
            )
            return int(cursor.lastrowid)

    def get_alerts(self, chat_id: int, include_disabled: bool = False) -> list[dict[str, Any]]:
        clause = "" if include_disabled else "AND is_enabled = 1"
        with self._lock, self._connection() as conn:
            rows = conn.execute(
                f"""
                SELECT * FROM alerts
                WHERE chat_id = ? {clause}
                ORDER BY is_enabled DESC, created_at DESC, id DESC
                """,
                (chat_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    def delete_alert(self, chat_id: int, alert_id: int) -> bool:
        with self._lock, self._connection() as conn:
            cursor = conn.execute(
                "DELETE FROM alerts WHERE id = ? AND chat_id = ?",
                (alert_id, chat_id),
            )
            return cursor.rowcount == 1

    def get_active_alerts(self) -> list[dict[str, Any]]:
        with self._lock, self._connection() as conn:
            rows = conn.execute(
                """
                SELECT a.*, u.notifications_enabled, u.price_alerts_enabled,
                       u.rsi_alerts_enabled
                FROM alerts a JOIN users u ON u.chat_id = a.chat_id
                WHERE a.is_enabled = 1 AND u.is_active = 1
                  AND u.telegram_user_id = u.chat_id
                """
            ).fetchall()
        return [dict(row) for row in rows]

    def apply_alert_observation(
        self,
        alert_id: int,
        *,
        value: float,
        should_trigger: bool,
        notification_message: Optional[str] = None,
    ) -> bool:
        """Persist one observation atomically and report a permitted trigger."""
        now = _utcnow()
        with self._lock, self._connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            alert = conn.execute(
                "SELECT * FROM alerts WHERE id = ? AND is_enabled = 1", (alert_id,)
            ).fetchone()
            if not alert:
                conn.execute("COMMIT")
                return False

            last_triggered = alert["last_triggered_at"]
            cooldown_passed = True
            if last_triggered:
                elapsed = datetime.now(timezone.utc) - datetime.fromisoformat(last_triggered)
                cooldown_passed = elapsed.total_seconds() >= int(alert["cooldown_seconds"])
            triggered = bool(should_trigger and cooldown_passed)
            enabled = 0 if triggered and alert["repeat_mode"] == "once" else 1
            conn.execute(
                """
                UPDATE alerts
                SET last_value = ?, last_checked_at = ?, last_triggered_at = ?,
                    trigger_count = trigger_count + ?, is_enabled = ?, updated_at = ?
                WHERE id = ?
                """,
                (
                    value, now, now if triggered else last_triggered,
                    1 if triggered else 0, enabled, now, alert_id,
                ),
            )
            if triggered and notification_message:
                conn.execute(
                    """
                    INSERT INTO notification_outbox (
                        chat_id, alert_id, message, created_at
                    ) VALUES (?, ?, ?, ?)
                    """,
                    (int(alert["chat_id"]), alert_id, notification_message, now),
                )
            conn.execute("COMMIT")
            return triggered

    def log_activity(
        self,
        chat_id: Optional[int],
        event_type: str,
        message: str,
        *,
        severity: str = "info",
        symbol: Optional[str] = None,
        payload: Optional[dict[str, Any]] = None,
    ) -> int:
        with self._lock, self._connection() as conn:
            cursor = conn.execute(
                """
                INSERT INTO activity_log (
                    chat_id, event_type, severity, symbol, message, payload_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    chat_id, event_type, severity, symbol, message,
                    json.dumps(payload, ensure_ascii=False) if payload else None,
                    _utcnow(),
                ),
            )
            return int(cursor.lastrowid)

    def list_activity(self, chat_id: int, limit: int = 20) -> list[dict[str, Any]]:
        with self._lock, self._connection() as conn:
            rows = conn.execute(
                """
                SELECT event_type, severity, symbol, message, created_at
                FROM activity_log WHERE chat_id = ?
                ORDER BY id DESC LIMIT ?
                """,
                (chat_id, max(1, min(limit, 50))),
            ).fetchall()
        return [dict(row) for row in rows]

    def reserve_execution_signal(self, candidate_id: str, symbol: str) -> bool:
        """Atomically reserve one deterministic candle candidate."""
        now = _utcnow()
        with self._lock, self._connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            cursor = conn.execute(
                """
                INSERT OR IGNORE INTO execution_signals (
                    candidate_id, symbol, status, created_at, updated_at
                ) VALUES (?, ?, 'reserved', ?, ?)
                """,
                (candidate_id, symbol, now, now),
            )
            conn.execute(
                "DELETE FROM execution_signals WHERE created_at < ?",
                (
                    (
                        datetime.now(timezone.utc) - timedelta(days=14)
                    ).isoformat(timespec="seconds"),
                ),
            )
            conn.execute("COMMIT")
            return cursor.rowcount == 1

    def update_execution_signal(self, candidate_id: str, status: str) -> None:
        with self._lock, self._connection() as conn:
            conn.execute(
                """
                UPDATE execution_signals
                SET status = ?, updated_at = ?
                WHERE candidate_id = ?
                """,
                (status, _utcnow(), candidate_id),
            )

    def get_verified_trade_account_scope(
        self,
        api_fingerprint: str,
    ) -> Optional[str]:
        """Return only a scope previously verified through Bybit userID."""
        fingerprint = _hex_digest(
            api_fingerprint,
            64,
            "API fingerprint",
        )
        with self._lock, self._connection() as conn:
            row = conn.execute(
                """
                SELECT account_scope
                FROM trade_account_aliases
                WHERE api_fingerprint = ? AND verified_uid = 1
                """,
                (fingerprint,),
            ).fetchone()
        return str(row["account_scope"]) if row else None

    def save_verified_trade_account_scope(
        self,
        api_fingerprint: str,
        account_scope: str,
    ) -> None:
        """Cache a mapping only after the caller verified Bybit userID."""
        fingerprint = _hex_digest(
            api_fingerprint,
            64,
            "API fingerprint",
        )
        scope = _hex_digest(account_scope, 24, "Account scope")
        with self._lock, self._connection() as conn:
            conn.execute(
                """
                INSERT INTO trade_account_aliases (
                    api_fingerprint, account_scope, verified_uid, verified_at
                ) VALUES (?, ?, 1, ?)
                ON CONFLICT(api_fingerprint) DO UPDATE SET
                    account_scope = excluded.account_scope,
                    verified_uid = 1,
                    verified_at = excluded.verified_at
                """,
                (fingerprint, scope, _utcnow()),
            )

    def migrate_trade_account_scope(
        self,
        fallback_scope: str,
        uid_scope: str,
        *,
        verified_api_fingerprint: Optional[str] = None,
    ) -> None:
        """Atomically fold transient fallback rows into the stable UID scope.

        When a verified fingerprint is supplied, its UID mapping is committed
        in the same transaction. The fallback scope itself is never aliased.
        """
        source_scope = _hex_digest(
            fallback_scope,
            24,
            "Fallback account scope",
        )
        target_scope = _hex_digest(uid_scope, 24, "UID account scope")
        verified_fingerprint = (
            _hex_digest(
                verified_api_fingerprint,
                64,
                "API fingerprint",
            )
            if verified_api_fingerprint is not None
            else None
        )
        if source_scope == target_scope:
            if verified_fingerprint is not None:
                self.save_verified_trade_account_scope(
                    verified_fingerprint,
                    target_scope,
                )
            return

        setup_columns = (
            "candidate_id", "snapshot_id", "strategy_version",
            "selector_reason", "symbol", "side", "status", "dry_run",
            "entry_order_link_id", "entry_order_id", "planned_qty",
            "planned_entry_price", "planned_take_profit", "planned_stop_loss",
            "planned_leverage", "planned_risk_usd", "planned_reward_usd",
            "planned_cost_usd", "planned_net_rr", "actual_entry_qty",
            "actual_entry_price", "opened_at_ms", "closed_at_ms",
            "decision_json", "snapshot_json", "sizing_context_json",
            "last_error", "created_at", "updated_at",
        )
        closed_columns = (
            "record_id", "order_id", "candidate_id", "symbol", "close_side",
            "position_side", "order_type", "exec_type", "qty", "closed_size",
            "order_price", "avg_entry_price", "avg_exit_price",
            "cum_entry_value", "cum_exit_value", "closed_pnl", "open_fee",
            "close_fee", "fee_data_complete", "leverage", "fill_count",
            "created_time_ms", "updated_time_ms", "raw_json", "synced_at",
        )

        with self._lock, self._connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                setup_rows = conn.execute(
                    "SELECT * FROM trade_setups WHERE account_scope = ?",
                    (source_scope,),
                ).fetchall()
                setup_names = ", ".join(("account_scope", *setup_columns))
                setup_placeholders = ", ".join("?" for _ in range(len(setup_columns) + 1))
                for row in setup_rows:
                    conn.execute(
                        f"""
                        INSERT INTO trade_setups ({setup_names})
                        VALUES ({setup_placeholders})
                        ON CONFLICT(account_scope, candidate_id) DO UPDATE SET
                            snapshot_id = COALESCE(
                                trade_setups.snapshot_id, excluded.snapshot_id
                            ),
                            strategy_version = CASE
                                WHEN excluded.updated_at > trade_setups.updated_at
                                THEN excluded.strategy_version
                                ELSE trade_setups.strategy_version
                            END,
                            selector_reason = COALESCE(
                                trade_setups.selector_reason,
                                excluded.selector_reason
                            ),
                            status = CASE
                                WHEN excluded.updated_at > trade_setups.updated_at
                                THEN excluded.status ELSE trade_setups.status
                            END,
                            entry_order_link_id = COALESCE(
                                trade_setups.entry_order_link_id,
                                excluded.entry_order_link_id
                            ),
                            entry_order_id = COALESCE(
                                trade_setups.entry_order_id,
                                excluded.entry_order_id
                            ),
                            planned_qty = COALESCE(
                                trade_setups.planned_qty, excluded.planned_qty
                            ),
                            planned_entry_price = COALESCE(
                                trade_setups.planned_entry_price,
                                excluded.planned_entry_price
                            ),
                            planned_take_profit = COALESCE(
                                trade_setups.planned_take_profit,
                                excluded.planned_take_profit
                            ),
                            planned_stop_loss = COALESCE(
                                trade_setups.planned_stop_loss,
                                excluded.planned_stop_loss
                            ),
                            planned_leverage = COALESCE(
                                trade_setups.planned_leverage,
                                excluded.planned_leverage
                            ),
                            planned_risk_usd = COALESCE(
                                trade_setups.planned_risk_usd,
                                excluded.planned_risk_usd
                            ),
                            planned_reward_usd = COALESCE(
                                trade_setups.planned_reward_usd,
                                excluded.planned_reward_usd
                            ),
                            planned_cost_usd = COALESCE(
                                trade_setups.planned_cost_usd,
                                excluded.planned_cost_usd
                            ),
                            planned_net_rr = COALESCE(
                                trade_setups.planned_net_rr,
                                excluded.planned_net_rr
                            ),
                            actual_entry_qty = COALESCE(
                                trade_setups.actual_entry_qty,
                                excluded.actual_entry_qty
                            ),
                            actual_entry_price = COALESCE(
                                trade_setups.actual_entry_price,
                                excluded.actual_entry_price
                            ),
                            opened_at_ms = COALESCE(
                                trade_setups.opened_at_ms, excluded.opened_at_ms
                            ),
                            closed_at_ms = CASE
                                WHEN trade_setups.closed_at_ms IS NULL
                                THEN excluded.closed_at_ms
                                WHEN excluded.closed_at_ms IS NULL
                                THEN trade_setups.closed_at_ms
                                ELSE MAX(
                                    trade_setups.closed_at_ms,
                                    excluded.closed_at_ms
                                )
                            END,
                            decision_json = COALESCE(
                                trade_setups.decision_json,
                                excluded.decision_json
                            ),
                            snapshot_json = COALESCE(
                                trade_setups.snapshot_json,
                                excluded.snapshot_json
                            ),
                            sizing_context_json = COALESCE(
                                trade_setups.sizing_context_json,
                                excluded.sizing_context_json
                            ),
                            last_error = CASE
                                WHEN excluded.updated_at > trade_setups.updated_at
                                THEN excluded.last_error
                                ELSE trade_setups.last_error
                            END,
                            created_at = MIN(
                                trade_setups.created_at, excluded.created_at
                            ),
                            updated_at = MAX(
                                trade_setups.updated_at, excluded.updated_at
                            )
                        """,
                        (target_scope, *(row[name] for name in setup_columns)),
                    )

                closed_rows = conn.execute(
                    "SELECT * FROM closed_trade_records WHERE account_scope = ?",
                    (source_scope,),
                ).fetchall()
                closed_names = ", ".join(("account_scope", *closed_columns))
                closed_placeholders = ", ".join(
                    "?" for _ in range(len(closed_columns) + 1)
                )
                for row in closed_rows:
                    conn.execute(
                        f"""
                        INSERT INTO closed_trade_records ({closed_names})
                        VALUES ({closed_placeholders})
                        ON CONFLICT(account_scope, record_id) DO UPDATE SET
                            candidate_id = COALESCE(
                                closed_trade_records.candidate_id,
                                excluded.candidate_id
                            ),
                            order_id = CASE
                                WHEN excluded.updated_time_ms >=
                                     closed_trade_records.updated_time_ms
                                THEN COALESCE(
                                    excluded.order_id,
                                    closed_trade_records.order_id
                                )
                                ELSE closed_trade_records.order_id
                            END,
                            symbol = CASE
                                WHEN excluded.updated_time_ms >=
                                     closed_trade_records.updated_time_ms
                                THEN excluded.symbol
                                ELSE closed_trade_records.symbol
                            END,
                            close_side = CASE
                                WHEN excluded.updated_time_ms >=
                                     closed_trade_records.updated_time_ms
                                THEN COALESCE(
                                    excluded.close_side,
                                    closed_trade_records.close_side
                                )
                                ELSE closed_trade_records.close_side
                            END,
                            position_side = CASE
                                WHEN excluded.updated_time_ms >=
                                     closed_trade_records.updated_time_ms
                                THEN COALESCE(
                                    excluded.position_side,
                                    closed_trade_records.position_side
                                )
                                ELSE closed_trade_records.position_side
                            END,
                            order_type = CASE
                                WHEN excluded.updated_time_ms >=
                                     closed_trade_records.updated_time_ms
                                THEN COALESCE(
                                    excluded.order_type,
                                    closed_trade_records.order_type
                                )
                                ELSE closed_trade_records.order_type
                            END,
                            exec_type = CASE
                                WHEN excluded.updated_time_ms >=
                                     closed_trade_records.updated_time_ms
                                THEN COALESCE(
                                    excluded.exec_type,
                                    closed_trade_records.exec_type
                                )
                                ELSE closed_trade_records.exec_type
                            END,
                            qty = CASE
                                WHEN excluded.updated_time_ms >=
                                     closed_trade_records.updated_time_ms
                                THEN COALESCE(excluded.qty, closed_trade_records.qty)
                                ELSE closed_trade_records.qty
                            END,
                            closed_size = CASE
                                WHEN excluded.updated_time_ms >=
                                     closed_trade_records.updated_time_ms
                                THEN COALESCE(
                                    excluded.closed_size,
                                    closed_trade_records.closed_size
                                )
                                ELSE closed_trade_records.closed_size
                            END,
                            order_price = CASE
                                WHEN excluded.updated_time_ms >=
                                     closed_trade_records.updated_time_ms
                                THEN COALESCE(
                                    excluded.order_price,
                                    closed_trade_records.order_price
                                )
                                ELSE closed_trade_records.order_price
                            END,
                            avg_entry_price = CASE
                                WHEN excluded.updated_time_ms >=
                                     closed_trade_records.updated_time_ms
                                THEN COALESCE(
                                    excluded.avg_entry_price,
                                    closed_trade_records.avg_entry_price
                                )
                                ELSE closed_trade_records.avg_entry_price
                            END,
                            avg_exit_price = CASE
                                WHEN excluded.updated_time_ms >=
                                     closed_trade_records.updated_time_ms
                                THEN COALESCE(
                                    excluded.avg_exit_price,
                                    closed_trade_records.avg_exit_price
                                )
                                ELSE closed_trade_records.avg_exit_price
                            END,
                            cum_entry_value = CASE
                                WHEN excluded.updated_time_ms >=
                                     closed_trade_records.updated_time_ms
                                THEN COALESCE(
                                    excluded.cum_entry_value,
                                    closed_trade_records.cum_entry_value
                                )
                                ELSE closed_trade_records.cum_entry_value
                            END,
                            cum_exit_value = CASE
                                WHEN excluded.updated_time_ms >=
                                     closed_trade_records.updated_time_ms
                                THEN COALESCE(
                                    excluded.cum_exit_value,
                                    closed_trade_records.cum_exit_value
                                )
                                ELSE closed_trade_records.cum_exit_value
                            END,
                            closed_pnl = CASE
                                WHEN excluded.updated_time_ms >=
                                     closed_trade_records.updated_time_ms
                                THEN excluded.closed_pnl
                                ELSE closed_trade_records.closed_pnl
                            END,
                            open_fee = CASE
                                WHEN excluded.updated_time_ms >=
                                     closed_trade_records.updated_time_ms
                                THEN COALESCE(
                                    excluded.open_fee,
                                    closed_trade_records.open_fee
                                )
                                ELSE closed_trade_records.open_fee
                            END,
                            close_fee = CASE
                                WHEN excluded.updated_time_ms >=
                                     closed_trade_records.updated_time_ms
                                THEN COALESCE(
                                    excluded.close_fee,
                                    closed_trade_records.close_fee
                                )
                                ELSE closed_trade_records.close_fee
                            END,
                            fee_data_complete = MAX(
                                closed_trade_records.fee_data_complete,
                                excluded.fee_data_complete
                            ),
                            leverage = CASE
                                WHEN excluded.updated_time_ms >=
                                     closed_trade_records.updated_time_ms
                                THEN COALESCE(
                                    excluded.leverage,
                                    closed_trade_records.leverage
                                )
                                ELSE closed_trade_records.leverage
                            END,
                            fill_count = CASE
                                WHEN excluded.updated_time_ms >=
                                     closed_trade_records.updated_time_ms
                                THEN COALESCE(
                                    excluded.fill_count,
                                    closed_trade_records.fill_count
                                )
                                ELSE closed_trade_records.fill_count
                            END,
                            created_time_ms = MIN(
                                closed_trade_records.created_time_ms,
                                excluded.created_time_ms
                            ),
                            updated_time_ms = MAX(
                                closed_trade_records.updated_time_ms,
                                excluded.updated_time_ms
                            ),
                            raw_json = CASE
                                WHEN excluded.updated_time_ms >=
                                     closed_trade_records.updated_time_ms
                                THEN excluded.raw_json
                                ELSE closed_trade_records.raw_json
                            END,
                            synced_at = MAX(
                                closed_trade_records.synced_at,
                                excluded.synced_at
                            )
                        """,
                        (target_scope, *(row[name] for name in closed_columns)),
                    )

                sync_rows = conn.execute(
                    "SELECT * FROM trade_sync_state WHERE account_scope = ?",
                    (source_scope,),
                ).fetchall()
                for row in sync_rows:
                    conn.execute(
                        """
                        INSERT INTO trade_sync_state (
                            account_scope, source, coverage_start_ms,
                            coverage_end_ms, last_success_ms, updated_at
                        ) VALUES (?, ?, ?, ?, ?, ?)
                        ON CONFLICT(account_scope, source) DO UPDATE SET
                            coverage_start_ms = MIN(
                                trade_sync_state.coverage_start_ms,
                                excluded.coverage_start_ms
                            ),
                            coverage_end_ms = MAX(
                                trade_sync_state.coverage_end_ms,
                                excluded.coverage_end_ms
                            ),
                            last_success_ms = MAX(
                                trade_sync_state.last_success_ms,
                                excluded.last_success_ms
                            ),
                            updated_at = MAX(
                                trade_sync_state.updated_at,
                                excluded.updated_at
                            )
                        """,
                        (
                            target_scope,
                            row["source"],
                            row["coverage_start_ms"],
                            row["coverage_end_ms"],
                            row["last_success_ms"],
                            row["updated_at"],
                        ),
                    )

                equity_rows = conn.execute(
                    "SELECT * FROM equity_snapshots WHERE account_scope = ?",
                    (source_scope,),
                ).fetchall()
                for row in equity_rows:
                    conn.execute(
                        """
                        INSERT INTO equity_snapshots (
                            account_scope, bucket_time_ms, captured_at_ms,
                            equity_usd, wallet_balance_usd, available_usd,
                            unrealized_pnl_usd, source
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                        ON CONFLICT(account_scope, bucket_time_ms) DO UPDATE SET
                            captured_at_ms = CASE
                                WHEN excluded.captured_at_ms >=
                                     equity_snapshots.captured_at_ms
                                THEN excluded.captured_at_ms
                                ELSE equity_snapshots.captured_at_ms
                            END,
                            equity_usd = CASE
                                WHEN excluded.captured_at_ms >=
                                     equity_snapshots.captured_at_ms
                                THEN excluded.equity_usd
                                ELSE equity_snapshots.equity_usd
                            END,
                            wallet_balance_usd = COALESCE(
                                equity_snapshots.wallet_balance_usd,
                                excluded.wallet_balance_usd
                            ),
                            available_usd = COALESCE(
                                equity_snapshots.available_usd,
                                excluded.available_usd
                            ),
                            unrealized_pnl_usd = COALESCE(
                                equity_snapshots.unrealized_pnl_usd,
                                excluded.unrealized_pnl_usd
                            ),
                            source = CASE
                                WHEN excluded.captured_at_ms >=
                                     equity_snapshots.captured_at_ms
                                THEN excluded.source
                                ELSE equity_snapshots.source
                            END
                        """,
                        (
                            target_scope,
                            row["bucket_time_ms"],
                            row["captured_at_ms"],
                            row["equity_usd"],
                            row["wallet_balance_usd"],
                            row["available_usd"],
                            row["unrealized_pnl_usd"],
                            row["source"],
                        ),
                    )

                conn.execute(
                    "DELETE FROM closed_trade_records WHERE account_scope = ?",
                    (source_scope,),
                )
                conn.execute(
                    "DELETE FROM trade_setups WHERE account_scope = ?",
                    (source_scope,),
                )
                conn.execute(
                    "DELETE FROM trade_sync_state WHERE account_scope = ?",
                    (source_scope,),
                )
                conn.execute(
                    "DELETE FROM equity_snapshots WHERE account_scope = ?",
                    (source_scope,),
                )
                if verified_fingerprint is not None:
                    conn.execute(
                        """
                        INSERT INTO trade_account_aliases (
                            api_fingerprint, account_scope, verified_uid,
                            verified_at
                        ) VALUES (?, ?, 1, ?)
                        ON CONFLICT(api_fingerprint) DO UPDATE SET
                            account_scope = excluded.account_scope,
                            verified_uid = 1,
                            verified_at = excluded.verified_at
                        """,
                        (
                            verified_fingerprint,
                            target_scope,
                            _utcnow(),
                        ),
                    )
                conn.execute("COMMIT")
            except Exception:
                conn.execute("ROLLBACK")
                raise

    def upsert_trade_setup(
        self,
        *,
        account_scope: str,
        candidate_id: str,
        snapshot_id: Optional[str],
        symbol: str,
        side: str,
        status: str,
        dry_run: bool,
        entry_order_link_id: Optional[str] = None,
        strategy_version: str = "trend_atr.v1",
        selector_reason: Optional[str] = None,
        plan: Optional[dict[str, Any]] = None,
        decision: Optional[dict[str, Any]] = None,
        snapshot: Optional[dict[str, Any]] = None,
        sizing_context: Optional[dict[str, Any]] = None,
    ) -> None:
        """Durably store a bot setup before a live entry can be submitted."""
        if not account_scope or not candidate_id or not symbol:
            raise ValueError("Trade setup requires account_scope, candidate_id and symbol")
        if side not in {"Buy", "Sell"}:
            raise ValueError("Trade setup side must be Buy or Sell")
        now = _utcnow()
        plan = dict(plan or {})

        def value(name: str) -> Optional[str]:
            raw = plan.get(name)
            return None if raw is None else str(raw)

        with self._lock, self._connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            unresolved = conn.execute(
                """
                SELECT candidate_id
                FROM trade_setups
                WHERE account_scope = ? AND symbol = ? AND dry_run = ?
                  AND candidate_id <> ?
                  AND status IN (
                      'planned', 'entry_submitted', 'entry_filled',
                      'open', 'closing', 'reconcile_required'
                  )
                LIMIT 1
                """,
                (
                    account_scope,
                    symbol.upper(),
                    int(bool(dry_run)),
                    candidate_id,
                ),
            ).fetchone()
            if unresolved:
                conn.execute("ROLLBACK")
                raise ValueError(
                    f"Unresolved trade setup already exists for {symbol.upper()}: "
                    f"{unresolved['candidate_id']}"
                )
            conn.execute(
                """
                INSERT INTO trade_setups (
                    account_scope, candidate_id, snapshot_id, strategy_version,
                    selector_reason, symbol, side, status, dry_run,
                    entry_order_link_id, planned_qty, planned_entry_price,
                    planned_take_profit, planned_stop_loss, planned_leverage,
                    planned_risk_usd, planned_reward_usd, planned_cost_usd,
                    planned_net_rr, decision_json, snapshot_json,
                    sizing_context_json, created_at, updated_at
                ) VALUES (
                    ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                    ?, ?, ?, ?, ?
                )
                ON CONFLICT(account_scope, candidate_id) DO UPDATE SET
                    snapshot_id = COALESCE(excluded.snapshot_id, trade_setups.snapshot_id),
                    strategy_version = excluded.strategy_version,
                    selector_reason = COALESCE(
                        excluded.selector_reason, trade_setups.selector_reason
                    ),
                    symbol = excluded.symbol,
                    side = excluded.side,
                    status = CASE
                        WHEN trade_setups.status IN (
                            'entry_filled', 'open', 'closing', 'closed',
                            'reconcile_required', 'previewed', 'not_filled',
                            'failed'
                        )
                         AND excluded.status IN ('planned', 'entry_submitted')
                        THEN trade_setups.status
                        ELSE excluded.status
                    END,
                    dry_run = excluded.dry_run,
                    entry_order_link_id = COALESCE(
                        excluded.entry_order_link_id,
                        trade_setups.entry_order_link_id
                    ),
                    planned_qty = COALESCE(
                        excluded.planned_qty, trade_setups.planned_qty
                    ),
                    planned_entry_price = COALESCE(
                        excluded.planned_entry_price,
                        trade_setups.planned_entry_price
                    ),
                    planned_take_profit = COALESCE(
                        excluded.planned_take_profit,
                        trade_setups.planned_take_profit
                    ),
                    planned_stop_loss = COALESCE(
                        excluded.planned_stop_loss,
                        trade_setups.planned_stop_loss
                    ),
                    planned_leverage = COALESCE(
                        excluded.planned_leverage,
                        trade_setups.planned_leverage
                    ),
                    planned_risk_usd = COALESCE(
                        excluded.planned_risk_usd,
                        trade_setups.planned_risk_usd
                    ),
                    planned_reward_usd = COALESCE(
                        excluded.planned_reward_usd,
                        trade_setups.planned_reward_usd
                    ),
                    planned_cost_usd = COALESCE(
                        excluded.planned_cost_usd,
                        trade_setups.planned_cost_usd
                    ),
                    planned_net_rr = COALESCE(
                        excluded.planned_net_rr,
                        trade_setups.planned_net_rr
                    ),
                    decision_json = COALESCE(
                        excluded.decision_json, trade_setups.decision_json
                    ),
                    snapshot_json = COALESCE(
                        excluded.snapshot_json, trade_setups.snapshot_json
                    ),
                    sizing_context_json = COALESCE(
                        excluded.sizing_context_json,
                        trade_setups.sizing_context_json
                    ),
                    updated_at = excluded.updated_at
                """,
                (
                    account_scope,
                    candidate_id,
                    snapshot_id,
                    strategy_version,
                    selector_reason,
                    symbol.upper(),
                    side,
                    status,
                    int(bool(dry_run)),
                    entry_order_link_id,
                    value("quantity"),
                    value("entry_price"),
                    value("take_profit"),
                    value("stop_loss"),
                    value("leverage"),
                    value("risk_usd"),
                    value("reward_usd"),
                    value("estimated_cost_usd"),
                    value("net_risk_reward"),
                    _json_payload(decision),
                    _json_payload(snapshot),
                    _json_payload(sizing_context),
                    now,
                    now,
                ),
            )
            conn.execute("COMMIT")

    def update_trade_setup(
        self,
        account_scope: str,
        candidate_id: str,
        **changes: Any,
    ) -> None:
        allowed = {
            "status",
            "entry_order_id",
            "entry_order_link_id",
            "actual_entry_qty",
            "actual_entry_price",
            "opened_at_ms",
            "closed_at_ms",
            "last_error",
        }
        selected = {
            key: value
            for key, value in changes.items()
            if key in allowed and value is not None
        }
        if not selected:
            return
        selected["updated_at"] = _utcnow()
        assignments = ", ".join(f"{column} = ?" for column in selected)
        with self._lock, self._connection() as conn:
            conn.execute(
                f"""
                UPDATE trade_setups
                SET {assignments}
                WHERE account_scope = ? AND candidate_id = ?
                """,
                (*selected.values(), account_scope, candidate_id),
            )

    def _matching_trade_candidate(
        self,
        conn: sqlite3.Connection,
        *,
        account_scope: str,
        symbol: str,
        position_side: Optional[str],
        avg_entry_price: Optional[str],
        closed_size: Optional[str],
        closed_at_ms: int,
    ) -> Optional[str]:
        if position_side not in {"Buy", "Sell"}:
            return None
        rows = conn.execute(
            """
            SELECT candidate_id, actual_entry_price, planned_entry_price,
                   actual_entry_qty, planned_qty, opened_at_ms
            FROM trade_setups
            WHERE account_scope = ? AND symbol = ? AND side = ?
              AND dry_run = 0
              AND status IN (
                  'entry_filled', 'open', 'closing', 'reconcile_required'
              )
              AND COALESCE(opened_at_ms, 0) <= ?
            ORDER BY COALESCE(opened_at_ms, 0) DESC
            LIMIT 8
            """,
            (account_scope, symbol, position_side, closed_at_ms),
        ).fetchall()
        if not rows:
            return None
        try:
            observed = Decimal(str(avg_entry_price))
        except (InvalidOperation, TypeError, ValueError):
            observed = Decimal("0")
        try:
            incoming_size = Decimal(str(closed_size))
        except (InvalidOperation, TypeError, ValueError):
            incoming_size = Decimal("0")
        matching: list[sqlite3.Row] = []
        for row in rows:
            reference_raw = row["actual_entry_price"] or row["planned_entry_price"]
            try:
                reference = Decimal(str(reference_raw))
                expected_size = Decimal(
                    str(row["actual_entry_qty"] or row["planned_qty"])
                )
            except (InvalidOperation, TypeError, ValueError):
                continue
            if (
                reference <= 0
                or observed <= 0
                or expected_size <= 0
                or incoming_size <= 0
            ):
                continue
            if abs(reference - observed) / reference <= Decimal("0.005"):
                linked_rows = conn.execute(
                    """
                    SELECT closed_size
                    FROM closed_trade_records
                    WHERE account_scope = ? AND candidate_id = ?
                    """,
                    (account_scope, row["candidate_id"]),
                ).fetchall()
                try:
                    already_closed = sum(
                        (
                            Decimal(str(linked["closed_size"]))
                            for linked in linked_rows
                            if linked["closed_size"] not in (None, "")
                        ),
                        Decimal("0"),
                    )
                except (InvalidOperation, TypeError, ValueError):
                    continue
                tolerance = max(
                    expected_size * Decimal("0.000001"),
                    Decimal("0.000000000001"),
                )
                remaining = expected_size - already_closed
                if remaining <= tolerance:
                    continue
                if incoming_size > remaining + tolerance:
                    continue
                matching.append(row)
        if len(matching) != 1:
            return None
        # Do not guess when stale state or external account activity leaves
        # multiple price-compatible lifecycles.
        return str(matching[0]["candidate_id"])

    def upsert_closed_trade_record(
        self,
        account_scope: str,
        record: dict[str, Any],
    ) -> bool:
        """Idempotently persist one normalized Bybit Closed PnL record."""
        required = {
            "record_id",
            "symbol",
            "closed_pnl",
            "created_time_ms",
            "updated_time_ms",
            "raw_json",
        }
        if any(record.get(name) is None for name in required):
            raise ValueError("Normalized closed trade record is incomplete")
        now = _utcnow()
        with self._lock, self._connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            existing = conn.execute(
                """
                SELECT candidate_id
                FROM closed_trade_records
                WHERE account_scope = ? AND record_id = ?
                """,
                (account_scope, record["record_id"]),
            ).fetchone()
            candidate_id = (
                str(existing["candidate_id"])
                if existing and existing["candidate_id"]
                else self._matching_trade_candidate(
                    conn,
                    account_scope=account_scope,
                    symbol=str(record["symbol"]),
                    position_side=record.get("position_side"),
                    avg_entry_price=record.get("avg_entry_price"),
                    closed_size=record.get("closed_size"),
                    closed_at_ms=int(record["updated_time_ms"]),
                )
            )
            conn.execute(
                """
                INSERT INTO closed_trade_records (
                    account_scope, record_id, order_id, candidate_id, symbol,
                    close_side, position_side, order_type, exec_type, qty,
                    closed_size, order_price, avg_entry_price, avg_exit_price,
                    cum_entry_value, cum_exit_value, closed_pnl, open_fee,
                    close_fee, fee_data_complete, leverage, fill_count,
                    created_time_ms, updated_time_ms, raw_json, synced_at
                ) VALUES (
                    ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                    ?, ?, ?, ?, ?, ?, ?
                )
                ON CONFLICT(account_scope, record_id) DO UPDATE SET
                    order_id = COALESCE(
                        excluded.order_id,
                        closed_trade_records.order_id
                    ),
                    candidate_id = COALESCE(
                        closed_trade_records.candidate_id,
                        excluded.candidate_id
                    ),
                    symbol = excluded.symbol,
                    close_side = COALESCE(
                        excluded.close_side,
                        closed_trade_records.close_side
                    ),
                    position_side = COALESCE(
                        excluded.position_side,
                        closed_trade_records.position_side
                    ),
                    order_type = COALESCE(
                        excluded.order_type,
                        closed_trade_records.order_type
                    ),
                    exec_type = COALESCE(
                        excluded.exec_type,
                        closed_trade_records.exec_type
                    ),
                    qty = COALESCE(excluded.qty, closed_trade_records.qty),
                    closed_size = COALESCE(
                        excluded.closed_size,
                        closed_trade_records.closed_size
                    ),
                    order_price = COALESCE(
                        excluded.order_price,
                        closed_trade_records.order_price
                    ),
                    avg_entry_price = COALESCE(
                        excluded.avg_entry_price,
                        closed_trade_records.avg_entry_price
                    ),
                    avg_exit_price = COALESCE(
                        excluded.avg_exit_price,
                        closed_trade_records.avg_exit_price
                    ),
                    cum_entry_value = COALESCE(
                        excluded.cum_entry_value,
                        closed_trade_records.cum_entry_value
                    ),
                    cum_exit_value = COALESCE(
                        excluded.cum_exit_value,
                        closed_trade_records.cum_exit_value
                    ),
                    closed_pnl = excluded.closed_pnl,
                    open_fee = COALESCE(
                        excluded.open_fee,
                        closed_trade_records.open_fee
                    ),
                    close_fee = COALESCE(
                        excluded.close_fee,
                        closed_trade_records.close_fee
                    ),
                    fee_data_complete = MAX(
                        closed_trade_records.fee_data_complete,
                        excluded.fee_data_complete
                    ),
                    leverage = COALESCE(
                        excluded.leverage,
                        closed_trade_records.leverage
                    ),
                    fill_count = COALESCE(
                        excluded.fill_count,
                        closed_trade_records.fill_count
                    ),
                    created_time_ms = MIN(
                        closed_trade_records.created_time_ms,
                        excluded.created_time_ms
                    ),
                    updated_time_ms = excluded.updated_time_ms,
                    raw_json = excluded.raw_json,
                    synced_at = excluded.synced_at
                WHERE excluded.updated_time_ms >=
                      closed_trade_records.updated_time_ms
                """,
                (
                    account_scope,
                    record["record_id"],
                    record.get("order_id"),
                    candidate_id,
                    str(record["symbol"]).upper(),
                    record.get("close_side"),
                    record.get("position_side"),
                    record.get("order_type"),
                    record.get("exec_type"),
                    record.get("qty"),
                    record.get("closed_size"),
                    record.get("order_price"),
                    record.get("avg_entry_price"),
                    record.get("avg_exit_price"),
                    record.get("cum_entry_value"),
                    record.get("cum_exit_value"),
                    record["closed_pnl"],
                    record.get("open_fee"),
                    record.get("close_fee"),
                    int(bool(record.get("fee_data_complete"))),
                    record.get("leverage"),
                    record.get("fill_count"),
                    int(record["created_time_ms"]),
                    int(record["updated_time_ms"]),
                    record["raw_json"],
                    now,
                ),
            )
            persisted = conn.execute(
                """
                SELECT candidate_id
                FROM closed_trade_records
                WHERE account_scope = ? AND record_id = ?
                """,
                (account_scope, record["record_id"]),
            ).fetchone()
            persisted_candidate = (
                str(persisted["candidate_id"])
                if persisted and persisted["candidate_id"]
                else None
            )
            if persisted_candidate:
                setup = conn.execute(
                    """
                    SELECT actual_entry_qty, planned_qty
                    FROM trade_setups
                    WHERE account_scope = ? AND candidate_id = ?
                    """,
                    (account_scope, persisted_candidate),
                ).fetchone()
                size_rows = conn.execute(
                    """
                    SELECT closed_size, updated_time_ms
                    FROM closed_trade_records
                    WHERE account_scope = ? AND candidate_id = ?
                    """,
                    (account_scope, persisted_candidate),
                ).fetchall()
                try:
                    expected_size = Decimal(
                        str(
                            (
                                setup["actual_entry_qty"]
                                or setup["planned_qty"]
                            )
                            if setup
                            else ""
                        )
                    )
                    closed_size = sum(
                        (
                            Decimal(str(row["closed_size"]))
                            for row in size_rows
                            if row["closed_size"] not in (None, "")
                        ),
                        Decimal("0"),
                    )
                except (InvalidOperation, TypeError, ValueError):
                    expected_size = Decimal("0")
                    closed_size = Decimal("0")
                tolerance = max(
                    expected_size * Decimal("0.000001"),
                    Decimal("0.000000000001"),
                )
                if (
                    expected_size > 0
                    and closed_size + tolerance >= expected_size
                ):
                    closed_at_ms = max(
                        int(row["updated_time_ms"]) for row in size_rows
                    )
                    conn.execute(
                        """
                        UPDATE trade_setups
                        SET status = 'closed',
                            closed_at_ms = CASE
                                WHEN closed_at_ms IS NULL OR closed_at_ms < ?
                                THEN ? ELSE closed_at_ms
                            END,
                            updated_at = ?
                        WHERE account_scope = ? AND candidate_id = ?
                        """,
                        (
                            closed_at_ms,
                            closed_at_ms,
                            now,
                            account_scope,
                            persisted_candidate,
                        ),
                    )
            conn.execute("COMMIT")
        return existing is None

    def list_closed_trade_records(
        self,
        account_scope: str,
        *,
        since_ms: int,
        bot_only: bool = False,
    ) -> list[dict[str, Any]]:
        bot_clause = "AND c.candidate_id IS NOT NULL" if bot_only else ""
        with self._lock, self._connection() as conn:
            rows = conn.execute(
                f"""
                SELECT c.*,
                       s.side AS setup_side,
                       s.planned_risk_usd,
                       s.planned_reward_usd,
                       s.planned_entry_price,
                       s.planned_take_profit,
                       s.planned_stop_loss,
                       s.opened_at_ms AS setup_opened_at_ms
                FROM closed_trade_records c
                LEFT JOIN trade_setups s
                  ON s.account_scope = c.account_scope
                 AND s.candidate_id = c.candidate_id
                WHERE c.account_scope = ?
                  AND c.updated_time_ms >= ?
                  {bot_clause}
                ORDER BY c.updated_time_ms ASC,
                         c.created_time_ms ASC,
                         c.record_id ASC
                """,
                (account_scope, int(since_ms)),
            ).fetchall()
        return [dict(row) for row in rows]

    def get_trade_sync_state(
        self,
        account_scope: str,
        source: str = "closed_pnl",
    ) -> dict[str, Any]:
        with self._lock, self._connection() as conn:
            row = conn.execute(
                """
                SELECT *
                FROM trade_sync_state
                WHERE account_scope = ? AND source = ?
                """,
                (account_scope, source),
            ).fetchone()
        return dict(row) if row else {}

    def update_trade_sync_state(
        self,
        account_scope: str,
        *,
        coverage_start_ms: int,
        coverage_end_ms: int,
        last_success_ms: int,
        source: str = "closed_pnl",
    ) -> None:
        with self._lock, self._connection() as conn:
            conn.execute(
                """
                INSERT INTO trade_sync_state (
                    account_scope, source, coverage_start_ms, coverage_end_ms,
                    last_success_ms, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(account_scope, source) DO UPDATE SET
                    coverage_start_ms = MIN(
                        trade_sync_state.coverage_start_ms,
                        excluded.coverage_start_ms
                    ),
                    coverage_end_ms = MAX(
                        trade_sync_state.coverage_end_ms,
                        excluded.coverage_end_ms
                    ),
                    last_success_ms = MAX(
                        trade_sync_state.last_success_ms,
                        excluded.last_success_ms
                    ),
                    updated_at = excluded.updated_at
                """,
                (
                    account_scope,
                    source,
                    int(coverage_start_ms),
                    int(coverage_end_ms),
                    int(last_success_ms),
                    _utcnow(),
                ),
            )

    def record_equity_snapshot(
        self,
        account_scope: str,
        *,
        captured_at_ms: int,
        equity_usd: Any,
        wallet_balance_usd: Any = None,
        available_usd: Any = None,
        unrealized_pnl_usd: Any = None,
        source: str = "runtime",
        bucket_seconds: int = 3_600,
    ) -> None:
        scope = str(account_scope).strip()
        if not scope:
            raise ValueError("Equity snapshot requires account_scope")
        captured = int(captured_at_ms)
        if captured <= 0:
            raise ValueError("Equity snapshot timestamp must be positive")
        bucket_ms = max(60, int(bucket_seconds)) * 1_000
        bucket_time = captured // bucket_ms * bucket_ms

        def text(value: Any, *, required: bool = False) -> Optional[str]:
            if value is None or value == "":
                if required:
                    raise ValueError("Equity snapshot requires equity_usd")
                return None
            try:
                number = Decimal(str(value))
            except (InvalidOperation, TypeError, ValueError) as error:
                raise ValueError("Equity snapshot contains invalid decimal") from error
            if not number.is_finite():
                raise ValueError("Equity snapshot contains non-finite decimal")
            if required and number <= 0:
                raise ValueError("Equity snapshot equity_usd must be positive")
            return format(number, "f")

        equity_text = text(equity_usd, required=True)
        wallet_text = text(wallet_balance_usd)
        available_text = text(available_usd)
        unrealized_text = text(unrealized_pnl_usd)

        with self._lock, self._connection() as conn:
            conn.execute(
                """
                INSERT INTO equity_snapshots (
                    account_scope, bucket_time_ms, captured_at_ms, equity_usd,
                    wallet_balance_usd, available_usd, unrealized_pnl_usd,
                    source
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(account_scope, bucket_time_ms) DO UPDATE SET
                    captured_at_ms = excluded.captured_at_ms,
                    equity_usd = excluded.equity_usd,
                    wallet_balance_usd = COALESCE(
                        excluded.wallet_balance_usd,
                        equity_snapshots.wallet_balance_usd
                    ),
                    available_usd = COALESCE(
                        excluded.available_usd,
                        equity_snapshots.available_usd
                    ),
                    unrealized_pnl_usd = COALESCE(
                        excluded.unrealized_pnl_usd,
                        equity_snapshots.unrealized_pnl_usd
                    ),
                    source = excluded.source
                WHERE excluded.captured_at_ms >=
                      equity_snapshots.captured_at_ms
                """,
                (
                    scope,
                    bucket_time,
                    captured,
                    equity_text,
                    wallet_text,
                    available_text,
                    unrealized_text,
                    source,
                ),
            )
            cutoff = captured - 730 * 24 * 60 * 60 * 1_000
            conn.execute(
                """
                DELETE FROM equity_snapshots
                WHERE account_scope = ? AND captured_at_ms < ?
                """,
                (scope, cutoff),
            )

    def list_equity_snapshots(
        self,
        account_scope: str,
        *,
        since_ms: int,
    ) -> list[dict[str, Any]]:
        with self._lock, self._connection() as conn:
            rows = conn.execute(
                """
                SELECT captured_at_ms, equity_usd, wallet_balance_usd,
                       available_usd, unrealized_pnl_usd, source
                FROM equity_snapshots
                WHERE account_scope = ? AND captured_at_ms >= ?
                ORDER BY captured_at_ms ASC
                """,
                (account_scope, int(since_ms)),
            ).fetchall()
        return [dict(row) for row in rows]

    def update_daily_equity_guard(
        self,
        equity: float,
        *,
        utc_day: Optional[str] = None,
        scope: str = "default",
    ) -> dict[str, float | str]:
        """Persist a UTC-day equity high-water mark across worker restarts."""
        value = float(equity)
        if not value > 0:
            raise ValueError("Equity для дневного guard должен быть положительным")
        day = utc_day or datetime.now(timezone.utc).date().isoformat()
        guard_key = f"{scope[:96]}|{day}"
        now = _utcnow()
        with self._lock, self._connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT * FROM daily_risk_state WHERE utc_day = ?",
                (guard_key,),
            ).fetchone()
            if row:
                start = float(row["start_equity"])
                high = max(float(row["high_water_equity"]), value)
                conn.execute(
                    """
                    UPDATE daily_risk_state
                    SET high_water_equity = ?, last_equity = ?, updated_at = ?
                    WHERE utc_day = ?
                    """,
                    (high, value, now, guard_key),
                )
            else:
                start = high = value
                conn.execute(
                    """
                    INSERT INTO daily_risk_state (
                        utc_day, start_equity, high_water_equity, last_equity, updated_at
                    ) VALUES (?, ?, ?, ?, ?)
                    """,
                    (guard_key, value, value, value, now),
                )
            cutoff = (
                datetime.now(timezone.utc) - timedelta(days=14)
            ).isoformat(timespec="seconds")
            conn.execute(
                "DELETE FROM daily_risk_state WHERE updated_at < ?",
                (cutoff,),
            )
            conn.execute("COMMIT")
        return {
            "utc_day": day,
            "start_equity": start,
            "high_water_equity": high,
            "last_equity": value,
            "drawdown": max(0.0, high - value),
        }

    def pending_notifications(self, limit: int = 50) -> list[dict[str, Any]]:
        now = _utcnow()
        with self._lock, self._connection() as conn:
            rows = conn.execute(
                """
                SELECT id, chat_id, alert_id, message, attempts, created_at,
                       next_attempt_at
                FROM notification_outbox
                WHERE status = 'pending' AND abandoned_at IS NULL
                  AND (next_attempt_at IS NULL OR next_attempt_at <= ?)
                ORDER BY attempts ASC, id ASC LIMIT ?
                """,
                (now, max(1, min(limit, 200))),
            ).fetchall()
        return [dict(row) for row in rows]

    def mark_notification_attempt(
        self,
        outbox_ids: int | Iterable[int],
        outcome: str | bool,
        *,
        error: str = "",
    ) -> None:
        """Acknowledge, back off, or abandon one durable delivery batch."""
        ids = (
            [int(outbox_ids)]
            if isinstance(outbox_ids, int)
            else [int(item) for item in outbox_ids]
        )
        if not ids:
            return
        normalized = (
            "ok"
            if outcome is True
            else "temporary_failure"
            if outcome is False
            else str(outcome)
        )
        if normalized not in {
            "ok",
            "missing",
            "unavailable",
            "temporary_failure",
            "permanent_failure",
        }:
            normalized = "temporary_failure"
        now_dt = datetime.now(timezone.utc)
        now = now_dt.isoformat(timespec="seconds")
        with self._lock, self._connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            for outbox_id in ids:
                row = conn.execute(
                    "SELECT attempts FROM notification_outbox WHERE id = ?",
                    (outbox_id,),
                ).fetchone()
                if not row:
                    continue
                attempts = int(row["attempts"]) + 1
                delivered = normalized == "ok"
                abandoned = normalized == "permanent_failure" or attempts >= 12
                delay_seconds = min(1_800, 15 * (2 ** min(attempts, 7)))
                next_attempt = (
                    now_dt + timedelta(seconds=delay_seconds)
                ).isoformat(timespec="seconds")
                conn.execute(
                    """
                    UPDATE notification_outbox
                    SET attempts = ?,
                        status = CASE WHEN ? THEN 'delivered' ELSE status END,
                        delivered_at = CASE WHEN ? THEN ? ELSE delivered_at END,
                        next_attempt_at = CASE
                            WHEN ? OR ? THEN NULL ELSE ?
                        END,
                        last_attempt_at = ?,
                        last_error = ?,
                        abandoned_at = CASE WHEN ? THEN ? ELSE abandoned_at END
                    WHERE id = ?
                    """,
                    (
                        attempts,
                        int(delivered),
                        int(delivered),
                        now,
                        int(delivered),
                        int(abandoned),
                        next_attempt,
                        now,
                        (error or normalized)[:500],
                        int(abandoned and not delivered),
                        now,
                        outbox_id,
                    ),
                )
            conn.execute("COMMIT")


_store: Optional[SQLiteStore] = None
_store_lock = threading.Lock()


def get_store() -> SQLiteStore:
    global _store
    if _store is None:
        with _store_lock:
            if _store is None:
                _store = SQLiteStore()
                logger.info(f"SQLite storage ready: {_store.path}")
    return _store
