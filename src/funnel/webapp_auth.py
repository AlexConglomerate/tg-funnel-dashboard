"""Проверка `initData` Telegram Mini App.

https://core.telegram.org/bots/webapps#validating-data-received-via-the-mini-app
"""

import hashlib
import hmac
import json
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from urllib.parse import parse_qsl

MAX_AGE = timedelta(hours=24)
# Небольшой запас на расхождение часов между нами и Telegram.
CLOCK_SKEW = timedelta(minutes=5)


class InitDataError(Exception):
    pass


@dataclass(frozen=True)
class WebAppInit:
    user_id: int
    auth_date: datetime
    user: dict[str, object]


def _secret(bot_token: str) -> bytes:
    return hmac.new(b"WebAppData", bot_token.encode(), hashlib.sha256).digest()


def sign(*, fields: dict[str, str], bot_token: str) -> str:
    """Подпись `hash` для набора полей: нужна проверке и тестам."""
    check_string = "\n".join(f"{key}={fields[key]}" for key in sorted(fields))
    return hmac.new(_secret(bot_token), check_string.encode(), hashlib.sha256).hexdigest()


def validate_init_data(
    *,
    init_data: str,
    bot_token: str,
    now: datetime,
    max_age: timedelta = MAX_AGE,
) -> WebAppInit:
    try:
        pairs = parse_qsl(init_data, keep_blank_values=True, strict_parsing=True)
    except ValueError as exc:
        raise InitDataError("initData не разбирается") from exc
    fields = dict(pairs)
    if len(fields) != len(pairs):
        raise InitDataError("повторяющиеся поля в initData")

    received_hash = fields.pop("hash", None)
    if not received_hash:
        raise InitDataError("нет hash")
    if not hmac.compare_digest(sign(fields=fields, bot_token=bot_token), received_hash):
        raise InitDataError("неверная подпись")

    try:
        auth_date = datetime.fromtimestamp(int(fields["auth_date"]), tz=UTC)
    except (KeyError, ValueError) as exc:
        raise InitDataError("нет или неверный auth_date") from exc
    if now - auth_date > max_age:
        raise InitDataError("initData просрочены")
    if auth_date - now > CLOCK_SKEW:
        raise InitDataError("auth_date в будущем")

    try:
        user = json.loads(fields["user"])
        user_id = int(user["id"])
    except (KeyError, ValueError, TypeError) as exc:
        raise InitDataError("нет user.id") from exc
    return WebAppInit(user_id=user_id, auth_date=auth_date, user=user)
