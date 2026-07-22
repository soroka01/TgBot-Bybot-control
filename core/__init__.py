# core/__init__.py
"""Основная бизнес-логика бота"""

from .market_data import (
    get_market_analysis,
    enrich_context_with_market_data,
    calculate_ema,
    calculate_rsi,
    calculate_macd
)
from .prompt_builder import (
    build_deepseek_prompt,
    get_prompt_summary
)

__all__ = [
    'get_market_analysis',
    'enrich_context_with_market_data',
    'calculate_ema',
    'calculate_rsi',
    'calculate_macd',
    'build_deepseek_prompt',
    'get_prompt_summary'
]
