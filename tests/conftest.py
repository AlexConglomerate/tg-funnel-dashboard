from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from funnel.config import FunnelConfig, load_config
from funnel.db import Database

ROOT = Path(__file__).resolve().parents[1]
T0 = datetime(2026, 11, 10, 12, 0, 0, tzinfo=UTC)


class Clock:
    """Управляемые часы для тестов."""

    def __init__(self, now: datetime = T0) -> None:
        self.now = now

    def __call__(self) -> datetime:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += timedelta(seconds=seconds)


@pytest.fixture
def config() -> FunnelConfig:
    return load_config(ROOT / "config" / "funnel.yaml")


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture
async def db(tmp_path: Path) -> AsyncIterator[Database]:
    database = await Database.connect(tmp_path / "test.db")
    yield database
    await database.close()
