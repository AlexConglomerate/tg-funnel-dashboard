import json
from datetime import timedelta
from urllib.parse import urlencode

import pytest

from funnel.webapp_auth import InitDataError, sign, validate_init_data
from tests.conftest import T0

TOKEN = "123456:TEST-token"


def make_init_data(*, user_id: int = 42, auth_date: int | None = None, token: str = TOKEN) -> str:
    fields = {
        "auth_date": str(auth_date if auth_date is not None else int(T0.timestamp())),
        "query_id": "AAH",
        "user": json.dumps({"id": user_id, "first_name": "Аня"}, ensure_ascii=False),
    }
    fields["hash"] = sign(fields=fields, bot_token=token)
    return urlencode(fields)


def test_valid_signature_accepted() -> None:
    init = validate_init_data(init_data=make_init_data(), bot_token=TOKEN, now=T0)
    assert init.user_id == 42


def test_tampered_field_rejected() -> None:
    tampered = make_init_data().replace("%22id%22%3A+42", "%22id%22%3A+43")
    assert tampered != make_init_data()
    with pytest.raises(InitDataError, match="подпись"):
        validate_init_data(init_data=tampered, bot_token=TOKEN, now=T0)


def test_other_bot_token_rejected() -> None:
    with pytest.raises(InitDataError):
        validate_init_data(init_data=make_init_data(token="999:other"), bot_token=TOKEN, now=T0)


def test_expired_auth_date_rejected() -> None:
    old = int((T0 - timedelta(hours=24, seconds=1)).timestamp())
    with pytest.raises(InitDataError, match="просрочены"):
        validate_init_data(init_data=make_init_data(auth_date=old), bot_token=TOKEN, now=T0)


def test_missing_hash_rejected() -> None:
    with pytest.raises(InitDataError):
        validate_init_data(init_data="auth_date=1&user=%7B%7D", bot_token=TOKEN, now=T0)
