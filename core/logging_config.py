"""
Улучшенная конфигурация логирования
"""
import logging
import os
from pathlib import Path
from core.config import config

class LoggingManager:
    """Менеджер логирования"""
    
    @staticmethod
    def setup_logging():
        """Настройка логирования"""
        
        # Создание директории для логов
        config.LOG_DIR.mkdir(exist_ok=True)
        
        # Создание форматтера
        formatter = logging.Formatter(
            '%(asctime)s - %(name)s - %(levelname)s - %(message)s'
        )
        
        # Основной логгер
        logger = logging.getLogger()
        logger.setLevel(logging.INFO)
        
        # Очистка существующих обработчиков
        logger.handlers.clear()
        
        # Обработчик для основного файла логов
        file_handler = logging.FileHandler(config.BOT_LOG_FILE, encoding='utf-8')
        file_handler.setLevel(logging.INFO)
        file_handler.setFormatter(formatter)
        logger.addHandler(file_handler)
        
        # Обработчик для консоли (только ошибки)
        console_handler = logging.StreamHandler()
        console_handler.setLevel(logging.ERROR)
        console_handler.setFormatter(formatter)
        logger.addHandler(console_handler)
        
        # Обработчик для предупреждений
        warning_handler = logging.FileHandler(config.WARNINGS_LOG_FILE, encoding='utf-8')
        warning_handler.setLevel(logging.WARNING)
        warning_handler.setFormatter(formatter)
        logger.addHandler(warning_handler)
        
        return logger

# Инициализация логирования
logging_manager = LoggingManager()
logger = logging_manager.setup_logging()
