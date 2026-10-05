"""Auto-trading entry point; implementation lives in the core.auto package."""

from __future__ import annotations

from core.auto.gates import collect_cycle
from core.auto.loop import main_loop
from core.auto.runtime import (
    EXECUTION_LOCK,
    ExecutionStopped,
    FatalExecutionError,
    execution_lock,
    get_runtime_status,
)
from utils.logger_setup import logger

__all__ = [
    "EXECUTION_LOCK",
    "ExecutionStopped",
    "FatalExecutionError",
    "collect_cycle",
    "execution_lock",
    "get_runtime_status",
    "main_loop",
]


if __name__ == "__main__":
    try:
        main_loop()
    except KeyboardInterrupt:
        logger.info("Авто-режим остановлен пользователем")
