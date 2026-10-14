"""Отправка сообщений и медиа одной игры. Ошибки Telegram не роняют игру — шаг просто пропускается."""

from __future__ import annotations

import contextlib
import html
import logging
from collections.abc import Callable, Sequence
from pathlib import PurePosixPath
from typing import Any

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError, TelegramBadRequest, TelegramForbiddenError, TelegramMigrateToChat
from aiogram.types import BufferedInputFile, InlineKeyboardMarkup, LinkPreviewOptions, Message

from ..library import Library, PackageEntry
from ..siq.model import Content, Kind
from . import render as R

log = logging.getLogger(__name__)

PHOTO_LIMIT = 10 * 1024 * 1024
UPLOAD_LIMIT = 50 * 1024 * 1024
CAPTION_LIMIT = 1024


def media_method(kind: Kind, filename: str, size: int) -> str:
    """Каким методом Bot API отправлять файл (остальное уходит документом)."""
    ext = PurePosixPath(filename.split("?", 1)[0]).suffix.casefold()
    if kind == Kind.IMAGE:
        if ext == ".gif":
            return "animation"
        if ext in (".jpg", ".jpeg", ".png", ".webp", ".bmp") and size <= PHOTO_LIMIT:
            return "photo"
    elif kind == Kind.AUDIO:
        if ext in (".mp3", ".m4a"):
            return "audio"
        if ext in (".ogg", ".oga", ".opus"):
            return "voice"
    elif kind == Kind.VIDEO:
        if ext in (".mp4", ".m4v", ".mov"):
            return "video"
    return "document"


def _file_id(m: Message) -> str | None:
    if m.photo:
        return m.photo[-1].file_id
    media = m.animation or m.audio or m.voice or m.video or m.document
    return media.file_id if media else None


def _duration(m: Message) -> float:
    media = m.audio or m.voice or m.video or m.animation
    return float(media.duration or 0) if media else 0.0


class TelegramUI:
    def __init__(
        self,
        bot: Bot,
        chat_id: int,
        thread_id: int | None,
        library: Library,
        on_lost: Callable[[], None] = lambda: None,
        on_migrate: Callable[[int], None] = lambda new_id: None,
    ) -> None:
        self.bot = bot
        self.chat_id = chat_id
        self.thread_id = thread_id
        self.library = library
        self._on_lost = on_lost
        self._on_migrate = on_migrate
        self._tracked: set[tuple[int, int]] = set()  # сообщения с живыми кнопками
        self._captions: set[tuple[int, int]] = set()  # медиа: правится подпись, а не текст

    # --------------------------------------------------------------- служебное

    def _track(self, chat_id: int, mid: int, kb: InlineKeyboardMarkup | None) -> None:
        if kb is None:
            self._tracked.discard((chat_id, mid))
        else:
            self._tracked.add((chat_id, mid))

    async def _call(self, func: Callable[..., Any], **kwargs: Any) -> Any:
        """Запрос в группу с обработкой миграции чата и потери доступа."""
        for _ in range(2):
            try:
                return await func(chat_id=self.chat_id, **kwargs)
            except TelegramMigrateToChat as e:
                log.info("Чат %s стал супергруппой %s", self.chat_id, e.migrate_to_chat_id)
                self.chat_id = e.migrate_to_chat_id
                self._on_migrate(self.chat_id)
            except TelegramForbiddenError as e:
                log.warning("Бот потерял доступ к чату %s: %s", self.chat_id, e)
                self._on_lost()
                return None
            except TelegramAPIError as e:
                log.warning("%s в чат %s не удался: %s", getattr(func, "__name__", func), self.chat_id, e)
                return None
        return None

    # ------------------------------------------------------------------ группа

    async def send(
        self, text: str, kb: InlineKeyboardMarkup | None = None, *, track: bool = True, preview: str | None = None
    ) -> int | None:
        extra: dict[str, Any] = {}
        if preview:
            extra["link_preview_options"] = LinkPreviewOptions(url=preview, is_disabled=False)
        m = await self._call(
            self.bot.send_message, text=R.cut(text, 4096), message_thread_id=self.thread_id, reply_markup=kb, **extra
        )
        if m is None:
            return None
        if kb is not None and track:
            self._track(self.chat_id, m.message_id, kb)
        return m.message_id

    async def edit(
        self, mid: int | None, text: str, kb: InlineKeyboardMarkup | None = None, *, chat_id: int | None = None
    ) -> None:
        if mid is None:
            return
        chat = chat_id or self.chat_id
        try:
            if (chat, mid) in self._captions:
                await self.bot.edit_message_caption(
                    chat_id=chat, message_id=mid, caption=R.cut(text, CAPTION_LIMIT), reply_markup=kb
                )
            else:
                await self.bot.edit_message_text(chat_id=chat, message_id=mid, text=R.cut(text, 4096), reply_markup=kb)
        except TelegramBadRequest as e:
            if "not modified" not in str(e):
                log.info("Не удалось изменить сообщение %s: %s", mid, e)
        except TelegramAPIError as e:
            log.info("Не удалось изменить сообщение %s: %s", mid, e)
        self._track(chat, mid, kb)

    async def set_kb(
        self, mid: int | None, kb: InlineKeyboardMarkup | None, *, chat_id: int | None = None, track: bool = True
    ) -> None:
        if mid is None:
            return
        chat = chat_id or self.chat_id
        try:
            await self.bot.edit_message_reply_markup(chat_id=chat, message_id=mid, reply_markup=kb)
        except TelegramBadRequest as e:
            if "not modified" not in str(e):
                log.info("Не удалось изменить кнопки %s: %s", mid, e)
        except TelegramAPIError as e:
            log.info("Не удалось изменить кнопки %s: %s", mid, e)
        self._track(chat, mid, kb if track else None)

    async def delete(self, mid: int | None) -> None:
        if mid is None:
            return
        with contextlib.suppress(TelegramAPIError):
            await self.bot.delete_message(chat_id=self.chat_id, message_id=mid)

    async def clear_tracked(self) -> None:
        """Снять все кнопки, оставшиеся от прерванного шага (пропуск вопроса, остановка)."""
        tracked, self._tracked = self._tracked, set()
        for chat, mid in tracked:
            with contextlib.suppress(TelegramAPIError):
                await self.bot.edit_message_reply_markup(chat_id=chat, message_id=mid, reply_markup=None)

    # ----------------------------------------------------------------- личка

    async def dm(self, user_id: int, text: str, kb: InlineKeyboardMarkup | None = None) -> int | None:
        try:
            m = await self.bot.send_message(chat_id=user_id, text=R.cut(text, 4096), reply_markup=kb)
        except TelegramAPIError as e:
            log.info("Не удалось написать %s в личку: %s", user_id, e)
            return None
        if kb is not None:
            self._track(user_id, m.message_id, kb)
        return m.message_id

    async def dm_edit(self, user_id: int, mid: int | None, text: str, kb: InlineKeyboardMarkup | None = None) -> None:
        await self.edit(mid, text, kb, chat_id=user_id)

    # ------------------------------------------------------------------ медиа

    async def send_content(
        self,
        text: str,
        items: Sequence[Content],
        entry: PackageEntry,
        kb: InlineKeyboardMarkup | None = None,
        *,
        track: bool = True,
    ) -> tuple[int | None, float]:
        """Текст и медиа вопроса/ответа. Кнопки вешаются на последнее сообщение.

        Возвращает id последнего сообщения и суммарную длительность аудио/видео.
        """
        media = [c for c in items if c.is_media or (c.kind == Kind.HTML and c.ref)]
        preview = next((R.youtube_link(c.value) for c in items if c.kind == Kind.HTML and not c.ref), None)
        if not media:
            return await self.send(text, kb, track=track, preview=preview), 0.0

        if len(media) == 1 and len(text) <= CAPTION_LIMIT:
            mid, duration = await self._send_media(media[0], entry, text, kb, track)
            if mid is None:
                mid = await self.send(f"{text}\n\n{self._missing(media[0])}", kb, track=track)
            return mid, duration

        last = await self.send(text, preview=preview)
        total, attached, notes = 0.0, False, []
        for i, item in enumerate(media):
            is_last = i == len(media) - 1
            mid, duration = await self._send_media(item, entry, None, kb if is_last else None, track)
            if mid is None:
                notes.append(self._missing(item))
                continue
            last, total, attached = mid, total + duration, is_last
        if notes:
            note_id = await self.send("\n".join(notes), None if attached else kb, track=track)
            if not attached:
                return note_id, total
        elif kb is not None and not attached:
            await self.set_kb(last, kb, track=track)
        return last, total

    @staticmethod
    def _missing(item: Content) -> str:
        if not item.ref and item.value.startswith(("http://", "https://")):
            return f'{R.media_missing()} <a href="{html.escape(item.value)}">ссылка</a>'
        return R.media_missing()

    async def _send_media(
        self,
        item: Content,
        entry: PackageEntry,
        caption: str | None,
        kb: InlineKeyboardMarkup | None,
        track: bool = True,
    ) -> tuple[int | None, float]:
        cached = self.library.cached(entry, item)
        if cached:
            method, file_id = cached
            m = await self._send_file(method, file_id, caption, kb)
            if m is not None:
                return self._sent(m, kb if track else None), _duration(m)
            self.library.forget(entry, item)

        if item.ref:
            data = await self.library.read_media(entry, item)
            if not data or len(data) > UPLOAD_LIMIT:  # пустые файлы в пакетах тоже встречаются
                return None, 0.0
            filename = PurePosixPath(item.value).name or "file"
            method = media_method(item.kind, filename, len(data))

            def source() -> Any:
                return BufferedInputFile(data, filename=filename)

        elif item.value.startswith(("http://", "https://")):
            method = media_method(item.kind, item.value, 0)

            def source() -> Any:
                return item.value

        else:
            return None, 0.0

        m = await self._send_file(method, source(), caption, kb)
        if m is None and method != "document" and item.ref:  # формат не подошёл — шлём файлом
            method = "document"
            m = await self._send_file(method, source(), caption, kb)
        if m is None:
            return None, 0.0
        if file_id := _file_id(m):
            self.library.remember(entry, item, method, file_id)
        return self._sent(m, kb if track else None), _duration(m)

    def _sent(self, m: Message, kb: InlineKeyboardMarkup | None) -> int:
        self._captions.add((m.chat.id, m.message_id))
        self._track(m.chat.id, m.message_id, kb)
        return m.message_id

    async def _send_file(
        self, method: str, src: Any, caption: str | None, kb: InlineKeyboardMarkup | None
    ) -> Message | None:
        common: dict[str, Any] = {"message_thread_id": self.thread_id, "reply_markup": kb}
        if caption:
            common["caption"] = R.cut(caption, CAPTION_LIMIT)
        match method:
            case "photo":
                return await self._call(self.bot.send_photo, photo=src, **common)
            case "animation":
                return await self._call(self.bot.send_animation, animation=src, **common)
            case "audio":
                return await self._call(self.bot.send_audio, audio=src, **common)
            case "voice":
                return await self._call(self.bot.send_voice, voice=src, **common)
            case "video":
                return await self._call(self.bot.send_video, video=src, supports_streaming=True, **common)
            case _:
                return await self._call(self.bot.send_document, document=src, **common)
