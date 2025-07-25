"""
Сервис для конвертации валют
"""
from typing import Union

from core.base_service import BaseService
from core.decorators import handle_errors, log_function_call
from core.exceptions import ValidationError
from services.market_service import market_service

class ConverterService(BaseService):
    """Сервис для конвертации валют"""
    
    def __init__(self):
        super().__init__()
        self.market_service = market_service
    
    @handle_errors("Ошибка валидации суммы", False)
    def _validate_amount(self, amount: float) -> bool:
        """Валидация суммы для конвертации"""
        if amount <= 0:
            raise ValidationError("Сумма должна быть положительной")
        
        if amount > 1e12:  # Защита от слишком больших чисел
            raise ValidationError("Сумма слишком большая")
        
        return True
    
    @handle_errors("Данные недоступны")
    @log_function_call()
    def convert_usd_to_btc(self, usd_amount: float) -> float:
        """Конвертировать USD в BTC"""
        self._validate_amount(usd_amount)
        
        current_price = self.market_service.get_current_price()
        btc_amount = usd_amount / current_price
        
        return round(btc_amount, 8)  # BTC обычно отображается с точностью до 8 знаков
    
    @handle_errors("Данные недоступны")
    @log_function_call()
    def convert_btc_to_usd(self, btc_amount: float) -> float:
        """Конвертировать BTC в USD"""
        self._validate_amount(btc_amount)
        
        current_price = self.market_service.get_current_price()
        usd_amount = btc_amount * current_price
        
        return round(usd_amount, 2)  # USD отображается с точностью до 2 знаков
    
    @handle_errors("Неподдерживаемая конвертация")
    def convert(self, amount: float, from_currency: str, to_currency: str) -> Union[float, str]:
        """Универсальный метод конвертации"""
        from_currency = from_currency.upper()
        to_currency = to_currency.upper()
        
        conversion_map = {
            ("USD", "BTC"): self.convert_usd_to_btc,
            ("BTC", "USD"): self.convert_btc_to_usd,
        }
        
        converter = conversion_map.get((from_currency, to_currency))
        if not converter:
            available_pairs = ", ".join([f"{f}->{t}" for f, t in conversion_map.keys()])
            raise ValidationError(f"Неподдерживаемая пара валют. Доступные: {available_pairs}")
        
        return converter(amount)
    
    def format_conversion_result(self, amount: float, from_currency: str, 
                               to_currency: str, result: float) -> str:
        """Форматировать результат конвертации"""
        from_currency = from_currency.upper()
        to_currency = to_currency.upper()
        
        # Определяем точность отображения
        if to_currency == "BTC":
            precision = 8
        else:
            precision = 2
        
        return (
            f"💱 Конвертация:\n"
            f"📥 {amount} {from_currency}\n"
            f"📤 {result:.{precision}f} {to_currency}\n"
            f"💲 По курсу: {self.market_service.get_current_price():.2f} USD/BTC"
        )

# Глобальный экземпляр сервиса
converter_service = ConverterService()
