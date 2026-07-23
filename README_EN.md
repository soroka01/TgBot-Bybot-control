# 🤖 Crypto Trading Bot for Bybit

> A Telegram control panel for one shared Bybit Unified futures account: live screens, explicit risk limits, personal alerts, DeepSeek analysis, and optional automation.

🌐 **Language:** [Русский](README.md) · [English](README_EN.md)

![Python](https://img.shields.io/badge/Python-3.10%2B-3776AB?logo=python&logoColor=white)
![Aiogram](https://img.shields.io/badge/aiogram-3.7%2B-2CA5E0?logo=telegram&logoColor=white)
![Bybit](https://img.shields.io/badge/Bybit-V5-F7A600)
![License](https://img.shields.io/badge/License-MIT-2EA44F)

## ✨ Overview

The bot combines monitoring of Bybit linear USDT contracts, manual actions, AI analysis, an automated trading loop, and personal alerts in one Telegram interface. It tries to maintain one editable bot message per chat instead of producing a chain of replies.

| Area | Implementation |
| --- | --- |
| 🖥️ Interface | Inline keyboards and one persisted screen per chat |
| 💼 Exchange scope | One connected Bybit Unified Account shared by all administrators |
| 🧪 Safe default | `DRY_RUN=True`: state-changing Bybit POST requests are simulated |
| 👥 User data | Preferences, alerts, and activity are separated by Telegram `chat_id` |
| 🗃️ Storage | Local SQLite with WAL, foreign keys, and a busy timeout |

> ⚠️ The current authorization model is intended for **private chats with trusted users**. Do not connect untrusted users or group chats to an instance that can access a real exchange account.

## 🚀 Features

### 🧭 Telegram UX

- `/start` and `/menu` open the main screen.
- Balance, positions, technical analysis, market overview, and auto-mode status refresh in the same message.
- Navigating to another screen cancels the previous live task so stale data cannot overwrite the new view.
- Slow calls from Telegram handlers run in worker threads and do not block the polling event loop.
- An unknown callback is handled by a fallback screen that links back to the main menu.

### 💼 Account and positions

- Unified Account balance: wallet balance, equity, available funds, and margin.
- Open-position list and details: side, size, entry/mark price, PnL, ROI, TP, SL, and leverage.
- Close one or all positions after a separate confirmation.
- Closed PnL history through Bybit V5.
- In `DRY_RUN`, closes, orders, leverage, and TP/SL changes are not sent to the exchange.

### 📈 Analytics and DeepSeek

- EMA, RSI, MACD, and ATR.
- 3m, 5m, 1h, and 4h timeframes.
- A 60-second candle-analysis cache while the current price remains live.
- DeepSeek JSON responses with structure, token, and numeric-field validation.
- CoinGecko overview: global market capitalization, volume, BTC dominance, and trending assets.

### 🤖 Automated mode and risk

- `hold`, `close`, `long`, and `short` signals for allowed assets.
- `Decimal` quantity rounding to the configured instrument step.
- Minimum-order, actual stop-loss risk, and risk/reward validation.
- Per-trade and total-portfolio risk limits.
- Stop-loss checks against the liquidation price.
- New entries are blocked while an existing position has no protective stop-loss.
- Protection against another entry in the same direction and handling of an opposite position.

### 🔔 Alerts and activity

- Price and RSI alerts above or below a threshold.
- One-time or repeating mode with a cooldown.
- Alerts trigger only on an actual threshold crossing.
- Personal default symbol, RSI interval, and notification toggles.
- Local history for alert creation/deletion/triggers, position actions, and auto-mode controls.

## 🗺️ Screens and access

| Screen | Purpose | Refresh | Access |
| --- | --- | --- | --- |
| 📊 Positions | Open positions and details | 2 seconds | Administrator |
| 💰 Balance | Unified Account and margin | 2 seconds | Administrator |
| 📈 AI recommendations | A DeepSeek decision without automatic execution | On request | Administrator |
| 🔍 Market analysis | Indicators and multi-timeframe context | 2 seconds; analysis cached for 60 seconds | Administrator |
| 📜 Trades | Bybit Closed PnL | On request | Administrator |
| 🤖 Auto mode | Status, start/stop, and local log tail | 2 seconds | Administrator |
| 🔔 Alerts | Create, list, and delete | On action | Any user |
| 🌍 Market overview | CoinGecko global/trending data | 30 seconds; 90-second cache | Any user |
| 🧾 Activity | Personal local events | On request | Any user |
| ⚙️ Settings | Asset and personal alert settings | On action | Any user |

## 🔄 Execution flow

```text
Telegram update
  └─> activity / access / live-screen middleware
       └─> handler + telegram_bot/ui.py
            ├─> api/bybit_api.py ──────> Bybit V5
            ├─> api/deepseek_api.py ───> DeepSeek API
            ├─> core/market_overview.py ──> CoinGecko
            ├─> core/* ────────────────> analysis, alerts, risk, auto mode
            └─> storage/database.py ───> SQLite

Telegram polling event loop
  ├─> async alert scheduler
  └─> dedicated daemon thread for the optional auto-trading loop
```

`BybitAPI` is the single exchange-adapter implementation, but handlers and background services create separate client instances when needed.

## 🏗️ Architecture

```text
TgBot-Bybot-control/
├── api/
│   ├── bybit_api.py             # Bybit V5 signing, reads, POSTs, and dry-run
│   ├── deepseek_api.py          # OpenAI-compatible DeepSeek client and local logs
│   └── tg_notify.py             # Auto-mode events routed to Telegram UI
├── core/
│   ├── alert_scheduler.py       # Async alert lifecycle
│   ├── alerts.py                # Price/RSI crossing logic
│   ├── auto_trading.py          # Signals, execution, and risk checks
│   ├── market_data.py           # Candles and indicators
│   ├── market_overview.py       # Cached CoinGecko overview
│   └── prompt_builder.py        # DeepSeek response contract
├── storage/database.py          # SQLite schema and repository
├── telegram_bot/
│   ├── bot.py                   # Polling entrypoint
│   ├── activity_middleware.py   # Profiles and trading access
│   ├── handlers/                # Telegram screens and actions
│   ├── keyboards/               # Inline keyboards
│   └── ui.py                    # Single-message rendering and live tasks
├── utils/                       # Formatting, calculations, and logging
├── config.py                    # Environment-backed runtime settings
├── main.py                      # CLI mode selector
├── start.bat                    # Windows Telegram launcher
└── run.bat                      # Windows interactive launcher
```

## ⚙️ Installation

Requires **Python 3.10+**.

```powershell
git clone https://github.com/soroka01/TgBot-Bybot-control.git
cd TgBot-Bybot-control
py -3 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
```

On Linux/macOS, activate the environment with `source .venv/bin/activate`.

## 🔐 Configuration

The project reads secrets only from the **process environment**. It does not load `.env` or `config.json`.

Minimal PowerShell example for the Telegram UI:

```powershell
$env:TELEGRAM_TOKEN="replace-with-telegram-token"
$env:ADMIN_TELEGRAM_IDS="123456789"
$env:BYBIT_API_KEY="replace-with-read-trade-key"
$env:BYBIT_API_SECRET="replace-with-api-secret"
$env:DEEPSEEK_API_KEY="replace-with-deepseek-key"
$env:DRY_RUN="True"
python main.py telegram
```

Equivalent Bash example:

```bash
export TELEGRAM_TOKEN="replace-with-telegram-token"
export ADMIN_TELEGRAM_IDS="123456789"
export BYBIT_API_KEY="replace-with-read-trade-key"
export BYBIT_API_SECRET="replace-with-api-secret"
export DEEPSEEK_API_KEY="replace-with-deepseek-key"
export DRY_RUN="True"
python main.py telegram
```

| Variable | Default | Purpose |
| --- | --- | --- |
| `TELEGRAM_TOKEN` | empty | Required for Telegram polling |
| `ADMIN_TELEGRAM_IDS` | empty | Comma-separated Telegram user IDs; without them new users receive no trading access |
| `BYBIT_API_KEY` | empty | Required for private balance/position reads and the trading loop |
| `BYBIT_API_SECRET` | empty | Signing secret for private Bybit requests |
| `DEEPSEEK_API_KEY` | empty | Required for AI recommendations and automated analysis |
| `DEEPSEEK_API_URL` | `https://api.deepseek.com/v1` | OpenAI-compatible endpoint |
| `DRY_RUN` | `True` | Simulates state-changing Bybit POST requests when true |
| `POLL_INTERVAL` | `180` | Delay between auto-mode cycles, in seconds |
| `MAX_LEVERAGE` | `10` | Maximum permitted leverage |
| `MAX_RISK_PER_TRADE_PERCENT` | `2` | Maximum risk per trade as a percentage of equity |
| `MAX_TOTAL_RISK_PERCENT` | `10` | Maximum total risk as a percentage of equity |
| `MIN_ORDER_SIZE_USDT` | `10` | Minimum nominal order value |
| `CRYPTO_DB_PATH` | `data/crypto_bot.sqlite3` | SQLite file path |

The assets (`BTC`, `ETH`, `SOL`, `XRP`, `BNB`, `DOGE`) and Bybit `linear` category are defined in `config.py`.

### `DRY_RUN` semantics

`DRY_RUN=True` prevents **POST requests** that change Bybit state. Public and private GET requests remain real, so balance/positions and the complete auto loop still require valid Bybit credentials. This is a local simulation, not Bybit testnet; the API host is fixed to production in the current version.

## ▶️ Running

```powershell
python main.py telegram  # Telegram UI
python main.py auto      # Auto loop in this process, without Telegram polling
python main.py           # Interactive selector
python main.py --help
```

Windows launchers:

- `start.bat` enters the project directory, creates `.venv` when missing, installs `requirements.txt`, and runs `python main.py telegram`.
- `run.bat` performs the same bootstrap and starts interactive `python main.py`.

Environment variables must be present before a `.bat` file starts. In standalone `auto` mode the Telegram UI is not registered, so events remain in the local log.

## 🗃️ Runtime data

| Path | Contents |
| --- | --- |
| `data/crypto_bot.sqlite3` | Profiles, roles, preferences, screens, alerts, and activity |
| `crypto_bot.log` | Rotating general runtime log |
| `api/deepseek_logs/` | Part of the context, reasoning when available, and DeepSeek responses |

Git ignores these files, but they can still contain sensitive account and trading context. Protect the project directory and its backups.

## 🛡️ Access model and security

- One process connects to **one shared Bybit account**; users do not receive subaccounts.
- Use the bot only in private chats. Profiles and roles are stored by `chat_id`, so the current schema does not safely separate members of a group chat.
- An administrator role is persisted in SQLite. Removing an ID from `ADMIN_TELEGRAM_IDS` does not revoke an already stored `is_admin`; revocation also requires clearing or updating the relevant database row.
- Auto-mode events currently go to every persisted active screen, not administrators only. Do not connect untrusted users to a trading instance.
- Use a Bybit key with read/trade permissions only and **no withdrawal permission**.
- Never put keys in Telegram, an issue, a commit, a screenshot, or a log.
- DeepSeek receives prepared market and account context; account for this when choosing data and a provider.
- Verify workflows with `DRY_RUN=True` first. `DRY_RUN=False` enables real operations.

## ⚠️ Limitations

- Only the `linear` USDT instruments defined in code are supported.
- The alert scheduler runs in the polling event loop, while auto-trading uses a separate daemon thread: this is one process, not one thread.
- SQLite is intended for one instance. Multiple concurrent processes need different coordination and storage.
- CoinGecko, DeepSeek, and Bybit can enforce rate limits or become temporarily unavailable.
- A background alert edits an existing screen and does not create a replacement if the user deleted it manually.
- Entering an alert threshold leaves the user's input as a separate Telegram message.

## 🧪 Verification

The repository currently has no automated test suite or CI. Changes need at least:

- Python syntax checks;
- a Telegram entrypoint import with a test environment;
- a temporary SQLite schema check;
- scenarios with two isolated private chats;
- one-time and repeating alert-crossing scenarios;
- a manual `DRY_RUN` check;
- `git diff --check`.

These are required smoke-check areas, not an automatically executed pipeline.

## 📄 License

Distributed under the [MIT License](LICENSE).

---

💙 One screen, explicit limits, and as much context as possible before a trading action.
