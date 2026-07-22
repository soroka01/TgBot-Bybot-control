"""Transactional SQLite repository for multi-user bot data.

SQLite is the durable source of truth for one bot process. Its synchronous
methods are called through asyncio.to_thread from Telegram handlers, so I/O
does not block the event loop.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator, Optional

from config import ALERT_DEFAULT_COOLDOWN_SECONDS, DATABASE_PATH
from utils.logger_setup import logger


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class SQLiteStore:
    """Repository with one schema for users, alerts and activity."""

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

                CREATE INDEX IF NOT EXISTS idx_alerts_active
                    ON alerts(is_enabled, kind, symbol, timeframe);
                CREATE INDEX IF NOT EXISTS idx_alerts_chat ON alerts(chat_id, is_enabled);
                CREATE INDEX IF NOT EXISTS idx_activity_chat_time
                    ON activity_log(chat_id, created_at DESC);
                """
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
                    is_admin = CASE WHEN excluded.is_admin = 1 THEN 1 ELSE users.is_admin END,
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

    def screen_targets(self) -> list[tuple[int, int]]:
        with self._lock, self._connection() as conn:
            rows = conn.execute(
                """
                SELECT chat_id, screen_message_id FROM users
                WHERE is_active = 1 AND notifications_enabled = 1
                  AND screen_message_id IS NOT NULL
                """
            ).fetchall()
        return [(int(row["chat_id"]), int(row["screen_message_id"])) for row in rows]

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
        if threshold <= 0 or (kind == "rsi" and threshold > 100):
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
                """
            ).fetchall()
        return [dict(row) for row in rows]

    def apply_alert_observation(
        self, alert_id: int, *, value: float, should_trigger: bool
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
