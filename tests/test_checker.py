from __future__ import annotations

import pytest

from sibot.game.checker import check_answer, check_text
from sibot.siq.model import Question


@pytest.mark.parametrize(
    ("given", "right", "wrong", "expected"),
    [
        ("Пушкин", ["Пушкин"], [], True),
        ("пушкина", ["Пушкин"], [], True),  # падеж
        ("Пушкен", ["Пушкин"], [], True),  # опечатка
        ("это Пушкин", ["Пушкин"], [], True),
        ("Лермонтов", ["Пушкин"], [], False),
        ("Пушкин", ["Александр Пушкин"], [], False),  # неполный ответ решает апелляция
        ("Пушкин", ["Пушкин (засчитывается: Александр Сергеевич)"], [], True),
        ("игры разума", ["«Игры разума»"], ["«Самый умный»"], True),
        ("Своя игра", ["«Игры разума»"], ['"Своя игра"'], False),
        ("в 1961 году", ["1961"], [], True),
        ("1962", ["1961"], [], False),  # в числах опечаток нет
        ("88", ["88 клавиш"], [], True),
        ("Ag", ["Au"], [], False),  # короткие ответы — только точно
        ("acdc", ["AC/DC"], [], True),
        ("beatles", ["The Beatles"], [], True),
        ("Петр Первый", ["Пётр I / Пётр Первый"], [], True),
        ("австралия", ["Австрия"], ["Австралия"], False),
        ("австрлия", ["Австрия"], ["Австралия"], False),  # ближе к «неверному» варианту автора
        ("Шварцнегер", ["Арнольд Шварценеггер", "Шварценеггер"], [], True),
        ("на скрипке", ["Скрипка"], [], True),
        ("", ["кит"], [], False),
    ],
)
def test_check_text(given: str, right: list[str], wrong: list[str], expected: bool) -> None:
    assert check_text(given, right, wrong) is expected


@pytest.mark.parametrize(
    ("given", "expected"), [("C", True), ("с", True), ("3", True), ("вариант 3", True), ("B", False)]
)
def test_select_answers(given: str, expected: bool) -> None:
    q = Question(
        price=100,
        answer_type="select",
        options=[("A", "Вариант 1"), ("B", "Вариант 2"), ("C", "Вариант 3"), ("D", "Вариант 4")],
        right=["C"],
    )
    assert check_answer(q, given) is expected


@pytest.mark.parametrize(("given", "expected"), [("100", True), ("104", True), ("95,5", True), ("106", False)])
def test_number_answers(given: str, expected: bool) -> None:
    q = Question(price=100, answer_type="number", right=["100"], deviation=5)
    assert check_answer(q, given) is expected
