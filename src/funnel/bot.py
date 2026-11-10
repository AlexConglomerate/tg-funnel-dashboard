"""Тонкие обработчики aiogram: достают данные из апдейта и вызывают `Registration`."""

import asyncio
import logging
from collections.abc import Awaitable, Callable
from typing import Any

from aiogram import BaseMiddleware, Bot, F, Router
from aiogram.client.session.middlewares.base import (
    BaseRequestMiddleware,
    NextRequestMiddlewareType,
)
from aiogram.enums import ChatType
from aiogram.exceptions import TelegramAPIError, TelegramForbiddenError, TelegramRetryAfter
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.methods import Response, TelegramMethod
from aiogram.methods.base import TelegramType
from aiogram.types import (
    ErrorEvent,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    MenuButtonDefault,
    MenuButtonWebApp,
    Message,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
    TelegramObject,
    WebAppInfo,
)

from funnel.config import FunnelConfig
from funnel.log import tg_id_ctx
from funnel.registration import Keyboard, Registration, Reply
from funnel.scheduler import RateLimiter

log = logging.getLogger(__name__)

ReplyMarkup = ReplyKeyboardMarkup | ReplyKeyboardRemove | None


class RateLimitMiddleware(BaseRequestMiddleware):
    """Все `send*` к Bot API идут через общий ограничитель: и ответы, и рассылка, и алерты."""

    def __init__(self, limiter: RateLimiter) -> None:
        self._limiter = limiter

    async def __call__(
        self,
        make_request: NextRequestMiddlewareType[TelegramType],
        bot: Bot,
        method: TelegramMethod[TelegramType],
    ) -> Response[TelegramType]:
        if not method.__api_method__.startswith("send"):
            return await make_request(bot, method)
        await self._limiter.acquire()
        try:
            return await make_request(bot, method)
        except TelegramRetryAfter as exc:
            # Telegram просит подождать: тормозим все отправки, а не только эту.
            self._limiter.pause(exc.retry_after)
            raise


class UserContextMiddleware(BaseMiddleware):
    """Кладёт `tg_id` в контекст логов на время обработки апдейта."""

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        user = data.get("event_from_user")
        token = tg_id_ctx.set(user.id if user is not None else None)
        try:
            return await handler(event, data)
        finally:
            tg_id_ctx.reset(token)


def _markup(*, keyboard: Keyboard, config: FunnelConfig) -> ReplyMarkup:
    if keyboard is Keyboard.PHONE:
        return ReplyKeyboardMarkup(
            keyboard=[[KeyboardButton(text=config.messages.phone_button, request_contact=True)]],
            one_time_keyboard=True,
            resize_keyboard=True,
        )
    if keyboard is Keyboard.REMOVE:
        return ReplyKeyboardRemove()
    return None


async def _answer(
    *,
    message: Message,
    replies: list[Reply],
    config: FunnelConfig,
    registration: Registration,
) -> None:
    for reply in replies:
        markup = _markup(keyboard=reply.keyboard, config=config)
        try:
            try:
                await message.answer(reply.text, reply_markup=markup)
            except TelegramRetryAfter as exc:
                # Пик на эфире: ограничитель уже на паузе, повторяем один раз после неё.
                await asyncio.sleep(exc.retry_after)
                await message.answer(reply.text, reply_markup=markup)
        except TelegramForbiddenError:
            await registration.bot_blocked(tg_id=message.chat.id)
            return


def build_router(
    *,
    registration: Registration,
    config: FunnelConfig,
    admin_ids: frozenset[int],
    webapp_url: str | None,
) -> Router:
    router = Router(name="funnel")
    router.message.filter(F.chat.type == ChatType.PRIVATE)
    router.message.outer_middleware(UserContextMiddleware())

    @router.message(CommandStart())
    async def on_start(message: Message, command: CommandObject, bot: Bot) -> None:
        assert message.from_user is not None
        replies = await registration.start(
            tg_id=message.from_user.id,
            username=message.from_user.username,
            first_name=message.from_user.first_name,
            payload=command.args,
        )
        await _answer(message=message, replies=replies, config=config, registration=registration)
        if message.from_user.id in admin_ids:
            # При запуске кнопку можно поставить только тем, кто уже писал боту.
            # Первый /start админа ставит её сразу, без перезапуска.
            await setup_menu_buttons(
                bot=bot, admin_ids=frozenset({message.from_user.id}), webapp_url=webapp_url
            )

    @router.message(Command("dashboard"), F.from_user.id.in_(admin_ids))
    async def on_dashboard(message: Message) -> None:
        if not webapp_url:
            await message.answer(
                "WEBAPP_URL не задан: запустите бота через ./scripts/dev.sh, "
                "он поднимет HTTPS-туннель.",
                parse_mode=None,
            )
            return
        await message.answer(
            "Статистика воронки:",
            reply_markup=InlineKeyboardMarkup(
                inline_keyboard=[
                    [
                        InlineKeyboardButton(
                            text="📊 Открыть дашборд", web_app=WebAppInfo(url=webapp_url)
                        )
                    ]
                ]
            ),
        )

    @router.message(F.contact)
    async def on_contact(message: Message) -> None:
        assert message.from_user is not None and message.contact is not None
        replies = await registration.contact(
            tg_id=message.from_user.id,
            username=message.from_user.username,
            first_name=message.from_user.first_name,
            contact_user_id=message.contact.user_id,
            phone=message.contact.phone_number,
        )
        await _answer(message=message, replies=replies, config=config, registration=registration)

    @router.message()
    async def on_other(message: Message) -> None:
        assert message.from_user is not None
        replies = await registration.other_message(tg_id=message.from_user.id)
        await _answer(message=message, replies=replies, config=config, registration=registration)

    return router


async def on_error(event: ErrorEvent) -> None:
    """Общий обработчик ошибок: traceback в лог, уровень ERROR уходит алертом."""
    update = event.update
    user = None
    if update.message is not None and update.message.from_user is not None:
        user = update.message.from_user.id
    token = tg_id_ctx.set(user)
    try:
        log.error("handler_failed update_id=%s", update.update_id, exc_info=event.exception)
    finally:
        tg_id_ctx.reset(token)


async def setup_menu_buttons(
    *, bot: Bot, admin_ids: frozenset[int], webapp_url: str | None
) -> None:
    """Кнопка меню «Дашборд» только у админов; без WEBAPP_URL возвращаем обычную."""
    button: MenuButtonWebApp | MenuButtonDefault
    if webapp_url:
        button = MenuButtonWebApp(text="Дашборд", web_app=WebAppInfo(url=webapp_url))
    else:
        button = MenuButtonDefault()
    for admin_id in admin_ids:
        try:
            await bot.set_chat_menu_button(chat_id=admin_id, menu_button=button)
        except TelegramAPIError as exc:
            # Обычно админ ещё не писал боту: кнопка появится после его /start.
            log.warning("menu_button_failed admin=%s error=%s", admin_id, exc)
