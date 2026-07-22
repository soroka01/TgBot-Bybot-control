# core/prompt_builder.py
"""
Модуль для динамической генерации промпта DeepSeek на основе конфигурации
"""
from typing import Iterable, List, Optional
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

from config import (
    TRADABLE_TOKENS,
    MAX_LEVERAGE,
    MAX_RISK_PER_TRADE_PERCENT,
    MAX_TOTAL_RISK_PERCENT,
    MIN_ORDER_SIZE_USDT,
    SYMBOL_LIMITS
)

def get_timeframes_info() -> str:
    """Возвращает информацию о таймфреймах для анализа"""
    return """- Multi-timeframe technical analysis for each token:
  * 3-minute: current moment (EMA20, EMA50, MACD, RSI14, volume, price series)
  * 5-minute: short-term trend (EMA20, EMA50, MACD, RSI14, price series)
  * 1-hour: medium-term trend (EMA20, EMA50, MACD, RSI14, ATR, volume, price series)
  * 4-hour: long-term context (EMA20, EMA50, MACD, RSI14, MACD series)"""

def get_minimum_sizes_section(tokens: Optional[Iterable[str]] = None) -> str:
    """Генерирует секцию с минимальными размерами позиций на основе SYMBOL_LIMITS"""
    lines = ["MINIMUM POSITION SIZES (Bybit requirements):"]

    selected_tokens = tokens if tokens is not None else SYMBOL_LIMITS.keys()
    for token in selected_tokens:
        limits = SYMBOL_LIMITS.get(token)
        if not limits:
            continue
        min_qty = limits["min_qty"]
        lines.append(f"- {token}: minimum {min_qty}")

    lines.append("Ensure your quantity calculations meet these minimums!")

    return "\n".join(lines)

def get_tokens_list(tokens: Optional[Iterable[str]] = None) -> str:
    """Возвращает список токенов для анализа"""
    return ", ".join(tokens if tokens is not None else TRADABLE_TOKENS)

def get_risk_parameters() -> dict:
    """Возвращает параметры риска из конфига"""
    return {
        "max_leverage": MAX_LEVERAGE,
        "risk_per_trade_min": 1,  # Минимум для осторожных сделок
        "risk_per_trade_max": MAX_RISK_PER_TRADE_PERCENT,
        "total_risk_max": MAX_TOTAL_RISK_PERCENT,
        "min_order_size": MIN_ORDER_SIZE_USDT
    }

def build_deepseek_prompt(tokens: Optional[List[str]] = None) -> str:
    """
    Строит полный промпт для DeepSeek на основе текущей конфигурации
    """
    selected_tokens = [token.upper() for token in (tokens or TRADABLE_TOKENS)]
    tokens_text = get_tokens_list(selected_tokens)
    timeframes = get_timeframes_info()
    min_sizes = get_minimum_sizes_section(selected_tokens)
    risk_params = get_risk_parameters()

    # Генерируем JSON example с актуальными токенами
    json_example_tokens = []
    for token in selected_tokens:
        json_example_tokens.append(f'''  "{token}": {{
    "trade_signal_args": {{
      "signal": "hold",
      "quantity": 0.0,
      "profit_target": 0.0,
      "stop_loss": 0.0,
      "invalidation_condition": "",
      "leverage": 1,
      "confidence": 0.5,
      "risk_usd": 0.0
    }}
  }}''')
    json_example = "{{\n" + ",\n".join(json_example_tokens) + "\n}}"
    prompt = f"""You are a trading decision engine for a Bybit futures trading bot.

Analyze the following tokens: {tokens_text}.

You will receive:
- Current market prices
- Existing positions (if any)
- Account balance and available funds
{timeframes}
Primary entry signals come from 3m/5m, but must align with 1h/4h trend direction

Your job:
1. For tokens WITH open positions:
   - Output "hold" to keep the position (update TP/SL if needed)
   - Output "close" only if you see a clear exit signal

2. For tokens WITHOUT positions:
   - Output "hold" (do nothing)
   - OR output "long"/"short" if you see a strong entry opportunity
   - Preserve capital first; use "hold" when there is no clear, high-quality setup
   - Risk per trade: {risk_params['risk_per_trade_min']}-{risk_params['risk_per_trade_max']}% of account equity
   - Use leverage 1-{risk_params['max_leverage']}x only when it is justified by the setup
   - Entry points MUST be technically sound based on indicators
   - TP/SL MUST be set based on chart structure (support/resistance, ATR, volatility)
   - Always set stop_loss to limit risk, but don't make it too tight
   - Minimum order size: ${risk_params['min_order_size']} USDT

{min_sizes}

IMPORTANT: Return only the JSON object. Do not include reasoning, markdown, or explanatory text.

OUTPUT FORMAT (CRITICAL):
After your reasoning, return ONLY valid JSON as your final answer. No markdown, no code blocks, no explanatory text.

Example JSON for all {len(selected_tokens)} tokens:
{json_example}

Required for every token:
- signal: "hold", "close", "long", or "short"
- quantity: coin amount (0 for hold on flat positions)
- profit_target: target price based on chart structure (resistance for LONG, support for SHORT)
- stop_loss: stop loss price based on invalidation level (NOT too tight, use ATR as reference)
- invalidation_condition: reason to exit (can be empty string)
- leverage: integer 1-{risk_params['max_leverage']}
- confidence: 0.0-1.0 (your confidence level - be honest!)
- risk_usd: dollar risk for this trade

TRADING STRATEGY:
- Use 3-minute data for entry timing
- Use 1-hour and 4-hour for trend direction
- Enter on pullbacks in trending markets
- Set TP at key resistance/support levels (not arbitrary percentages)
- Set SL beyond recent swing high/low + some buffer
- Don't trade choppy/ranging markets without clear setup
- Risk more on high-confidence setups (0.8+ confidence)
- Use a minimum risk/reward ratio of 1.5 after accounting for entry and stop distance
- Multiple positions are OK only when their combined stop-loss risk stays within the portfolio limit

CRITICAL TP/SL RULES:
- For LONG positions (Buy): profit_target MUST be HIGHER than current price, stop_loss MUST be LOWER
- For SHORT positions (Sell): profit_target MUST be LOWER than current price, stop_loss MUST be HIGHER
- Example: BTC at $110000, SHORT position → profit_target=$108000 (lower), stop_loss=$112000 (higher)
- For an existing LONG, SL must stay above liquidation; for an existing SHORT, SL must stay below liquidation
- Do not move a stop past liquidation. If the required safe stop is unclear, output "hold" with existing TP/SL values.

For "hold" on existing positions: set quantity to current size, update stop_loss and profit_target.
For "close": set quantity to current size.
For new "long"/"short": calculate quantity from actual stop distance:
quantity = risk_usd / abs(entry_price - stop_loss).
The resulting order must respect both the minimum size and available margin.

FINAL REMINDER:
- Think and reason as much as you need internally
- But your FINAL OUTPUT must be ONLY the JSON object
- Start with {{ and end with }}
- Include exactly these {len(selected_tokens)} tokens: {tokens_text}
- No additional text, no explanations after the JSON
- NEVER use placeholders like {...} in the JSON output
- Output must be valid, parseable JSON for ALL tokens

Example: Available balance = $1000, risk_per_trade = 2% = $20,
entry_price = $50000, stop_loss = $49000 (2% risk),
quantity = $20 / ($50000 - $49000) = 0.002 BTC
"""

    return prompt


def get_prompt_summary() -> dict:
    """
    Возвращает краткую сводку параметров промпта для логирования
    """
    risk_params = get_risk_parameters()

    return {
        "tokens": TRADABLE_TOKENS,
        "tokens_count": len(TRADABLE_TOKENS),
        "max_leverage": risk_params['max_leverage'],
        "risk_per_trade": f"{risk_params['risk_per_trade_min']}-{risk_params['risk_per_trade_max']}%",
        "total_risk_limit": f"{risk_params['total_risk_max']}%",
        "min_order_size": f"${risk_params['min_order_size']}",
        "timeframes": ["3m", "5m", "1h", "4h"],
        "minimum_sizes": {token: limits["min_qty"] for token, limits in SYMBOL_LIMITS.items()}
    }


if __name__ == "__main__":
    # Тест: печатаем сгенерированный промпт
    print("="*80)
    print("GENERATED DEEPSEEK PROMPT")
    print("="*80)
    print(build_deepseek_prompt())
    print("\n")
    print("="*80)
    print("PROMPT SUMMARY")
    print("="*80)
    import json
    print(json.dumps(get_prompt_summary(), indent=2, ensure_ascii=False))
