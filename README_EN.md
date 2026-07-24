# 🤖 Crypto Trading Bot for Bybit

> One Telegram message for controlling a Bybit Unified Account: positions, durable trade history and statistics, a live text chart, alerts, safe AI setup selection, and optional automated execution.

🌐 **Language:** [Русский](README.md) · [English](README_EN.md)

![Python](https://img.shields.io/badge/Python-3.10%2B-3776AB?logo=python&logoColor=white)
![Aiogram](https://img.shields.io/badge/aiogram-3.x-2CA5E0?logo=telegram&logoColor=white)
![Bybit](https://img.shields.io/badge/Bybit-V5-F7A600)
![License](https://img.shields.io/badge/License-MIT-2EA44F)

## What it is

The bot controls one shared Bybit Unified futures account and is designed for the owner's private Telegram chat. It does not promise returns and does not turn an LLM into a trader: DeepSeek may only select a setup precomputed by local code or decline it.

| Layer | Design |
| --- | --- |
| Telegram | One canonical bot message per chat; navigation and live data edit it |
| AI | Selects an existing `candidate_id`; cannot set quantity, leverage, TP, or SL |
| Risk | Local code calculates size, fees, spread, slippage, net R/R, and leverage |
| Bybit | Live instrument rules, stable `orderLinkId`, and execution reconciliation |
| Storage | SQLite for exact entry plans, Closed PnL, equity snapshots, profiles, alerts, and outbox |
| Default | `TRADING_MODE=dry`; mutating requests never reach the exchange |

> ⚠️ This is a technical risk-control tool, not financial advice. Losing trades, slippage, liquidation, API failures, and loss of capital remain possible.

## Highlights

### One Telegram screen

- `/start` creates the first bot message; subsequent UI operations edit it.
- A temporary Telegram failure, rate limit, or network error never creates a duplicate.
- A replacement bot message is allowed only when Telegram explicitly reports that the old one is gone or cannot be edited.
- Callbacks from an old message or replaced keyboard are rejected.
- Edits are serialized per chat; stale live tasks are cancelled and awaited.
- Auto events appear as a small banner without destroying the current route.
- After restart, a persisted screen is changed to an honest “bot restarted” state.

A PNG chart is intentionally not used. Telegram can convert text to media, but returning to text is awkward and media captions are limited to 1,024 characters. Market and trade screens therefore use accessible, fast sparklines built from confirmed closed candles and cumulative PnL:

```text
BTCUSDT · 15m
$117,420  +1.82% over 32 candles
▁▂▃▃▄▅▅▆▇▆▇█
L 114.8k · EMA20 116.9k · H 118.2k
```

### AI is a selector, not an executor

1. Code loads positions, equity, bid/ask/mark, funding, and closed 3m/5m/1h/4h candles.
2. Code determines the regime and builds an allowed setup with fixed entry reference, TP, and SL.
3. DeepSeek receives a compact allow-listed snapshot without raw Bybit responses, keys, or free-form text.
4. DeepSeek returns only `hold` or `select_candidate` with an existing ID; it cannot close positions.
5. A strict local schema checks the `snapshot_id`, expiry, symbols, states, and extra fields.
6. Bid/ask, spread, and price drift are checked again immediately before an order.

Malformed JSON, an expired snapshot, incomplete timeframes, or an invented ID rejects the complete batch without orders.

The default is the current `deepseek-v4-flash` model with JSON Output and thinking mode disabled. Override it through `DEEPSEEK_MODEL`.

### Deterministic risk

- quantity comes from equity and stop distance;
- the model cannot influence quantity, leverage, or risk budget;
- taker fees, current spread, and estimated adverse slippage are included in risk and net R/R;
- open-position risk is measured from executable `markPrice` to SL, not historical entry;
- quantity and price levels use `Decimal` with live `qtyStep` and `tickSize`;
- `minOrderQty`, `minNotionalValue`, `maxMktOrderQty`, instrument status, and max leverage are enforced;
- leverage is the minimum required, capped by `AUTO_LEVERAGE` and the instrument;
- automated entries are allowed only for a Unified Account in `REGULAR_MARGIN`; `ISOLATED_MARGIN`, `PORTFOLIO_MARGIN`, and unknown modes are blocked;
- new entries are also blocked by an unprotected position, exposure-increasing open order, unsafe position status, unsupported USDC/inverse/options exposure, malformed account-wide balance fields, or the daily loss guard;
- automatic stops may tighten but never widen;
- exits are owned only by TP/SL, deterministic safety guards, or an owner-confirmed manual action; implicit reversals are prohibited;
- each candle candidate can be reserved only once in SQLite;
- the final plan, AI reason, snapshot, and sizing context are committed before `create-order`; failure of this mandatory write blocks a new LIVE entry.

### Reliable Bybit execution

- GET requests use backoff; mutating POST requests are never blindly retried after a timeout.
- Every logical order has a stable unique `orderLinkId`.
- A lost response triggers lookup by that ID through realtime/history instead of a second order.
- `order/create` is treated as an asynchronous acknowledgement only.
- Success is shown after terminal `Filled` status and actual position verification.
- Auto entry uses a marketable IOC Limit with a hard price boundary and attached Full TP/SL.
- An entry is also checked for TP and SL; if protection cannot be restored, the bot attempts an emergency close.
- A partially filled safety exit is immediately reconciled and retried a bounded number of times; an uncertain remainder fail-stops auto mode.
- `set_trading_stop` always sends paired TP and SL with `tpslMode=Full`, Market execution, and MarkPrice triggers.
- Signature time is synchronized with Bybit and rate-limit headers are respected.
- Positions, active orders, and Closed PnL are paginated.

## Screens

| Screen | Content | Refresh |
| --- | --- | --- |
| 📊 Positions | Positions, PnL, ROI, TP/SL, confirmed close | 8s |
| 💰 Balance | Wallet, equity, margin, available balance | 10s |
| 📈 Live chart | Sparkline, H/L, EMA20/50, RSI, ATR, spread | 15s |
| 🔍 Market | Price, 24h change, regime, RSI, and spread | 15s |
| 🧠 AI setups | Read-only selection and deterministic trade plan | On request |
| 🤖 Auto | Lifecycle, mode, limits, last cycle, and error | 5s |
| 🔔 Alerts | Price/RSI crossing, once/repeat | 15s scheduler |
| 🧾 Activity | Personal activity log | On request |
| 📜 Trades | PnL curve, statistics, and recent trades over 1D–1Y | On request + 15m sync |

Trading, account, and AI callbacks are restricted to IDs in `ADMIN_TELEGRAM_IDS`. Group chats are blocked. Persisted SQLite `is_admin` is never an authorization source.

### Trade history and statistics

- Periods are rolling `24/7/14/30/90/180/365` days, with a switch between bot-attributed trades and the complete linear USDT account.
- Bybit Closed PnL is fetched through every cursor page in windows no wider than seven days. The auto loop refreshes the latest seven days every 15 minutes, while the screen backfills the selected period on demand.
- `closedPnl` is the authoritative net PnL: Bybit already includes trading fees and funding. `openFee`/`closeFee` are retained as a cost breakdown and are never subtracted twice.
- Partial closes attributed to one bot candidate are grouped into one logical trade. External and manual account closes remain individual exchange records.
- The audit layer calculates net/gross PnL, W/L/BE, win rate, profit factor, expectancy, median, average win/loss, payoff, drawdown/recovery, streaks, fees, turnover, hold time, R/SQN, Long/Short, and per-symbol statistics. Telegram keeps a compact subset plus five recent trades, and exposes a metric only when enough data exists.
- SQLite retains the original Bybit record, exact local plan, snapshot, selector decision, and sizing context for later auditing without overloading Telegram.
- Equity change and account-level drawdown percentages appear only after equity snapshots exist. They are not cash-flow adjusted, so Closed PnL remains the primary strategy measure. Sharpe/Sortino are intentionally omitted without a sufficient, correctly sampled daily equity curve.

## Architecture

```text
Telegram update
  └─> private-chat/access/stale-screen guards
       └─> single-message Screen Manager
            ├─> handlers ───────────────> read screens / confirmed actions
            ├─> alert scheduler ────────> durable notification outbox
            └─> atomic auto worker
                  ├─> closed-candle features
                  ├─> deterministic candidates
                  ├─> DeepSeek selector
                  ├─> deterministic risk engine
                  ├─> durable trade plan before entry
                  └─> serialized Bybit execution + reconciliation

Bybit Closed PnL ──> 7-day cursor sync ──> SQLite trade journal
                                               └─> Decimal analytics ──> one-message screen
```

```text
api/
  bybit_api.py          signing, metadata, pagination, orders, reconciliation
  deepseek_api.py       current model, JSON Output, bounded/private logging
core/
  decision_engine.py    snapshot, candidates, strict AI schema
  risk_engine.py        Decimal sizing, costs, gates, portfolio risk
  market_data.py        closed candles and technical features
  chart.py              text sparkline
  trade_journal.py       account-scoped Closed PnL sync and entry audit trail
  trade_analytics.py     Decimal metrics and partial-close grouping
  auto_trading.py       cycle and serialized side effects
  alerts.py             crossing logic
storage/database.py     SQLite repository, trade history, equity, and outbox
telegram_bot/ui.py      one-message state, locks, revisions, and live tasks
telegram_bot/handlers/
  history.py            compact period/scope performance screen
tests/                  network-free safety tests
```

## Installation

Python 3.10+ is required.

```powershell
git clone https://github.com/soroka01/TgBot-Bybot-control.git
cd TgBot-Bybot-control
py -3 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
Copy-Item .env.example .env
```

`start.bat` creates `.venv` when needed, installs missing dependencies, and launches the Telegram UI. Use the commands in “Running” for console modes.

## Configuration

`config.py` loads a local `.env`, while existing process environment variables take precedence. `.env`, keys, SQLite, runtime logs, and DeepSeek logs are ignored by Git.

Minimal safe `.env`:

```dotenv
TELEGRAM_TOKEN=...
ADMIN_TELEGRAM_IDS=123456789

BYBIT_API_KEY=...
BYBIT_API_SECRET=...
BYBIT_ENV=testnet
TRADING_MODE=dry

DEEPSEEK_API_KEY=...
DEEPSEEK_MODEL=deepseek-v4-flash
```

### Modes

| Variable | Values | Meaning |
| --- | --- | --- |
| `BYBIT_ENV` | `testnet`, `demo`, `mainnet` | Selects an allow-listed official API host |
| `TRADING_MODE` | `dry`, `live` | `dry` blocks every mutating Bybit request |
| `LIVE_TRADING_CONFIRMATION` | exact phrase | Additional interlock for `live` |

Real execution requires all three:

```dotenv
BYBIT_ENV=mainnet
TRADING_MODE=live
LIVE_TRADING_CONFIRMATION=I_ACCEPT_LIVE_TRADING_RISK
```

A typo cannot enable LIVE; startup fails instead. Legacy `DRY_RUN` is accepted only as a migration fallback. New configurations should use `TRADING_MODE`.

`dry` is a preview without fabricated paper PnL: GET requests are real but POST requests are blocked. The selector decision and calculated plan are kept for auditing but excluded from real-trade performance. Use a separate Bybit Demo/Testnet account for actual fills and position state, never a mainnet key.

### Main limits

| Variable | Default | Purpose |
| --- | ---: | --- |
| `TRADABLE_TOKENS` | `BTC,ETH,SOL,XRP,BNB,DOGE` | Allowed linear USDT assets |
| `POLL_INTERVAL` | `180` | Auto-loop delay in seconds |
| `MAX_RISK_PER_TRADE_PERCENT` | `1` | Maximum trade risk as equity percentage |
| `MAX_TOTAL_RISK_PERCENT` | `5` | Maximum portfolio risk |
| `MAX_DAILY_LOSS_PERCENT` | `3` | Closed realized loss and account-scoped UTC equity drawdown from its high-water |
| `MAX_POSITION_NOTIONAL_PERCENT` | `100` | Maximum notional relative to equity |
| `AUTO_LEVERAGE` | `2` | Cap for automatically selected leverage |
| `MIN_NET_RISK_REWARD_RATIO` | `1.5` | Minimum R/R after costs |
| `MAX_SPREAD_PERCENT` | `0.15` | Reject entries with a wide spread |
| `MAX_PRICE_DRIFT_PERCENT` | `0.25` | Maximum movement after snapshot |
| `BYBIT_MAX_SLIPPAGE_PERCENT` | `0.30` | Hard price boundary for IOC entries and reduce-only exits |
| `SIGNAL_VALIDITY_SECONDS` | `90` | AI decision TTL |

See [.env.example](.env.example) for the complete safe template.

## Running

```powershell
python main.py telegram
python main.py auto
python main.py
```

Before auto mode starts, the application validates keys, model ID, mode, and bounds. Telegram LIVE mode also requires an in-screen confirmation.

## Verification

```powershell
python -m unittest discover -s tests -v
python -m compileall -q main.py config.py api core storage telegram_bot tests utils
git diff --check
```

Tests cover HMAC, malformed API responses, blind-POST prevention, reconciliation and partial fills, paired TP/SL, dynamic precision, strict AI contracts, stale snapshots, closed candles, journal-before-entry, idempotent seven-day backfill, Decimal metrics, sparkline rendering, risk sizing, portfolio/daily guards, durable outbox, and the single-message UI.

Use Bybit Demo/Testnet for integration checks. Repository tests never submit orders.

## Runtime data and privacy

| Path | Data |
| --- | --- |
| `data/crypto_bot.sqlite3` | Entry plans, raw Closed PnL, sync watermarks, equity snapshots, profiles, screens, alerts, outbox, and activity |
| `crypto_bot.log` | Rotating runtime log with 10-day retention |
| `api/deepseek_logs/` | Only when `DEEPSEEK_LOG_RESPONSES=true` |

DeepSeek response logging is disabled by default. When enabled, only the model, `snapshot_id`, and final JSON are persisted—not raw wallet context or reasoning.

The trade journal is scoped by the official Bybit environment and numeric account UID. API key/secret and the full `/v5/user/query-api` response are never stored; only a one-way SHA-256 key fingerprint remains for verified offline-cache access. The SQLite file contains sensitive trading history: do not publish it, and include it in backups.

Use a Bybit API key with read/trade permissions and **without withdrawal permission**.

## Limitations

- The bot supports linear USDT contracts and one process/one Unified Account; automated entries require `REGULAR_MARGIN`.
- SQLite does not coordinate multiple simultaneously running application instances.
- REST reconciliation is safer than trusting an ACK, but a private WebSocket could further reduce latency.
- Initial statistics are limited by available Bybit Closed PnL and locally accumulated snapshots; the bot backfills at most the selected year and does not later delete those trade records.
- Auto mode is deliberately conservative and may find no setup for long periods.
- Editing an existing Telegram message usually does not produce a full push notification. Alerts are durable in-app banners, not a must-not-miss channel.
- CoinGecko, Telegram, DeepSeek, and Bybit may be unavailable or change rate limits.
- No filter guarantees profit or removes market risk.

## License

[MIT](LICENSE)

---

One screen. AI cannot size positions. Execution happens only after local checks and exchange confirmation.
