"""DeepSeek JSON selector client with bounded output and privacy-safe logging."""

from __future__ import annotations

import json
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from loguru import logger
from openai import OpenAI

from config import (
    DEEPSEEK_API_KEY,
    DEEPSEEK_API_URL,
    DEEPSEEK_LOG_RESPONSES,
    DEEPSEEK_LOG_RETENTION_DAYS,
    DEEPSEEK_MAX_TOKENS,
    DEEPSEEK_MODEL,
    DEEPSEEK_TIMEOUT_SECONDS,
)


class DeepSeekAPI:
    def __init__(
        self,
        api_key: str = DEEPSEEK_API_KEY,
        base_url: str = DEEPSEEK_API_URL,
        model: str = DEEPSEEK_MODEL,
    ) -> None:
        if not api_key:
            raise ValueError("Не задан DEEPSEEK_API_KEY")
        self.model = model
        self.client = OpenAI(
            api_key=api_key,
            base_url=base_url,
            timeout=DEEPSEEK_TIMEOUT_SECONDS,
            # One retry stays inside the snapshot TTL enforced by config.
            max_retries=1,
        )
        self.log_dir = Path(__file__).parent / "deepseek_logs"

    def close(self) -> None:
        self.client.close()

    def validate_model(self) -> None:
        """Fail early with a clear message when a retired model ID is configured."""
        models = self.client.models.list()
        available = {item.id for item in models.data}
        if self.model not in available:
            raise ValueError(
                f"DeepSeek model {self.model!r} недоступна; "
                f"доступны: {', '.join(sorted(available))}"
            )

    def _purge_old_logs(self) -> None:
        if DEEPSEEK_LOG_RETENTION_DAYS < 1 or not self.log_dir.exists():
            return
        cutoff = datetime.now(timezone.utc) - timedelta(days=DEEPSEEK_LOG_RETENTION_DAYS)
        for path in self.log_dir.glob("decision_*.json"):
            try:
                modified = datetime.fromtimestamp(path.stat().st_mtime, timezone.utc)
                if modified < cutoff:
                    path.unlink()
            except OSError as error:
                logger.warning(f"Не удалось очистить старый AI-лог {path.name}: {error}")

    def _save_response_log(self, content: str, context: dict[str, Any]) -> None:
        if not DEEPSEEK_LOG_RESPONSES:
            return
        try:
            self.log_dir.mkdir(exist_ok=True)
            self._purge_old_logs()
            stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
            path = self.log_dir / f"decision_{stamp}_{uuid.uuid4().hex[:8]}.json"
            payload = {
                "created_at": datetime.now(timezone.utc).isoformat(),
                "model": self.model,
                "snapshot_id": context.get("snapshot_id"),
                "response": json.loads(content),
            }
            path.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        except Exception as error:
            logger.warning(f"Не удалось сохранить минимальный AI-лог: {error}")

    def analyze(
        self,
        system_prompt: str,
        context_json: dict[str, Any],
        temperature: float = 0.0,
    ) -> str:
        started = time.monotonic()
        response = self.client.chat.completions.create(
            model=self.model,
            messages=[
                {"role": "system", "content": system_prompt},
                {
                    "role": "user",
                    "content": json.dumps(
                        context_json,
                        ensure_ascii=False,
                        separators=(",", ":"),
                    ),
                },
            ],
            temperature=temperature,
            max_tokens=DEEPSEEK_MAX_TOKENS,
            response_format={"type": "json_object"},
            stream=False,
            extra_body={
                "thinking": {"type": "disabled"},
                "user_id": "crypto-bot-selector",
            },
        )
        if not response.choices:
            raise ValueError("DeepSeek не вернул choices")
        choice = response.choices[0]
        if choice.finish_reason != "stop":
            raise ValueError(
                f"DeepSeek завершил ответ с finish_reason={choice.finish_reason!r}"
            )
        content = choice.message.content
        if not content or not content.strip():
            raise ValueError("DeepSeek вернул пустой JSON")
        content = content.strip()
        # Parse here as an early JSON-mode sanity check.  The domain schema is
        # validated separately by decision_engine.
        try:
            json.loads(content)
        except json.JSONDecodeError as error:
            raise ValueError(f"DeepSeek JSON повреждён: {error}") from error
        logger.info(
            f"DeepSeek {self.model}: {len(content)} символов за "
            f"{time.monotonic() - started:.1f}с"
        )
        self._save_response_log(content, context_json)
        return content
