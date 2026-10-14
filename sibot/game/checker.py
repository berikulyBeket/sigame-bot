"""Автоматическая проверка ответов, когда ведущий — бот.

Правила намеренно строгие: опечатки прощаются, а «почти правильные» ответы
(например, только фамилия вместо имени и фамилии) — нет. Спорные случаи
решает апелляция: остальные игроки голосуют, засчитать ли ответ.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Iterable

from ..siq.model import Question

_QUOTES = str.maketrans("", "", "«»„“”\"'`‘’‚")
# кириллица, похожая на латиницу: игрок может написать «С» вместо «C»
_HOMOGLYPHS = str.maketrans("авсенкмортху", "abcehkmoptxy")
_FILLERS = frozenset({"это", "ответ", "в", "году", "год", "г", "примерно", "около"})
_ACCEPT = re.compile(r"^(?:засчит\w*|принима\w*|принят\w*|также|тоже|или|допуска\w*|зач[её]т\w*)\s*:?\s*", re.I)
_ARTICLE = re.compile(r"^(?:the|a|an) ")


def normalize(text: str) -> str:
    s = unicodedata.normalize("NFKC", text).casefold().replace("ё", "е")
    s = s.translate(_QUOTES)
    s = re.sub(r"[^\w\s]|_", " ", s)
    s = " ".join(s.split())
    return _ARTICLE.sub("", s)


def variants(answer: str) -> list[str]:
    """Нормализованные формы ответа: целиком, без скобок, альтернативы через « / »."""
    raw = [answer, re.sub(r"\([^)]*\)|\[[^\]]*\]", " ", answer)]
    for inner in re.findall(r"[(\[]([^)\]]*)[)\]]", answer):
        inner = inner.strip()
        if m := _ACCEPT.match(inner):  # «Пушкин (засчитывается: Александр Сергеевич)»
            raw += re.split(r"[,;/]|\sили\s", inner[m.end() :])
    raw += re.split(r"\s+/\s+|;", answer)  # «Пётр I / Пётр Первый»
    return list(dict.fromkeys(n for r in raw if (n := normalize(r))))


def levenshtein(a: str, b: str, limit: int) -> int:
    """Расстояние Левенштейна; как только оно больше limit, возвращает limit + 1."""
    if a == b:
        return 0
    if abs(len(a) - len(b)) > limit:
        return limit + 1
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i] + [0] * len(b)
        for j, cb in enumerate(b, 1):
            cur[j] = min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (ca != cb))
        if min(cur) > limit:
            return limit + 1
        prev = cur
    return prev[-1]


def _allowed_typos(length: int) -> int:
    return 0 if length <= 4 else 1 if length <= 7 else 2 if length <= 12 else 3


def _close(word: str, ref: str) -> bool:
    k = _allowed_typos(len(ref))
    return word == ref or (k > 0 and levenshtein(word, ref, k) <= k)


def similarity(given: str, ref: str) -> float | None:
    """Насколько нормализованный ответ похож на эталон; None — не засчитывается."""
    if given == ref:
        return 1.0
    g_compact, r_compact = given.replace(" ", ""), ref.replace(" ", "")
    if g_compact == r_compact:
        return 0.99
    ref_digits = re.findall(r"\d+", ref)
    if ref_digits:  # в числах опечаток не бывает: 1812 ≠ 1813
        if re.findall(r"\d+", given) != ref_digits:
            return None
        r_words = [w for w in ref.split() if not w.isdigit()]
        g_words = [w for w in given.split() if not w.isdigit()]
        if len(r_words) <= 1 and all(w in _FILLERS or any(_close(w, r) for r in r_words) for w in g_words):
            return 0.95
    k = _allowed_typos(len(r_compact))
    if k and (d := levenshtein(g_compact, r_compact, k)) <= k:
        return 0.9 - 0.01 * d
    g_words, r_words = given.split(), ref.split()
    if len(g_words) <= len(r_words) + 2 and all(any(_close(g, r) for g in g_words) for r in r_words):
        return 0.8  # «это Пушкин», «Пушкин А. С.»
    return None


def _best(given: str, answers: Iterable[str]) -> float | None:
    scores = [s for a in answers for v in variants(a) if (s := similarity(given, v)) is not None]
    return max(scores, default=None)


def check_text(given: str, right: Iterable[str], wrong: Iterable[str] = ()) -> bool:
    g = normalize(given)
    if not g:
        return False
    best_right = _best(g, right)
    if best_right is None:
        return False
    best_wrong = _best(g, wrong)  # автор пакета явно указал, что такое не засчитывать
    return best_wrong is None or best_right > best_wrong


def _label_key(label: str) -> str:
    return normalize(label).translate(_HOMOGLYPHS)


def resolve_option(q: Question, given: str) -> str | None:
    """Метка варианта по ответу игрока: «C», «с», «3» или текст варианта."""
    s = given.strip()
    key = _label_key(s)
    for label, _ in q.options:
        if _label_key(label) == key:
            return label
    if s.isdigit() and 1 <= int(s) <= len(q.options):
        return q.options[int(s) - 1][0]
    for label, text in q.options:
        if check_text(s, [text]):
            return label
    return None


def _number(text: str) -> float | None:
    t = text.replace(" ", "").replace(" ", "").replace(",", ".").replace("−", "-")
    return float(t) if re.fullmatch(r"[-+]?\d+(?:\.\d+)?", t) else None


def check_answer(q: Question, given: str | None) -> bool:
    if not given or not given.strip():
        return False
    if q.answer_type == "select" and q.options:
        label = resolve_option(q, given)
        if label is None:
            return False
        if _label_key(label) in {_label_key(r) for r in q.right}:
            return True
        text = dict(q.options)[label]  # в некоторых пакетах верным указан текст варианта
        return check_text(text, q.right, q.wrong)
    if q.answer_type == "number":
        value = _number(given)
        refs = [n for r in q.right if (n := _number(r)) is not None]
        if value is not None and refs:
            return any(abs(value - r) <= q.deviation + 1e-9 for r in refs)
    return check_text(given, q.right, q.wrong)
