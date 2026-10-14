"""Запуск: python -m sibot"""

from __future__ import annotations

import asyncio
import contextlib
import logging

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.client.telegram import TelegramAPIServer
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramUnauthorizedError

from .config import Config
from .library import Library
from .registry import Registry
from .tg.handlers import build_router, set_commands
from .tg.middlewares import RetryAfterMiddleware

log = logging.getLogger("sibot")


async def main() -> None:
    config = Config.from_env()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    api = TelegramAPIServer.from_base(config.api_url, is_local=config.api_local) if config.api_url else None
    session = AiohttpSession(api=api) if api else AiohttpSession()
    session.middleware(RetryAfterMiddleware())
    bot = Bot(
        config.token,
        session=session,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML, link_preview_is_disabled=True),
    )
    try:
        me = await bot.get_me()
    except TelegramUnauthorizedError:
        await bot.session.close()
        raise SystemExit("Telegram не принял BOT_TOKEN — проверьте токен в .env") from None
    library = Library(config.packages_dir, config.data_dir / "uploads")
    registry = Registry()

    dp = Dispatcher(registry=registry, library=library, config=config, me=me)
    dp.include_router(build_router())
    dp.shutdown.register(registry.shutdown)

    try:
        await set_commands(bot)
        log.info("Бот @%s запущен, пакетов: %d", me.username, len(library.available(0)))
        await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())
    finally:
        await bot.session.close()


if __name__ == "__main__":
    with contextlib.suppress(KeyboardInterrupt):
        asyncio.run(main())
