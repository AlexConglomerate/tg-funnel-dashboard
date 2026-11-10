"""Подсчёт статистики для дашборда."""

from datetime import UTC, datetime, timedelta
from typing import Literal

from pydantic import BaseModel

from funnel.config import FunnelConfig
from funnel.db import STATUSES, Database, from_db, to_db

RECENT_LIMIT = 10
MINUTE_BUCKETS_UNDER = timedelta(hours=3)


class Totals(BaseModel):
    started: int
    with_phone: int
    conversion_pct: float
    blocked: int


class FunnelStep(BaseModel):
    key: str
    label: str
    count: int
    pct_of_prev: float | None


class SourceRow(BaseModel):
    source: str
    started: int
    with_phone: int
    conversion_pct: float


class TimelinePoint(BaseModel):
    t: str
    started: int
    with_phone: int


class Timeline(BaseModel):
    bucket: Literal["minute", "hour"]
    points: list[TimelinePoint]


class RecentUser(BaseModel):
    name: str | None
    username: str | None
    source: str
    phone_masked: str | None
    started_at: str
    phone_at: str | None


class Stats(BaseModel):
    totals: Totals
    funnel: list[FunnelStep]
    by_source: list[SourceRow]
    timeline: Timeline
    queue: dict[str, int]
    recent: list[RecentUser]
    generated_at: str


def pct(*, part: int, whole: int) -> float:
    return round(part * 100 / whole, 1) if whole else 0.0


def mask_phone(phone: str | None) -> str | None:
    """Видны только 4 последние цифры. Для российских номеров оставляем `+7`, как в спеке."""
    if phone is None:
        return None
    digits = "".join(ch for ch in phone if ch.isdigit())
    if len(digits) < 4:
        return "***"
    tail = f"{digits[-4:-2]}-{digits[-2:]}"
    prefix = "+7" if len(digits) == 11 and digits[0] == "7" else "+*"
    return f"{prefix} *** ***-{tail}"


async def _funnel(*, db: Database, config: FunnelConfig, totals: Totals) -> list[FunnelStep]:
    rows = await db.fetchall(
        "SELECT step_key, COUNT(DISTINCT tg_id) AS n FROM scheduled_messages"
        " WHERE status = 'sent' GROUP BY step_key"
    )
    sent = {row["step_key"]: int(row["n"]) for row in rows}
    steps = [
        ("start", "Нажали /start", totals.started),
        ("phone", "Оставили телефон", totals.with_phone),
        *((f.key, f"Сообщение «{f.key}»", sent.get(f.key, 0)) for f in config.followups),
    ]
    result: list[FunnelStep] = []
    prev: int | None = None
    for key, label, count in steps:
        result.append(
            FunnelStep(
                key=key,
                label=label,
                count=count,
                pct_of_prev=None if prev is None else pct(part=count, whole=prev),
            )
        )
        prev = count
    return result


def _bucket_start(*, dt: datetime, bucket: Literal["minute", "hour"]) -> datetime:
    dt = dt.replace(second=0, microsecond=0)
    return dt.replace(minute=0) if bucket == "hour" else dt


async def _timeline(db: Database) -> Timeline:
    started = [from_db(row[0]) for row in await db.fetchall("SELECT started_at FROM users")]
    phones = [
        from_db(row[0])
        for row in await db.fetchall("SELECT phone_at FROM users WHERE phone_at IS NOT NULL")
    ]
    events = started + phones
    if not events:
        return Timeline(bucket="minute", points=[])

    first, last = min(events), max(events)
    bucket: Literal["minute", "hour"] = "minute" if last - first < MINUTE_BUCKETS_UNDER else "hour"
    step = timedelta(minutes=1) if bucket == "minute" else timedelta(hours=1)

    counts: dict[datetime, list[int]] = {}
    cursor = _bucket_start(dt=first, bucket=bucket)
    end = _bucket_start(dt=last, bucket=bucket)
    while cursor <= end:  # пустые интервалы тоже нужны: ось времени честная
        counts[cursor] = [0, 0]
        cursor += step
    for dt in started:
        counts[_bucket_start(dt=dt, bucket=bucket)][0] += 1
    for dt in phones:
        counts[_bucket_start(dt=dt, bucket=bucket)][1] += 1

    return Timeline(
        bucket=bucket,
        points=[TimelinePoint(t=to_db(t), started=s, with_phone=p) for t, (s, p) in counts.items()],
    )


async def compute_stats(*, db: Database, config: FunnelConfig, now: datetime) -> Stats:
    row = await db.fetchone(
        "SELECT COUNT(*) AS started,"
        " COUNT(phone) AS with_phone,"
        " COUNT(blocked_at) AS blocked FROM users"
    )
    assert row is not None
    totals = Totals(
        started=row["started"],
        with_phone=row["with_phone"],
        blocked=row["blocked"],
        conversion_pct=pct(part=row["with_phone"], whole=row["started"]),
    )

    by_source = [
        SourceRow(
            source=r["source"],
            started=r["started"],
            with_phone=r["with_phone"],
            conversion_pct=pct(part=r["with_phone"], whole=r["started"]),
        )
        for r in await db.fetchall(
            "SELECT source, COUNT(*) AS started, COUNT(phone) AS with_phone FROM users"
            " GROUP BY source ORDER BY started DESC, source"
        )
    ]

    queue = dict.fromkeys(STATUSES, 0)
    for r in await db.fetchall(
        "SELECT status, COUNT(*) AS n FROM scheduled_messages GROUP BY status"
    ):
        queue[r["status"]] = r["n"]

    recent = [
        RecentUser(
            name=r["first_name"],
            username=r["username"],
            source=r["source"],
            phone_masked=mask_phone(r["phone"]),
            started_at=r["started_at"],
            phone_at=r["phone_at"],
        )
        for r in await db.fetchall(
            "SELECT first_name, username, source, phone, started_at, phone_at FROM users"
            " ORDER BY started_at DESC LIMIT ?",
            (RECENT_LIMIT,),
        )
    ]

    return Stats(
        totals=totals,
        funnel=await _funnel(db=db, config=config, totals=totals),
        by_source=by_source,
        timeline=await _timeline(db),
        queue=queue,
        recent=recent,
        generated_at=to_db(now.astimezone(UTC)),
    )
