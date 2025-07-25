"""
Базовый сервисный класс
"""
import logging
from abc import ABC, abstractmethod
from typing import Any, Optional
from core.decorators import handle_errors, log_function_call

logger = logging.getLogger(__name__)

class BaseService(ABC):
    """Базовый класс для всех сервисов"""
    
    def __init__(self):
        self.logger = logging.getLogger(self.__class__.__name__)
    
    @handle_errors("Ошибка в сервисе")
    @log_function_call()
    def execute_safely(self, method_name: str, *args, **kwargs) -> Any:
        """Безопасное выполнение метода сервиса"""
        method = getattr(self, method_name)
        return method(*args, **kwargs)

class DataService(BaseService):
    """Базовый класс для сервисов работы с данными"""
    
    @abstractmethod
    def get(self, identifier: Any) -> Optional[Any]:
        """Получить данные по идентификатору"""
        pass
    
    @abstractmethod
    def save(self, data: Any) -> bool:
        """Сохранить данные"""
        pass
    
    @abstractmethod
    def delete(self, identifier: Any) -> bool:
        """Удалить данные по идентификатору"""
        pass

class APIService(BaseService):
    """Базовый класс для API сервисов"""
    
    @abstractmethod
    def call_api(self, method: str, **kwargs) -> Any:
        """Вызов API метода"""
        pass
