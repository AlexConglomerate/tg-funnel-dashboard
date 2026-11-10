"""SQLite: схема и запросы. Чистый SQL, одно соединение на процесс."""

import asyncio
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import aiosqlite

SCHEMA = """
CREATE TABLE IF NOT EXISTS users(
  tg_id        INTEGER PRIMARY KEY,
  username     TEXT,
  first_name   TEXT,
  source       TEXT NOT NULL DEFAULT 'direct',
  phone        TEXT,
  started_at   TEXT NOT NULL,
  phone_at     TEXT,
  blocked_at   TEXT
);
CREATE TABLE IF NOT EXISTS scheduled_messages(
  id           INTEGER PRIMARY KEY,
  tg_id        INTEGER NOT NULL REFERENCES users(tg_id),
  step_key     TEXT NOT NULL,
  send_at      TEXT NOT NULL,
  status       TEXT NOT NULL,
  attempts     INTEGER NOT NULL DEFAULT 0,
  last_error   TEXT,
  sent_at      TEXT,
  UNIQUE(tg_id, step_key)
);
CREATE INDEX IF NOT EXISTS scheduled_messages_status_send_at
  ON scheduled_messages(status, send_at);
CREATE INDEX IF NOT EXISTS users_started_at ON users(started_at);
"""

STATUSES = ("pending", "sent", "failed", "blocked")


def utcnow() -> datetime:
    return datetime.now(UTC)


def to_db(dt: datetime) -> str:
    """UTC с миллисекундами и `Z`: фиксированная длина, строки сравниваются как время."""
    return dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def from_db(value: str) -> datetime:
    return datetime.fromisoformat(value).astimezone(UTC)


@dataclass(frozen=True)
class User:
    tg_id: int
    username: str | None
    first_name: str | None
    source: str
    phone: str | None
    started_at: datetime
    phone_at: datetime | None
    blocked_at: datetime | None


@dataclass(frozen=True)
class ScheduledMessage:
    id: int
    tg_id: int
    step_key: str
    send_at: datetime
    status: str
    attempts: int


def _user(row: aiosqlite.Row) -> User:
    return User(
        tg_id=row["tg_id"],
        username=row["username"],
        first_name=row["first_name"],
        source=row["source"],
        phone=row["phone"],
        started_at=from_db(row["started_at"]),
        phone_at=from_db(row["phone_at"]) if row["phone_at"] else None,
        blocked_at=from_db(row["blocked_at"]) if row["blocked_at"] else None,
    )


class Database:
    """Обёртка над одним соединением aiosqlite.

    Все запросы идут в одном потоке aiosqlite по очереди. Многошаговые записи
    выполняются под `asyncio.Lock`, чтобы транзакции разных корутин не перемешивались.
    """

    def __init__(self, conn: aiosqlite.Connection) -> None:
        self._conn = conn
        self._write_lock = asyncio.Lock()

    @classmethod
    async def connect(cls, path: Path | str) -> "Database":
        if str(path) != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        conn = await aiosqlite.connect(path, isolation_level=None)
        conn.row_factory = aiosqlite.Row
        await conn.execute("PRAGMA journal_mode=WAL")
        await conn.execute("PRAGMA synchronous=NORMAL")
        await conn.execute("PRAGMA foreign_keys=ON")
        await conn.execute("PRAGMA busy_timeout=5000")
        db = cls(conn)
        await db.init_schema()
        return db

    async def close(self) -> None:
        await self._conn.close()

    async def init_schema(self) -> None:
        await self._conn.executescript(SCHEMA)

    async def ping(self) -> None:
        await self._conn.execute_fetchall("SELECT 1")

    async def fetchall(self, sql: str, params: Sequence[Any] = ()) -> list[aiosqlite.Row]:
        return list(await self._conn.execute_fetchall(sql, params))

    async def fetchone(self, sql: str, params: Sequence[Any] = ()) -> aiosqlite.Row | None:
        rows = await self.fetchall(sql, params)
        return rows[0] if rows else None

    # --- users ---

    async def get_user(self, tg_id: int) -> User | None:
        row = await self.fetchone("SELECT * FROM users WHERE tg_id = ?", (tg_id,))
        return _user(row) if row else None

    async def upsert_user(
        self,
        *,
        tg_id: int,
        username: str | None,
        first_name: str | None,
        source: str,
        now: datetime,
    ) -> tuple[User, bool]:
        """Создаёт пользователя или обновляет имя. `source` пишется только при создании.

        Возвращает пользователя и признак «создан сейчас». Пометку о блокировке снимает:
        раз человек снова пишет боту, он его разблокировал.
        """
        async with self._write_lock:
            await self._conn.execute("BEGIN IMMEDIATE")
            try:
                cursor = await self._conn.execute(
                    "INSERT OR IGNORE INTO users(tg_id, username, first_name, source, started_at)"
                    " VALUES (?, ?, ?, ?, ?)",
                    (tg_id, username, first_name, source, to_db(now)),
                )
                created = cursor.rowcount == 1
                if not created:
                    await self._conn.execute(
                        "UPDATE users SET username = ?, first_name = ?, blocked_at = NULL"
                        " WHERE tg_id = ?",
                        (username, first_name, tg_id),
                    )
                await self._conn.execute("COMMIT")
            except BaseException:
                await self._conn.execute("ROLLBACK")
                raise
        user = await self.get_user(tg_id)
        assert user is not None
        return user, created

    async def save_phone(
        self,
        *,
        tg_id: int,
        phone: str,
        now: datetime,
        followups: Iterable[tuple[str, datetime]],
    ) -> bool:
        """Сохраняет телефон и планирует followups одной транзакцией.

        Возвращает False, если телефон уже был: тогда ничего не меняется.
        """
        async with self._write_lock:
            await self._conn.execute("BEGIN IMMEDIATE")
            try:
                cursor = await self._conn.execute(
                    "UPDATE users SET phone = ?, phone_at = ? WHERE tg_id = ? AND phone IS NULL",
                    (phone, to_db(now), tg_id),
                )
                if cursor.rowcount != 1:
                    await self._conn.execute("ROLLBACK")
                    return False
                await self._conn.executemany(
                    "INSERT OR IGNORE INTO scheduled_messages(tg_id, step_key, send_at, status)"
                    " VALUES (?, ?, ?, 'pending')",
                    [(tg_id, key, to_db(send_at)) for key, send_at in followups],
                )
                await self._conn.execute("COMMIT")
            except BaseException:
                await self._conn.execute("ROLLBACK")
                raise
        return True

    # --- scheduled_messages ---

    async def fetch_due(self, *, now: datetime, limit: int) -> list[ScheduledMessage]:
        rows = await self.fetchall(
            "SELECT id, tg_id, step_key, send_at, status, attempts FROM scheduled_messages"
            " WHERE status = 'pending' AND send_at <= ? ORDER BY send_at, id LIMIT ?",
            (to_db(now), limit),
        )
        return [
            ScheduledMessage(
                id=row["id"],
                tg_id=row["tg_id"],
                step_key=row["step_key"],
                send_at=from_db(row["send_at"]),
                status=row["status"],
                attempts=row["attempts"],
            )
            for row in rows
        ]

    async def mark_sent(self, *, message_id: int, now: datetime) -> None:
        async with self._write_lock:
            await self._conn.execute(
                "UPDATE scheduled_messages SET status = 'sent', sent_at = ?, last_error = NULL"
                " WHERE id = ?",
                (to_db(now), message_id),
            )

    async def mark_blocked(self, *, tg_id: int, now: datetime) -> None:
        """Пользователь заблокировал бота: помечаем его и все его pending-сообщения."""
        async with self._write_lock:
            await self._conn.execute("BEGIN IMMEDIATE")
            try:
                await self._conn.execute(
                    "UPDATE users SET blocked_at = ? WHERE tg_id = ?", (to_db(now), tg_id)
                )
                await self._conn.execute(
                    "UPDATE scheduled_messages SET status = 'blocked'"
                    " WHERE tg_id = ? AND status = 'pending'",
                    (tg_id,),
                )
                await self._conn.execute("COMMIT")
            except BaseException:
                await self._conn.execute("ROLLBACK")
                raise

    async def mark_failed(self, *, message_id: int, error: str) -> None:
        async with self._write_lock:
            await self._conn.execute(
                "UPDATE scheduled_messages SET status = 'failed', last_error = ? WHERE id = ?",
                (error, message_id),
            )

    async def record_attempt_error(
        self,
        *,
        message_id: int,
        error: str,
        max_attempts: int,
        retry_at: datetime,
    ) -> tuple[str, int]:
        """`attempts += 1`; на `max_attempts` статус `failed`, иначе повтор в `retry_at`.

        Возвращает новый статус и число попыток.
        """
        async with self._write_lock:
            cursor = await self._conn.execute(
                "UPDATE scheduled_messages SET"
                " attempts = attempts + 1,"
                " last_error = ?,"
                " status = CASE WHEN attempts + 1 >= ? THEN 'failed' ELSE 'pending' END,"
                " send_at = CASE WHEN attempts + 1 >= ? THEN send_at ELSE ? END"
                " WHERE id = ? RETURNING status, attempts",
                (error, max_attempts, max_attempts, to_db(retry_at), message_id),
            )
            row = await cursor.fetchone()
            await cursor.close()
        assert row is not None
        return str(row["status"]), int(row["attempts"])
