"""Runtime configuration.

No .env or JSON configuration files are used. Secrets are read only from the
process environment, while safe defaults live in this tracked Python module.
"""

from __future__ import annotations

import os
from pathlib import Path


BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
DATABASE_PATH = Path(os.getenv("CRYPTO_DB_PATH", str(DATA_DIR / "crypto_bot.sqlite3")))

# Credentials are intentionally never stored in the repository.
BYBIT_API_KEY = os.getenv("BYBIT_API_KEY", "")
BYBIT_API_SECRET = os.getenv("BYBIT_API_SECRET", "")
DEEPSEEK_API_KEY = os.getenv("DEEPSEEK_API_KEY", "")
DEEPSEEK_API_URL = os.getenv("DEEPSEEK_API_URL", "https://api.deepseek.com/v1")
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "")
ADMIN_TELEGRAM_IDS = frozenset(
    int(value) for value in os.getenv("ADMIN_TELEGRAM_IDS", "").split(",") if value.strip().isdigit()
)

# Trading settings apply to the shared exchange account. Per-user display,
# alert and notification preferences are stored in SQLite.
POLL_INTERVAL = int(os.getenv("POLL_INTERVAL", "180"))
BYBIT_CATEGORY = "linear"
DRY_RUN = os.getenv("DRY_RUN", "True").lower() in {"true", "1", "yes"}
MAX_LEVERAGE = int(os.getenv("MAX_LEVERAGE", "10"))
MAX_RISK_PER_TRADE_PERCENT = float(os.getenv("MAX_RISK_PER_TRADE_PERCENT", "2"))
MAX_TOTAL_RISK_PERCENT = float(os.getenv("MAX_TOTAL_RISK_PERCENT", "10"))
MIN_ORDER_SIZE_USDT = float(os.getenv("MIN_ORDER_SIZE_USDT", "10"))
TRADABLE_TOKENS = ["BTC", "ETH", "SOL", "XRP", "BNB", "DOGE"]

# Alert scheduler settings. The scheduler is part of the bot event loop; no
# second process, Redis or thread-based scheduler is required.
ALERT_CHECK_INTERVAL_SECONDS = 15
ALERT_DEFAULT_COOLDOWN_SECONDS = 60

SYMBOL_LIMITS = {
    "BTC": {"min_qty": 0.001, "qty_step": 0.001, "price_scale": 1},
    "ETH": {"min_qty": 0.01, "qty_step": 0.01, "price_scale": 2},
    "SOL": {"min_qty": 0.1, "qty_step": 0.1, "price_scale": 3},
    "BNB": {"min_qty": 0.01, "qty_step": 0.01, "price_scale": 2},
    "XRP": {"min_qty": 1, "qty_step": 1, "price_scale": 4},
    "DOGE": {"min_qty": 1, "qty_step": 1, "price_scale": 5},
}


def validate_config() -> list[str]:
    """Return startup diagnostics without printing or exposing secrets."""
    errors: list[str] = []
    if not TELEGRAM_TOKEN:
        errors.append("TELEGRAM_TOKEN не задан")
    if not DRY_RUN and (not BYBIT_API_KEY or not BYBIT_API_SECRET):
        errors.append("Для LIVE-режима нужны ключи Bybit")
    if MAX_LEVERAGE < 1 or MAX_LEVERAGE > 100:
        errors.append("MAX_LEVERAGE должен быть в диапазоне 1–100")
    if MIN_ORDER_SIZE_USDT <= 0:
        errors.append("MIN_ORDER_SIZE_USDT должен быть больше нуля")
    return errors
