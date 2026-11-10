from datetime import timedelta

from funnel.config import FunnelConfig
from funnel.db import Database, to_db
from funnel.stats import compute_stats, mask_phone
from tests.conftest import T0


async def seed(db: Database) -> None:
    """10 пришли: 6 vk_ads, 4 direct. Телефон: 4 из vk_ads, 1 из direct.
    gift доставлен 3 раза, warmup 1 раз, один пользователь заблокировал бота."""
    users = []
    for i in range(10):
        source = "vk_ads" if i < 6 else "direct"
        has_phone = i in (0, 1, 2, 3, 6)
        started = T0 + timedelta(minutes=i)
        users.append(
            (
                100 + i,
                f"user{i}",
                f"Имя{i}",
                source,
                f"+7999000{i:04d}" if has_phone else None,
                to_db(started),
                to_db(started + timedelta(seconds=30)) if has_phone else None,
                to_db(T0) if i == 3 else None,
            )
        )
    for row in users:
        await db.fetchall(
            "INSERT INTO users(tg_id, username, first_name, source, phone,"
            " started_at, phone_at, blocked_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            row,
        )
    messages = [
        (100, "gift", "sent"),
        (101, "gift", "sent"),
        (102, "gift", "sent"),
        (103, "gift", "blocked"),
        (106, "gift", "failed"),
        (100, "warmup", "sent"),
        (101, "warmup", "pending"),
        (102, "warmup", "pending"),
        (103, "warmup", "blocked"),
        (106, "warmup", "pending"),
    ]
    for tg_id, key, status in messages:
        await db.fetchall(
            "INSERT INTO scheduled_messages(tg_id, step_key, send_at, status) VALUES (?, ?, ?, ?)",
            (tg_id, key, to_db(T0), status),
        )


async def test_stats_counts(db: Database, config: FunnelConfig) -> None:
    await seed(db)
    stats = await compute_stats(db=db, config=config, now=T0 + timedelta(hours=1))

    assert stats.totals.model_dump() == {
        "started": 10,
        "with_phone": 5,
        "conversion_pct": 50.0,
        "blocked": 1,
    }

    assert [(s.key, s.count, s.pct_of_prev) for s in stats.funnel] == [
        ("start", 10, None),
        ("phone", 5, 50.0),
        ("gift", 3, 60.0),
        ("warmup", 1, 33.3),
    ]
    assert stats.funnel[2].label == "Сообщение «gift»"

    assert [s.model_dump() for s in stats.by_source] == [
        {"source": "vk_ads", "started": 6, "with_phone": 4, "conversion_pct": 66.7},
        {"source": "direct", "started": 4, "with_phone": 1, "conversion_pct": 25.0},
    ]
    assert stats.queue == {"pending": 3, "sent": 4, "failed": 1, "blocked": 2}


async def test_recent_masks_phone(db: Database, config: FunnelConfig) -> None:
    await seed(db)
    stats = await compute_stats(db=db, config=config, now=T0)
    assert len(stats.recent) == 10
    assert stats.recent[0].name == "Имя9"  # самый поздний /start первым
    phones = [r.phone_masked for r in stats.recent if r.phone_masked]
    assert "+7 *** ***-00-06" in phones
    assert all("9990" not in p for p in phones)  # полный номер не уходит


async def test_timeline_minutes_with_gaps(db: Database, config: FunnelConfig) -> None:
    await seed(db)
    stats = await compute_stats(db=db, config=config, now=T0)
    assert stats.timeline.bucket == "minute"
    points = stats.timeline.points
    assert len(points) == 10  # 12:00 … 12:09, телефон в ту же минуту, что /start
    assert sum(p.started for p in points) == 10
    assert sum(p.with_phone for p in points) == 5
    assert points[0].t == "2026-11-10T12:00:00.000Z"


async def test_timeline_hours_for_long_span(db: Database, config: FunnelConfig) -> None:
    for i, offset in enumerate((0, 5 * 60)):  # минуты
        await db.fetchall(
            "INSERT INTO users(tg_id, source, started_at) VALUES (?, 'direct', ?)",
            (i, to_db(T0 + timedelta(minutes=offset))),
        )
    stats = await compute_stats(db=db, config=config, now=T0)
    assert stats.timeline.bucket == "hour"
    assert [p.started for p in stats.timeline.points] == [1, 0, 0, 0, 0, 1]


async def test_empty_db(db: Database, config: FunnelConfig) -> None:
    stats = await compute_stats(db=db, config=config, now=T0)
    assert stats.totals.conversion_pct == 0.0
    assert stats.funnel[1].pct_of_prev == 0.0
    assert stats.timeline.points == []


def test_mask_phone() -> None:
    assert mask_phone("+79991231234") == "+7 *** ***-12-34"
    assert mask_phone("+380501234567") == "+* *** ***-45-67"
    assert mask_phone(None) is None
