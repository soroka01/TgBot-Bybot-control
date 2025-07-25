"""
Улучшенный сервис для работы с базой данных
"""
import sqlite3
import json
from contextlib import contextmanager
from typing import Any, Dict, List, Optional
from pathlib import Path

from core.base_service import DataService
from core.config import config
from core.decorators import handle_errors, log_function_call, retry
from core.exceptions import DataError

class DatabaseService(DataService):
    """Сервис для работы с базой данных"""
    
    def __init__(self, db_path: Optional[Path] = None):
        super().__init__()
        self.db_path = db_path or config.DB_PATH
        self._init_db()
    
    @contextmanager
    def get_connection(self):
        """Контекстный менеджер для работы с БД"""
        conn = None
        try:
            conn = sqlite3.connect(self.db_path)
            conn.row_factory = sqlite3.Row
            yield conn
        except sqlite3.Error as e:
            if conn:
                conn.rollback()
            raise DataError(f"Ошибка базы данных: {e}") from e
        finally:
            if conn:
                conn.close()
    
    @handle_errors("Ошибка инициализации базы данных")
    @log_function_call()
    def _init_db(self):
        """Инициализация базы данных"""
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS users (
                    user_id INTEGER PRIMARY KEY,
                    data TEXT,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            ''')
            
            # Создание индексов для оптимизации
            cursor.execute('''
                CREATE INDEX IF NOT EXISTS idx_users_updated_at 
                ON users(updated_at)
            ''')
            
            conn.commit()
    
    @handle_errors("Ошибка получения данных пользователя", {})
    @retry(attempts=3)
    def get(self, user_id: int) -> Dict[str, Any]:
        """Получить данные пользователя"""
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                'SELECT data FROM users WHERE user_id = ?', 
                (user_id,)
            )
            row = cursor.fetchone()
            return json.loads(row['data']) if row else {}
    
    @handle_errors("Ошибка сохранения данных пользователя", False)
    @retry(attempts=3)
    def save(self, user_id: int, data: Dict[str, Any]) -> bool:
        """Сохранить данные пользователя"""
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute('''
                INSERT OR REPLACE INTO users (user_id, data, updated_at) 
                VALUES (?, ?, CURRENT_TIMESTAMP)
            ''', (user_id, json.dumps(data, ensure_ascii=False)))
            conn.commit()
            return True
    
    @handle_errors("Ошибка удаления данных пользователя", False)
    def delete(self, user_id: int) -> bool:
        """Удалить данные пользователя"""
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute('DELETE FROM users WHERE user_id = ?', (user_id,))
            conn.commit()
            return cursor.rowcount > 0
    
    @handle_errors("Ошибка получения всех пользователей", [])
    def get_all_users(self) -> List[int]:
        """Получить список всех пользователей"""
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute('SELECT user_id FROM users')
            return [row['user_id'] for row in cursor.fetchall()]
    
    # Методы для работы с алертами
    @handle_errors("Ошибка получения алертов пользователя", [])
    def get_user_alerts(self, user_id: int, alert_type: str = 'alerts') -> List[Dict[str, Any]]:
        """Получить алерты пользователя"""
        user_data = self.get(user_id)
        return user_data.get(alert_type, [])
    
    @handle_errors("Ошибка сохранения алертов пользователя", False)
    def save_user_alerts(self, user_id: int, alerts: List[Dict[str, Any]], 
                        alert_type: str = 'alerts') -> bool:
        """Сохранить алерты пользователя"""
        user_data = self.get(user_id)
        user_data[alert_type] = alerts
        return self.save(user_id, user_data)
    
    @handle_errors("Ошибка добавления алерта", False)
    def add_user_alert(self, user_id: int, alert: Dict[str, Any], 
                      alert_type: str = 'alerts') -> bool:
        """Добавить алерт пользователю"""
        alerts = self.get_user_alerts(user_id, alert_type)
        
        # Проверка лимита
        if len(alerts) >= config.MAX_ALERTS_PER_USER:
            raise DataError(f"Превышен лимит алертов ({config.MAX_ALERTS_PER_USER})")
        
        alerts.append(alert)
        return self.save_user_alerts(user_id, alerts, alert_type)
    
    @handle_errors("Ошибка удаления алерта", False)
    def remove_user_alert(self, user_id: int, alert_index: int, 
                         alert_type: str = 'alerts') -> bool:
        """Удалить алерт пользователя по индексу"""
        alerts = self.get_user_alerts(user_id, alert_type)
        
        if 0 <= alert_index < len(alerts):
            alerts.pop(alert_index)
            return self.save_user_alerts(user_id, alerts, alert_type)
        
        raise DataError("Неверный индекс алерта")
    
    @handle_errors("Ошибка очистки алертов", False)
    def clear_user_alerts(self, user_id: int, alert_type: str = 'alerts') -> bool:
        """Очистить все алерты пользователя"""
        return self.save_user_alerts(user_id, [], alert_type)

# Глобальный экземпляр сервиса
db_service = DatabaseService()
