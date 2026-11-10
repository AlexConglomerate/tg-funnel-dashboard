import asyncio
import logging
from pathlib import Path

from funnel.alerts import TelegramAlertHandler
from funnel.db import Database


def record(*, name: str = "funnel.scheduler", exc: Exception | None = None) -> logging.LogRecord:
    exc_info = None
    if exc is not None:
        try:
            raise exc
        except Exception as caught:
            exc_info = (type(caught), caught, caught.__traceback__)
    return logging.LogRecord(name, logging.ERROR, __file__, 10, "pass failed", None, exc_info)


def test_alert_text_and_antispam() -> None:
    now = [0.0]

    async def send(*, chat_id: int, text: str) -> None:
        raise AssertionError("не вызывается в этом тесте")

    handler = TelegramAlertHandler(send=send, admin_ids=[1], clock=lambda: now[0])
    text = handler.build_text(record(exc=ValueError("boom")))
    assert text is not None
    assert "funnel.scheduler" in text and "ValueError: boom" in text and "test_alerts.py:" in text

    for _ in range(37):
        assert handler.build_text(record(exc=ValueError("again"))) is None
    # Другой тип ошибки идёт отдельно.
    assert handler.build_text(record(exc=KeyError("k"))) is not None

    now[0] = 61
    text = handler.build_text(record(exc=ValueError("later")))
    assert text is not None and "и ещё 37 раз" in text


async def test_send_failure_does_not_raise() -> None:
    sent: list[int] = []

    async def send(*, chat_id: int, text: str) -> None:
        if chat_id == 1:
            raise RuntimeError("network down")
        sent.append(chat_id)

    handler = TelegramAlertHandler(send=send, admin_ids=[1, 2])
    handler.attach(asyncio.get_running_loop())
    logger = logging.getLogger("funnel.test_alerts")
    logger.addHandler(handler)
    try:
        logger.error("something broke")
        await asyncio.sleep(0)
        await handler.drain()
    finally:
        logger.removeHandler(handler)
    assert sent == [2]  # первому не ушло, второму ушло, исключение наружу не вылетело


async def test_location_points_to_our_code_when_library_raised(tmp_path: Path) -> None:
    db = await Database.connect(tmp_path / "a.db")
    await db.close()
    try:
        await db.ping()  # падает внутри aiosqlite, вызвано из funnel/db.py
    except Exception as caught:
        exc_info = (type(caught), caught, caught.__traceback__)
    rec = logging.LogRecord("funnel.api", logging.ERROR, __file__, 1, "x", None, exc_info)

    async def send(*, chat_id: int, text: str) -> None: ...

    text = TelegramAlertHandler(send=send, admin_ids=[1]).build_text(rec)
    assert text is not None
    assert "aiosqlite" in text
    assert "(из src/funnel/db.py:" in text
