"""Запуск: бот (long polling) + uvicorn + планировщик в одном asyncio-цикле."""

import asyncio
import contextlib
import logging
import signal
import sys
from collections.abc import Generator
from importlib.metadata import version

import uvicorn
from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.client.session.aiohttp import AiohttpSession
from pydantic import ValidationError

from funnel.alerts import TelegramAlertHandler
from funnel.api import create_app
from funnel.bot import RateLimitMiddleware, build_router, on_error, setup_menu_buttons
from funnel.config import ConfigError, load_config
from funnel.db import Database
from funnel.log import setup_logging
from funnel.registration import Registration
from funnel.scheduler import RateLimiter, Scheduler
from funnel.settings import Settings

log = logging.getLogger("funnel")

# Лимит Telegram около 30 сообщений в секунду, держим запас.
SEND_RATE = 25


class _Server(uvicorn.Server):
    """Сигналы ловим сами для всего процесса, uvicorn их не перехватывает."""

    @contextlib.contextmanager
    def capture_signals(self) -> Generator[None]:
        yield


async def run(settings: Settings) -> None:
    config = load_config(settings.funnel_config)
    log.info(
        "funnel %s config=%s followups=%d",
        version("tg-funnel-dashboard"),
        settings.funnel_config,
        len(config.followups),
    )
    if settings.webapp_url:
        log.info("webapp_url=%s", settings.webapp_url)
    else:
        log.warning("WEBAPP_URL не задан: дашборд недоступен, запустите ./scripts/dev.sh")
    if settings.proxy_url:
        log.info("bot api через прокси")

    db = await Database.connect(settings.db_path)
    limiter = RateLimiter(rate=SEND_RATE)
    session = AiohttpSession(proxy=settings.proxy_url) if settings.proxy_url else AiohttpSession()
    session.middleware(RateLimitMiddleware(limiter))
    bot = Bot(
        token=settings.bot_token.get_secret_value(),
        session=session,
        default=DefaultBotProperties(parse_mode=config.parse_mode),
    )

    async def send_text(*, chat_id: int, text: str) -> None:
        await bot.send_message(chat_id=chat_id, text=text)

    async def send_alert(*, chat_id: int, text: str) -> None:
        await bot.send_message(chat_id=chat_id, text=text, parse_mode=None)

    alerts = TelegramAlertHandler(send=send_alert, admin_ids=settings.admin_ids)
    alerts.attach(asyncio.get_running_loop())
    logging.getLogger().addHandler(alerts)

    try:
        me = await bot.get_me()
    except Exception as exc:
        await session.close()
        await db.close()
        raise SystemExit(
            f"Нет связи с api.telegram.org ({type(exc).__name__}: {exc}).\n"
            "Если Bot API недоступен из этой сети, задайте PROXY_SSH или PROXY_URL в .env."
        ) from exc
    await setup_menu_buttons(bot=bot, admin_ids=settings.admin_ids, webapp_url=settings.webapp_url)

    registration = Registration(db=db, config=config)
    dp = Dispatcher()
    dp.include_router(
        build_router(
            registration=registration,
            config=config,
            admin_ids=settings.admin_ids,
            webapp_url=settings.webapp_url,
        )
    )
    dp.errors.register(on_error)

    scheduler = Scheduler(db=db, config=config, send=send_text, limiter=limiter)
    app = create_app(
        db=db,
        config=config,
        bot_token=settings.bot_token.get_secret_value(),
        admin_ids=settings.admin_ids,
        scheduler=scheduler,
    )
    server = _Server(
        uvicorn.Config(
            app,
            host="127.0.0.1",
            port=settings.http_port,
            log_config=None,
            lifespan="off",
        )
    )

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop.set)

    tasks = {
        asyncio.create_task(
            dp.start_polling(bot, handle_signals=False, close_bot_session=False), name="bot"
        ),
        asyncio.create_task(server.serve(), name="http"),
        asyncio.create_task(scheduler.run_forever(), name="scheduler"),
    }
    log.info("bot started @%s http=http://127.0.0.1:%d", me.username, settings.http_port)

    stop_task = asyncio.create_task(stop.wait(), name="stop")
    done, _ = await asyncio.wait({*tasks, stop_task}, return_when=asyncio.FIRST_COMPLETED)
    for task in done:
        if task is not stop_task and not task.cancelled() and task.exception() is not None:
            log.error("task %s crashed", task.get_name(), exc_info=task.exception())

    log.info("shutting down")
    scheduler.stop()
    server.should_exit = True
    with contextlib.suppress(RuntimeError):  # polling мог уже остановиться
        await dp.stop_polling()
    stop_task.cancel()
    await asyncio.wait(tasks, timeout=10)
    await alerts.drain()
    logging.getLogger().removeHandler(alerts)
    await session.close()
    await db.close()
    log.info("stopped")


def main() -> None:
    try:
        settings = Settings()  # поля приходят из окружения и .env
    except ValidationError as exc:
        sys.exit(f"Ошибка в .env / переменных окружения:\n{exc}")
    setup_logging(level=settings.log_level)
    try:
        asyncio.run(run(settings))
    except ConfigError as exc:
        sys.exit(f"Ошибка конфига воронки: {exc}")


if __name__ == "__main__":
    main()
