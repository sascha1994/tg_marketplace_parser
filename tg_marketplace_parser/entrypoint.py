from datetime import datetime, timezone
import json
import logging
import os

from .app import ParserApp
from .config import ConfigError, Settings


class JsonLogFormatter(logging.Formatter):
    """Форматирует записи журнала в однострочный JSON для систем сбора логов."""

    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "timestamp": datetime.fromtimestamp(record.created, tz=timezone.utc).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False)


def configure_logging() -> None:
    level = os.getenv("LOG_LEVEL", "INFO").upper()
    log_format = os.getenv("LOG_FORMAT", "text").strip().lower()
    root = logging.getLogger()
    root.setLevel(level)
    if log_format == "json":
        handler = logging.StreamHandler()
        handler.setFormatter(JsonLogFormatter())
        root.handlers = [handler]
    else:
        logging.basicConfig(
            level=level,
            format="%(asctime)s %(levelname)s %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
            force=True,
        )


async def main() -> None:
    configure_logging()
    try:
        settings = Settings.from_env()
    except ConfigError as exc:
        raise SystemExit(f"Ошибка конфигурации: {exc}") from exc
    app = ParserApp(settings)
    await app.run()
