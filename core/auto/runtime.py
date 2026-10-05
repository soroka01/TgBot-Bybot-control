"""Runtime state, locks and control exceptions for auto-trading."""

from __future__ import annotations

import threading
from typing import Any


# All exchange mutations, including manual Telegram closes, share this lock.
EXECUTION_LOCK = threading.RLock()
FEE_REFRESH_SECONDS = 3_600
TRADE_HISTORY_SYNC_SECONDS = 15 * 60
MAX_SAFETY_CLOSE_ATTEMPTS = 3
SUPPORTED_AUTO_MARGIN_MODES = {"REGULAR_MARGIN"}
PARTIAL_TERMINAL_ORDER_STATUSES = {
    "PartiallyFilledCanceled",
    "PartiallyFilledCancelled",
}

_runtime_lock = threading.Lock()
_runtime: dict[str, Any] = {
    "state": "stopped",
    "iteration": 0,
    "last_cycle_at": None,
    "last_snapshot_id": None,
    "last_summary": "Ещё не запускался",
    "last_error": None,
}


class FatalExecutionError(RuntimeError):
    """A live position may be unsafe; automation must stop immediately."""


class ExecutionStopped(RuntimeError):
    """The owner stopped automation before a new entry was submitted."""


def execution_lock() -> threading.RLock:
    return EXECUTION_LOCK


def get_runtime_status() -> dict[str, Any]:
    with _runtime_lock:
        return dict(_runtime)


def _set_runtime(**values: Any) -> None:
    with _runtime_lock:
        _runtime.update(values)
