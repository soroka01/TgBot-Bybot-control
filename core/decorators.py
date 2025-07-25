"""
Утилиты для работы с декораторами
"""
import functools
import logging
from typing import Callable, Any, Awaitable
from core.exceptions import BotError
import asyncio

logger = logging.getLogger(__name__)

def handle_errors(error_message: str = "Произошла ошибка", 
                 return_value: Any = None,
                 raise_exception: bool = False):
    """
    Декоратор для обработки ошибок (поддерживает async функции)
    
    Args:
        error_message: Сообщение об ошибке
        return_value: Значение возвращаемое при ошибке
        raise_exception: Поднимать ли исключение
    """
    def decorator(func: Callable) -> Callable:
        @functools.wraps(func)
        async def async_wrapper(*args, **kwargs):
            try:
                return await func(*args, **kwargs)
            except Exception as e:
                logger.error(f"Ошибка в функции {func.__name__}: {e}")
                if raise_exception:
                    raise BotError(f"{error_message}: {e}") from e
                return return_value
        
        @functools.wraps(func)
        def sync_wrapper(*args, **kwargs):
            try:
                return func(*args, **kwargs)
            except Exception as e:
                logger.error(f"Ошибка в функции {func.__name__}: {e}")
                if raise_exception:
                    raise BotError(f"{error_message}: {e}") from e
                return return_value
        
        # Возвращаем подходящий wrapper в зависимости от того, является ли функция асинхронной
        if asyncio.iscoroutinefunction(func):
            return async_wrapper
        else:
            return sync_wrapper
    return decorator

def log_function_call(level: int = logging.INFO):
    """
    Декоратор для логирования вызовов функций (поддерживает async функции)
    
    Args:
        level: Уровень логирования
    """
    def decorator(func: Callable) -> Callable:
        @functools.wraps(func)
        async def async_wrapper(*args, **kwargs):
            logger.log(level, f"Вызов функции {func.__name__}")
            result = await func(*args, **kwargs)
            logger.log(level, f"Функция {func.__name__} выполнена")
            return result
        
        @functools.wraps(func)
        def sync_wrapper(*args, **kwargs):
            logger.log(level, f"Вызов функции {func.__name__}")
            result = func(*args, **kwargs)
            logger.log(level, f"Функция {func.__name__} выполнена")
            return result
        
        # Возвращаем подходящий wrapper в зависимости от того, является ли функция асинхронной
        if asyncio.iscoroutinefunction(func):
            return async_wrapper
        else:
            return sync_wrapper
    return decorator

def retry(attempts: int = 3, delay: float = 1.0):
    """
    Декоратор для повторных попыток выполнения функции
    
    Args:
        attempts: Количество попыток
        delay: Задержка между попытками
    """
    def decorator(func: Callable) -> Callable:
        @functools.wraps(func)
        def wrapper(*args, **kwargs):
            import time
            last_exception = None
            
            for attempt in range(attempts):
                try:
                    return func(*args, **kwargs)
                except Exception as e:
                    last_exception = e
                    if attempt < attempts - 1:
                        logger.warning(
                            f"Попытка {attempt + 1} функции {func.__name__} неудачна: {e}. "
                            f"Повтор через {delay} сек."
                        )
                        time.sleep(delay)
                    else:
                        logger.error(f"Все попытки выполнения {func.__name__} исчерпаны")
            
            raise last_exception
        return wrapper
    return decorator
