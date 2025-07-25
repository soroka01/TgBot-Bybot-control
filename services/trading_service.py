"""
Сервис для торговых операций
"""
from typing import Tuple, Dict, Any, List

from core.base_service import APIService
from core.config import config
from core.factories import session_factory
from core.decorators import handle_errors, log_function_call, retry
from core.exceptions import APIError

class TradingService(APIService):
    """Сервис для торговых операций"""
    
    def __init__(self):
        super().__init__()
        self.session = session_factory.get_session()
    
    @handle_errors("Ошибка при размещении ордера")
    @retry(attempts=2)
    @log_function_call()
    def place_market_buy_order(self, amount_usdt: float) -> Dict[str, Any]:
        """Разместить рыночный ордер на покупку"""
        try:
            order = self.session.place_active_order(
                category="spot",
                symbol=config.SYMBOL,
                side="Buy",
                orderType="Market",
                qty=str(amount_usdt),
                marketUnit="quoteCoin"
            )
            
            if order.get('retCode') != 0:
                raise APIError(f"Ошибка API: {order.get('retMsg', 'Неизвестная ошибка')}")
            
            return order.get('result', {})
            
        except Exception as e:
            raise APIError(f"Ошибка размещения ордера: {e}") from e
    
    @handle_errors("Ошибка получения истории торгов", [])
    @retry(attempts=3)
    def get_trade_history(self, limit: int = 10) -> List[Dict[str, Any]]:
        """Получить историю торговых операций"""
        try:
            response = self.session.get_order_history(
                category="spot", 
                symbol=config.SYMBOL, 
                limit=limit
            )
            
            if response.get('retCode') != 0:
                raise APIError(f"Ошибка API: {response.get('retMsg', 'Неизвестная ошибка')}")
            
            orders = response.get('result', {}).get('list', [])
            return orders
            
        except Exception as e:
            raise APIError(f"Ошибка получения истории: {e}") from e
    
    @handle_errors("Ошибка получения баланса", ("N/A", "N/A"))
    @retry(attempts=3)
    def get_balance(self) -> Tuple[str, str]:
        """Получить баланс BTC и USDT"""
        try:
            response = self.session.get_wallet_balance(
                accountType="UNIFIED",
                coin="BTC,USDT"
            )
            
            if response.get('retCode') != 0:
                raise APIError(f"Ошибка API: {response.get('retMsg', 'Неизвестная ошибка')}")
            
            balance_list = response.get('result', {}).get('list', [])
            if not balance_list:
                raise APIError("Нет данных о балансе")
            
            coins = balance_list[0].get('coin', [])
            
            btc_balance = "N/A"
            usdt_balance = "N/A"
            
            for coin in coins:
                if coin.get('coin') == 'BTC':
                    btc_balance = coin.get('walletBalance', 'N/A')
                elif coin.get('coin') == 'USDT':
                    usdt_balance = coin.get('walletBalance', 'N/A')
            
            return btc_balance, usdt_balance
            
        except Exception as e:
            raise APIError(f"Ошибка получения баланса: {e}") from e
    
    def format_trade_history(self, history: List[Dict[str, Any]]) -> str:
        """Форматировать историю торгов для отображения"""
        if not history:
            return "История торговых операций пуста"
        
        formatted_lines = []
        for order in history:
            order_id = order.get('orderId', 'N/A')
            qty = order.get('qty', 'N/A')
            price = order.get('price', 'N/A')
            status = order.get('orderStatus', 'N/A')
            side = order.get('side', 'N/A')
            
            formatted_lines.append(
                f"📋 ID: {order_id}\n"
                f"💰 Количество: {qty}\n"
                f"💲 Цена: {price}\n"
                f"📊 Статус: {status}\n"
                f"🔄 Тип: {side}\n"
                f"{'─' * 20}"
            )
        
        return "\n".join(formatted_lines)
    
    def call_api(self, method: str, **kwargs) -> Any:
        """Общий метод для вызова торгового API"""
        try:
            api_method = getattr(self.session, method)
            return api_method(**kwargs)
        except AttributeError:
            raise APIError(f"Торговый метод {method} не найден")
        except Exception as e:
            raise APIError(f"Ошибка вызова торгового API {method}: {e}") from e

# Глобальный экземпляр сервиса
trading_service = TradingService()
