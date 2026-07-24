"""Durable Bybit trade journal and bounded Closed PnL reconciliation.

Bybit Closed PnL is the authoritative net result.  The local journal adds the
code-approved setup and sizing context that the exchange cannot return later.
All monetary values remain decimal strings until analytics explicitly parse
them.
"""

from __future__ import annotations

import hashlib
import json
import threading
import time
from dataclasses import asdict, dataclass, is_dataclass
from decimal import Decimal, InvalidOperation
from functools import wraps
from typing import Any, Iterable, Optional

from api.bybit_api import BybitAPI
from storage.database import SQLiteStore, get_store
from utils.logger_setup import logger


DAY_MS = 24 * 60 * 60 * 1_000
MAX_WINDOW_MS = 7 * DAY_MS
RECENT_OVERLAP_MS = 2 * DAY_MS
SYNC_FRESH_MS = 60 * 1_000
MAX_LOOKBACK_DAYS = 365
UID_RETRY_SECONDS = 60.0
_CLOSED_PNL_SYNC_LOCK = threading.Lock()


@dataclass(frozen=True)
class SyncSummary:
    account_scope: str
    requested_start_ms: int
    synced_through_ms: int
    fetched: int
    inserted: int
    ignored: int
    windows: int
    skipped_fresh: bool = False
    skipped_busy: bool = False


def _single_closed_pnl_sync(method):
    """Avoid duplicate year-long backfills from concurrent Telegram clicks."""

    @wraps(method)
    def wrapped(self, *args, **kwargs):
        if _CLOSED_PNL_SYNC_LOCK.acquire(blocking=False):
            try:
                return method(self, *args, **kwargs)
            finally:
                _CLOSED_PNL_SYNC_LOCK.release()

        days = max(
            1,
            min(
                int(kwargs.get("lookback_days", MAX_LOOKBACK_DAYS)),
                MAX_LOOKBACK_DAYS,
            ),
        )
        now = int(kwargs.get("now_ms") or time.time() * 1_000)
        scope = self.account_scope
        state = self.store.get_trade_sync_state(scope)
        return SyncSummary(
            account_scope=scope,
            requested_start_ms=now - days * DAY_MS,
            synced_through_ms=int(state.get("coverage_end_ms") or 0),
            fetched=0,
            inserted=0,
            ignored=0,
            windows=0,
            skipped_busy=True,
        )

    return wrapped


def _decimal_text(value: Any, *, required: bool = False) -> Optional[str]:
    if value is None or value == "":
        if required:
            raise ValueError("Отсутствует обязательное decimal-поле Closed PnL")
        return None
    try:
        number = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as error:
        raise ValueError(f"Некорректное decimal-поле Closed PnL: {value!r}") from error
    if not number.is_finite():
        raise ValueError("Closed PnL содержит нечисловое значение")
    return format(number, "f")


def _equity_number(
    container: dict[str, Any],
    field: str,
    *,
    required: bool = False,
) -> Optional[Decimal]:
    """Parse one wallet field without treating documented empty values as zero."""
    raw = container.get(field)
    if raw is None or raw == "":
        if required:
            raise ValueError(f"Bybit не вернул обязательное поле {field}")
        return None
    if isinstance(raw, bool):
        raise ValueError(f"Bybit вернул некорректное поле {field}")
    try:
        value = Decimal(str(raw))
    except (InvalidOperation, TypeError, ValueError) as error:
        raise ValueError(f"Bybit вернул некорректное поле {field}") from error
    if not value.is_finite():
        raise ValueError(f"Bybit вернул некорректное поле {field}")
    return value


def _history_equity_overview(wallet_response: dict[str, Any]) -> Optional[dict[str, Any]]:
    """Return the fields needed by analytics for every supported UTA margin mode.

    Bybit intentionally leaves some account-wide fields empty in UTA isolated
    margin.  Trading still uses its strict account parser; only this optional
    analytics snapshot accepts those documented omissions.
    """
    if not isinstance(wallet_response, dict):
        raise ValueError("Bybit вернул повреждённый ответ баланса аккаунта")
    result = wallet_response.get("result")
    rows = result.get("list") if isinstance(result, dict) else None
    if not isinstance(rows, list) or not rows or not isinstance(rows[0], dict):
        raise ValueError("Bybit не вернул баланс аккаунта")
    account = rows[0]

    equity = _equity_number(account, "totalEquity")
    if equity is None:
        coins = account.get("coin")
        if not isinstance(coins, list):
            raise ValueError("Bybit вернул повреждённый список активов аккаунта")
        equity = Decimal("0")
        for index, coin in enumerate(coins):
            if not isinstance(coin, dict):
                raise ValueError(
                    f"Bybit вернул повреждённый актив account.coin[{index}]"
                )
            usd_value = _equity_number(coin, "usdValue")
            if usd_value is not None:
                equity += usd_value
    if equity == 0:
        return None
    if equity < 0:
        raise ValueError("Bybit вернул отрицательный equity аккаунта")

    wallet_balance = _equity_number(account, "totalWalletBalance")
    unrealized_pnl = _equity_number(account, "totalPerpUPL")
    available = _equity_number(account, "totalAvailableBalance")

    # Account-wide available balance is not applicable to isolated margin.
    # Use Bybit's documented per-coin derivatives formula when all operands
    # are present; otherwise leave this optional metric unknown.
    if available is None:
        coins = account.get("coin")
        if coins is not None and not isinstance(coins, list):
            raise ValueError("Bybit вернул повреждённый список активов аккаунта")
        usdt = next(
            (
                coin
                for coin in (coins or [])
                if isinstance(coin, dict) and coin.get("coin") == "USDT"
            ),
            None,
        )
        if usdt is not None:
            operands = [
                _equity_number(usdt, field)
                for field in (
                    "walletBalance",
                    "totalPositionIM",
                    "totalOrderIM",
                    "locked",
                    "bonus",
                )
            ]
            if all(value is not None for value in operands):
                wallet, position_im, order_im, locked, bonus = operands
                assert (
                    wallet is not None
                    and position_im is not None
                    and order_im is not None
                    and locked is not None
                    and bonus is not None
                )
                available = max(
                    Decimal("0"),
                    wallet - position_im - order_im - locked - bonus,
                )

    return {
        "equity_usd": equity,
        "balance_usd": wallet_balance,
        "available_usd": available,
        "unrealized_pnl_usd": unrealized_pnl,
    }


def _positive_milliseconds(value: Any, fallback: Any = None) -> int:
    raw = value if value not in (None, "") else fallback
    try:
        result = int(raw)
    except (TypeError, ValueError) as error:
        raise ValueError(f"Некорректная временная метка Closed PnL: {raw!r}") from error
    if result <= 0:
        raise ValueError("Временная метка Closed PnL должна быть положительной")
    return result


def _stable_record_id(row: dict[str, Any]) -> str:
    order_id = str(row.get("orderId") or "").strip()
    if order_id:
        return f"order:{order_id}"
    identity = "|".join(
        str(row.get(name) or "")
        for name in (
            "symbol",
            "side",
            "createdTime",
            "orderPrice",
            "qty",
            "orderType",
            "execType",
        )
    )
    if not identity.replace("|", ""):
        raise ValueError("Closed PnL не содержит стабильного идентификатора")
    return "fallback:" + hashlib.sha256(identity.encode("utf-8")).hexdigest()[:32]


def normalize_closed_pnl(row: dict[str, Any]) -> Optional[dict[str, Any]]:
    """Validate one untrusted Bybit row and return a storage-safe record."""
    if not isinstance(row, dict):
        raise ValueError("Closed PnL row должен быть объектом")
    symbol = str(row.get("symbol") or "").strip().upper()
    # category=linear mixes USDT and USDC while the endpoint has no settleCoin.
    # The bot and its risk model are deliberately USDT-only.
    if not symbol.endswith("USDT"):
        return None
    base_asset = symbol[:-4]
    if (
        not base_asset
        or not base_asset.isascii()
        or not base_asset.isalnum()
    ):
        raise ValueError("Closed PnL содержит некорректный USDT symbol")
    close_side = str(row.get("side") or "").strip()
    if close_side not in {"Buy", "Sell"}:
        raise ValueError("Closed PnL содержит неизвестную сторону закрытия")
    position_side = (
        "Buy"
        if close_side == "Sell"
        else "Sell"
    )
    updated_time = _positive_milliseconds(row.get("updatedTime"))
    created_time = _positive_milliseconds(row.get("createdTime"), updated_time)
    if updated_time < created_time:
        raise ValueError("Closed PnL updatedTime раньше createdTime")
    open_fee = _decimal_text(row.get("openFee"))
    close_fee = _decimal_text(row.get("closeFee"))
    try:
        fill_count = int(row.get("fillCount")) if row.get("fillCount") not in (None, "") else None
    except (TypeError, ValueError) as error:
        raise ValueError("Closed PnL содержит некорректный fillCount") from error
    if fill_count is not None and fill_count < 0:
        raise ValueError("Closed PnL содержит отрицательный fillCount")
    qty = _decimal_text(row.get("qty"))
    closed_size = _decimal_text(
        row.get("closedSize") or row.get("qty"),
        required=True,
    )
    if Decimal(closed_size) <= 0:
        raise ValueError("Closed PnL closedSize должен быть положительным")
    if qty is not None and Decimal(qty) <= 0:
        raise ValueError("Closed PnL qty должен быть положительным")
    payload = json.dumps(
        row,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    return {
        "record_id": _stable_record_id(row),
        "order_id": str(row.get("orderId") or "").strip() or None,
        "symbol": symbol,
        "close_side": close_side,
        "position_side": position_side,
        "order_type": str(row.get("orderType") or "").strip() or None,
        "exec_type": str(row.get("execType") or "").strip() or None,
        "qty": qty,
        "closed_size": closed_size,
        "order_price": _decimal_text(row.get("orderPrice")),
        "avg_entry_price": _decimal_text(row.get("avgEntryPrice")),
        "avg_exit_price": _decimal_text(row.get("avgExitPrice")),
        "cum_entry_value": _decimal_text(row.get("cumEntryValue")),
        "cum_exit_value": _decimal_text(row.get("cumExitValue")),
        "closed_pnl": _decimal_text(row.get("closedPnl"), required=True),
        "open_fee": open_fee,
        "close_fee": close_fee,
        "fee_data_complete": open_fee is not None and close_fee is not None,
        "leverage": _decimal_text(row.get("leverage")),
        "fill_count": fill_count,
        "created_time_ms": created_time,
        "updated_time_ms": updated_time,
        "raw_json": payload,
    }


def _iter_windows_newest_first(start_ms: int, end_ms: int) -> Iterable[tuple[int, int]]:
    if start_ms > end_ms:
        raise ValueError("Начало диапазона истории позже конца")
    cursor_end = int(end_ms)
    start = int(start_ms)
    while True:
        cursor_start = max(start, cursor_end - MAX_WINDOW_MS)
        yield cursor_start, cursor_end
        if cursor_start == start:
            return
        # The one-millisecond boundary overlap is intentional; storage upsert
        # removes duplicates and avoids relying on undocumented inclusivity.
        cursor_end = cursor_start


def _merge_ranges(ranges: Iterable[tuple[int, int]]) -> list[tuple[int, int]]:
    ordered = sorted((int(start), int(end)) for start, end in ranges if start <= end)
    merged: list[list[int]] = []
    for start, end in ordered:
        if not merged or start > merged[-1][1] + 1:
            merged.append([start, end])
        else:
            merged[-1][1] = max(merged[-1][1], end)
    return [(item[0], item[1]) for item in merged]


class TradeJournal:
    """Account-scoped journal facade used by auto trading and Telegram."""

    def __init__(
        self,
        bybit: BybitAPI,
        store: Optional[SQLiteStore] = None,
    ) -> None:
        self.bybit = bybit
        self.store = store or get_store()
        # Only a verified UID scope is cached. A fallback scope must remain
        # retryable so a temporary /v5/user/query-api outage cannot pin the
        # journal to an API-key-derived identity forever.
        self._account_scope: Optional[str] = None
        self._next_uid_retry_at = 0.0

    def _account_scopes(self) -> tuple[str, str, str]:
        base = str(getattr(self.bybit, "base", "")).rstrip("/")
        api_key = str(getattr(self.bybit, "api_key", ""))
        fingerprint = hashlib.sha256(
            f"{base}|{api_key}".encode("utf-8")
        ).hexdigest()
        fallback_scope = hashlib.sha256(
            f"{base}|key-fingerprint:{fingerprint}".encode("utf-8")
        ).hexdigest()[:24]
        return base, fingerprint, fallback_scope

    def _resolve_account_scope(
        self,
        *,
        require_uid: bool,
        force_uid_retry: bool = False,
    ) -> str:
        if self._account_scope is not None:
            return self._account_scope

        base, fingerprint, fallback_scope = self._account_scopes()
        verified_scope = self.store.get_verified_trade_account_scope(
            fingerprint
        )
        if verified_scope:
            self._account_scope = verified_scope
            return verified_scope
        monotonic_now = time.monotonic()
        if not force_uid_retry and monotonic_now < self._next_uid_retry_at:
            if require_uid:
                raise RuntimeError(
                    "LIVE-вход заблокирован: Bybit UID пока не подтверждён"
                )
            return fallback_scope

        try:
            user_id = self.bybit.get_account_user_id()
            if not str(user_id).isdigit():
                raise ValueError("Bybit вернул некорректный userID")
            uid_scope = hashlib.sha256(
                f"{base}|uid:{user_id}".encode("utf-8")
            ).hexdigest()[:24]
            self.store.migrate_trade_account_scope(
                fallback_scope,
                uid_scope,
                verified_api_fingerprint=fingerprint,
            )
        except Exception as error:
            self._next_uid_retry_at = monotonic_now + UID_RETRY_SECONDS
            logger.warning(
                "Не удалось подтвердить Bybit UID для trade journal "
                f"({type(error).__name__}); будет выполнена повторная попытка"
            )
            if require_uid:
                raise RuntimeError(
                    "LIVE-вход заблокирован: не удалось подтвердить Bybit UID"
                ) from error
            return fallback_scope

        self._account_scope = uid_scope
        self._next_uid_retry_at = 0.0
        return uid_scope

    @property
    def account_scope(self) -> str:
        return self._resolve_account_scope(require_uid=False)

    def verified_account_scope(self) -> str:
        """Return a UID-backed scope or block a state-changing LIVE action."""
        return self._resolve_account_scope(
            require_uid=True,
            force_uid_retry=True,
        )

    def prepare_entry(
        self,
        *,
        candidate: dict[str, Any],
        plan: Any,
        cycle: dict[str, Any],
        decision: Optional[dict[str, Any]],
        order_link_id: str,
        sizing_context: dict[str, Any],
        dry_run: bool,
    ) -> None:
        """Persist the exact approved plan before the exchange write."""
        if is_dataclass(plan):
            raw_plan = asdict(plan)
        else:
            raw_plan = {
                name: getattr(plan, name)
                for name in (
                    "quantity",
                    "entry_price",
                    "take_profit",
                    "stop_loss",
                    "leverage",
                    "risk_usd",
                    "reward_usd",
                    "estimated_cost_usd",
                    "net_risk_reward",
                )
                if hasattr(plan, name)
            }
        plan_payload = {key: str(value) for key, value in raw_plan.items()}
        symbol = str(candidate["symbol"]).upper()
        account_scope = (
            self.account_scope if dry_run else self.verified_account_scope()
        )
        snapshot = cycle.get("snapshot", {})
        audit_snapshot = {
            "schema_version": snapshot.get("schema_version"),
            "snapshot_id": snapshot.get("snapshot_id"),
            "as_of": snapshot.get("as_of"),
            "valid_until": snapshot.get("valid_until"),
            "symbol": snapshot.get("symbols", {}).get(symbol),
            "candidate": candidate,
        }
        self.store.upsert_trade_setup(
            account_scope=account_scope,
            candidate_id=str(candidate["id"]),
            snapshot_id=(
                str(snapshot.get("snapshot_id"))
                if snapshot.get("snapshot_id") is not None
                else None
            ),
            strategy_version=str(candidate.get("strategy") or "trend_atr.v1"),
            selector_reason=(
                str(decision.get("reason_code"))
                if decision and decision.get("reason_code") is not None
                else None
            ),
            symbol=symbol,
            side=str(candidate["side"]),
            status="entry_submitted",
            dry_run=dry_run,
            entry_order_link_id=order_link_id,
            plan=plan_payload,
            decision=decision,
            snapshot=audit_snapshot,
            sizing_context=sizing_context,
        )
        account = cycle.get("account") or {}
        if account.get("equity_usd") is not None:
            try:
                self.record_equity(account, source="entry")
            except Exception as error:
                # The plan is already durable, but no exchange write has
                # happened yet. Mark the lifecycle terminal before refusing
                # the entry so a transient snapshot failure cannot leave a
                # phantom unresolved setup.
                try:
                    self.store.update_trade_setup(
                        account_scope,
                        str(candidate["id"]),
                        status="failed",
                        last_error=(
                            "Не удалось записать entry equity snapshot "
                            f"({type(error).__name__})"
                        ),
                    )
                except Exception as status_error:
                    logger.error(
                        "Не удалось отметить trade setup failed после ошибки "
                        f"equity snapshot ({type(status_error).__name__})"
                    )
                raise

    def update_setup(self, candidate_id: str, **changes: Any) -> None:
        self.store.update_trade_setup(
            self.account_scope,
            str(candidate_id),
            **changes,
        )

    def record_equity(
        self,
        account: dict[str, Any],
        *,
        source: str,
        captured_at_ms: Optional[int] = None,
    ) -> None:
        self.store.record_equity_snapshot(
            self.account_scope,
            captured_at_ms=int(captured_at_ms or time.time() * 1_000),
            equity_usd=account["equity_usd"],
            wallet_balance_usd=account.get("balance_usd"),
            available_usd=account.get("available_usd"),
            unrealized_pnl_usd=account.get("unrealized_pnl_usd"),
            source=source,
        )

    def record_current_equity(self) -> bool:
        account = _history_equity_overview(self.bybit.get_wallet_balance())
        if account is None:
            return False
        self.record_equity(account, source="history_sync")
        return True

    def import_closed_pnl_rows(
        self,
        rows: Iterable[dict[str, Any]],
    ) -> tuple[int, int, int]:
        normalized: list[dict[str, Any]] = []
        ignored = 0
        for row in rows:
            try:
                item = normalize_closed_pnl(row)
            except ValueError as error:
                ignored += 1
                logger.warning(f"Пропущена повреждённая Closed PnL запись: {error}")
                continue
            if item is None:
                ignored += 1
                continue
            normalized.append(item)
        normalized.sort(
            key=lambda item: (
                int(item["updated_time_ms"]),
                int(item["created_time_ms"]),
                str(item["record_id"]),
            )
        )
        inserted = 0
        for item in normalized:
            if self.store.upsert_closed_trade_record(self.account_scope, item):
                inserted += 1
        return len(normalized), inserted, ignored

    @_single_closed_pnl_sync
    def sync_closed_pnl(
        self,
        *,
        lookback_days: int,
        force: bool = False,
        now_ms: Optional[int] = None,
    ) -> SyncSummary:
        days = max(1, min(int(lookback_days), MAX_LOOKBACK_DAYS))
        now = int(now_ms or time.time() * 1_000)
        required_start = now - days * DAY_MS
        state = self.store.get_trade_sync_state(self.account_scope)
        coverage_start = int(state.get("coverage_start_ms") or now)
        coverage_end = int(state.get("coverage_end_ms") or 0)
        last_success = int(state.get("last_success_ms") or 0)
        needs_backfill = not state or required_start < coverage_start
        fresh = (
            bool(state)
            and not needs_backfill
            and coverage_end >= now - SYNC_FRESH_MS
            and last_success >= now - SYNC_FRESH_MS
        )
        if fresh and not force:
            return SyncSummary(
                self.account_scope,
                required_start,
                coverage_end,
                0,
                0,
                0,
                0,
                skipped_fresh=True,
            )

        ranges: list[tuple[int, int]] = []
        if not state:
            ranges.append((required_start, now))
        else:
            if needs_backfill:
                ranges.append((required_start, coverage_start))
            refresh_start = max(required_start, min(coverage_end, now) - RECENT_OVERLAP_MS)
            if force or coverage_end < now or last_success < now - SYNC_FRESH_MS:
                ranges.append((refresh_start, now))
        ranges = _merge_ranges(ranges)

        fetched = inserted = ignored = windows = 0
        # Recent data is requested first so a partial outage still leaves the
        # most useful records durably cached. The watermark advances only after
        # every required window succeeds.
        for range_start, range_end in sorted(ranges, reverse=True):
            for window_start, window_end in _iter_windows_newest_first(
                range_start,
                range_end,
            ):
                response = self.bybit.get_closed_pnl(
                    limit=100,
                    start_time=window_start,
                    end_time=window_end,
                    all_pages=True,
                )
                rows = response.get("result", {}).get("list", [])
                if not isinstance(rows, list):
                    raise ValueError("Bybit Closed PnL result.list должен быть массивом")
                accepted, new_rows, rejected = self.import_closed_pnl_rows(rows)
                fetched += len(rows)
                inserted += new_rows
                ignored += rejected + (len(rows) - accepted - rejected)
                windows += 1

        new_coverage_start = (
            required_start if not state else min(required_start, coverage_start)
        )
        self.store.update_trade_sync_state(
            self.account_scope,
            coverage_start_ms=new_coverage_start,
            coverage_end_ms=now,
            last_success_ms=now,
        )
        return SyncSummary(
            self.account_scope,
            required_start,
            now,
            fetched,
            inserted,
            ignored,
            windows,
        )

    def closed_records(
        self,
        *,
        lookback_days: int,
        bot_only: bool,
        now_ms: Optional[int] = None,
    ) -> list[dict[str, Any]]:
        now = int(now_ms or time.time() * 1_000)
        since = now - max(1, min(int(lookback_days), MAX_LOOKBACK_DAYS)) * DAY_MS
        return self.store.list_closed_trade_records(
            self.account_scope,
            since_ms=since,
            bot_only=bot_only,
        )

    def equity_snapshots(
        self,
        *,
        lookback_days: int,
        now_ms: Optional[int] = None,
    ) -> list[dict[str, Any]]:
        now = int(now_ms or time.time() * 1_000)
        since = now - max(1, min(int(lookback_days), MAX_LOOKBACK_DAYS)) * DAY_MS
        return self.store.list_equity_snapshots(
            self.account_scope,
            since_ms=since,
        )
