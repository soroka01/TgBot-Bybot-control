"""Auto-trading notifications routed into the bot's single editable screen."""

from utils.logger_setup import logger


def send_telegram_message(text: str) -> bool:
    """Compatibility wrapper: update the active UI instead of sending a new message."""
    from telegram_bot.ui import publish_event

    return publish_event(text)


def notify(text: str) -> None:
    """Publish a status event without creating a separate Telegram message."""
    if not send_telegram_message(text):
        logger.debug(f"[Telegram UI unavailable] {text}")
