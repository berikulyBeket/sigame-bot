"""Модель пакета вопросов SIGame (.siq) — независимо от версии формата."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Protocol


class Kind(StrEnum):
    TEXT = "text"
    IMAGE = "image"
    AUDIO = "audio"
    VIDEO = "video"
    HTML = "html"


MEDIA_KINDS = frozenset({Kind.IMAGE, Kind.AUDIO, Kind.VIDEO})


@dataclass(slots=True)
class Content:
    """Фрагмент вопроса или ответа."""

    kind: Kind
    value: str  # текст, имя файла внутри пакета (ref=True) или URL
    ref: bool = False  # ссылка на файл внутри пакета
    replic: bool = False  # реплика ведущего («устный» текст)
    duration: float = 0.0  # длительность фрагмента из пакета, сек

    @property
    def is_media(self) -> bool:
        return self.kind in MEDIA_KINDS


class QType(StrEnum):
    SIMPLE = "simple"
    STAKE = "stake"  # вопрос со ставкой (аукцион)
    SECRET = "secret"  # кот в мешке: тема и цена объявляются после передачи
    SECRET_PUBLIC = "secretPublicPrice"  # кот в мешке: тема и цена известны заранее
    SECRET_NO_QUESTION = "secretNoQuestion"  # кот без вопроса: очки просто дарятся
    NO_RISK = "noRisk"  # вопрос без риска: ×2 за верный ответ, без штрафа
    FOR_ALL = "forAll"  # отвечают все, каждый ± номинал
    STAKE_ALL = "stakeAll"  # отвечают все, каждый со своей ставкой


SECRET_TYPES = frozenset({QType.SECRET, QType.SECRET_PUBLIC, QType.SECRET_NO_QUESTION})


@dataclass(slots=True)
class NumberSet:
    """Набор допустимых стоимостей «кота в мешке»."""

    minimum: int = 0
    maximum: int = 0
    step: int = 0

    def options(self, round_prices: Sequence[int], limit: int = 12) -> list[int]:
        """Конкретные варианты стоимости.

        0/0 по правилам SIGame означает выбор между минимальной и максимальной
        стоимостью вопросов раунда.
        """
        lo, hi, step = self.minimum, self.maximum, self.step
        if lo == 0 and hi == 0:
            prices = [p for p in round_prices if p > 0]
            return sorted({min(prices), max(prices)}) if prices else [0]
        if hi < lo:
            lo, hi = hi, lo
        if lo == hi:
            return [lo]
        if step <= 0 or step >= hi - lo:
            return [lo, hi]
        values = list(range(lo, hi + 1, step))
        if len(values) > limit:  # слишком много кнопок — берём равномерную выборку
            last = len(values) - 1
            values = sorted({values[round(i * last / (limit - 1))] for i in range(limit)})
        return values


@dataclass(slots=True)
class Question:
    price: int
    type: QType = QType.SIMPLE
    content: list[Content] = field(default_factory=list)
    answer_content: list[Content] = field(default_factory=list)  # показывается вместе с ответом
    right: list[str] = field(default_factory=list)
    wrong: list[str] = field(default_factory=list)
    comment: str = ""
    # кот в мешке
    secret_theme: str = ""
    secret_price: NumberSet | None = None
    selection: str = "exceptCurrent"  # any | exceptCurrent | current
    # тип ответа: text | select (варианты) | number (число с допуском)
    answer_type: str = "text"
    options: list[tuple[str, str]] = field(default_factory=list)  # (метка, текст)
    deviation: float = 0.0

    @property
    def answer(self) -> str:
        return next((r for r in self.right if r.strip()), "")

    @property
    def text(self) -> str:
        return "\n".join(c.value for c in self.content if c.kind == Kind.TEXT)


@dataclass(slots=True)
class Theme:
    name: str
    questions: list[Question]
    comment: str = ""


@dataclass(slots=True)
class Round:
    name: str
    themes: list[Theme]
    final: bool = False
    comment: str = ""

    @property
    def prices(self) -> list[int]:
        return [q.price for t in self.themes for q in t.questions]

    @property
    def question_count(self) -> int:
        return sum(len(t.questions) for t in self.themes)


class MediaSource(Protocol):
    def size(self, kind: Kind, name: str) -> int | None: ...

    def read(self, kind: Kind, name: str) -> bytes | None: ...


@dataclass(slots=True)
class Package:
    name: str
    rounds: list[Round]
    authors: list[str] = field(default_factory=list)
    comment: str = ""
    logo: str | None = None  # имя файла в Images
    restriction: str = ""
    difficulty: int | None = None
    media: MediaSource | None = None

    @property
    def question_count(self) -> int:
        return sum(r.question_count if not r.final else 1 for r in self.rounds)

    @property
    def has_final(self) -> bool:
        return any(r.final for r in self.rounds)
