from loguru import logger
import sys
from datetime import timezone, timedelta

# UTC+3 timezone
UTC_PLUS_3 = timezone(timedelta(hours=3))

# Настройка логирования: файл + stdout, ротация по размеру, уровни
logger.remove()  # убираем стандартный handler

# Единый формат для консоли и файла (без выравнивания)
log_format = "{time:DD-MM-YYYY HH:mm:ss} | {level} | {message}"

logger.add(
    sys.stdout,
    level="INFO",
    format=f"<green>{{time:DD-MM-YYYY HH:mm:ss}}</green> | <level>{{level}}</level> | <level>{{message}}</level>",
    filter=lambda record: record["extra"].update(time=record["time"].astimezone(UTC_PLUS_3)) or True
)
logger.add(
    "crypto_bot.log",
    rotation="10 MB",
    retention="10 days",
    level="DEBUG",
    encoding="utf-8",
    format=log_format,
    filter=lambda record: record["extra"].update(time=record["time"].astimezone(UTC_PLUS_3)) or True
)

# Пример использования:
# logger.info("Информация")
# logger.error("Ошибка")
# logger.debug("Отладка")
# logger.warning("Внимание")
