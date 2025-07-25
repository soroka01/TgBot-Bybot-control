"""
Базовые исключения для приложения
"""

class BotError(Exception):
    """Базовое исключение для бота"""
    pass

class APIError(BotError):
    """Ошибка API"""
    pass

class DataError(BotError):
    """Ошибка данных"""
    pass

class ValidationError(BotError):
    """Ошибка валидации"""
    pass

class ConfigError(BotError):
    """Ошибка конфигурации"""
    pass
