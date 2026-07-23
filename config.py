"""Central, environment-driven runtime configuration.

The local ``.env`` file is loaded for developer convenience, but process
environment variables always win.  Secrets and machine-specific values stay
outside version control.
"""

from __future__ import annotations

import os
import re
import math
from pathlib import Path
from urllib.parse import urlparse

from dotenv import load_dotenv


BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env", override=False)

DATA_DIR = BASE_DIR / "data"
_database_path_raw = os.getenv(
    "CRYPTO_DB_PATH",
    str(DATA_DIR / "crypto_bot.sqlite3"),
).strip()
if not _database_path_raw:
    _database_path_raw = str(DATA_DIR / "crypto_bot.sqlite3")
DATABASE_PATH = Path(_database_path_raw)
_CONFIG_ERRORS: list[str] = []


def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    normalized = raw.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    _CONFIG_ERRORS.append(f"{name}: ожидается true/false")
    return default


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except ValueError:
        _CONFIG_ERRORS.append(f"{name}: ожидается целое число")
        return default


def _env_float(name: str, default: float) -> float:
    try:
        result = float(os.getenv(name, str(default)))
    except ValueError:
        _CONFIG_ERRORS.append(f"{name}: ожидается число")
        return default
    if not math.isfinite(result):
        _CONFIG_ERRORS.append(f"{name}: ожидается конечное число")
        return default
    return result


def _tokens_from_env() -> list[str]:
    raw = os.getenv("TRADABLE_TOKENS", "BTC,ETH,SOL,XRP,BNB,DOGE")
    tokens: list[str] = []
    for item in raw.split(","):
        token = item.strip().upper()
        if not token:
            continue
        if not re.fullmatch(r"[A-Z0-9]{2,15}", token):
            _CONFIG_ERRORS.append(f"TRADABLE_TOKENS: недопустимый символ {token!r}")
            continue
        if token not in tokens:
            tokens.append(token)
    return tokens


# Credentials
BYBIT_API_KEY = os.getenv("BYBIT_API_KEY", "").strip()
BYBIT_API_SECRET = os.getenv("BYBIT_API_SECRET", "").strip()
DEEPSEEK_API_KEY = os.getenv("DEEPSEEK_API_KEY", "").strip()
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "").strip()

# Keep the old single-id variable working, but never infer an administrator
# from persisted database state.
_admin_ids_raw = os.getenv("ADMIN_TELEGRAM_IDS") or os.getenv("TELEGRAM_CHAT_ID", "")
ADMIN_TELEGRAM_IDS = frozenset(
    int(value.strip())
    for value in _admin_ids_raw.split(",")
    if value.strip().lstrip("-").isdigit()
)

# Bybit transport.  The endpoint is selected from official hosts so API
# credentials cannot be redirected by a typo in a custom URL.
BYBIT_CATEGORY = "linear"
_legacy_testnet = _env_bool("BYBIT_TESTNET", False)
BYBIT_ENV = os.getenv("BYBIT_ENV", "testnet" if _legacy_testnet else "mainnet").strip().lower()
_BYBIT_HOSTS = {
    "mainnet": "https://api.bybit.com",
    "testnet": "https://api-testnet.bybit.com",
    "demo": "https://api-demo.bybit.com",
}
if BYBIT_ENV not in _BYBIT_HOSTS:
    _CONFIG_ERRORS.append("BYBIT_ENV: ожидается mainnet, testnet или demo")
BYBIT_BASE_URL = _BYBIT_HOSTS.get(BYBIT_ENV, _BYBIT_HOSTS["mainnet"])
BYBIT_RECV_WINDOW_MS = _env_int("BYBIT_RECV_WINDOW_MS", 5_000)
BYBIT_HTTP_TIMEOUT_SECONDS = _env_float("BYBIT_HTTP_TIMEOUT_SECONDS", 15.0)
BYBIT_MAX_SLIPPAGE_PERCENT = _env_float("BYBIT_MAX_SLIPPAGE_PERCENT", 0.30)

# DeepSeek.  deepseek-chat/reasoner were retired on 2026-07-24; Flash is the
# current cost-efficient model and remains configurable.
DEEPSEEK_API_URL = os.getenv("DEEPSEEK_API_URL", "https://api.deepseek.com").rstrip("/")
DEEPSEEK_MODEL = os.getenv("DEEPSEEK_MODEL", "deepseek-v4-flash").strip()
DEEPSEEK_TIMEOUT_SECONDS = _env_float("DEEPSEEK_TIMEOUT_SECONDS", 30.0)
DEEPSEEK_MAX_TOKENS = _env_int("DEEPSEEK_MAX_TOKENS", 2_048)
DEEPSEEK_LOG_RESPONSES = _env_bool("DEEPSEEK_LOG_RESPONSES", False)
DEEPSEEK_LOG_RETENTION_DAYS = _env_int("DEEPSEEK_LOG_RETENTION_DAYS", 7)

# Trading settings apply to the shared exchange account.  Code builds every
# direction and price level; the model may only select an existing candidate.
POLL_INTERVAL = _env_int("POLL_INTERVAL", 180)
_trading_mode_raw = os.getenv("TRADING_MODE", "").strip().lower()
if _trading_mode_raw:
    TRADING_MODE = _trading_mode_raw
    if TRADING_MODE not in {"dry", "live"}:
        _CONFIG_ERRORS.append("TRADING_MODE: ожидается dry или live")
        TRADING_MODE = "dry"
else:
    # Backward-compatible migration path.  Invalid DRY_RUN values are recorded
    # as startup errors and therefore can never silently enable LIVE.
    TRADING_MODE = "dry" if _env_bool("DRY_RUN", True) else "live"
DRY_RUN = TRADING_MODE == "dry"
LIVE_TRADING_CONFIRMATION = os.getenv("LIVE_TRADING_CONFIRMATION", "").strip()
LIVE_TRADING_CONFIRMATION_PHRASE = "I_ACCEPT_LIVE_TRADING_RISK"
MAX_LEVERAGE = _env_int("MAX_LEVERAGE", 5)
AUTO_LEVERAGE = _env_int("AUTO_LEVERAGE", 2)
MAX_RISK_PER_TRADE_PERCENT = _env_float("MAX_RISK_PER_TRADE_PERCENT", 1.0)
MAX_TOTAL_RISK_PERCENT = _env_float("MAX_TOTAL_RISK_PERCENT", 5.0)
MAX_DAILY_LOSS_PERCENT = _env_float("MAX_DAILY_LOSS_PERCENT", 3.0)
MAX_POSITION_NOTIONAL_PERCENT = _env_float("MAX_POSITION_NOTIONAL_PERCENT", 100.0)
MIN_ORDER_SIZE_USDT = _env_float("MIN_ORDER_SIZE_USDT", 10.0)
MIN_NET_RISK_REWARD_RATIO = _env_float("MIN_NET_RISK_REWARD_RATIO", 1.5)
MAX_SPREAD_PERCENT = _env_float("MAX_SPREAD_PERCENT", 0.15)
MAX_PRICE_DRIFT_PERCENT = _env_float("MAX_PRICE_DRIFT_PERCENT", 0.25)
ESTIMATED_SLIPPAGE_PERCENT = _env_float("ESTIMATED_SLIPPAGE_PERCENT", 0.05)
FALLBACK_TAKER_FEE_RATE = _env_float("FALLBACK_TAKER_FEE_RATE", 0.0006)
SIGNAL_VALIDITY_SECONDS = _env_int("SIGNAL_VALIDITY_SECONDS", 90)
TP_SL_MIN_CHANGE_PERCENT = _env_float("TP_SL_MIN_CHANGE_PERCENT", 0.05)
TRADABLE_TOKENS = _tokens_from_env()

# Alert scheduler settings.  The scheduler is part of the bot event loop; no
# second process, Redis or thread-based scheduler is required.
ALERT_CHECK_INTERVAL_SECONDS = 15
ALERT_DEFAULT_COOLDOWN_SECONDS = 60


def validate_config(mode: str = "telegram") -> list[str]:
    """Return actionable startup diagnostics without exposing secret values."""
    errors = list(_CONFIG_ERRORS)
    if mode == "telegram" and not TELEGRAM_TOKEN:
        errors.append("TELEGRAM_TOKEN не задан")
    if mode in {"auto", "ai"}:
        if not DEEPSEEK_API_KEY:
            errors.append("DEEPSEEK_API_KEY не задан")
        if not BYBIT_API_KEY or not BYBIT_API_SECRET:
            errors.append(
                "BYBIT_API_KEY и BYBIT_API_SECRET нужны для чтения счёта "
                "в auto/AI даже в DRY-режиме"
            )
    if not DRY_RUN:
        if not BYBIT_API_KEY or not BYBIT_API_SECRET:
            errors.append("Для LIVE-режима нужны BYBIT_API_KEY и BYBIT_API_SECRET")
        if not ADMIN_TELEGRAM_IDS and mode == "telegram":
            errors.append("Для LIVE-режима задайте ADMIN_TELEGRAM_IDS")
        if LIVE_TRADING_CONFIRMATION != LIVE_TRADING_CONFIRMATION_PHRASE:
            errors.append(
                "LIVE-режим заблокирован: задайте "
                f"LIVE_TRADING_CONFIRMATION={LIVE_TRADING_CONFIRMATION_PHRASE}"
            )
    if not 1 <= MAX_LEVERAGE <= 100:
        errors.append("MAX_LEVERAGE должен быть в диапазоне 1–100")
    if not 1 <= AUTO_LEVERAGE <= MAX_LEVERAGE:
        errors.append("AUTO_LEVERAGE должен быть в диапазоне 1–MAX_LEVERAGE")
    if not 0 < MAX_RISK_PER_TRADE_PERCENT <= 5:
        errors.append("MAX_RISK_PER_TRADE_PERCENT должен быть больше 0 и не выше 5")
    if not MAX_RISK_PER_TRADE_PERCENT <= MAX_TOTAL_RISK_PERCENT <= 25:
        errors.append(
            "MAX_TOTAL_RISK_PERCENT должен быть не меньше риска на сделку и не выше 25"
        )
    if not 0 < MAX_DAILY_LOSS_PERCENT <= 25:
        errors.append("MAX_DAILY_LOSS_PERCENT должен быть больше 0 и не выше 25")
    if MIN_ORDER_SIZE_USDT <= 0:
        errors.append("MIN_ORDER_SIZE_USDT должен быть больше нуля")
    if POLL_INTERVAL < 30:
        errors.append("POLL_INTERVAL должен быть не меньше 30 секунд")
    if not 0.01 <= BYBIT_MAX_SLIPPAGE_PERCENT <= 10:
        errors.append("BYBIT_MAX_SLIPPAGE_PERCENT должен быть в диапазоне 0.01–10")
    if not 0 <= ESTIMATED_SLIPPAGE_PERCENT <= BYBIT_MAX_SLIPPAGE_PERCENT:
        errors.append(
            "ESTIMATED_SLIPPAGE_PERCENT должен быть от 0 до BYBIT_MAX_SLIPPAGE_PERCENT"
        )
    if not 0 < MAX_SPREAD_PERCENT <= 2:
        errors.append("MAX_SPREAD_PERCENT должен быть больше 0 и не выше 2")
    if not 0 < MAX_PRICE_DRIFT_PERCENT <= 5:
        errors.append("MAX_PRICE_DRIFT_PERCENT должен быть больше 0 и не выше 5")
    if MAX_POSITION_NOTIONAL_PERCENT <= 0:
        errors.append("MAX_POSITION_NOTIONAL_PERCENT должен быть больше 0")
    if not 1 <= BYBIT_HTTP_TIMEOUT_SECONDS <= 120:
        errors.append("BYBIT_HTTP_TIMEOUT_SECONDS должен быть в диапазоне 1–120")
    if not 1_000 <= BYBIT_RECV_WINDOW_MS <= 60_000:
        errors.append("BYBIT_RECV_WINDOW_MS должен быть в диапазоне 1000–60000")
    if not 5 <= DEEPSEEK_TIMEOUT_SECONDS <= 300:
        errors.append("DEEPSEEK_TIMEOUT_SECONDS должен быть в диапазоне 5–300")
    if not 64 <= DEEPSEEK_MAX_TOKENS <= 8_192:
        errors.append("DEEPSEEK_MAX_TOKENS должен быть в диапазоне 64–8192")
    if not 1 <= DEEPSEEK_LOG_RETENTION_DAYS <= 365:
        errors.append("DEEPSEEK_LOG_RETENTION_DAYS должен быть в диапазоне 1–365")
    if not 1 <= MIN_NET_RISK_REWARD_RATIO <= 10:
        errors.append("MIN_NET_RISK_REWARD_RATIO должен быть в диапазоне 1–10")
    if not 0 <= FALLBACK_TAKER_FEE_RATE <= 0.01:
        errors.append("FALLBACK_TAKER_FEE_RATE должен быть в диапазоне 0–0.01")
    if not 0 <= TP_SL_MIN_CHANGE_PERCENT <= 5:
        errors.append("TP_SL_MIN_CHANGE_PERCENT должен быть в диапазоне 0–5")
    if not 30 <= SIGNAL_VALIDITY_SECONDS <= 300:
        errors.append("SIGNAL_VALIDITY_SECONDS должен быть в диапазоне 30–300")
    if DEEPSEEK_TIMEOUT_SECONDS * 2 + 10 > SIGNAL_VALIDITY_SECONDS:
        errors.append(
            "SIGNAL_VALIDITY_SECONDS должен покрывать две AI-попытки: "
            "минимум 2 × DEEPSEEK_TIMEOUT_SECONDS + 10"
        )
    if not TRADABLE_TOKENS:
        errors.append("TRADABLE_TOKENS не содержит допустимых токенов")
    if len(TRADABLE_TOKENS) > 12:
        errors.append("TRADABLE_TOKENS должен содержать не больше 12 токенов")
    deepseek_url = urlparse(DEEPSEEK_API_URL)
    if (
        deepseek_url.scheme != "https"
        or not deepseek_url.hostname
        or deepseek_url.username is not None
        or deepseek_url.password is not None
    ):
        errors.append(
            "DEEPSEEK_API_URL должен быть корректным HTTPS URL без userinfo"
        )
    return list(dict.fromkeys(errors))
