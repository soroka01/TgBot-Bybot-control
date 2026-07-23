# 🤖 Crypto Trading Bot for Bybit

> Одно сообщение в Telegram для контроля Bybit Unified Account: позиции, живой текстовый график, алерты, безопасный AI-отбор сетапов и опциональное автоисполнение.

🌐 **Язык:** [Русский](README.md) · [English](README_EN.md)

![Python](https://img.shields.io/badge/Python-3.10%2B-3776AB?logo=python&logoColor=white)
![Aiogram](https://img.shields.io/badge/aiogram-3.x-2CA5E0?logo=telegram&logoColor=white)
![Bybit](https://img.shields.io/badge/Bybit-V5-F7A600)
![License](https://img.shields.io/badge/License-MIT-2EA44F)

## Что это

Бот обслуживает один общий фьючерсный Bybit Unified Account и рассчитан на личный Telegram-чат владельца. Он не обещает доходность и не превращает LLM в трейдера: DeepSeek может только выбрать заранее рассчитанный кодом сетап или отказаться от него.

| Контур | Как устроен |
| --- | --- |
| Telegram | Один канонический bot message на чат; навигация и live-данные редактируют его |
| AI | Выбирает только существующий `candidate_id`; не задаёт объём, плечо, TP или SL |
| Риск | Размер позиции, комиссии, spread, slippage, net R/R и плечо считает код |
| Bybit | Динамические правила инструмента, стабильный `orderLinkId`, проверка исполнения |
| Хранилище | SQLite: профили, экраны, алерты, журнал, outbox и защита от повторного сигнала |
| Default | `TRADING_MODE=dry`; изменяющие запросы не достигают биржи |

> ⚠️ Это технический инструмент управления риском, а не финансовая рекомендация. Убыточные сделки, проскальзывание, ликвидация, сбои API и потеря капитала всё равно возможны.

## Главное

### Один Telegram-экран

- `/start` создаёт первое сообщение бота; дальше интерфейс редактирует только его.
- Временная ошибка Telegram, rate limit или сеть не создают дубликат.
- Новый bot message допускается только если Telegram точно сообщает, что старый удалён или больше не редактируется.
- Callback со старого сообщения или уже заменённой клавиатуры отклоняется.
- Все edits сериализованы per chat; старая live-задача отменяется с ожиданием завершения.
- Auto-событие показывается компактным banner поверх текущего экрана и не уничтожает открытый route.
- После рестарта сохранённый экран переводится в честное состояние «бот перезапущен».

Настоящий PNG-график намеренно не используется. Telegram позволяет перейти из text message в media message, но обратный переход неудобен, а caption ограничен 1024 символами. Поэтому экран `📈 Живой график` показывает доступный и быстрый sparkline из закрытых свечей:

```text
BTCUSDT · 15м
$117 420  +1.82% за 32 свечи
▁▂▃▃▄▅▅▆▇▆▇█
L 114.8k · EMA20 116.9k · H 118.2k
```

### AI — селектор, а не исполнитель

1. Код получает позиции, equity, bid/ask/mark, funding и закрытые свечи 3m/5m/1h/4h.
2. Код определяет режим рынка и строит допустимый кандидат с фиксированными entry reference, TP и SL.
3. В DeepSeek уходит компактный whitelist snapshot без raw Bybit response, ключей и свободного текста.
4. DeepSeek возвращает только `hold` или `select_candidate` с существующим ID; закрывать позиции модель не может.
5. Локальная строгая схема проверяет `snapshot_id`, срок действия, символы, состояния и отсутствие лишних полей.
6. Перед ордером код заново проверяет bid/ask, spread и уход цены.

Если JSON испорчен, snapshot устарел, timeframe неполон или ID выдуман, весь batch отклоняется без ордеров.

По умолчанию используется актуальная `deepseek-v4-flash` с JSON Output и отключённым thinking mode. Модель настраивается через `DEEPSEEK_MODEL`.

### Детерминированный риск

- количество рассчитывается из equity и расстояния до SL;
- модель не может влиять на quantity, leverage или бюджет риска;
- в риск и net R/R входят taker fees, текущий spread и оценка adverse slippage;
- риск открытых позиций считается от исполнимого `markPrice` до SL, а не от исторического entry;
- quantity и уровни округляются через `Decimal` по live `qtyStep` и `tickSize`;
- проверяются `minOrderQty`, `minNotionalValue`, `maxMktOrderQty`, статус инструмента и максимальное плечо;
- плечо выбирается минимально необходимое, но не выше `AUTO_LEVERAGE` и лимита инструмента;
- авто-входы разрешены только для Unified Account в режиме `REGULAR_MARGIN`; `ISOLATED_MARGIN`, `PORTFOLIO_MARGIN` и неизвестные режимы блокируются;
- новые входы также блокируются при позиции без SL, активном увеличивающем ордере, опасном статусе позиции, неподдерживаемой USDC/inverse/options экспозиции, некорректных account-wide полях баланса или достижении дневного loss guard;
- auto-stop разрешено только подтягивать, но никогда не расширять;
- выходами управляют только TP/SL, детерминированный safety guard или подтверждённая ручная команда владельца; автоматический разворот запрещён;
- один и тот же candle candidate резервируется в SQLite только один раз.

### Надёжное исполнение Bybit

- GET-запросы повторяются с backoff; state-changing POST не повторяется вслепую после timeout.
- Каждый логический ордер получает стабильный уникальный `orderLinkId`.
- При потерянном ответе бот ищет этот ID через realtime/history вместо создания второго ордера.
- Ответ `order/create` считается только асинхронным ACK.
- Успех показывается после terminal status `Filled` и проверки фактической позиции.
- Auto-entry отправляется как рыночный IOC Limit с жёсткой границей цены и прикреплёнными Full TP/SL.
- Entry дополнительно проверяет наличие TP и SL; если восстановить защиту не удалось, бот пытается аварийно закрыть позицию.
- Частично исполненный safety-exit немедленно сверяется по остатку и повторяется ограниченно; неопределённый остаток аварийно останавливает auto-mode.
- `set_trading_stop` всегда отправляет TP и SL парой с `tpslMode=Full`, Market execution и MarkPrice trigger.
- Время подписи синхронизируется с сервером Bybit; rate-limit headers учитываются.
- Позиции и активные ордера читаются с пагинацией.

## Экраны

| Экран | Содержимое | Обновление |
| --- | --- | --- |
| 📊 Позиции | Позиции, PnL, ROI, TP/SL, подтверждённое закрытие | 8с |
| 💰 Баланс | Wallet, equity, margin, available balance | 10с |
| 📈 Живой график | Sparkline, H/L, EMA20/50, RSI, ATR, spread | 15с |
| 🔍 Рынок | Цена, 24h change, режим, RSI и spread | 15с |
| 🧠 AI-сетапы | Read-only выбор и полный deterministic trade plan | По запросу |
| 🤖 Авто | Lifecycle, режим, лимиты, последний цикл и ошибка | 5с |
| 🔔 Алерты | Price/RSI crossing, once/repeat | 15с scheduler |
| 🧾 События | Личный activity log | По запросу |
| 📜 Сделки | Bybit Closed PnL | По запросу |

Торговые, account и AI callback доступны только ID из `ADMIN_TELEGRAM_IDS`. Групповые чаты блокируются. Сохранённый `is_admin` в SQLite не является источником авторизации.

## Архитектура

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
                  └─> serialized Bybit execution + reconciliation
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
  auto_trading.py       cycle and serialized side effects
  alerts.py             crossing logic
storage/database.py     SQLite repository, dedupe and notification outbox
telegram_bot/ui.py      one-message state, locks, revisions and live tasks
tests/                  network-free safety tests
```

## Установка

Требуется Python 3.10+.

```powershell
git clone https://github.com/soroka01/TgBot-Bybot-control.git
cd TgBot-Bybot-control
py -3 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
Copy-Item .env.example .env
```

`start.bat` создаёт `.venv` при необходимости, устанавливает отсутствующие зависимости и запускает Telegram UI. Для console mode используйте команды из раздела «Запуск».

## Конфигурация

`config.py` загружает локальный `.env`, но уже заданные process environment variables имеют приоритет. `.env`, ключи, SQLite, runtime logs и DeepSeek logs игнорируются Git.

Минимальный безопасный `.env`:

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

### Режимы

| Переменная | Значения | Смысл |
| --- | --- | --- |
| `BYBIT_ENV` | `testnet`, `demo`, `mainnet` | Выбирает только официальный API host |
| `TRADING_MODE` | `dry`, `live` | `dry` блокирует все изменяющие Bybit requests |
| `LIVE_TRADING_CONFIRMATION` | точная фраза | Дополнительный interlock для `live` |

Для реального исполнения нужны одновременно:

```dotenv
BYBIT_ENV=mainnet
TRADING_MODE=live
LIVE_TRADING_CONFIRMATION=I_ACCEPT_LIVE_TRADING_RISK
```

Опечатка в mode не включает LIVE: startup завершается с ошибкой. Старый `DRY_RUN` поддерживается только как migration fallback; для новой конфигурации используйте `TRADING_MODE`.

`dry` — preview без локального paper ledger: GET остаются реальными, но POST не отправляются. Для проверки реальных fills и состояния позиций используйте отдельный Bybit Demo/Testnet account, а не mainnet key.

### Основные лимиты

| Переменная | Default | Назначение |
| --- | ---: | --- |
| `TRADABLE_TOKENS` | `BTC,ETH,SOL,XRP,BNB,DOGE` | Разрешённые USDT linear assets |
| `POLL_INTERVAL` | `180` | Пауза auto-loop, секунд |
| `MAX_RISK_PER_TRADE_PERCENT` | `1` | Максимальный риск сделки от equity |
| `MAX_TOTAL_RISK_PERCENT` | `5` | Максимальный риск портфеля |
| `MAX_DAILY_LOSS_PERCENT` | `3` | Закрытый realized loss и account-scoped UTC equity drawdown от high-water |
| `MAX_POSITION_NOTIONAL_PERCENT` | `100` | Верхняя граница notional от equity |
| `AUTO_LEVERAGE` | `2` | Верхняя граница автоматически выбранного плеча |
| `MIN_NET_RISK_REWARD_RATIO` | `1.5` | Минимальный R/R после издержек |
| `MAX_SPREAD_PERCENT` | `0.15` | Запрет входа при широком spread |
| `MAX_PRICE_DRIFT_PERCENT` | `0.25` | Допустимый уход цены после snapshot |
| `BYBIT_MAX_SLIPPAGE_PERCENT` | `0.30` | Жёсткая граница цены IOC-entry и reduce-only exit |
| `SIGNAL_VALIDITY_SECONDS` | `90` | TTL AI-решения |

Полный перечень с безопасными defaults находится в [.env.example](.env.example).

## Запуск

```powershell
python main.py telegram
python main.py auto
python main.py
```

Перед запуском `auto` проверяются ключи, model ID, режим и bounds. В Telegram LIVE дополнительно требует отдельного подтверждения на экране.

## Проверка

```powershell
python -m unittest discover -s tests -v
python -m compileall -q main.py config.py api core storage telegram_bot tests utils
git diff --check
```

Тесты покрывают HMAC, malformed API responses, запрет blind POST retry, reconciliation и partial fills, paired TP/SL, динамическую precision, строгий AI contract, stale snapshot, closed candles, sparkline, risk sizing, portfolio/daily guards, durable outbox и single-message UI.

Для интеграционной проверки используйте Bybit Demo/Testnet. Автотесты репозитория не отправляют ордера.

## Runtime и приватность

| Путь | Данные |
| --- | --- |
| `data/crypto_bot.sqlite3` | Профили, экраны, алерты, outbox, activity, signal dedupe и UTC equity high-water |
| `crypto_bot.log` | Rotating runtime log, retention 10 дней |
| `api/deepseek_logs/` | Только при `DEEPSEEK_LOG_RESPONSES=true` |

DeepSeek response logging выключен по умолчанию. При включении сохраняются только model, `snapshot_id` и финальный JSON — не raw wallet context и не reasoning.

Используйте Bybit API key только с read/trade permissions и **без withdrawal permission**.

## Ограничения

- Поддерживаются `linear` USDT contracts и один процесс/один Unified Account; авто-вход требует `REGULAR_MARGIN`.
- SQLite не координирует несколько одновременно запущенных экземпляров приложения.
- REST reconciliation надёжнее принятия ACK, но private WebSocket мог бы уменьшить latency ещё сильнее.
- Auto-mode намеренно консервативен и может долго не находить сетапов.
- Telegram edit существующего сообщения обычно не создаёт полноценное push-уведомление. Алерты здесь — durable in-app banners, но не канал для событий, которые нельзя пропустить.
- CoinGecko, Telegram, DeepSeek и Bybit могут быть недоступны или менять rate limits.
- Ни один фильтр не гарантирует прибыль и не устраняет рыночный риск.

## Лицензия

[MIT](LICENSE)

---

Один экран. AI без доступа к размеру позиции. Исполнение только после локальных проверок и подтверждения биржи.
