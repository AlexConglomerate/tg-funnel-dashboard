"""Формат логов: `время уровень модуль [tg_id=…] сообщение`."""

import logging
import sys
from contextvars import ContextVar

# Текущий пользователь: ставится в middleware бота и в планировщике на время отправки.
tg_id_ctx: ContextVar[int | None] = ContextVar("tg_id", default=None)

FORMAT = "%(asctime)s %(levelname)-7s %(name)s%(ctx)s %(message)s"


class ContextFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        tg_id = tg_id_ctx.get()
        record.ctx = f" tg_id={tg_id}" if tg_id is not None else ""
        return True


def setup_logging(*, level: str) -> None:
    handler = logging.StreamHandler(sys.stdout)
    handler.addFilter(ContextFilter())
    handler.setFormatter(logging.Formatter(FORMAT))
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(level)
    # Без этого на пике каждая строка апдейта и каждый запрос дашборда попадали бы в лог.
    logging.getLogger("aiogram.event").setLevel(logging.WARNING)
    logging.getLogger("uvicorn.access").setLevel(logging.WARNING)
    logging.getLogger("aiosqlite").setLevel(logging.WARNING)
