# 🤖 Crypto Trading Bot for Bybit

> Telegram-панель для одного общего фьючерсного Bybit Unified Account: live-экраны, явные лимиты риска, личные алерты, анализ DeepSeek и опциональная автоматизация.

🌐 **Язык:** [Русский](README.md) · [English](README_EN.md)

![Python](https://img.shields.io/badge/Python-3.10%2B-3776AB?logo=python&logoColor=white)
![Aiogram](https://img.shields.io/badge/aiogram-3.7%2B-2CA5E0?logo=telegram&logoColor=white)
![Bybit](https://img.shields.io/badge/Bybit-V5-F7A600)
![License](https://img.shields.io/badge/License-MIT-2EA44F)

## ✨ Обзор

Бот объединяет наблюдение за линейными USDT-контрактами Bybit, ручные действия, AI-анализ, автоматический торговый цикл и персональные алерты в одном Telegram-интерфейсе. Для каждого чата бот старается поддерживать одно редактируемое сообщение вместо цепочки ответов.

| Область | Реализация |
| --- | --- |
| 🖥️ Интерфейс | Inline-клавиатуры и один сохраняемый экран на чат |
| 💼 Биржевой контур | Один подключённый Bybit Unified Account для всех администраторов |
| 🧪 Безопасный default | `DRY_RUN=True`: изменяющие Bybit POST-запросы симулируются |
| 👥 Пользовательские данные | Настройки, алерты и журнал разделяются по Telegram `chat_id` |
| 🗃️ Хранилище | Локальная SQLite с WAL, foreign keys и busy timeout |

> ⚠️ Текущая модель авторизации рассчитана на **приватные чаты с доверенными пользователями**. Не подключайте недоверенных пользователей или групповые чаты к экземпляру, который имеет доступ к реальному биржевому аккаунту.

## 🚀 Возможности

### 🧭 Telegram UX

- `/start` и `/menu` открывают главный экран.
- Баланс, позиции, технический анализ, обзор рынка и статус авто-режима обновляются в том же сообщении.
- Переход на другой экран отменяет старую live-задачу, чтобы она не перезаписала новый экран.
- Медленные запросы из Telegram handlers выполняются через worker threads и не блокируют event loop polling.
- Неизвестный callback обрабатывается fallback-экраном с возвратом в главное меню.

### 💼 Аккаунт и позиции

- Баланс Unified Account: wallet balance, equity, свободные средства и маржа.
- Список и детали открытых позиций: side, size, entry/mark price, PnL, ROI, TP, SL и leverage.
- Закрытие одной или всех позиций с отдельным подтверждением.
- История Closed PnL через Bybit V5.
- В `DRY_RUN` закрытия, ордера, leverage и TP/SL не отправляются на биржу.

### 📈 Аналитика и DeepSeek

- EMA, RSI, MACD и ATR.
- Таймфреймы 3m, 5m, 1h и 4h.
- Кэш свечного анализа на 60 секунд при сохранении live-цены.
- DeepSeek-ответ в JSON с проверкой структуры, токенов и числовых полей.
- Глобальный обзор CoinGecko: капитализация, объём, BTC dominance и trending assets.

### 🤖 Авто-режим и риск

- Сигналы `hold`, `close`, `long` и `short` для разрешённых активов.
- Округление количества через `Decimal` по заданному шагу инструмента.
- Проверки минимального ордера, фактического риска до stop-loss и risk/reward.
- Лимиты риска одной сделки и всего портфеля.
- Проверка stop-loss относительно liquidation price.
- Блокировка новых входов при уже открытой позиции без защитного stop-loss.
- Защита от повторного входа в том же направлении и обработка противоположной позиции.

### 🔔 Алерты и журнал

- Ценовые и RSI-алерты: выше/ниже порога.
- Одноразовый или повторяемый режим с cooldown.
- Срабатывание только при фактическом пересечении порога.
- Личный default symbol, RSI interval и notification toggles.
- Локальный журнал создания, удаления и срабатывания алертов, действий с позициями и авто-режимом.

## 🗺️ Экраны и доступ

| Экран | Что показывает | Обновление | Доступ |
| --- | --- | --- | --- |
| 📊 Позиции | Открытые позиции и детали | 2 секунды | Администратор |
| 💰 Баланс | Unified Account и маржа | 2 секунды | Администратор |
| 📈 AI-рекомендации | Решение DeepSeek без автоматического исполнения | По запросу | Администратор |
| 🔍 Анализ рынка | Индикаторы и multi-timeframe context | 2 секунды, анализ кэшируется 60 секунд | Администратор |
| 📜 Сделки | Closed PnL Bybit | По запросу | Администратор |
| 🤖 Авто-режим | Статус, start/stop и локальный log tail | 2 секунды | Администратор |
| 🔔 Алерты | Создание, список и удаление | По действию | Любой пользователь |
| 🌍 Обзор рынка | CoinGecko global/trending data | 30 секунд, кэш 90 секунд | Любой пользователь |
| 🧾 Журнал | Личные локальные события | По запросу | Любой пользователь |
| ⚙️ Настройки | Актив и параметры личных алертов | По действию | Любой пользователь |

## 🔄 Поток выполнения

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

`BybitAPI` — единая реализация биржевого адаптера, но handlers и фоновые сервисы создают отдельные экземпляры клиента по мере необходимости.

## 🏗️ Архитектура

```text
TgBot-Bybot-control/
├── api/
│   ├── bybit_api.py             # Bybit V5 signing, reads, POSTs and dry-run
│   ├── deepseek_api.py          # OpenAI-compatible DeepSeek client and local logs
│   └── tg_notify.py             # Auto-mode events routed to Telegram UI
├── core/
│   ├── alert_scheduler.py       # Async alert lifecycle
│   ├── alerts.py                # Price/RSI crossing logic
│   ├── auto_trading.py          # Signals, execution and risk checks
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
├── utils/                       # Formatting, calculations and logging
├── config.py                    # Environment-backed runtime settings
├── main.py                      # CLI mode selector
├── start.bat                    # Windows Telegram launcher
└── run.bat                      # Windows interactive launcher
```

## ⚙️ Установка

Требуется **Python 3.10+**.

```powershell
git clone https://github.com/soroka01/TgBot-Bybot-control.git
cd TgBot-Bybot-control
py -3 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
```

На Linux/macOS активация окружения выполняется командой `source .venv/bin/activate`.

## 🔐 Конфигурация

Проект читает секреты только из **окружения процесса**. Он не загружает `.env` и `config.json`.

Минимальный PowerShell-пример для Telegram UI:

```powershell
$env:TELEGRAM_TOKEN="replace-with-telegram-token"
$env:ADMIN_TELEGRAM_IDS="123456789"
$env:BYBIT_API_KEY="replace-with-read-trade-key"
$env:BYBIT_API_SECRET="replace-with-api-secret"
$env:DEEPSEEK_API_KEY="replace-with-deepseek-key"
$env:DRY_RUN="True"
python main.py telegram
```

Эквивалентный Bash-пример:

```bash
export TELEGRAM_TOKEN="replace-with-telegram-token"
export ADMIN_TELEGRAM_IDS="123456789"
export BYBIT_API_KEY="replace-with-read-trade-key"
export BYBIT_API_SECRET="replace-with-api-secret"
export DEEPSEEK_API_KEY="replace-with-deepseek-key"
export DRY_RUN="True"
python main.py telegram
```

| Переменная | Default | Назначение |
| --- | --- | --- |
| `TELEGRAM_TOKEN` | пусто | Обязательна для Telegram polling |
| `ADMIN_TELEGRAM_IDS` | пусто | Telegram user IDs администраторов через запятую; без них новые пользователи не получают торговый доступ |
| `BYBIT_API_KEY` | пусто | Нужна для приватных balance/position reads и торгового цикла |
| `BYBIT_API_SECRET` | пусто | Секрет подписи приватных запросов Bybit |
| `DEEPSEEK_API_KEY` | пусто | Нужна для AI-рекомендаций и авто-анализа |
| `DEEPSEEK_API_URL` | `https://api.deepseek.com/v1` | OpenAI-compatible endpoint |
| `DRY_RUN` | `True` | При `True` симулирует изменяющие Bybit POST-запросы |
| `POLL_INTERVAL` | `180` | Пауза между циклами авто-режима, секунд |
| `MAX_LEVERAGE` | `10` | Максимально разрешённое плечо |
| `MAX_RISK_PER_TRADE_PERCENT` | `2` | Максимальный риск одной сделки, % equity |
| `MAX_TOTAL_RISK_PERCENT` | `10` | Максимальный суммарный риск, % equity |
| `MIN_ORDER_SIZE_USDT` | `10` | Минимальный номинал ордера |
| `CRYPTO_DB_PATH` | `data/crypto_bot.sqlite3` | Путь к SQLite |

Список активов (`BTC`, `ETH`, `SOL`, `XRP`, `BNB`, `DOGE`) и Bybit category `linear` заданы в `config.py`.

### Семантика `DRY_RUN`

`DRY_RUN=True` предотвращает отправку **POST-запросов**, меняющих состояние Bybit. Публичные и приватные GET-запросы остаются реальными, поэтому balance/positions и полноценный auto-loop всё равно требуют действующие Bybit credentials. Это локальная симуляция, а не Bybit testnet; API host в текущей версии фиксирован на production.

## ▶️ Запуск

```powershell
python main.py telegram  # Telegram UI
python main.py auto      # Авто-цикл в текущем процессе, без Telegram polling
python main.py           # Интерактивный выбор
python main.py --help
```

Windows launchers:

- `start.bat` переходит в каталог проекта, создаёт `.venv` при отсутствии, устанавливает `requirements.txt` и запускает `python main.py telegram`.
- `run.bat` выполняет тот же bootstrap и запускает интерактивный `python main.py`.

Переменные окружения должны быть заданы до запуска `.bat`. В standalone `auto` Telegram UI не регистрируется, поэтому события остаются в локальном логе.

## 🗃️ Runtime-данные

| Путь | Содержимое |
| --- | --- |
| `data/crypto_bot.sqlite3` | Профили, роли, настройки, экраны, алерты и activity log |
| `crypto_bot.log` | Общий runtime log с ротацией |
| `api/deepseek_logs/` | Часть контекста, reasoning при наличии и ответы DeepSeek |

Эти файлы игнорируются Git, но могут содержать чувствительную информацию об аккаунте и торговом контексте. Защищайте каталог проекта и резервные копии.

## 🛡️ Модель доступа и безопасность

- Один процесс подключён к **одному общему Bybit account**; пользователи не получают субсчета.
- Используйте бота только в private chats. Профиль и роль хранятся по `chat_id`, поэтому текущая схема не предназначена для безопасного разграничения участников группового чата.
- Выданная роль администратора сохраняется в SQLite. Удаление ID из `ADMIN_TELEGRAM_IDS` не отзывает уже сохранённый `is_admin`; для отзыва требуется также очистить или обновить соответствующую запись БД.
- События авто-режима сейчас отправляются на все сохранённые активные экраны, а не только администраторам. Не подключайте недоверенных пользователей к торговому экземпляру.
- Используйте Bybit key только с read/trade permissions и **без withdrawal permission**.
- Не передавайте ключи в Telegram, issue, commit, screenshot или log.
- DeepSeek получает подготовленный рыночный и account context; учитывайте это при выборе данных и провайдера.
- Сначала проверьте сценарии с `DRY_RUN=True`. `DRY_RUN=False` разрешает реальные операции.

## ⚠️ Ограничения

- Поддерживаются только заданные в коде `linear` USDT-инструменты.
- Alert scheduler работает в polling event loop, а auto-trading — в отдельном daemon thread; это один процесс, но не один поток.
- SQLite подходит для одного экземпляра. Несколько параллельных процессов потребуют другой координации и хранилища.
- CoinGecko, DeepSeek и Bybit могут применять rate limits или быть временно недоступны.
- Фоновые алерты редактируют существующий экран и не создают новый, если пользователь удалил его вручную.
- Ввод порога алерта остаётся отдельным пользовательским сообщением в Telegram.

## 🧪 Проверка

В репозитории пока нет автоматизированного test suite и CI. Перед изменениями нужны как минимум:

- синтаксическая проверка Python-модулей;
- импорт Telegram entrypoint с тестовым окружением;
- временная SQLite и проверка схемы;
- сценарии двух изолированных private chats;
- одноразовые и повторяемые crossing-сценарии алертов;
- ручная проверка `DRY_RUN`;
- `git diff --check`.

Эти пункты описывают необходимый smoke-check, а не автоматически выполняемый pipeline.

## 📄 Лицензия

Проект распространяется по лицензии [MIT](LICENSE).

---

💙 Один экран, явные ограничения и максимум контекста перед любым торговым действием.
