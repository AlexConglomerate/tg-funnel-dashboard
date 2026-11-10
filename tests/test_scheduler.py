import asyncio
from collections.abc import Callable

import pytest
from aiogram.exceptions import TelegramForbiddenError, TelegramNetworkError, TelegramRetryAfter
from aiogram.methods import SendMessage

from funnel.config import FunnelConfig
from funnel.db import Database
from funnel.registration import Registration
from funnel.scheduler import RateLimiter, Scheduler
from tests.conftest import Clock

TG_ID = 2002


def method() -> SendMessage:
    return SendMessage(chat_id=TG_ID, text="x")


class FakeSender:
    """Записывает отправки; `fail` решает, что бросить для очередной отправки."""

    def __init__(
        self, fail: Callable[[int, str], Exception | None] = lambda chat_id, text: None
    ) -> None:
        self.sent: list[tuple[int, str]] = []
        self.calls = 0
        self._fail = fail

    async def __call__(self, *, chat_id: int, text: str) -> None:
        self.calls += 1
        error = self._fail(chat_id, text)
        if error is not None:
            raise error
        self.sent.append((chat_id, text))


async def register(*, db: Database, config: FunnelConfig, clock: Clock, tg_id: int = TG_ID) -> None:
    reg = Registration(db=db, config=config, clock=clock)
    await reg.start(tg_id=tg_id, username=None, first_name="Тест", payload=None)
    await reg.contact(
        tg_id=tg_id, username=None, first_name="Тест", contact_user_id=tg_id, phone="+79990001122"
    )


async def statuses(db: Database) -> dict[str, str]:
    rows = await db.fetchall(
        "SELECT step_key, status FROM scheduled_messages WHERE tg_id = ?", (TG_ID,)
    )
    return {r["step_key"]: r["status"] for r in rows}


def make(
    *,
    db: Database,
    config: FunnelConfig,
    clock: Clock,
    sender: FakeSender,
    limiter: RateLimiter | None = None,
) -> Scheduler:
    return Scheduler(db=db, config=config, send=sender, clock=clock, limiter=limiter)


async def test_due_message_sent_and_not_due_untouched(
    db: Database, config: FunnelConfig, clock: Clock
) -> None:
    await register(db=db, config=config, clock=clock)
    sender = FakeSender()
    scheduler = make(db=db, config=config, clock=clock, sender=sender)

    clock.advance(9)
    assert await scheduler.run_once() == 0
    assert sender.sent == []

    clock.advance(1)
    assert await scheduler.run_once() == 1
    assert sender.sent == [(TG_ID, config.followups[0].text)]
    assert await statuses(db) == {"gift": "sent", "warmup": "pending"}
    assert scheduler.last_tick == clock.now

    clock.advance(50)
    await scheduler.run_once()
    assert await statuses(db) == {"gift": "sent", "warmup": "sent"}
    assert len(sender.sent) == 2


async def test_overdue_after_restart_sent_at_once(
    db: Database, config: FunnelConfig, clock: Clock
) -> None:
    await register(db=db, config=config, clock=clock)
    clock.advance(3600)  # процесс лежал час
    sender = FakeSender()
    await make(db=db, config=config, clock=clock, sender=sender).run_once()
    assert [text for _, text in sender.sent] == [f.text for f in config.followups]


async def test_text_taken_from_config_at_send_time(
    db: Database, config: FunnelConfig, clock: Clock
) -> None:
    await register(db=db, config=config, clock=clock)
    edited = config.model_copy(
        update={
            "followups": [
                config.followups[0].model_copy(update={"text": "Новый текст"}),
                config.followups[1],
            ]
        }
    )
    sender = FakeSender()
    clock.advance(10)
    await make(db=db, config=edited, clock=clock, sender=sender).run_once()
    assert sender.sent == [(TG_ID, "Новый текст")]


async def test_removed_key_fails_with_reason(
    db: Database, config: FunnelConfig, clock: Clock
) -> None:
    await register(db=db, config=config, clock=clock)
    edited = config.model_copy(update={"followups": [config.followups[1]]})
    clock.advance(10)
    await make(db=db, config=edited, clock=clock, sender=FakeSender()).run_once()
    row = await db.fetchone(
        "SELECT status, last_error FROM scheduled_messages WHERE step_key = 'gift'"
    )
    assert row is not None
    assert row["status"] == "failed"
    assert "gift" in row["last_error"]


async def test_forbidden_blocks_all_pending(
    db: Database, config: FunnelConfig, clock: Clock
) -> None:
    await register(db=db, config=config, clock=clock)
    sender = FakeSender(
        lambda chat_id, text: TelegramForbiddenError(method(), "bot was blocked by the user")
    )
    clock.advance(10)
    await make(db=db, config=config, clock=clock, sender=sender).run_once()

    assert await statuses(db) == {"gift": "blocked", "warmup": "blocked"}
    user = await db.get_user(TG_ID)
    assert user is not None and user.blocked_at == clock.now

    clock.advance(100)  # warmup созрел бы, но он уже blocked
    await make(db=db, config=config, clock=clock, sender=sender).run_once()
    assert sender.calls == 1


async def test_retry_after_keeps_pending(db: Database, config: FunnelConfig, clock: Clock) -> None:
    await register(db=db, config=config, clock=clock)
    errors: list[Exception | None] = [TelegramRetryAfter(method(), "flood", retry_after=3), None]
    sender = FakeSender(lambda chat_id, text: errors.pop(0))
    limiter = RateLimiter(rate=1000)
    scheduler = make(db=db, config=config, clock=clock, sender=sender, limiter=limiter)
    clock.advance(10)

    await scheduler.run_once()
    row = await db.fetchone(
        "SELECT status, attempts FROM scheduled_messages WHERE step_key = 'gift'"
    )
    assert row is not None and (row["status"], row["attempts"]) == ("pending", 0)
    assert scheduler.paused

    await scheduler.run_once()
    assert await statuses(db) == {"gift": "sent", "warmup": "pending"}


async def test_three_errors_mark_failed(db: Database, config: FunnelConfig, clock: Clock) -> None:
    await register(db=db, config=config, clock=clock)
    sender = FakeSender(lambda chat_id, text: TelegramNetworkError(method(), "timeout"))
    scheduler = make(db=db, config=config, clock=clock, sender=sender)
    clock.advance(10)

    for attempt in (1, 2):
        await scheduler.run_once()
        row = await db.fetchone(
            "SELECT status, attempts FROM scheduled_messages WHERE step_key = 'gift'"
        )
        assert row is not None and (row["status"], row["attempts"]) == ("pending", attempt)
        clock.advance(10)  # повтор не раньше, чем через retry_delay

    await scheduler.run_once()
    row = await db.fetchone(
        "SELECT status, attempts, last_error FROM scheduled_messages WHERE step_key = 'gift'"
    )
    assert row is not None
    assert (row["status"], row["attempts"]) == ("failed", 3)
    assert "TelegramNetworkError" in row["last_error"]


async def test_batch_sent_in_parallel(db: Database, config: FunnelConfig, clock: Clock) -> None:
    for tg_id in range(1, 21):
        await register(db=db, config=config, clock=clock, tg_id=tg_id)
    in_flight = 0
    peak = 0

    class SlowSender:
        async def __call__(self, *, chat_id: int, text: str) -> None:
            nonlocal in_flight, peak
            in_flight += 1
            peak = max(peak, in_flight)
            await asyncio.sleep(0.01)
            in_flight -= 1

    clock.advance(10)
    scheduler = Scheduler(db=db, config=config, send=SlowSender(), clock=clock)
    assert await scheduler.run_once() == 20
    assert peak > 1


async def test_rate_limiter_spaces_requests(monkeypatch: pytest.MonkeyPatch) -> None:
    fake_time = [0.0]

    async def fake_sleep(seconds: float) -> None:
        fake_time[0] += seconds

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)
    limiter = RateLimiter(rate=25, burst=5, clock=lambda: fake_time[0])
    for _ in range(30):
        await limiter.acquire()
    # 5 из запаса сразу, остальные 25 по одному на 1/25 секунды.
    assert fake_time[0] == pytest.approx(1.0)


async def test_rate_limiter_pause(monkeypatch: pytest.MonkeyPatch) -> None:
    fake_time = [0.0]

    async def fake_sleep(seconds: float) -> None:
        fake_time[0] += seconds

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)
    limiter = RateLimiter(rate=25, burst=5, clock=lambda: fake_time[0])
    limiter.pause(7)
    assert limiter.paused
    await limiter.acquire()
    assert fake_time[0] == pytest.approx(7.0)
    assert not limiter.paused
