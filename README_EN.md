# 🤖 Crypto Trading Bot for Bybit

> Control a Bybit futures account through Telegram: one live screen, risk controls, AI analysis, automation, and personal alerts.

🌐 **Language:** [Русский](README.md) · [English](README_EN.md)

![Python](https://img.shields.io/badge/Python-3.10%2B-3776AB?logo=python&logoColor=white)
![Aiogram](https://img.shields.io/badge/Telegram-aiogram%203-2CA5E0?logo=telegram&logoColor=white)
![Bybit](https://img.shields.io/badge/Exchange-Bybit%20V5-F7A600)
![License](https://img.shields.io/badge/License-MIT-green)

## ✨ Project concept

This is not a conventional chat bot that floods a conversation with replies. Each chat has **one control message**: buttons, live data, operation results, and system events edit that message in place. The chat stays clean while the user always sees the current screen.

| Area | How it works |
| --- | --- |
| 🖥️ Interface | One editable inline-keyboard screen per chat |
| 🔄 Live data | Balance, positions, and market data refresh every two seconds |
| 🧪 Safe start | `DRY_RUN=True` by default; trading POST requests are simulated |
| 👥 Multiple users | Alerts, preferences, and activity logs are isolated by Telegram chat ID |
| 🛡️ Shared account | Only administrators can access the real trading workflow |

## 🚀 Features

### 🧭 Telegram UX

- **Single-screen UI** — the bot edits the active message rather than sending a chain of new messages.
- **Live navigation** — opening another screen cancels the previous refresh task, so stale data cannot overwrite the new view.
- **No stuck buttons** — every callback is acknowledged; buttons on outdated messages guide users back to the current menu.
- **Safe slow operations** — Bybit, DeepSeek, SQLite, and other blocking calls run outside the event loop.
- **Spam-free automatic events** — auto-trading and alert notifications edit the screen and are coalesced during bursts.

### 💼 Account and positions

- 💰 Unified Account balance: wallet balance, equity, available funds, position margin, and order margin.
- 📊 Open positions: side, size, entry price, mark price, PnL, and ROI.
- 🔍 Position details with stop-loss, take-profit, leverage, and derived metrics.
- ❌ Close an individual position or all positions with a separate confirmation step.
- 📜 Closed-PnL history from Bybit.
- 🧪 In `DRY_RUN`, closing positions and creating orders are shown as simulations and are never sent to the exchange.

### 📈 Analytics and AI

- Technical indicators: **EMA, RSI, MACD, and ATR**.
- Multi-timeframe analysis: 3 minutes, 5 minutes, 1 hour, and 4 hours.
- DeepSeek AI recommendations in strict JSON with number and token validation.
- Expensive indicator caching: prices remain live without re-fetching candles every two seconds.
- 🌍 Market overview: market capitalization, 24-hour volume, BTC dominance, and trending assets.

### 🤖 Automated mode and risk

- Orders only for allowed tokens and only when the exchange minimum lot size is met.
- `Decimal`-based quantity rounding to the instrument step.
- Actual stop-loss risk is calculated instead of using only the nominal order amount.
- Risk/reward, liquidation buffer, maximum per-trade risk, and total portfolio risk checks.
- New trades are blocked when an existing position is unprotected by a stop-loss.
- Protection against pyramiding in the same direction and correct handling of an opposite position.

### 🔔 Personal alerts

- 💲 Price alerts and 📊 RSI alerts.
- Trigger direction: value **above** or **below** a selected threshold.
- **One-time** and **recurring** modes.
- Cooldown between triggers.
- Alerts fire only on an **actual crossing** of the level: creating an alert does not immediately produce noise when the price is already beyond the threshold.
- Each user can choose a default asset and RSI interval in their alert profile.
- Creation, deletion, and triggering are written to a local activity log.

### 🧾 Activity log and roles

- The log records alert creation, deletion, and triggers; auto-mode starts and stops; and position-close requests.
- `ADMIN_TELEGRAM_IDS` separates users: owners control the shared Bybit account, while everyone else can use alerts, market overview, and their private log.
- Important: users do not receive separate exchange subaccounts — all trading always belongs to the one connected Bybit account.

## 🗺️ Bot screens

| Screen | Purpose | Refresh | Access |
| --- | --- | --- | --- |
| 📊 Positions | List and details of open positions | 2 seconds | Administrator |
| 💰 Balance | Unified Account and margin | 2 seconds | Administrator |
| 📈 AI recommendations | Build and show a DeepSeek decision | On request | Administrator |
| 🔍 Market analysis | Indicators and technical context | 2 seconds / candle cache | Administrator |
| 🔔 Alerts | Create, list, and delete personal alerts | On action | Any user |
| 🌍 Market overview | Global market data and trends | 30 seconds / 90-second cache | Any user |
| 📜 Trades | Bybit closed-PnL history | On request | Administrator |
| 🧾 Activity | Personal local events | On request | Any user |
| 🤖 Auto mode | Status, start, stop, and logs | 2 seconds | Administrator |
| ⚙️ Settings | Tokens and alert profile | On action | Any user |

Open the main screen with `/start` or `/menu`.

## 🏗️ Architecture

```text
TgBot-Bybot-control/
├── api/                         # Bybit V5 and DeepSeek clients
│   ├── bybit_api.py             # Request signing, retries, dry-run
│   ├── deepseek_api.py          # AI analysis
│   └── tg_notify.py             # Events through the single screen
├── core/                        # Business logic
│   ├── auto_trading.py          # Signal execution and risk controls
│   ├── alerts.py                # Threshold crossings
│   ├── alert_scheduler.py       # One async scheduler
│   ├── market_data.py           # Indicators and candles
│   ├── market_overview.py       # External market overview
│   └── prompt_builder.py        # AI prompt contract
├── storage/
│   └── database.py              # SQLite repository and schema
├── telegram_bot/
│   ├── ui.py                    # One-message editing
│   ├── activity_middleware.py   # Profiles and role controls
│   ├── handlers/                # Telegram screens
│   └── keyboards/               # Inline keyboards
├── utils/                       # Calculations, formatting, logging
├── config.py                    # Safe defaults without secrets
├── requirements.txt
└── main.py                      # CLI entry point
```

There are no legacy parallel `features`, `services`, or `tasks` layers, and no second thread-based scheduler. The project uses one event loop, one Bybit adapter, and one trading-execution path.

## 🗃️ Database and scaling

By default, the bot creates `data/crypto_bot.sqlite3`; Git ignores it.

| Table | Stores |
| --- | --- |
| `users` | Telegram profile, role, status, locale, time zone, notification preferences, last screen, and default asset/interval |
| `alerts` | Type, symbol, threshold, direction, interval, recurring flag, cooldown, last value, and trigger count |
| `activity_log` | Personal actions, alert events, and system records |

### Why SQLite

- ✅ Reliable persistent storage without another server.
- ✅ WAL mode, foreign keys, transactions, and a busy timeout.
- ✅ A good fit for one Telegram-bot process and typical multi-user load.
- ➡️ Use PostgreSQL for multiple concurrently running instances, replication, or much higher concurrency.
- ➡️ Redis is not the source of truth for alerts; it could be added later only for caching or distributed locks.

The UI and domain logic do not write SQL directly, so storage can be replaced without rewriting the Telegram screens.

## ⚙️ Install and run

Requires **Python 3.10+**.

```powershell
pip install -r requirements.txt
python main.py
```

Launch modes:

```powershell
python main.py telegram  # Telegram bot
python main.py auto      # Automated mode from the console
python main.py --help    # Help
```

### 🔐 Process environment variables

The project **does not create or load `.env` files**, does not use `config.json`, and does not store keys in Git.

| Variable | Purpose |
| --- | --- |
| `TELEGRAM_TOKEN` | Required to run the Telegram bot |
| `BYBIT_API_KEY`, `BYBIT_API_SECRET` | Bybit private data and live trading |
| `DEEPSEEK_API_KEY` | AI recommendations |
| `ADMIN_TELEGRAM_IDS` | Comma-separated Telegram user IDs of owners |
| `DRY_RUN` | `True` by default; `False` allows real POST requests |
| `CRYPTO_DB_PATH` | Optional path to the SQLite file |

> ⚠️ Without `ADMIN_TELEGRAM_IDS`, trading buttons are intentionally unavailable to everyone. This prevents a random bot user from reaching the shared exchange account.

## 🛡️ Security and limitations

- Never share API keys in a chat, commit, or screenshot.
- Give the Bybit API the minimum permissions: read and trade, with no withdrawal permission.
- Test workflows with `DRY_RUN=True` first.
- `DRY_RUN=False` sends real trading POST requests; the account owner remains responsible for every trading decision.
- A background alert never creates a new message. If a user manually deletes the active screen, a new one appears only after their next action.
- Entering an alert threshold leaves the user's input message in Telegram — this is a platform limitation; the bot response still edits the one control screen.

## 🧪 Change verification

No production tests are included in the repository. Before publishing, the project uses buffer checks:

- Python module compilation;
- Telegram-bot import;
- SQLite schema check;
- two isolated users;
- one-time and recurring alert crossings;
- `git diff --check`.

## 📄 License

This project is available under the [MIT License](LICENSE).

```text
Copyright (c) 2026 soroka01
```

The license permits use, modification, and distribution provided that the license text and warranty disclaimer are retained.

---

💙 Built for tidy crypto-account control: fewer messages, more context, and more predictable behavior.
