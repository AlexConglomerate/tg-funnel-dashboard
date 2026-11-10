"""Лог-обработчик: записи уровня ERROR уходят сообщением всем админам."""

import asyncio
import contextlib
import logging
import os
import time
import traceback
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from typing import Protocol

log = logging.getLogger(__name__)


class AlertSender(Protocol):
    async def __call__(self, *, chat_id: int, text: str) -> None: ...


@dataclass
class _Window:
    last_sent: float
    suppressed: int = 0


PROJECT_DIR = os.path.dirname(os.path.abspath(__file__))


def _short(filename: str) -> str:
    with contextlib.suppress(ValueError):
        return os.path.relpath(filename)
    return filename


def _location(record: logging.LogRecord) -> str:
    """Файл:строка, где упало: последняя строка traceback, иначе место вызова лога.

    Если последний кадр внутри библиотеки, добавляем последний кадр из нашего кода.
    """
    if not (record.exc_info and record.exc_info[2] is not None):
        return f"{_short(record.pathname)}:{record.lineno}"
    frames = traceback.extract_tb(record.exc_info[2])
    last = frames[-1]
    location = f"{_short(last.filename)}:{last.lineno}"
    if not last.filename.startswith(PROJECT_DIR):
        ours = [f for f in frames if f.filename.startswith(PROJECT_DIR)]
        if ours:
            location += f" (из {_short(ours[-1].filename)}:{ours[-1].lineno})"
    return location


def _error_type(record: logging.LogRecord) -> str:
    if record.exc_info and record.exc_info[0] is not None:
        return record.exc_info[0].__name__
    return str(record.msg)


class TelegramAlertHandler(logging.Handler):
    """Шлёт алерты асинхронно и никогда не роняет код, который пишет в лог.

    Антиспам: одинаковая ошибка (модуль + тип исключения) не чаще раза в `cooldown`
    секунд, пропущенные повторы досчитываются в следующий алерт.
    """

    def __init__(
        self,
        *,
        send: AlertSender,
        admin_ids: Iterable[int],
        cooldown: float = 60.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        super().__init__(level=logging.ERROR)
        self._send = send
        self._admin_ids = tuple(admin_ids)
        self._cooldown = cooldown
        self._clock = clock
        self._windows: dict[tuple[str, str], _Window] = {}
        self._loop: asyncio.AbstractEventLoop | None = None
        self._tasks: set[asyncio.Task[None]] = set()

    def attach(self, loop: asyncio.AbstractEventLoop) -> None:
        self._loop = loop

    def build_text(self, record: logging.LogRecord) -> str | None:
        """Текст алерта или None, если сработал антиспам."""
        key = (record.name, _error_type(record))
        now = self._clock()
        window = self._windows.get(key)
        if window is not None and now - window.last_sent < self._cooldown:
            window.suppressed += 1
            return None
        suppressed = window.suppressed if window is not None else 0
        self._windows[key] = _Window(last_sent=now)

        message = record.getMessage()
        if record.exc_info and record.exc_info[1] is not None:
            exc = record.exc_info[1]
            message = f"{message}\n{type(exc).__name__}: {exc}"
        lines = [
            f"🚨 {record.levelname} в {record.name}",
            message[:3000],
            f"📍 {_location(record)}",
        ]
        if suppressed:
            lines.append(f"…и ещё {suppressed} раз за последние {int(self._cooldown)} с")
        return "\n".join(lines)

    def emit(self, record: logging.LogRecord) -> None:
        # Свои ошибки не алертим: иначе сбой отправки алерта зациклился бы.
        if record.name == log.name or self._loop is None or self._loop.is_closed():
            return
        try:
            text = self.build_text(record)
            if text is None:
                return
            self._loop.call_soon_threadsafe(self._spawn, text)
        except Exception:
            self.handleError(record)

    def _spawn(self, text: str) -> None:
        task = asyncio.ensure_future(self._deliver(text))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _deliver(self, text: str) -> None:
        for admin_id in self._admin_ids:
            try:
                await self._send(chat_id=admin_id, text=text)
            except Exception as exc:
                log.warning(
                    "alert_send_failed admin=%s error=%s: %s", admin_id, type(exc).__name__, exc
                )

    async def drain(self) -> None:
        """Дождаться отправки уже поставленных алертов (при остановке)."""
        if self._tasks:
            await asyncio.wait(self._tasks, timeout=5)
