"""Данные inline-кнопок. g — id игры, n — номер шага: кнопки прошлых шагов не срабатывают."""

from aiogram.filters.callback_data import CallbackData


class LobbyCb(CallbackData, prefix="L"):
    g: str
    a: str  # действие
    v: str = ""


class PickCb(CallbackData, prefix="P"):
    g: str
    n: int
    t: int  # тема
    q: int  # вопрос


class InfoCb(CallbackData, prefix="I"):
    g: str
    r: int  # номер раунда
    t: int


class PressCb(CallbackData, prefix="B"):
    g: str
    n: int


class PassCb(CallbackData, prefix="S"):
    g: str
    n: int


class ChoiceCb(CallbackData, prefix="C"):
    """Универсальный выбор: give, price, bid, rm, bet, opt."""

    g: str
    n: int
    k: str
    v: str


class JudgeCb(CallbackData, prefix="J"):
    g: str
    n: int
    u: int  # игрок
    v: int  # 1 — верно, 0 — неверно


class AppealCb(CallbackData, prefix="A"):
    g: str
    i: int  # номер вопроса


class VoteCb(CallbackData, prefix="V"):
    g: str
    i: int  # номер голосования
    v: int


class NoopCb(CallbackData, prefix="N"):
    pass
