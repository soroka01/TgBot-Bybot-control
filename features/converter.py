from features.market import get_price_or_change

# Функция для конвертации валют
def get_converted_amount(amount, from_currency, to_currency):
    current_price = get_price_or_change('price')
    if current_price is None:
        return "Данные недоступны"
    
    conversions = {
        ("USD", "BTC"): amount / current_price,
        ("BTC", "USD"): amount * current_price
    }
    
    converted_amount = conversions.get((from_currency, to_currency), "Неподдерживаемая конвертация")
    if isinstance(converted_amount, float):
        return round(converted_amount, 3)
    return converted_amount