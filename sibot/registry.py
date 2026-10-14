"""Активные игры: одна на чат."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .game.session import GameSession


class Registry:
    def __init__(self) -> None:
        self._by_chat: dict[int, GameSession] = {}
        self._by_gid: dict[str, GameSession] = {}

    def add(self, session: GameSession) -> None:
        self._by_chat[session.chat_id] = session
        self._by_gid[session.gid] = session

    def remove(self, session: GameSession) -> None:
        if self._by_chat.get(session.chat_id) is session:
            del self._by_chat[session.chat_id]
        if self._by_gid.get(session.gid) is session:
            del self._by_gid[session.gid]

    def move(self, session: GameSession, new_chat_id: int) -> None:
        """Группа превратилась в супергруппу и сменила id."""
        self.remove(session)
        session.chat_id = new_chat_id
        self.add(session)

    def by_chat(self, chat_id: int) -> GameSession | None:
        return self._by_chat.get(chat_id)

    def get(self, gid: str) -> GameSession | None:
        return self._by_gid.get(gid)

    def for_user(self, user_id: int) -> list[GameSession]:
        return [s for s in self._by_chat.values() if s.involves(user_id)]

    def all(self) -> list[GameSession]:
        return list(self._by_chat.values())

    async def shutdown(self) -> None:
        """Остановка бота: прерываем все игры и ждём, пока они попрощаются."""
        tasks = [t for s in self.all() if (t := s.abort("бот перезапускается")) is not None]
        if tasks:
            await asyncio.wait(tasks, timeout=10)
