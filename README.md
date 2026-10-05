# 🤖 Crypto Trading Bot for Bybit

> Telegram-панель управления Bybit Unified Account с AI-выбором сетапов и жёстким контролем риска в коде.

🌐 **Язык / Language:** [Русский](README.md) · [English](README_EN.md)

![Python](https://img.shields.io/badge/Python-3.14%2B-3776AB?logo=python&logoColor=white)
![aiogram](https://img.shields.io/badge/aiogram-3.30%2B-2CA5E0?logo=telegram&logoColor=white)
![DeepSeek](https://img.shields.io/badge/AI-DeepSeek-4D6BFE)
![Bybit](https://img.shields.io/badge/Exchange-Bybit-F7A600)
![License](https://img.shields.io/badge/License-MIT-green)

## 📌 Overview

Бот показывает в Telegram позиции, историю сделок, графики и алерты по Bybit Unified Account. DeepSeek выбирает из торговых вариантов, рассчитанных кодом; риск и исполнение контролирует код. По умолчанию включён режим `dry` без отправки изменяющих торговых запросов.

> [!WARNING]
> Ни один фильтр не гарантирует прибыль и не устраняет рыночный риск. Режим `live` отправляет реальные ордера.

## ✨ Features

- 📊 Позиции с PnL, ROI, TP/SL и подтверждаемым закрытием; баланс (wallet, equity, margin).
- 📈 Живой график (свечи, EMA20/50, объём) и обзор рынка (цена, 24h change, режим, RSI, spread).
- 🧠 AI-сетапы: read-only выбор и полный детерминированный торговый план.
- 🤖 Авто-режим с лимитами риска, execution gates и защитой позиций.
- 🔔 Алерты по цене и RSI (once/repeat) и личный журнал событий.
- 📜 История и статистика сделок за 1Д–1ГОД на основе Bybit Closed PnL.
- 🛡️ Режимы `dry` / `live` с отдельным подтверждением для реального исполнения.

## 🏗️ How it works

```mermaid
flowchart TD
    A["Market data"] --> B["Calculated setups"]
    B --> C["AI selection"]
    C --> D["Risk checks"]
    D --> E["dry / Bybit execution"]
    E --> F["SQLite + Telegram"]
```

## 🚀 Quick start

### Requirements

- Python 3.14+
- Telegram-бот (токен от @BotFather) и ваш числовой Telegram ID
- Bybit API key (Read + Contract Trading, **без права вывода**)
- Ключ DeepSeek API

### Installation

```powershell
git clone https://github.com/soroka01/TgBot-Bybot-control.git
cd TgBot-Bybot-control
py -3 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
Copy-Item .env.example .env
```

### Configuration

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

### Run

```powershell
python main.py telegram   # Telegram UI
python main.py auto       # авто-режим в консоли
python main.py            # интерактивный выбор режима
```

`start.bat` создаёт `.venv` при необходимости, устанавливает отсутствующие зависимости и запускает Telegram UI. Перед запуском проверяются ключи, model ID, режим и bounds. В Telegram LIVE дополнительно требует отдельного подтверждения на экране.

## ⚙️ Configuration

`config.py` загружает локальный `.env`, но уже заданные process environment variables имеют приоритет. Полный перечень с безопасными defaults — в [.env.example](.env.example).

### Режимы

| Переменная | Значения | Описание |
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

`dry` — preview без выдуманного paper PnL: GET остаются реальными, но POST не отправляются. Решение и рассчитанный план сохраняются для аудита, но не входят в статистику реальных сделок. Для проверки настоящих fills используйте отдельный Bybit Demo/Testnet account, а не mainnet key.

### Основные лимиты

| Переменная | Default | Описание |
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

### Экраны Telegram

| Экран | Содержимое | Обновление |
| --- | --- | --- |
| 📊 Позиции | Позиции, PnL, ROI, TP/SL, подтверждённое закрытие | 8с |
| 💰 Баланс | Wallet, equity, margin, available balance | 10с |
| 📈 Живой график | Свечи, EMA20/50, объём, live price и 14D low | 30с |
| 🔍 Рынок | Цена, 24h change, режим, RSI и spread | 15с |
| 🧠 AI-сетапы | Read-only выбор и полный deterministic trade plan | По запросу |
| 🤖 Авто | Lifecycle, режим, лимиты, последний цикл и ошибка | 5с |
| 🔔 Алерты | Price/RSI crossing, once/repeat | 15с scheduler |
| 🧾 События | Личный activity log | По запросу |
| 📜 Сделки | PnL-кривая, статистика и последние сделки за 1Д–1ГОД | По запросу + sync 15м |

### История и статистика сделок

- Периоды — скользящие `24/7/14/30/90/180/365` суток; можно переключить сделки бота и весь USDT linear account.
- Bybit Closed PnL загружается всеми cursor-страницами в окнах не шире 7 дней. Auto-loop обновляет последние 7 дней раз в 15 минут, экран при необходимости достраивает историю до выбранного периода.
- `closedPnl` считается авторитетным net PnL: Bybit уже включил комиссии и funding. `openFee`/`closeFee` сохраняются как расшифровка и повторно не вычитаются.
- Частичные закрытия одного bot candidate объединяются в одну сделку; внешние и ручные закрытия остаются отдельными exchange records.
- Считаются net/gross PnL, W/L/BE, win rate, profit factor, expectancy, median, payoff, drawdown/recovery, серии, комиссии, оборот, удержание, R/SQN, Long/Short и статистика по инструментам. Telegram показывает компактный срез и последние пять сделок; метрика появляется только при достаточных данных.
- В SQLite сохраняются исходная запись Bybit, локальный план, snapshot, решение селектора и sizing context для последующего аудита.
- Изменение equity и drawdown в процентах строятся только по накопленным equity snapshots и не корректируются на ввод/вывод средств, поэтому основным остаётся Closed PnL. Sharpe/Sortino не показываются без достаточной дневной equity-кривой.
- Для UTA 2.0 isolated margin пустые account-wide margin/available fields не считаются ошибкой equity-snapshot: available вычисляется из USDT coin fields, нулевой equity пропускается. Строгая проверка торгового контура не ослабляется.

## 🗂️ Project structure

```text
.
├── main.py              # Точка входа: telegram / auto / интерактивное меню
├── config.py            # Загрузка и валидация настроек из .env
├── start.bat            # Windows-лаунчер (venv, зависимости, запуск Telegram UI)
├── .env.example         # Шаблон конфигурации без секретов
├── requirements.txt     # Зависимости
├── api/                 # Клиенты Bybit, DeepSeek и Telegram-уведомлений
├── core/                # Рынок, сетапы, риск, алерты, аналитика сделок
│   └── auto/            # Авто-режим: цикл, gates, исполнение, защита позиций
├── storage/             # SQLite-хранилище
├── telegram_bot/        # aiogram: handlers, keyboards, UI-экраны
├── tools/               # Вспомогательные скрипты (backtest, загрузка истории)
├── utils/               # Логгер и хелперы
└── data/                # Локальная БД (создаётся при работе, не в Git)
```

## 🔒 Security & privacy

- Торговые, account и AI callback доступны только ID из `ADMIN_TELEGRAM_IDS`; групповые чаты блокируются. Сохранённый `is_admin` в SQLite не является источником авторизации.
- Используйте Bybit API key только с read/trade permissions и **без withdrawal permission**.
- `.env`, ключи, SQLite, журналы и DeepSeek logs игнорируются Git.
- Логирование ответов DeepSeek выключено по умолчанию (`DEEPSEEK_LOG_RESPONSES`); при включении сохраняются только model, `snapshot_id` и финальный JSON — не raw wallet context и не reasoning.
- Trade journal привязан к official Bybit environment и числовому account UID. API key/secret и полный ответ `/v5/user/query-api` в БД не сохраняются; остаётся только односторонний SHA-256 fingerprint ключа.
- SQLite-файл содержит чувствительную торговую историю: не публикуйте его и включите в резервное копирование.

| Путь | Данные |
| --- | --- |
| `data/crypto_bot.sqlite3` | Планы входа, raw Closed PnL, sync watermarks, equity snapshots, профили, экраны, алерты, outbox и activity |
| `crypto_bot.log` | Rotating журнал, retention 10 дней |
| `api/deepseek_logs/` | Только при `DEEPSEEK_LOG_RESPONSES=true` |

## ⚠️ Limitations

- Поддерживаются `linear` USDT contracts и один процесс/один Unified Account; авто-вход требует `REGULAR_MARGIN`.
- SQLite не координирует несколько одновременно запущенных экземпляров.
- Статистика ограничена доступным Bybit Closed PnL и локально накопленными snapshots; бот загружает максимум выбранный год и затем записи не удаляет.
- Auto-mode намеренно консервативен и может долго не находить сетапов.
- Telegram edit существующего сообщения обычно не создаёт push-уведомление: алерты — durable in-app banners, а не канал для критичных событий.
- CoinGecko, Telegram, DeepSeek и Bybit могут быть недоступны или менять rate limits.

## 📄 License

[MIT](LICENSE).

## 💬 Support

Можно [форкнуть репозиторий](https://github.com/soroka01/TgBot-Bybot-control/fork) и доработать под себя. Если проект пригодился, поставьте [Star](https://github.com/soroka01/TgBot-Bybot-control) — так я увижу, что он был кому-то полезен.

---

with love ❤️
