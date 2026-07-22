# api/deepseek_api.py
import json
from pathlib import Path
from datetime import datetime
from openai import OpenAI

import sys
sys.path.insert(0, str(Path(__file__).parent.parent))

from config import DEEPSEEK_API_KEY, DEEPSEEK_API_URL

class DeepSeekAPI:
    def __init__(self, api_key=DEEPSEEK_API_KEY, base_url=DEEPSEEK_API_URL):
        if not api_key:
            raise ValueError("Не указан DEEPSEEK_API_KEY")
        self.client = OpenAI(api_key=api_key, base_url=base_url)

        # Создаем директорию для логов DeepSeek
        self.log_dir = Path(__file__).parent / "deepseek_logs"
        self.log_dir.mkdir(exist_ok=True)

    def _save_deepseek_log(self, reasoning: str, response: str, context: dict):
        """Сохраняет reasoning и ответ DeepSeek в отдельный файл"""
        try:
            timestamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
            log_file = self.log_dir / f"deepseek_{timestamp}.txt"

            with open(log_file, "w", encoding="utf-8") as f:
                f.write("="*80 + "\n")
                f.write(f"DEEPSEEK LOG - {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n")
                f.write("="*80 + "\n\n")

                # Контекст (сокращенно)
                f.write("📊 КОНТЕКСТ:\n")
                f.write("-"*80 + "\n")
                f.write(json.dumps(context, indent=2, ensure_ascii=False)[:2000])
                if len(json.dumps(context)) > 2000:
                    f.write("\n... (контекст обрезан для краткости)")
                f.write("\n\n")

                # Reasoning (процесс мышления)
                if reasoning:
                    f.write("💭 REASONING (процесс мышления):\n")
                    f.write("-"*80 + "\n")
                    f.write(reasoning)
                    f.write("\n\n")

                # Финальный ответ
                f.write("✅ ФИНАЛЬНЫЙ ОТВЕТ:\n")
                f.write("-"*80 + "\n")
                f.write(response)
                f.write("\n\n")

                f.write("="*80 + "\n")

            return str(log_file)
        except Exception as e:
            from loguru import logger
            logger.warning(f"Не удалось сохранить лог DeepSeek: {e}")
            return None

    def analyze(self, system_prompt: str, context_json: dict, temperature: float = 0.0):
        from loguru import logger

        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": json.dumps(context_json, ensure_ascii=False)}
        ]

        # Используем DeepSeek V3.1 Chat (быстрая модель без reasoning)
        model = "deepseek-chat"
        logger.info(f"🧠 Используем модель: {model}")

        response = self.client.chat.completions.create(
            model=model,
            messages=messages,
            temperature=temperature,
            max_tokens=8192,
            stream=False
        )

        # DeepSeek Chat возвращает только content (без reasoning)
        message = response.choices[0].message
        content = message.content

        # Проверяем reasoning (на случай если модель поменяется)
        reasoning = ""
        if hasattr(message, 'reasoning_content') and message.reasoning_content:
            reasoning = message.reasoning_content
            logger.info(f"💭 DeepSeek reasoning ({len(reasoning)} символов)")

        # Проверяем, что есть ответ
        if not content or content.strip() == "":
            logger.error("❌ DeepSeek не вернул ответ (content пустой)")
            raise ValueError("DeepSeek не вернул ответ. Возможно, не хватает токенов или промпт некорректен.")

        logger.info(f"✅ DeepSeek вернул ответ ({len(content)} символов)")

        # Сохраняем reasoning и ответ в отдельный файл
        log_file = self._save_deepseek_log(reasoning, content, context_json)
        if log_file:
            logger.info(f"📝 Лог DeepSeek сохранен: {log_file}")

        return content.strip()
