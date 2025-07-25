# 🤖 Telegram Bot для Торговли на Bybit

[![Python](https://img.shields.io/badge/Python-3.8+-blue.svg)](https://python.org)
[![aiogram](https://img.shields.io/badge/aiogram-3.15.0-green.svg)](https://aiogram.dev)
[![Bybit](https://img.shields.io/badge/Bybit-API-orange.svg)](https://bybit.com)

Полнофункциональный Telegram бот для мониторинга и торговли криптовалютами на бирже Bybit. Бот предоставляет широкий спектр функций: от базовой статистики до автоматических уведомлений по RSI и ценовым алертам.

## 📋 Основные возможности

### 📊 Аналитика и мониторинг
- **Статистика портфеля** - детальная информация о балансе и позициях
- **RSI индикаторы** - автоматический расчет и мониторинг RSI
- **Ценовые алерты** - уведомления при достижении заданных уровней цен
- **График цен** - визуализация движения цены с техническими индикаторами

### 💱 Торговые операции
- **Автоматическая торговля** - выполнение сделок по сигналам RSI
- **Управление позициями** - открытие и закрытие позиций
- **Конвертер валют** - быстрая конвертация между USD и BTC
- **Настройка стоп-лоссов** - защита от убытков

### 🔔 Уведомления
- **RSI алерты** - уведомления при пересечении уровней 30, 35, 60, 70
- **Ценовые уведомления** - настраиваемые алерты на изменение цены
- **Новости криптовалют** - актуальные новости рынка
- **Системные уведомления** - информация о работе бота

### 👤 Управление аккаунтом
- **Персонализация** - изменение имени в системе
- **Настройки уведомлений** - гибкая настройка типов алертов
- **История операций** - журнал всех действий

## 🏗️ Архитектура

Проект построен на современной асинхронной архитектуре с использованием aiogram 3.x:

```
rsitgbotbybit/
├── main.py                 # Точка входа в приложение
├── requirements.txt        # Зависимости проекта
├── buttons.py             # Клавиатуры и UI элементы
│
├── core/                  # Основные компоненты
│   ├── config.py          # Конфигурация приложения
│   ├── factories.py       # Фабрики для Bot и Dispatcher
│   ├── decorators.py      # Декораторы для обработки ошибок
│   ├── exceptions.py      # Пользовательские исключения
│   └── logging_config.py  # Настройка логирования
│
├── handlers/              # Обработчики сообщений
│   ├── base_handler.py    # Базовый класс для всех обработчиков
│   ├── main_handler.py    # Основные команды (/start, /help)
│   ├── trading_handler.py # Торговые операции
│   ├── alert_handler.py   # Управление алертами
│   ├── stat_handler.py    # Статистика и аналитика
│   ├── converter_handler.py # Конвертер валют
│   ├── news_account_handler.py # Новости и аккаунт
│   └── handler_manager.py # Менеджер обработчиков
│
├── services/              # Бизнес-логика
│   ├── database_service.py    # Работа с базой данных
│   ├── trading_service.py     # Торговые операции
│   ├── market_service.py      # Рыночные данные
│   ├── alert_service.py       # Система алертов
│   ├── news_service.py        # Новостной сервис
│   └── converter_service.py   # Конвертация валют
│
├── features/              # Основные фичи
│   ├── market.py          # Рыночная аналитика
│   ├── trade.py           # Торговые операции
│   ├── news.py            # Новостная лента
│   └── converter.py       # Валютный конвертер
│
├── tasks/                 # Фоновые задачи
│   ├── scheduler.py       # Планировщик задач
│   ├── runner.py          # Исполнитель задач
│   └── task_service.py    # Сервис управления задачами
│
└── logs/                  # Логирование
    ├── bot.log           # Основные логи
    └── warnings.log      # Предупреждения
```

## 🚀 Быстрый старт

### 1. Клонирование репозитория
```bash
git clone https://github.com/soroka01/TgBot-Bybot-control.git
cd rsitgbotbybit
```

### 2. Установка зависимостей
```bash
pip install -r requirements.txt
```

### 3. Настройка конфигурации

Создайте файл `.env` в корневой директории:
```env
# Telegram Bot Token (получить у @BotFather)
TG_TOKEN=your_telegram_bot_token_here

# Bybit API ключи (необязательно для базовых функций)
BY_KEY=your_bybit_api_key
BY_SECRET=your_bybit_secret_key
```

### 4. Настройка пользователей

Отредактируйте `core/config.py`, добавив ваши Telegram ID:
```python
TG_IDS = [123456, 123123132, 21123213123]  # Замените на ваши ID
```

### 5. Запуск бота
```bash
python main.py
```

## ⚙️ Конфигурация

### Основные настройки

В файле `core/config.py` можно настроить:

```python
# Торговая пара
SYMBOL = "BTCUSDT"

# Таймфрейм для анализа (в минутах)
DEFAULT_TIMEFRAME = 60

# Пороги RSI для алертов
RSI_THRESHOLDS = {
    35: True,   # Покупка
    30: True,   # Сильная покупка
    70: False,  # Продажа
    60: False   # Ранняя продажа
}

# Максимальное количество алертов на пользователя
MAX_ALERTS_PER_USER = 15
```

### API ключи Bybit

Для полной функциональности требуются API ключи Bybit:

1. Зарегистрируйтесь на [Bybit](https://bybit.com)
2. Создайте API ключи в разделе "API Management"
3. Добавьте их в файл `.env`

⚠️ **Важно**: Установите только необходимые разрешения для API ключей (чтение данных, торговля).

## 🎮 Использование

### Основные команды

- `/start` - Запуск бота и отображение главного меню
- `/help` - Справка по всем доступным командам

### Главное меню

- **📊 Стата** - Просмотр статистики портфеля и позиций
- **💸 Бабит** - Торговое меню с операциями покупки/продажи
- **🔔 Уведомления** - Настройка и управление алертами
- **👤 Аккаунт** - Управление аккаунтом и настройками
- **📰 Новости** - Актуальные новости криптовалют
- **💱 Конвертер** - Конвертация между USD и BTC

### Система алертов

Бот поддерживает два типа алертов:

1. **RSI алерты** - автоматические уведомления при пересечении уровней RSI
2. **Ценовые алерты** - уведомления при достижении заданной цены

### Торговые операции

- Автоматическая торговля по сигналам RSI
- Ручные операции покупки/продажи
- Управление позициями и стоп-лоссами

## 🔧 Разработка

### Требования

- Python 3.8+
- aiogram 3.15.0
- Bybit API access

### Структура FSM (Finite State Machine)

Бот использует современную систему состояний aiogram:

```python
from aiogram.fsm.state import State, StatesGroup

class TradeStates(StatesGroup):
    waiting_amount = State()
    waiting_confirmation = State()
    
class AlertStates(StatesGroup):
    waiting_price = State()
    waiting_type = State()
```

### Добавление новых обработчиков

1. Создайте класс-наследник от `BaseHandler`
2. Определите router и обработчики
3. Зарегистрируйте в `handler_manager.py`

Пример:
```python
from handlers.base_handler import BaseHandler
from aiogram import Router, F

class MyHandler(BaseHandler):
    def __init__(self):
        super().__init__()
        self.router = Router()
        self.setup_handlers()
    
    def setup_handlers(self):
        @self.router.message(F.text == "Моя команда")
        async def my_command(message: Message):
            await self.send_message_safely(message, "Ответ")
```

### Логирование

Система логирования настроена в `core/logging_config.py`:
- Основные логи: `logs/bot.log`
- Предупреждения: `logs/warnings.log`

## 🐛 Решение проблем

### Частые ошибки

1. **Ошибка токена**
   ```
   Telegram token is invalid
   ```
   Проверьте корректность токена в `.env`

2. **Ошибка API Bybit**
   ```
   Invalid API key
   ```
   Убедитесь в правильности API ключей

3. **Ошибка базы данных**
   ```
   Database locked
   ```
   Перезапустите бота

### Отладка

Включите подробное логирование:
```python
# В core/logging_config.py
LOG_LEVEL = "DEBUG"
```

## 🤝 Вклад в проект

1. Форкните репозиторий
2. Создайте ветку для новой функции (`git checkout -b feature/amazing-feature`)
3. Зафиксируйте изменения (`git commit -m 'Add amazing feature'`)
4. Отправьте в ветку (`git push origin feature/amazing-feature`)
5. Откройте Pull Request

## 📄 Лицензия

Этот проект распространяется под лицензией MIT. См. файл `LICENSE` для подробностей.

## 🔗 Полезные ссылки

- [Документация aiogram](https://aiogram.dev/)
- [Bybit API Documentation](https://bybit-exchange.github.io/docs/)
- [Telegram Bot API](https://core.telegram.org/bots/api)

---

<div align="center">
    <p>Создано с ❤️ для криптанов</p>
    <p>⭐ Поставьте звезду, если проект был полезен!</p>
</div>