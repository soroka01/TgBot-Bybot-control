# main.py
"""
Главный файл запуска - выбор режима работы бота
"""
import sys
from pathlib import Path

# Добавляем текущую директорию в путь
sys.path.insert(0, str(Path(__file__).parent))

from utils.logger_setup import logger
from config import TELEGRAM_TOKEN


def print_banner():
    """Выводит баннер приложения"""
    banner = """
    ╔══════════════════════════════════════════════════════════╗
    ║                                                          ║
    ║          🤖 CRYPTO TRADING BOT FOR BYBIT 🤖             ║
    ║                                                          ║
    ║          Powered by DeepSeek AI & Aiogram                ║
    ║                                                          ║
    ╚══════════════════════════════════════════════════════════╝
    """
    print(banner)


def show_menu():
    """Показывает меню выбора режима"""
    print("\n" + "="*60)
    print("ВЫБЕРИТЕ РЕЖИМ РАБОТЫ:")
    print("="*60)
    print("\n1. 🤖 Telegram Bot - Управление через Telegram")
    print("2. ⚡ Авто-режим - Автоматическая торговля")
    print("3. ❌ Выход")
    print("\n" + "="*60)


def run_telegram_bot():
    """Запуск Telegram бота"""
    logger.info("Запуск Telegram бота...")

    if not TELEGRAM_TOKEN:
        logger.error("❌ TELEGRAM_TOKEN не установлен!")
        logger.error("Задайте TELEGRAM_TOKEN перед запуском приложения")
        return

    from telegram_bot.bot import run_telegram_bot as run_bot
    run_bot()


def run_auto_mode():
    """Запуск автоматической торговли"""
    logger.info("Запуск автоматического режима...")

    from core.auto_trading import main_loop
    main_loop()


def main():
    """Главная функция"""
    print_banner()

    # Если есть аргументы командной строки
    if len(sys.argv) > 1:
        mode = sys.argv[1].lower()

        if mode in ("telegram", "tg", "bot"):
            run_telegram_bot()
            return
        elif mode in ("auto", "trading"):
            run_auto_mode()
            return
        elif mode in ("help", "-h", "--help"):
            print("\nИспользование:")
            print("  python main.py          - Интерактивное меню")
            print("  python main.py telegram - Запуск Telegram бота")
            print("  python main.py auto     - Запуск авто-торговли")
            return

    # Интерактивный режим
    while True:
        show_menu()
        try:
            choice = input("\nВыберите опцию (1-3): ").strip()

            if choice == "1":
                run_telegram_bot()
                break
            elif choice == "2":
                run_auto_mode()
                break
            elif choice == "3":
                logger.info("👋 Выход из программы")
                break
            else:
                print("❌ Неверный выбор. Попробуйте снова.")
        except KeyboardInterrupt:
            print("\n\n👋 Выход из программы")
            break
        except Exception as e:
            logger.error(f"❌ Ошибка: {e}")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        logger.info("\n👋 Программа остановлена пользователем")
    except Exception as e:
        logger.critical(f"💥 Критическая ошибка: {e}")
