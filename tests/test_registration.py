from datetime import timedelta
from typing import TypedDict

import pytest

from funnel.config import FunnelConfig
from funnel.db import Database
from funnel.registration import Keyboard, Registration, normalize_phone, parse_source
from tests.conftest import Clock


class UserKw(TypedDict):
    tg_id: int
    username: str | None
    first_name: str | None


TG_ID = 1001
USER = UserKw(tg_id=TG_ID, username="anya", first_name="Аня")


@pytest.fixture
def reg(db: Database, config: FunnelConfig, clock: Clock) -> Registration:
    return Registration(db=db, config=config, clock=clock)


async def send_own_contact(reg: Registration, phone: str = "+7 (999) 123-45-67") -> list[str]:
    replies = await reg.contact(**USER, contact_user_id=TG_ID, phone=phone)
    return [r.text for r in replies]


@pytest.mark.parametrize(
    ("payload", "expected"),
    [
        ("vk_ads", "vk_ads"),
        ("insta-stories_2", "insta-stories_2"),
        ("a" * 64, "a" * 64),
        (None, "direct"),
        ("", "direct"),
        ("a" * 65, "direct"),
        ("vk ads", "direct"),
        ("<script>", "direct"),
        ("рекламa", "direct"),
    ],
)
def test_parse_source(payload: str | None, expected: str) -> None:
    assert parse_source(payload) == expected


def test_normalize_phone() -> None:
    assert normalize_phone("+7 (999) 123-45-67") == "+79991234567"
    assert normalize_phone("79991234567") == "+79991234567"


async def test_start_new_user_asks_phone(
    reg: Registration, db: Database, config: FunnelConfig
) -> None:
    replies = await reg.start(**USER, payload="vk_ads")
    assert [r.text for r in replies] == [config.messages.welcome, config.messages.ask_phone]
    assert replies[1].keyboard is Keyboard.PHONE
    user = await db.get_user(TG_ID)
    assert user is not None and user.source == "vk_ads" and user.phone is None


async def test_repeat_start_keeps_first_source(
    reg: Registration, db: Database, clock: Clock
) -> None:
    await reg.start(**USER, payload="vk_ads")
    clock.advance(30)
    await reg.start(
        **UserKw(tg_id=TG_ID, username="anya", first_name="Анна"), payload="insta_stories"
    )
    user = await db.get_user(TG_ID)
    assert user is not None
    assert user.source == "vk_ads"
    assert user.first_name == "Анна"
    assert user.started_at == clock.now - timedelta(seconds=30)


async def test_own_contact_saves_phone_and_schedules(
    reg: Registration, db: Database, config: FunnelConfig, clock: Clock
) -> None:
    await reg.start(**USER, payload=None)
    clock.advance(5)
    texts = await send_own_contact(reg)
    assert texts == [config.messages.registered]

    user = await db.get_user(TG_ID)
    assert user is not None
    assert user.phone == "+79991234567"
    assert user.phone_at == clock.now

    rows = await db.fetchall(
        "SELECT step_key, send_at, status FROM scheduled_messages ORDER BY send_at"
    )
    assert [(r["step_key"], r["status"]) for r in rows] == [
        (f.key, "pending") for f in config.followups
    ]
    # Отсчёт от момента отправки телефона, а не от /start.
    for row, followup in zip(rows, config.followups, strict=True):
        assert row["send_at"] == (clock.now + timedelta(seconds=followup.delay_seconds)).strftime(
            "%Y-%m-%dT%H:%M:%S.000Z"
        )


async def test_repeat_contact_no_duplicates(
    reg: Registration, db: Database, config: FunnelConfig, clock: Clock
) -> None:
    await reg.start(**USER, payload=None)
    await send_own_contact(reg)
    clock.advance(20)
    texts = await send_own_contact(reg, phone="+7 000 000-00-00")
    assert texts == [config.messages.already_registered]

    rows = await db.fetchall("SELECT COUNT(*) AS n FROM scheduled_messages")
    assert rows[0]["n"] == len(config.followups)
    user = await db.get_user(TG_ID)
    assert user is not None and user.phone == "+79991234567"


async def test_foreign_contact_rejected(
    reg: Registration, db: Database, config: FunnelConfig
) -> None:
    await reg.start(**USER, payload=None)
    replies = await reg.contact(**USER, contact_user_id=555, phone="+79990000000")
    assert [r.text for r in replies] == [config.messages.not_own_contact]
    assert replies[0].keyboard is Keyboard.PHONE
    user = await db.get_user(TG_ID)
    assert user is not None and user.phone is None


async def test_contact_without_user_id_rejected(reg: Registration, config: FunnelConfig) -> None:
    replies = await reg.contact(**USER, contact_user_id=None, phone="+79990000000")
    assert [r.text for r in replies] == [config.messages.not_own_contact]


async def test_start_after_registration(reg: Registration, config: FunnelConfig) -> None:
    await reg.start(**USER, payload=None)
    await send_own_contact(reg)
    replies = await reg.start(**USER, payload="other")
    assert [r.text for r in replies] == [config.messages.already_registered]


async def test_other_message(reg: Registration, config: FunnelConfig) -> None:
    await reg.start(**USER, payload=None)
    replies = await reg.other_message(tg_id=TG_ID)
    assert [(r.text, r.keyboard) for r in replies] == [(config.messages.use_button, Keyboard.PHONE)]
    await send_own_contact(reg)
    assert await reg.other_message(tg_id=TG_ID) == []


async def test_start_clears_blocked(reg: Registration, db: Database, clock: Clock) -> None:
    await reg.start(**USER, payload=None)
    await db.mark_blocked(tg_id=TG_ID, now=clock.now)
    await reg.start(**USER, payload=None)
    user = await db.get_user(TG_ID)
    assert user is not None and user.blocked_at is None


async def test_bot_blocked_marks_user_and_queue(reg: Registration, db: Database) -> None:
    await reg.start(**USER, payload=None)
    await send_own_contact(reg)
    await reg.bot_blocked(tg_id=TG_ID)
    user = await db.get_user(TG_ID)
    assert user is not None and user.blocked_at is not None
    rows = await db.fetchall("SELECT DISTINCT status FROM scheduled_messages")
    assert [r["status"] for r in rows] == ["blocked"]
