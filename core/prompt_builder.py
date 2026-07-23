"""Prompt facade kept for the CLI and Telegram recommendation screen."""

from __future__ import annotations

from typing import Iterable, Optional

from config import (
    AUTO_LEVERAGE,
    MAX_RISK_PER_TRADE_PERCENT,
    MAX_TOTAL_RISK_PERCENT,
    MIN_NET_RISK_REWARD_RATIO,
    TRADABLE_TOKENS,
)
from core.decision_engine import build_selector_prompt


def build_deepseek_prompt(tokens: Optional[Iterable[str]] = None) -> str:
    """Return the selector prompt.

    ``tokens`` remains accepted for source compatibility; the authoritative
    symbol set lives in the signed-in-code snapshot, not in free-form prose.
    """
    del tokens
    return build_selector_prompt()


def get_prompt_summary() -> dict:
    return {
        "tokens": list(TRADABLE_TOKENS),
        "tokens_count": len(TRADABLE_TOKENS),
        "ai_role": "candidate_selector",
        "execution_leverage_cap": AUTO_LEVERAGE,
        "risk_per_trade": f"до {MAX_RISK_PER_TRADE_PERCENT}%",
        "total_risk_limit": f"{MAX_TOTAL_RISK_PERCENT}%",
        "minimum_net_rr": MIN_NET_RISK_REWARD_RATIO,
        "timeframes": ["3m", "5m", "1h", "4h"],
        "candles": "closed_only",
    }
