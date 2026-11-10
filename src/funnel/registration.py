"""Логика воронки без aiogram: что сохранить и что ответить пользователю."""

import logging
import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import Enum

from funnel.config import FunnelConfig
from funnel.db import Database, utcnow

log = logging.getLogger(__name__)

SOURCE_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
DEFAULT_SOURCE = "direct"


class Keyboard(Enum):
    NONE = "none"  # клавиатуру не трогаем
    PHONE = "phone"  # показать кнопку «отправить номер»
    REMOVE = "remove"  # убрать reply-клавиатуру


@dataclass(frozen=True)
class Reply:
    text: str
    keyboard: Keyboard = Keyboard.NONE


def parse_source(payload: str | None) -> str:
    """Метка источника из deep link `?start=<payload>`; мусор превращается в `direct`."""
    if payload and SOURCE_RE.fullmatch(payload):
        return payload
    return DEFAULT_SOURCE


def normalize_phone(phone: str) -> str:
    """Только цифры с `+` в начале: `8 (999) 123-45-67` → `+89991234567`."""
    return "+" + re.sub(r"\D", "", phone)


class Registration:
    def __init__(
        self,
        *,
        db: Database,
        config: FunnelConfig,
        clock: Callable[[], datetime] = utcnow,
    ) -> None:
        self._db = db
        self._config = config
        self._clock = clock

    def _ask_phone(self) -> Reply:
        return Reply(self._config.messages.ask_phone, Keyboard.PHONE)

    async def start(
        self,
        *,
        tg_id: int,
        username: str | None,
        first_name: str | None,
        payload: str | None,
    ) -> list[Reply]:
        user, created = await self._db.upsert_user(
            tg_id=tg_id,
            username=username,
            first_name=first_name,
            source=parse_source(payload),
            now=self._clock(),
        )
        log.info("start source=%s new=%s", user.source, created)
        if user.phone:
            return [Reply(self._config.messages.already_registered, Keyboard.REMOVE)]
        return [Reply(self._config.messages.welcome), self._ask_phone()]

    async def contact(
        self,
        *,
        tg_id: int,
        username: str | None,
        first_name: str | None,
        contact_user_id: int | None,
        phone: str,
    ) -> list[Reply]:
        if contact_user_id != tg_id:
            log.info("contact_rejected reason=not_own")
            return [Reply(self._config.messages.not_own_contact, Keyboard.PHONE)]

        # Контакт мог прийти без /start (например, база очищена): заводим пользователя.
        await self._db.upsert_user(
            tg_id=tg_id,
            username=username,
            first_name=first_name,
            source=DEFAULT_SOURCE,
            now=self._clock(),
        )
        now = self._clock()
        followups = [
            (followup.key, now + timedelta(seconds=followup.delay_seconds))
            for followup in self._config.followups
        ]
        saved = await self._db.save_phone(
            tg_id=tg_id, phone=normalize_phone(phone), now=now, followups=followups
        )
        if not saved:
            log.info("contact_repeat")
            return [Reply(self._config.messages.already_registered, Keyboard.REMOVE)]
        log.info("phone_saved followups=%d", len(followups))
        return [Reply(self._config.messages.registered, Keyboard.REMOVE)]

    async def bot_blocked(self, *, tg_id: int) -> None:
        """Ответ не ушёл: пользователь заблокировал бота. Это не ошибка, а событие воронки."""
        await self._db.mark_blocked(tg_id=tg_id, now=self._clock())
        log.info("user_blocked")

    async def other_message(self, *, tg_id: int) -> list[Reply]:
        user = await self._db.get_user(tg_id)
        if user is not None and user.phone:
            log.debug("message_ignored reason=registered")
            return []
        return [Reply(self._config.messages.use_button, Keyboard.PHONE)]
