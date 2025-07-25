"""
Сервис для работы с алертами и уведомлениями
"""
from typing import List, Dict, Any, Optional
from datetime import datetime

from core.base_service import BaseService
from core.config import config
from core.decorators import handle_errors, log_function_call
from core.exceptions import ValidationError
from services.database_service import db_service
from services.market_service import market_service

class AlertService(BaseService):
    """Сервис для работы с алертами"""
    
    def __init__(self):
        super().__init__()
        self.db_service = db_service
        self.market_service = market_service
    
    @handle_errors("Ошибка валидации алерта", False)
    def _validate_price_alert(self, price: float) -> bool:
        """Валидация ценового алерта"""
        if price <= 0:
            raise ValidationError("Цена должна быть положительной")
        
        # Проверка на разумность цены (не более чем в 10 раз отличается от текущей)
        try:
            current_price = self.market_service.get_current_price()
            if price > current_price * 10 or price < current_price / 10:
                raise ValidationError("Цена слишком сильно отличается от текущей")
        except Exception:
            # Если не удалось получить текущую цену, пропускаем валидацию
            pass
        
        return True
    
    @handle_errors("Ошибка валидации RSI алерта", False)
    def _validate_rsi_alert(self, rsi_level: float) -> bool:
        """Валидация RSI алерта"""
        if not (0 <= rsi_level <= 100):
            raise ValidationError("RSI должен быть в диапазоне 0-100")
        return True
    
    @handle_errors("Ошибка добавления ценового алерта", False)
    @log_function_call()
    def add_price_alert(self, user_id: int, price: float, permanent: bool = False) -> bool:
        """Добавить ценовой алерт"""
        self._validate_price_alert(price)
        
        alert = {
            "price": price,
            "permanent": permanent,
            "created_at": datetime.now().isoformat(),
            "triggered": False
        }
        
        return self.db_service.add_user_alert(user_id, alert, "price_alerts")
    
    @handle_errors("Ошибка добавления RSI алерта", False)
    @log_function_call()
    def add_rsi_alert(self, user_id: int, rsi_level: float, condition: str, permanent: bool = False) -> bool:
        """Добавить RSI алерт"""
        self._validate_rsi_alert(rsi_level)
        
        if condition not in ["above", "below"]:
            raise ValidationError("Условие должно быть 'above' или 'below'")
        
        alert = {
            "level": rsi_level,
            "condition": condition,
            "permanent": permanent,
            "created_at": datetime.now().isoformat(),
            "triggered": False
        }
        
        return self.db_service.add_user_alert(user_id, alert, "rsi_alerts")
    
    @handle_errors("Ошибка получения алертов", [])
    def get_price_alerts(self, user_id: int) -> List[Dict[str, Any]]:
        """Получить ценовые алерты пользователя"""
        return self.db_service.get_user_alerts(user_id, "price_alerts")
    
    @handle_errors("Ошибка получения RSI алертов", [])
    def get_rsi_alerts(self, user_id: int) -> List[Dict[str, Any]]:
        """Получить RSI алерты пользователя"""
        return self.db_service.get_user_alerts(user_id, "rsi_alerts")
    
    @handle_errors("Ошибка удаления алерта", False)
    def remove_alert(self, user_id: int, alert_index: int, alert_type: str) -> bool:
        """Удалить алерт по индексу"""
        if alert_type not in ["price_alerts", "rsi_alerts"]:
            raise ValidationError("Неверный тип алерта")
        
        return self.db_service.remove_user_alert(user_id, alert_index, alert_type)
    
    @handle_errors("Ошибка очистки алертов", False)
    def clear_alerts(self, user_id: int, alert_type: str = None) -> bool:
        """Очистить алерты пользователя"""
        if alert_type:
            return self.db_service.clear_user_alerts(user_id, alert_type)
        else:
            # Очистить все типы алертов
            price_cleared = self.db_service.clear_user_alerts(user_id, "price_alerts")
            rsi_cleared = self.db_service.clear_user_alerts(user_id, "rsi_alerts")
            return price_cleared and rsi_cleared
    
    def format_price_alerts(self, alerts: List[Dict[str, Any]]) -> str:
        """Форматировать ценовые алерты для отображения"""
        if not alerts:
            return "У вас нет активных ценовых алертов"
        
        formatted_lines = []
        for i, alert in enumerate(alerts, 1):
            price = alert.get('price', 'N/A')
            permanent = "Навсегда" if alert.get('permanent', False) else "Однократно"
            created = alert.get('created_at', 'N/A')
            
            formatted_lines.append(f"{i}. 💲 {price} USDT - {permanent}")
        
        return "📋 Ваши ценовые алерты:\n" + "\n".join(formatted_lines)
    
    def format_rsi_alerts(self, alerts: List[Dict[str, Any]]) -> str:
        """Форматировать RSI алерты для отображения"""
        if not alerts:
            return "У вас нет активных RSI алертов"
        
        formatted_lines = []
        for i, alert in enumerate(alerts, 1):
            level = alert.get('level', 'N/A')
            condition = ">" if alert.get('condition') == 'above' else "<"
            permanent = "Навсегда" if alert.get('permanent', False) else "Однократно"
            
            formatted_lines.append(f"{i}. 📊 RSI {condition} {level} - {permanent}")
        
        return "📋 Ваши RSI алерты:\n" + "\n".join(formatted_lines)
    
    @handle_errors("Ошибка проверки ценовых алертов")
    @log_function_call()
    def check_price_alerts(self) -> List[Dict[str, Any]]:
        """Проверить ценовые алерты всех пользователей"""
        triggered_alerts = []
        
        try:
            current_price = self.market_service.get_current_price()
        except Exception as e:
            self.logger.error(f"Не удалось получить текущую цену для проверки алертов: {e}")
            return triggered_alerts
        
        # Получаем всех пользователей
        all_users = self.db_service.get_all_users()
        
        for user_id in all_users:
            try:
                alerts = self.get_price_alerts(user_id)
                user_triggered = []
                
                for i, alert in enumerate(alerts):
                    alert_price = alert.get('price', 0)
                    
                    # Проверяем, сработал ли алерт
                    if current_price >= alert_price and not alert.get('triggered', False):
                        alert['triggered'] = True
                        user_triggered.append({
                            'user_id': user_id,
                            'alert': alert,
                            'current_price': current_price,
                            'index': i
                        })
                
                # Обновляем алерты пользователя
                if user_triggered:
                    # Удаляем неперманентные сработавшие алерты
                    remaining_alerts = []
                    for alert in alerts:
                        if alert.get('triggered', False) and not alert.get('permanent', False):
                            continue  # Пропускаем неперманентные сработавшие алерты
                        remaining_alerts.append(alert)
                    
                    self.db_service.save_user_alerts(user_id, remaining_alerts, "price_alerts")
                    triggered_alerts.extend(user_triggered)
                    
            except Exception as e:
                self.logger.error(f"Ошибка проверки алертов для пользователя {user_id}: {e}")
        
        return triggered_alerts
    
    @handle_errors("Ошибка проверки RSI алертов")
    @log_function_call()
    def check_rsi_alerts(self, timeframe: str = "1") -> List[Dict[str, Any]]:
        """Проверить RSI алерты всех пользователей"""
        triggered_alerts = []
        
        try:
            current_rsi = self.market_service.calculate_rsi(timeframe)
        except Exception as e:
            self.logger.error(f"Не удалось получить RSI для проверки алертов: {e}")
            return triggered_alerts
        
        # Получаем всех пользователей
        all_users = self.db_service.get_all_users()
        
        for user_id in all_users:
            try:
                alerts = self.get_rsi_alerts(user_id)
                user_triggered = []
                
                for i, alert in enumerate(alerts):
                    level = alert.get('level', 0)
                    condition = alert.get('condition', 'below')
                    
                    # Проверяем условие
                    triggered = False
                    if condition == 'below' and current_rsi < level:
                        triggered = True
                    elif condition == 'above' and current_rsi > level:
                        triggered = True
                    
                    if triggered and not alert.get('triggered', False):
                        alert['triggered'] = True
                        user_triggered.append({
                            'user_id': user_id,
                            'alert': alert,
                            'current_rsi': current_rsi,
                            'index': i
                        })
                
                # Обновляем алерты пользователя
                if user_triggered:
                    # Удаляем неперманентные сработавшие алерты
                    remaining_alerts = []
                    for alert in alerts:
                        if alert.get('triggered', False) and not alert.get('permanent', False):
                            continue
                        remaining_alerts.append(alert)
                    
                    self.db_service.save_user_alerts(user_id, remaining_alerts, "rsi_alerts")
                    triggered_alerts.extend(user_triggered)
                    
            except Exception as e:
                self.logger.error(f"Ошибка проверки RSI алертов для пользователя {user_id}: {e}")
        
        return triggered_alerts

# Глобальный экземпляр сервиса
alert_service = AlertService()
