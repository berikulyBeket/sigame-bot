"""Чтение пакетов SIGame: .siq-архивов и распакованных папок с content.xml.

Поддерживаются форматы версий 3–4 (scenario/atom, type/param) и 5
(params/item, numberSet). Старые типы вопросов приводятся к новым так же,
как это делает сам SIGame при открытии пакета.
"""

from __future__ import annotations

import logging
import re
import unicodedata
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path
from urllib.parse import unquote

from .model import Content, Kind, NumberSet, Package, QType, Question, Round, Theme

log = logging.getLogger(__name__)

MAX_XML_BYTES = 30 * 1024 * 1024
MAX_MEDIA_BYTES = 50 * 1024 * 1024  # больше Telegram всё равно не примет от бота

FOLDERS = {Kind.IMAGE: "images", Kind.AUDIO: "audio", Kind.VIDEO: "video", Kind.HTML: "html"}

KINDS = {
    "text": Kind.TEXT,
    "say": Kind.TEXT,
    "image": Kind.IMAGE,
    "voice": Kind.AUDIO,
    "audio": Kind.AUDIO,
    "video": Kind.VIDEO,
    "html": Kind.HTML,
}

V5_TYPES = {
    "stake": QType.STAKE,
    "auction": QType.STAKE,
    "secret": QType.SECRET,
    "cat": QType.SECRET,
    "bagcat": QType.SECRET,
    "secretPublicPrice": QType.SECRET_PUBLIC,
    "secretNoQuestion": QType.SECRET_NO_QUESTION,
    "noRisk": QType.NO_RISK,
    "sponsored": QType.NO_RISK,
    "forAll": QType.FOR_ALL,
    "stakeAll": QType.STAKE_ALL,
}

_NUMBER_SET = re.compile(r"\[(\d+);(\d+)\](?:/(\d+))?")
_DTD = re.compile(rb"<!\s*(DOCTYPE|ENTITY)", re.IGNORECASE)


class PackageError(Exception):
    """Пакет повреждён или не поддерживается."""


# --------------------------------------------------------------------------- вход


def load_package(path: str | Path) -> Package:
    path = Path(path)
    if path.is_dir():
        xml_path = path / "content.xml"
        if not xml_path.is_file():
            raise PackageError("В папке пакета нет content.xml")
        if xml_path.stat().st_size > MAX_XML_BYTES:
            raise PackageError("content.xml слишком большой")
        return parse_content(xml_path.read_bytes(), DirMedia(path))
    try:
        with zipfile.ZipFile(path) as zf:
            info = next((i for i in zf.infolist() if i.filename.casefold() == "content.xml"), None)
            if info is None:
                raise PackageError("В архиве нет content.xml — это точно пакет SIGame?")
            if info.file_size > MAX_XML_BYTES:
                raise PackageError("content.xml слишком большой")
            xml = zf.read(info)
    except (zipfile.BadZipFile, OSError) as e:
        raise PackageError("Не удалось открыть файл как архив .siq") from e
    return parse_content(xml, ZipMedia(path))


def parse_content(xml: bytes, media=None) -> Package:
    if _DTD.search(xml):
        raise PackageError("DTD и сущности в content.xml не поддерживаются")
    try:
        root = ET.fromstring(xml)
    except ET.ParseError as e:
        raise PackageError(f"Ошибка в content.xml: {e}") from e
    if _tag(root) != "package":
        raise PackageError("Это не пакет SIGame: корневой элемент не <package>")

    authors, comment = _info(_child(root, "info"))
    rounds = [r for r in map(_parse_round, _children(_child(root, "rounds"), "round")) if r]
    if not rounds:
        raise PackageError("В пакете нет ни одного вопроса")
    logo = (root.get("logo") or "").strip()
    return Package(
        name=(root.get("name") or "").strip() or "Без названия",
        rounds=rounds,
        authors=authors,
        comment=comment,
        logo=logo[1:] if logo.startswith("@") and len(logo) > 1 else None,
        restriction=(root.get("restriction") or "").strip(),
        difficulty=_int(root.get("difficulty"), None),
        media=media,
    )


def parse_number_set(value: str) -> NumberSet | None:
    """Стоимость в формате SIGame 4: «500», «[100;500]» или «[100;1000]/200»."""
    value = (value or "").strip()
    if re.fullmatch(r"-?\d+", value):
        n = int(value)
        return NumberSet(n, n, 0)
    m = _NUMBER_SET.fullmatch(value)
    if not m:
        return None
    lo, hi = int(m.group(1)), int(m.group(2))
    step = int(m.group(3)) if m.group(3) else hi - lo
    return NumberSet(lo, hi, step)


# ------------------------------------------------------------------- структура


def _parse_round(el: ET.Element) -> Round | None:
    final = (el.get("type") or "").strip().casefold() == "final"
    themes = []
    for t_el in _children(_child(el, "themes"), "theme"):
        questions = []
        for q_el in _children(_child(t_el, "questions"), "question"):
            try:
                q = _parse_question(q_el, final)
            except (ValueError, TypeError) as e:  # кривой вопрос не должен ронять весь пакет
                log.warning("Пропущен повреждённый вопрос: %s", e)
                continue
            if q is not None:
                questions.append(q)
        if questions:
            _, theme_comment = _info(_child(t_el, "info"))
            name = (t_el.get("name") or "").strip() or "Без названия"
            themes.append(Theme(name=name, questions=questions, comment=theme_comment))
    if not themes:
        return None
    _, comment = _info(_child(el, "info"))
    return Round(name=(el.get("name") or "").strip() or "Раунд", themes=themes, final=final, comment=comment)


def _parse_question(el: ET.Element, final: bool) -> Question | None:
    price = _int(el.get("price"), 0)
    if price < 0:  # price="-1" — пустая ячейка табло
        return None
    _, comment = _info(_child(el, "info"))
    q = Question(
        price=price,
        right=_answers(_child(el, "right")),
        wrong=_answers(_child(el, "wrong")),
        comment=comment,
    )
    if _child(el, "scenario") is not None or _child(el, "type") is not None:
        _fill_v4(q, el)
    else:
        _fill_v5(q, el)
    if final:  # в финале особые типы не имеют смысла
        q.type = QType.SIMPLE
    if not q.content and q.type != QType.SECRET_NO_QUESTION and not q.answer:
        return None
    return q


def _fill_v4(q: Question, el: ET.Element) -> None:
    type_el = _child(el, "type")
    name = ((type_el.get("name") if type_el is not None else "") or "").strip()
    params = {(p.get("name") or "").strip(): _text(p) for p in _children(type_el, "param")}

    if name in ("auction", "stake"):
        q.type = QType.STAKE
    elif name in ("sponsored", "noRisk"):
        q.type = QType.NO_RISK
    elif name in ("cat", "bagcat"):
        bag = name == "bagcat"
        knows = params.get("knows", "after").casefold() if bag else "after"
        to_self = params.get("self", "false").casefold() if bag else "false"
        q.type = {"never": QType.SECRET_NO_QUESTION, "before": QType.SECRET_PUBLIC}.get(knows, QType.SECRET)
        q.secret_theme = params.get("theme", "")
        q.secret_price = parse_number_set(params.get("cost", ""))
        q.selection = {"true": "any", "only": "current"}.get(to_self, "exceptCurrent")
    # simple и собственные типы авторов играем как обычные вопросы

    target, marker = q.content, False
    for atom in _children(_child(el, "scenario"), "atom"):
        kind_name = (atom.get("type") or "text").strip().casefold()
        if kind_name == "marker":  # всё после маркера показывается вместе с ответом
            if not marker:
                marker, target = True, q.answer_content
            continue
        text = _text(atom)
        if not text:
            continue
        kind = KINDS.get(kind_name, Kind.TEXT)
        ref = kind != Kind.TEXT and text.startswith("@")
        target.append(
            Content(
                kind=kind,
                value=text[1:] if ref else text,
                ref=ref,
                replic=kind_name == "say",
                duration=max(_float(atom.get("time")), 0.0),
            )
        )


def _fill_v5(q: Question, el: ET.Element) -> None:
    q.type = V5_TYPES.get((el.get("type") or "").strip(), QType.SIMPLE)
    params = {(p.get("name") or "").strip(): p for p in _children(_child(el, "params"), "param")}

    if "question" in params:
        q.content = _items(params["question"])
    if "answer" in params:
        q.answer_content = _items(params["answer"])
    script = _child(el, "script")
    if script is not None and not q.content:
        _fill_script(q, script)

    if "theme" in params:
        q.secret_theme = _text(params["theme"])
    if "price" in params:
        ns = _child(params["price"], "numberSet")
        if ns is not None:
            q.secret_price = NumberSet(_int(ns.get("minimum"), 0), _int(ns.get("maximum"), 0), _int(ns.get("step"), 0))
        else:
            q.secret_price = parse_number_set(_text(params["price"]))
    if "selectionMode" in params:
        q.selection = _text(params["selectionMode"]) or "exceptCurrent"

    answer_type = _text(params["answerType"]).casefold() if "answerType" in params else ""
    if answer_type == "select" and "answerOptions" in params:
        for opt in _children(params["answerOptions"], "param"):
            label = (opt.get("name") or "").strip()
            items = _items(opt)
            text = " ".join(c.value for c in items if c.kind == Kind.TEXT)
            if not text:
                text = "[изображение]" if any(c.kind == Kind.IMAGE for c in items) else "…"
            if label:
                q.options.append((label, text))
        if q.options:
            q.answer_type = "select"
    elif answer_type == "number":
        q.answer_type = "number"
        if "answerDeviation" in params:
            q.deviation = abs(_float(_text(params["answerDeviation"])))


def _fill_script(q: Question, script: ET.Element) -> None:
    """Пакеты со «сценарием» шагов: берём показываемый контент до и после запроса ответа."""
    asked = False
    for step in _children(script, "step"):
        step_type = (step.get("type") or "").strip()
        if step_type == "askAnswer":
            asked = True
            continue
        for p in _children(step, "param"):
            name = (p.get("name") or "").strip()
            if name == "answer":
                q.answer_content.extend(_items(p))
            elif name == "content" and step_type == "showContent":
                (q.answer_content if asked else q.content).extend(_items(p))


def _items(param: ET.Element) -> list[Content]:
    result = []
    for item in _children(param, "item"):
        value = _text(item)
        if not value:
            continue
        kind = KINDS.get((item.get("type") or "text").strip().casefold(), Kind.TEXT)
        result.append(
            Content(
                kind=kind,
                value=value,
                ref=kind != Kind.TEXT and (item.get("isRef") or "").strip().casefold() == "true",
                replic=kind == Kind.TEXT and (item.get("placement") or "").strip().casefold() == "replic",
                duration=_duration(item.get("duration")),
            )
        )
    return result


# ---------------------------------------------------------------------- утилиты


def _tag(el: ET.Element) -> str:
    return el.tag.rsplit("}", 1)[-1] if isinstance(el.tag, str) else ""


def _children(el: ET.Element | None, name: str) -> list[ET.Element]:
    return [] if el is None else [c for c in el if _tag(c) == name]


def _child(el: ET.Element | None, name: str) -> ET.Element | None:
    return None if el is None else next((c for c in el if _tag(c) == name), None)


def _text(el: ET.Element | None) -> str:
    return "" if el is None else "".join(el.itertext()).strip()


def _info(el: ET.Element | None) -> tuple[list[str], str]:
    authors = [t for a in _children(_child(el, "authors"), "author") if (t := _text(a))]
    return authors, _text(_child(el, "comments"))


def _answers(el: ET.Element | None) -> list[str]:
    return [t for a in _children(el, "answer") if (t := _text(a))]


def _int(value: str | None, default):
    try:
        return int((value or "").strip())
    except ValueError:
        return default


def _float(value: str | None) -> float:
    try:
        return float((value or "").strip().replace(",", "."))
    except ValueError:
        return 0.0


def _duration(value: str | None) -> float:
    """«00:00:05» или просто число секунд."""
    value = (value or "").strip()
    if ":" not in value:
        return max(_float(value), 0.0)
    total = 0.0
    for part in value.split(":"):
        total = total * 60 + _float(part)
    return max(total, 0.0)


def _norm(name: str) -> str:
    return unicodedata.normalize("NFC", name).casefold()


def _variants(name: str) -> list[str]:
    return list(dict.fromkeys([name, unquote(name)]))


# ------------------------------------------------------------------------ медиа


class ZipMedia:
    """Медиафайлы внутри .siq. Имена в архиве часто закодированы (%20 и т. п.)."""

    def __init__(self, path: Path):
        self.path = Path(path)
        self._index: dict[tuple[str, str], str] = {}
        self._any: dict[str, str] = {}
        with zipfile.ZipFile(self.path) as zf:
            for info in zf.infolist():
                folder, sep, name = info.filename.partition("/")
                if info.is_dir() or not sep or not name:
                    continue
                for variant in _variants(name):
                    self._index.setdefault((folder.casefold(), _norm(variant)), info.filename)
                    self._any.setdefault(_norm(variant), info.filename)

    def _entry(self, kind: Kind, name: str) -> str | None:
        folder = FOLDERS.get(kind, "")
        for variant in _variants(name):
            if (hit := self._index.get((folder, _norm(variant)))) is not None:
                return hit
        for variant in _variants(name):  # файл лежит не в «своей» папке
            if (hit := self._any.get(_norm(variant))) is not None:
                return hit
        return None

    def size(self, kind: Kind, name: str) -> int | None:
        entry = self._entry(kind, name)
        if entry is None:
            return None
        with zipfile.ZipFile(self.path) as zf:
            return zf.getinfo(entry).file_size

    def read(self, kind: Kind, name: str) -> bytes | None:
        entry = self._entry(kind, name)
        if entry is None:
            return None
        with zipfile.ZipFile(self.path) as zf:
            info = zf.getinfo(entry)
            if info.file_size > MAX_MEDIA_BYTES:
                return None
            return zf.read(info)


class DirMedia:
    """Медиафайлы распакованного пакета (папка с content.xml, Images/, Audio/…)."""

    def __init__(self, root: Path):
        self.root = Path(root).resolve()
        self._files: dict[tuple[str, str], Path] | None = None

    def _scan(self) -> dict[tuple[str, str], Path]:
        if self._files is None:
            files: dict[tuple[str, str], Path] = {}
            for folder in self.root.iterdir():
                if folder.is_dir():
                    for f in folder.rglob("*"):
                        if f.is_file():
                            rel = f.relative_to(folder).as_posix()
                            for variant in _variants(rel):
                                files.setdefault((folder.name.casefold(), _norm(variant)), f)
            self._files = files
        return self._files

    def _path(self, kind: Kind, name: str) -> Path | None:
        files = self._scan()
        folder = FOLDERS.get(kind, "")
        for variant in _variants(name):
            if (hit := files.get((folder, _norm(variant)))) is not None:
                return hit
        for variant in _variants(name):
            for (_, fname), path in files.items():
                if fname == _norm(variant):
                    return path
        return None

    def size(self, kind: Kind, name: str) -> int | None:
        path = self._path(kind, name)
        return path.stat().st_size if path else None

    def read(self, kind: Kind, name: str) -> bytes | None:
        path = self._path(kind, name)
        if path is None or path.stat().st_size > MAX_MEDIA_BYTES:
            return None
        return path.read_bytes()
