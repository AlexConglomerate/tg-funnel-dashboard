"""Отправка отложенных сообщений из базы и общий ограничитель скорости."""

import asyncio
import contextlib
import logging
import time
from collections.abc import Callable
from datetime import datetime, timedelta
from typing import Protocol

from aiogram.exceptions import TelegramForbiddenError, TelegramRetryAfter

from funnel.config import FunnelConfig
from funnel.db import Database, ScheduledMessage, utcnow
from funnel.log import tg_id_ctx

log = logging.getLogger(__name__)


class RateLimiter:
    """Token bucket: в среднем не больше `rate` запросов в секунду, всплеск до `burst`.

    `pause()` останавливает все отправки, когда Telegram ответил `RetryAfter`.
    """

    def __init__(
        self,
        *,
        rate: float,
        burst: int = 5,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._rate = rate
        self._burst = burst
        self._clock = clock
        self._tokens = float(burst)
        self._updated = clock()
        self._paused_until = 0.0
        self._lock = asyncio.Lock()

    @property
    def paused(self) -> bool:
        return self._clock() < self._paused_until

    def pause(self, seconds: float) -> None:
        self._paused_until = max(self._paused_until, self._clock() + seconds)

    async def acquire(self) -> None:
        # Под замком ждёт один, остальные стоят в очереди за ним: порядок сохраняется.
        async with self._lock:
            while True:
                now = self._clock()
                if now < self._paused_until:
                    await asyncio.sleep(self._paused_until - now)
                    continue
                self._tokens = min(self._burst, self._tokens + (now - self._updated) * self._rate)
                self._updated = now
                # Допуск на погрешность float: иначе 0.999… ждал бы бесконечно малые интервалы.
                if self._tokens >= 1 - 1e-9:
                    self._tokens = max(0.0, self._tokens - 1)
                    return
                await asyncio.sleep((1 - self._tokens) / self._rate)


class Sender(Protocol):
    async def __call__(self, *, chat_id: int, text: str) -> None: ...


class Scheduler:
    def __init__(
        self,
        *,
        db: Database,
        config: FunnelConfig,
        send: Sender,
        limiter: RateLimiter | None = None,
        clock: Callable[[], datetime] = utcnow,
        batch_size: int = 50,
        max_attempts: int = 3,
        retry_delay: timedelta = timedelta(seconds=10),
        interval: float = 1.0,
    ) -> None:
        self._db = db
        self._config = config
        self._send = send
        self._limiter = limiter
        self._clock = clock
        self._batch_size = batch_size
        self._max_attempts = max_attempts
        self._retry_delay = retry_delay
        self._interval = interval
        self._stopped = asyncio.Event()
        self.last_tick: datetime | None = None

    @property
    def paused(self) -> bool:
        """Отправка стоит из-за `RetryAfter`: проход цикла может затянуться, это не зависание."""
        return self._limiter is not None and self._limiter.paused

    def stop(self) -> None:
        self._stopped.set()

    async def run_forever(self) -> None:
        log.info("scheduler started batch=%d", self._batch_size)
        while not self._stopped.is_set():
            processed = 0
            try:
                processed = await self.run_once()
            except Exception:
                log.exception("scheduler pass failed")
            if processed < self._batch_size:
                # Пачка неполная: созревшее разобрали, ждём следующего тика.
                # Полная пачка: возможно, есть ещё созревшие, берём сразу без паузы.
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(self._stopped.wait(), timeout=self._interval)
        log.info("scheduler stopped")

    async def run_once(self) -> int:
        """Один проход: отправить созревшие сообщения. Возвращает размер пачки."""
        self.last_tick = self._clock()
        due = await self._db.fetch_due(now=self.last_tick, limit=self._batch_size)
        if due:
            # Параллельно, а скорость держит ограничитель: иначе задержка сети
            # ограничила бы отправку несколькими сообщениями в секунду.
            await asyncio.gather(*(self._deliver(message) for message in due))
        return len(due)

    async def _deliver(self, message: ScheduledMessage) -> None:
        token = tg_id_ctx.set(message.tg_id)
        try:
            await self._deliver_one(message)
        finally:
            tg_id_ctx.reset(token)

    async def _deliver_one(self, message: ScheduledMessage) -> None:
        text = self._config.followup_text(message.step_key)
        if text is None:
            error = f"step_key {message.step_key!r} нет в followups конфига"
            await self._db.mark_failed(message_id=message.id, error=error)
            log.error("followup_failed step=%s error=%s", message.step_key, error)
            return

        try:
            await self._send(chat_id=message.tg_id, text=text)
        except TelegramForbiddenError:
            await self._db.mark_blocked(tg_id=message.tg_id, now=self._clock())
            log.info("user_blocked step=%s", message.step_key)
        except TelegramRetryAfter as exc:
            # Строка остаётся pending, попытка не считается; ограничитель уже на паузе.
            if self._limiter is not None:
                self._limiter.pause(exc.retry_after)
            log.warning(
                "followup_retry_after step=%s seconds=%s", message.step_key, exc.retry_after
            )
        except Exception as exc:
            error = f"{type(exc).__name__}: {exc}"
            status, attempts = await self._db.record_attempt_error(
                message_id=message.id,
                error=error,
                max_attempts=self._max_attempts,
                retry_at=self._clock() + self._retry_delay,
            )
            if status == "failed":
                log.error(
                    "followup_failed step=%s attempts=%d error=%s",
                    message.step_key,
                    attempts,
                    error,
                )
            else:
                log.warning(
                    "followup_error step=%s attempts=%d error=%s",
                    message.step_key,
                    attempts,
                    error,
                )
        else:
            await self._db.mark_sent(message_id=message.id, now=self._clock())
            log.info("followup_sent step=%s", message.step_key)
