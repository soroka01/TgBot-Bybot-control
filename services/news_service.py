"""
Сервис для работы с новостями
"""
import requests
from typing import Dict, Any, List

from core.base_service import APIService
from core.decorators import handle_errors, log_function_call, retry
from core.exceptions import APIError

class NewsService(APIService):
    """Сервис для получения новостей о криптовалютах"""
    
    def __init__(self):
        super().__init__()
        self.base_url = "https://api.coingecko.com/api/v3"
        self.timeout = 10
    
    @handle_errors("Последние новости недоступны")
    @retry(attempts=3, delay=2.0)
    @log_function_call()
    def get_latest_crypto_news(self) -> str:
        """Получить последние новости о криптовалютах"""
        try:
            # Пример использования CoinGecko API для получения трендов
            url = f"{self.base_url}/search/trending"
            
            response = requests.get(url, timeout=self.timeout)
            response.raise_for_status()
            
            data = response.json()
            trending_coins = data.get('coins', [])
            
            if not trending_coins:
                return "📰 Новости недоступны в данный момент"
            
            news_lines = ["📰 Топ трендовых криптовалют:"]
            
            for i, coin in enumerate(trending_coins[:5], 1):
                coin_data = coin.get('item', {})
                name = coin_data.get('name', 'N/A')
                symbol = coin_data.get('symbol', 'N/A')
                market_cap_rank = coin_data.get('market_cap_rank', 'N/A')
                
                news_lines.append(
                    f"{i}. 🪙 {name} ({symbol.upper()}) - Ранг: {market_cap_rank}"
                )
            
            return "\n".join(news_lines)
            
        except requests.RequestException as e:
            raise APIError(f"Ошибка получения новостей: {e}") from e
        except Exception as e:
            raise APIError(f"Неожиданная ошибка при получении новостей: {e}") from e
    
    @handle_errors("Информация о рынке недоступна")
    @retry(attempts=3)
    def get_market_summary(self) -> str:
        """Получить краткую сводку по рынку"""
        try:
            url = f"{self.base_url}/global"
            
            response = requests.get(url, timeout=self.timeout)
            response.raise_for_status()
            
            data = response.json()
            global_data = data.get('data', {})
            
            total_market_cap = global_data.get('total_market_cap', {}).get('usd', 0)
            total_volume = global_data.get('total_volume', {}).get('usd', 0)
            btc_dominance = global_data.get('market_cap_percentage', {}).get('btc', 0)
            
            summary = (
                f"🌍 Общая капитализация: ${total_market_cap:,.0f}\n"
                f"📊 Объем торгов 24ч: ${total_volume:,.0f}\n"
                f"₿ Доминация BTC: {btc_dominance:.1f}%"
            )
            
            return summary
            
        except requests.RequestException as e:
            raise APIError(f"Ошибка получения данных рынка: {e}") from e
    
    def call_api(self, method: str, **kwargs) -> Any:
        """Общий метод для вызова новостного API"""
        try:
            url = f"{self.base_url}/{method}"
            response = requests.get(url, timeout=self.timeout, **kwargs)
            response.raise_for_status()
            return response.json()
        except requests.RequestException as e:
            raise APIError(f"Ошибка вызова API {method}: {e}") from e

# Глобальный экземпляр сервиса
news_service = NewsService()
