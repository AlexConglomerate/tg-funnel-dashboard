from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import datetime, timedelta

import httpx
import pytest

from funnel.api import create_app
from funnel.config import FunnelConfig
from funnel.db import Database
from tests.conftest import T0, Clock
from tests.test_webapp_auth import TOKEN, make_init_data

ADMIN = 42


@dataclass
class FakeScheduler:
    last_tick: datetime | None = T0
    paused: bool = False


@pytest.fixture
def scheduler() -> FakeScheduler:
    return FakeScheduler()


@pytest.fixture
async def client(
    db: Database, config: FunnelConfig, clock: Clock, scheduler: FakeScheduler
) -> AsyncIterator[httpx.AsyncClient]:
    app = create_app(
        db=db,
        config=config,
        bot_token=TOKEN,
        admin_ids=frozenset({ADMIN}),
        scheduler=scheduler,
        clock=clock,
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as c:
        yield c


async def test_stats_without_header_401(client: httpx.AsyncClient) -> None:
    assert (await client.get("/api/stats")).status_code == 401


async def test_stats_bad_signature_401(client: httpx.AsyncClient) -> None:
    bad = make_init_data(user_id=ADMIN, token="1:other")
    response = await client.get("/api/stats", headers={"Authorization": f"tma {bad}"})
    assert response.status_code == 401


async def test_stats_not_admin_403(client: httpx.AsyncClient) -> None:
    init = make_init_data(user_id=7)
    response = await client.get("/api/stats", headers={"Authorization": f"tma {init}"})
    assert response.status_code == 403


async def test_stats_admin_ok(client: httpx.AsyncClient) -> None:
    init = make_init_data(user_id=ADMIN)
    response = await client.get("/api/stats", headers={"Authorization": f"tma {init}"})
    assert response.status_code == 200
    body = response.json()
    assert set(body) == {
        "totals",
        "funnel",
        "by_source",
        "timeline",
        "queue",
        "recent",
        "generated_at",
    }
    assert [step["key"] for step in body["funnel"]] == ["start", "phone", "gift", "warmup"]


async def test_index_served(client: httpx.AsyncClient) -> None:
    response = await client.get("/")
    assert response.status_code == 200
    assert "telegram-web-app.js" in response.text


async def test_health_ok(client: httpx.AsyncClient) -> None:
    response = await client.get("/health")
    assert response.status_code == 200
    assert response.json()["status"] == "ok"


async def test_health_stale_tick_503(client: httpx.AsyncClient, scheduler: FakeScheduler) -> None:
    scheduler.last_tick = T0 - timedelta(seconds=11)
    assert (await client.get("/health")).status_code == 503
    scheduler.paused = True  # ждём RetryAfter, это не зависание
    assert (await client.get("/health")).status_code == 200


async def test_health_db_down_503(client: httpx.AsyncClient, db: Database) -> None:
    await db.close()
    response = await client.get("/health")
    assert response.status_code == 503
    assert response.json()["reason"].startswith("db")
