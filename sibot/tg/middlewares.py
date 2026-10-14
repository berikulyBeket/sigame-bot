"""Повтор запросов, отклонённых Telegram из-за лимитов (429 Too Many Requests)."""

from __future__ import annotations

import asyncio
import logging

from aiogram import Bot
from aiogram.client.session.middlewares.base import BaseRequestMiddleware, NextRequestMiddlewareType
from aiogram.exceptions import TelegramRetryAfter
from aiogram.methods import TelegramMethod
from aiogram.methods.base import Response, TelegramType

log = logging.getLogger(__name__)


class RetryAfterMiddleware(BaseRequestMiddleware):
    """В группе бот может отправлять ~20 сообщений в минуту; при превышении ждём и повторяем."""

    def __init__(self, attempts: int = 3, max_wait: float = 60) -> None:
        self.attempts = attempts
        self.max_wait = max_wait

    async def __call__(
        self,
        make_request: NextRequestMiddlewareType[TelegramType],
        bot: Bot,
        method: TelegramMethod[TelegramType],
    ) -> Response[TelegramType]:
        for attempt in range(1, self.attempts + 1):
            try:
                return await make_request(bot, method)
            except TelegramRetryAfter as e:
                if attempt == self.attempts or e.retry_after > self.max_wait:
                    raise
                log.info("Лимит Telegram на %s: ждём %s с", type(method).__name__, e.retry_after)
                await asyncio.sleep(e.retry_after + 0.5)
        raise AssertionError("unreachable")
