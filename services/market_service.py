"""
Сервис для работы с рыночными данными и техническими индикаторами
"""
import requests
import numpy as np
import matplotlib.pyplot as plt
import io
from typing import Optional, Tuple, Dict, Any
from datetime import datetime, timedelta

from core.base_service import APIService
from core.config import config
from core.factories import session_factory
from core.decorators import handle_errors, log_function_call, retry
from core.exceptions import APIError

class MarketService(APIService):
    """Сервис для работы с рыночными данными"""
    
    def __init__(self):
        super().__init__()
        self.session = session_factory.get_session()
    
    @handle_errors("Ошибка получения данных о цене", (None, None))
    @retry(attempts=3, delay=2.0)
    def get_price_data(self, interval: str = "D") -> Tuple[Optional[float], Optional[float]]:
        """Получить данные о цене (открытие, закрытие)"""
        try:
            response = self.session.get_kline(
                category="spot", 
                symbol=config.SYMBOL, 
                interval=interval, 
                limit=1
            )
            
            klines = response.get('result', {}).get('list', [])
            if not klines:
                raise APIError("Данные о цене не найдены")
            
            kline = klines[0]
            open_price, close_price = float(kline[1]), float(kline[4])
            return open_price, close_price
            
        except Exception as e:
            raise APIError(f"Ошибка получения данных о цене: {e}") from e
    
    @handle_errors("Данные недоступны")
    @log_function_call()
    def get_current_price(self) -> float:
        """Получить текущую цену"""
        _, close_price = self.get_price_data()
        if close_price is None:
            raise APIError("Не удалось получить текущую цену")
        return close_price
    
    @handle_errors("Данные недоступны")
    def get_daily_change_percent(self) -> str:
        """Получить дневное изменение в процентах"""
        open_price, close_price = self.get_price_data()
        if open_price is None or close_price is None:
            raise APIError("Не удалось получить данные для расчета изменения")
        
        change_percent = ((close_price - open_price) / open_price) * 100
        return f"{round(change_percent, 2)}%"
    
    def get_price_or_change(self, data_type: str) -> str:
        """Получить цену или изменение"""
        if data_type == 'price':
            return str(self.get_current_price())
        elif data_type == 'change':
            return self.get_daily_change_percent()
        else:
            raise ValueError("data_type должен быть 'price' или 'change'")
    
    @handle_errors("Ошибка вычисления RSI")
    @retry(attempts=3)
    def calculate_rsi(self, timeframe: str, period: int = 14) -> float:
        """Вычислить RSI"""
        try:
            response = self.session.get_kline(
                category="spot", 
                symbol=config.SYMBOL, 
                interval=timeframe, 
                limit=100
            )
            
            klines = response.get('result', {}).get('list', [])
            if not klines:
                raise APIError("Не удалось получить данные для RSI")
            
            # Извлечение цен закрытия и реверс для правильного порядка
            close_prices = [float(kline[4]) for kline in reversed(klines)]
            close_prices = np.array(close_prices, dtype=float)
            
            if len(close_prices) < period + 1:
                raise APIError(f"Недостаточно данных для расчета RSI (нужно минимум {period + 1})")
            
            # Расчет изменений цен
            deltas = np.diff(close_prices)
            
            # Начальные значения для первого периода
            seed = deltas[:period]
            up = np.mean(seed[seed >= 0])
            down = -np.mean(seed[seed < 0])
            
            if down == 0:
                return 100.0
            
            rs = up / down
            rsi = np.zeros_like(close_prices)
            rsi[:period] = 100.0 - 100.0 / (1.0 + rs)
            
            # Расчет RSI для остальных точек
            for i in range(period, len(close_prices)):
                delta = deltas[i - 1]
                
                if delta > 0:
                    upval, downval = delta, 0.0
                else:
                    upval, downval = 0.0, -delta
                
                # Экспоненциальное скользящее среднее
                up = (up * (period - 1) + upval) / period
                down = (down * (period - 1) + downval) / period
                
                if down == 0:
                    rsi[i] = 100.0
                else:
                    rs = up / down
                    rsi[i] = 100.0 - 100.0 / (1.0 + rs)
            
            return round(rsi[-1], 2)
            
        except Exception as e:
            raise APIError(f"Ошибка вычисления RSI: {e}") from e
    
    @handle_errors("Ошибка получения соотношения покупок/продаж")
    @retry(attempts=3)
    def get_buy_sell_ratio(self, timeframe: str) -> str:
        """Получить соотношение покупок и продаж"""
        period_map = {
            "60": "1h",
            "D": "1d",
            "1": "5min"
        }
        
        period = period_map.get(str(timeframe), "1h")
        url = (f"https://api.bybit.com/v5/market/account-ratio"
               f"?category=linear&symbol={config.SYMBOL}&period={period}&limit=1")
        
        try:
            response = requests.get(url, timeout=10)
            response.raise_for_status()
            
            data = response.json()
            if not data.get('result', {}).get('list'):
                raise APIError("Нет данных о соотношении покупок/продаж")
            
            ratio_data = data['result']['list'][0]
            buy_ratio = ratio_data.get('buyRatio', 'N/A')
            sell_ratio = ratio_data.get('sellRatio', 'N/A')
            
            return f"🟢 {buy_ratio}  {sell_ratio} 🔴"
            
        except requests.RequestException as e:
            raise APIError(f"Ошибка запроса соотношения: {e}") from e
    
    @handle_errors("Ошибка создания графика", (None, None))
    @log_function_call()
    def create_price_chart(self, weeks: int = 5) -> Tuple[Optional[io.BytesIO], Optional[float]]:
        """Создать график цены за указанное количество недель"""
        try:
            limit = weeks * 7
            response = self.session.get_kline(
                category="spot", 
                symbol=config.SYMBOL, 
                interval='D', 
                limit=limit
            )
            
            klines = response.get('result', {}).get('list', [])
            if not klines:
                raise APIError("Нет данных для построения графика")
            
            # Сортировка данных за последние 14 дней для поиска минимума
            recent_klines = sorted(klines[:14], key=lambda x: float(x[3]))  # x[3] - low price
            min_price_14d = float(recent_klines[0][3])
            
            # Создание графика
            plt.figure(figsize=(12, 6))
            plt.style.use('dark_background')
            
            width = 0.4
            width2 = 0.05
            green_color = '#00ff88'
            red_color = '#ff4444'
            
            for i, kline in enumerate(klines):
                timestamp, open_p, high_p, low_p, close_p = kline[0], float(kline[1]), float(kline[2]), float(kline[3]), float(kline[4])
                
                color = green_color if close_p > open_p else red_color
                
                # Основная свеча
                plt.bar(i, close_p - open_p, width, bottom=open_p, color=color, alpha=0.8)
                # Тень сверху
                plt.bar(i, high_p - close_p, width2, bottom=close_p, color=color)
                # Тень снизу  
                plt.bar(i, low_p - open_p, width2, bottom=open_p, color=color)
            
            # Линия минимума за 14 дней
            plt.axhline(y=min_price_14d, color='purple', linestyle='--', alpha=0.7, label=f'Min 14d: {min_price_14d:.2f}')
            
            plt.title(f'{config.SYMBOL} - {weeks} недель', color='white', fontsize=14)
            plt.ylabel('Цена (USDT)', color='white')
            plt.xticks([])  # Убираем подписи по X
            plt.legend()
            plt.tight_layout()
            
            # Сохранение в буфер
            buf = io.BytesIO()
            plt.savefig(buf, format='png', dpi=100, bbox_inches='tight')
            buf.seek(0)
            plt.close()
            
            return buf, min_price_14d
            
        except Exception as e:
            plt.close()  # Очистка на случай ошибки
            raise APIError(f"Ошибка создания графика: {e}") from e
    
    def call_api(self, method: str, **kwargs) -> Any:
        """Общий метод для вызова API"""
        try:
            api_method = getattr(self.session, method)
            return api_method(**kwargs)
        except AttributeError:
            raise APIError(f"Метод {method} не найден")
        except Exception as e:
            raise APIError(f"Ошибка вызова API {method}: {e}") from e

# Глобальный экземпляр сервиса
market_service = MarketService()
