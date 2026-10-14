"""Фейковый Telegram: бот работает по-настоящему, но запросы уходят не в сеть, а сюда."""

from __future__ import annotations

import asyncio
import itertools
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.client.session.base import BaseSession
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramForbiddenError
from aiogram.methods import (
    AnswerCallbackQuery,
    DeleteMessage,
    EditMessageCaption,
    EditMessageReplyMarkup,
    EditMessageText,
    GetChatMember,
    GetFile,
    GetMe,
    SendAnimation,
    SendAudio,
    SendDocument,
    SendMessage,
    SendPhoto,
    SendVideo,
    SendVoice,
    SetMyCommands,
)
from aiogram.types import (
    Animation,
    Audio,
    CallbackQuery,
    Chat,
    ChatMemberMember,
    ChatMemberOwner,
    Document,
    File,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
    PhotoSize,
    Update,
    User,
    Video,
    Voice,
)

from sibot.config import Config
from sibot.game.session import GameSession
from sibot.library import Library
from sibot.registry import Registry
from sibot.tg.handlers import build_router

ROOT = Path(__file__).resolve().parent.parent
BOT = User(id=777, is_bot=True, first_name="SIGame", username="sigame_test_bot", can_read_all_group_messages=True)
GROUP = -100500
SEND_METHODS = (SendMessage, SendPhoto, SendAudio, SendVideo, SendDocument, SendVoice, SendAnimation)


@dataclass
class Sent:
    chat_id: int
    message_id: int
    text: str
    kb: InlineKeyboardMarkup | None
    kind: str
    deleted: bool = False


class FakeTelegram(BaseSession):
    def __init__(self) -> None:
        super().__init__()
        self.requests: list[Any] = []
        self.messages: dict[tuple[int, int], Sent] = {}
        self.order: list[Sent] = []
        self.blocked: set[int] = set()  # пользователи, не открывавшие личку с ботом
        self.admins: set[int] = set()
        self.answers: list[AnswerCallbackQuery] = []
        self.files: dict[str, bytes] = {}  # file_id -> содержимое (для скачивания документов)
        self._ids = itertools.count(1000)

    async def close(self) -> None:
        pass

    async def stream_content(self, url: str, *args: Any, **kwargs: Any):
        yield self.files[url.rsplit("/", 1)[-1]]

    async def make_request(self, bot: Bot, method: Any, timeout: int | None = None) -> Any:
        self.requests.append(method)
        if isinstance(method, GetMe):
            return BOT
        if isinstance(method, SEND_METHODS):
            return self._send(method).as_(bot)  # как настоящая сессия: ответ привязан к боту
        key = (int(getattr(method, "chat_id", 0) or 0), getattr(method, "message_id", 0))
        stored = self.messages.get(key)
        if isinstance(method, EditMessageText):
            if stored:
                stored.text, stored.kb = method.text, method.reply_markup
            return True
        if isinstance(method, EditMessageCaption):
            if stored:
                stored.text, stored.kb = method.caption or "", method.reply_markup
            return True
        if isinstance(method, EditMessageReplyMarkup):
            if stored:
                stored.kb = method.reply_markup
            return True
        if isinstance(method, DeleteMessage):
            if stored:
                stored.deleted = True
            return True
        if isinstance(method, AnswerCallbackQuery):
            self.answers.append(method)
            return True
        if isinstance(method, SetMyCommands):
            return True
        if isinstance(method, GetFile):
            return File(file_id=method.file_id, file_unique_id=method.file_id, file_path=f"documents/{method.file_id}")
        if isinstance(method, GetChatMember):
            user = User(id=method.user_id, is_bot=False, first_name=f"U{method.user_id}")
            if method.user_id in self.admins:
                return ChatMemberOwner(user=user, is_anonymous=False)
            return ChatMemberMember(user=user)
        raise AssertionError(f"Неожиданный запрос {type(method).__name__}")

    def _send(self, method: Any) -> Message:
        chat_id = int(method.chat_id)
        if chat_id in self.blocked:
            raise TelegramForbiddenError(
                method=method, message="Forbidden: bot can't initiate conversation with a user"
            )
        mid = next(self._ids)
        kind = type(method).__name__.removeprefix("Send").lower()
        text = getattr(method, "text", None) or getattr(method, "caption", None) or ""
        kb = method.reply_markup if isinstance(method.reply_markup, InlineKeyboardMarkup) else None
        sent = Sent(chat_id, mid, text, kb, kind)
        self.messages[(chat_id, mid)] = sent
        self.order.append(sent)
        fid = f"file{mid}"
        media: dict[str, Any] = {
            "photo": {"photo": [PhotoSize(file_id=fid, file_unique_id=fid, width=10, height=10)]},
            "audio": {"audio": Audio(file_id=fid, file_unique_id=fid, duration=3)},
            "video": {"video": Video(file_id=fid, file_unique_id=fid, width=10, height=10, duration=5)},
            "voice": {"voice": Voice(file_id=fid, file_unique_id=fid, duration=3)},
            "animation": {"animation": Animation(file_id=fid, file_unique_id=fid, width=1, height=1, duration=1)},
            "document": {"document": Document(file_id=fid, file_unique_id=fid)},
        }.get(kind, {})
        return Message(
            message_id=mid,
            date=datetime.now(UTC),
            chat=Chat(id=chat_id, type="private" if chat_id > 0 else "supergroup"),
            from_user=BOT,
            text=text if kind == "message" else None,
            caption=text if kind != "message" else None,
            reply_markup=kb,
            **media,
        )


class Harness:
    """Бот с диспетчером и фейковым Telegram + удобные действия от имени игроков."""

    def __init__(self, tmp_path: Path, *, time_scale: float = 0.02, packages_dir: Path | None = None) -> None:
        self.tg = FakeTelegram()
        self.bot = Bot(
            "123456:TEST",
            session=self.tg,
            default=DefaultBotProperties(parse_mode=ParseMode.HTML, link_preview_is_disabled=True),
        )
        self.library = Library(packages_dir or ROOT / "packages", tmp_path / "uploads")
        self.registry = Registry()
        self.config = Config(token="x", data_dir=tmp_path, time_scale=time_scale)
        self.dp = Dispatcher(registry=self.registry, library=self.library, config=self.config, me=BOT)
        self.dp.include_router(build_router())
        self._updates = itertools.count(1)
        self._msg_ids = itertools.count(1)
        self._cq_ids = itertools.count(1)

    @staticmethod
    def user(uid: int, name: str) -> User:
        return User(id=uid, is_bot=False, first_name=name)

    @staticmethod
    def _chat(chat_id: int) -> Chat:
        if chat_id < 0:
            return Chat(id=chat_id, type="supergroup", title="Тестовый чат")
        return Chat(id=chat_id, type="private")

    async def say(self, user: User, text: str, chat_id: int = GROUP) -> None:
        msg = Message(
            message_id=next(self._msg_ids), date=datetime.now(UTC), chat=self._chat(chat_id), from_user=user, text=text
        )
        await self.dp.feed_update(self.bot, Update(update_id=next(self._updates), message=msg))

    async def send_document(self, user: User, name: str, data: bytes, chat_id: int = GROUP) -> None:
        file_id = f"doc{len(self.tg.files)}"
        self.tg.files[file_id] = data
        doc = Document(file_id=file_id, file_unique_id=f"u{file_id}", file_name=name, file_size=len(data))
        msg = Message(
            message_id=next(self._msg_ids),
            date=datetime.now(UTC),
            chat=self._chat(chat_id),
            from_user=user,
            document=doc,
        )
        await self.dp.feed_update(self.bot, Update(update_id=next(self._updates), message=msg))

    async def dm(self, user: User, text: str) -> None:
        await self.say(user, text, chat_id=user.id)

    async def click(self, user: User, data: str, chat_id: int = GROUP) -> AnswerCallbackQuery:
        msg = Message(message_id=1, date=datetime.now(UTC), chat=self._chat(chat_id), from_user=BOT, text="…")
        cq = CallbackQuery(id=str(next(self._cq_ids)), from_user=user, chat_instance="ci", message=msg, data=data)
        before = len(self.tg.answers)
        await self.dp.feed_update(self.bot, Update(update_id=next(self._updates), callback_query=cq))
        assert len(self.tg.answers) == before + 1, "обработчик не ответил на нажатие"
        return self.tg.answers[-1]

    # ------------------------------------------------------------- поиск

    def buttons(self, prefix: str, chat_id: int | None = GROUP) -> list[InlineKeyboardButton]:
        """Живые кнопки с callback_data, начинающимся с prefix (свежие сообщения — первыми)."""
        found = []
        for sent in reversed(self.tg.order):
            if sent.deleted or sent.kb is None or (chat_id is not None and sent.chat_id != chat_id):
                continue
            for row in sent.kb.inline_keyboard:
                found += [b for b in row if b.callback_data and b.callback_data.startswith(prefix)]
        return found

    def button(self, prefix: str, text: str | None = None, chat_id: int | None = GROUP) -> InlineKeyboardButton:
        for b in self.buttons(prefix, chat_id):
            if text is None or b.text == text:
                return b
        raise AssertionError(f"Нет кнопки {prefix!r} {text!r}")

    def texts(self, chat_id: int = GROUP) -> list[str]:
        return [s.text for s in self.tg.order if s.chat_id == chat_id]

    def all_text(self, chat_id: int = GROUP) -> str:
        return "\n".join(self.texts(chat_id))

    def session(self) -> GameSession:
        s = self.registry.by_chat(GROUP)
        assert s is not None
        return s


async def until(predicate: Callable[[], Any], timeout: float = 5.0, interval: float = 0.003) -> Any:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while True:
        result = predicate()
        if result:
            return result
        if loop.time() > deadline:
            raise AssertionError("Не дождались условия")
        await asyncio.sleep(interval)


async def waiting(s: GameSession, label: str, timeout: float = 5.0) -> int:
    """Дождаться, пока игра начнёт ждать действие label; вернуть номер шага."""
    await until(lambda: s.waiting_for is not None and s.waiting_for.startswith(label), timeout)
    assert s._waiter is not None
    return s._waiter.step
