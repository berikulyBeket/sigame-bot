"""Библиотека пакетов: встроенные (папка packages/) и загруженные в чаты."""

from __future__ import annotations

import asyncio
import hashlib
import logging
import zipfile
from dataclasses import dataclass, field
from pathlib import Path

from .siq import Content, Kind, Package, PackageError, load_package

log = logging.getLogger(__name__)

KEEP_UPLOADS = 50


def _digest(text: str) -> str:
    return hashlib.sha1(text.encode()).hexdigest()[:8]


@dataclass(slots=True, eq=False)
class PackageEntry:
    key: str
    package: Package
    path: Path
    builtin: bool
    chats: set[int] = field(default_factory=set)  # где загружен (для пользовательских пакетов)

    @property
    def title(self) -> str:
        return self.package.name


class Library:
    def __init__(self, packages_dir: Path, uploads_dir: Path):
        self.packages_dir = Path(packages_dir)
        self.uploads_dir = Path(uploads_dir)
        self._entries: dict[str, PackageEntry] = {}
        # (пакет, тип, имя файла) -> (метод отправки, file_id): медиа загружается в Telegram один раз
        self._file_ids: dict[tuple[str, str, str], tuple[str, str]] = {}
        self.reload_builtins()

    # ------------------------------------------------------------------ пакеты

    def reload_builtins(self) -> None:
        if not self.packages_dir.is_dir():
            log.warning("Папка с пакетами %s не найдена", self.packages_dir)
            return
        for path in sorted(self.packages_dir.iterdir()):
            is_siq = path.is_file() and path.suffix.casefold() == ".siq"
            is_dir = path.is_dir() and (path / "content.xml").is_file()
            if not (is_siq or is_dir):
                continue
            try:
                package = load_package(path)
            except PackageError as e:
                log.warning("Пакет %s пропущен: %s", path.name, e)
                continue
            key = "b" + _digest(path.name)
            self._entries[key] = PackageEntry(key, package, path, builtin=True)
            log.info("Пакет «%s»: %d вопросов", package.name, package.question_count)

    def get(self, key: str) -> PackageEntry | None:
        return self._entries.get(key)

    def available(self, chat_id: int) -> list[PackageEntry]:
        return [e for e in self._entries.values() if e.builtin or chat_id in e.chats]

    def default(self) -> PackageEntry | None:
        builtins = [e for e in self._entries.values() if e.builtin]
        return next((e for e in builtins if e.path.stem.casefold() == "demo"), builtins[0] if builtins else None)

    def upload_path(self, unique_id: str) -> Path:
        self.uploads_dir.mkdir(parents=True, exist_ok=True)
        safe = "".join(ch for ch in unique_id if ch.isalnum() or ch in "-_") or "package"
        return self.uploads_dir / f"{safe}.siq"

    async def add_upload(self, path: Path, chat_id: int) -> PackageEntry:
        """Регистрирует скачанный .siq. Бросает PackageError, если файл не пакет."""
        key = "u" + _digest(path.name)
        entry = self._entries.get(key)
        if entry is None:
            package = await asyncio.to_thread(load_package, path)
            entry = PackageEntry(key, package, path, builtin=False)
            self._entries[key] = entry
            self._prune_uploads()
        entry.chats.add(chat_id)
        return entry

    def _prune_uploads(self) -> None:
        """Храним только последние KEEP_UPLOADS загруженных пакетов."""
        files = sorted(self.uploads_dir.glob("*.siq"), key=lambda p: p.stat().st_mtime, reverse=True)
        for old in files[KEEP_UPLOADS:]:
            old.unlink(missing_ok=True)
            for key in [k for k, e in self._entries.items() if e.path == old]:
                del self._entries[key]

    # ------------------------------------------------------------------- медиа

    def cached(self, entry: PackageEntry, item: Content) -> tuple[str, str] | None:
        return self._file_ids.get((entry.key, item.kind, item.value))

    def remember(self, entry: PackageEntry, item: Content, method: str, file_id: str) -> None:
        self._file_ids[(entry.key, item.kind, item.value)] = (method, file_id)

    def forget(self, entry: PackageEntry, item: Content) -> None:
        self._file_ids.pop((entry.key, item.kind, item.value), None)

    async def read_media(self, entry: PackageEntry, item: Content) -> bytes | None:
        media = entry.package.media
        if media is None or not item.ref:
            return None
        try:
            return await asyncio.to_thread(media.read, item.kind, item.value)
        except (OSError, zipfile.BadZipFile, KeyError) as e:
            log.warning("Не удалось прочитать %s из пакета %s: %s", item.value, entry.path.name, e)
            return None

    @staticmethod
    def logo(entry: PackageEntry) -> Content | None:
        logo = entry.package.logo
        return Content(Kind.IMAGE, logo, ref=True) if logo else None
