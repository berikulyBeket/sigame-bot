"""Тайминги игры (секунды) и пресеты темпа."""

from __future__ import annotations

from dataclasses import dataclass, fields, replace


@dataclass(frozen=True, slots=True)
class Timings:
    choose: float  # выбор вопроса на табло
    press: float  # нажатие кнопки «Ответить»
    answer: float  # ответ после нажатия
    read_cps: float  # скорость «чтения» вопроса, символов в секунду
    read_min: float  # минимальная пауза перед кнопкой
    read_max: float  # максимальная пауза перед кнопкой
    give: float  # кот в мешке: кому отдать и какую цену выбрать
    stake: float  # ход в торгах
    final_remove: float  # финал: убрать тему
    final_bet: float  # ставки (финал и «ставки для всех»)
    final_answer: float  # ответ в финале
    all_answer: float  # «вопрос для всех»
    host_extra: float  # сколько ждать решения ведущего сверх времени ответа
    appeal_window: float  # сколько висит кнопка апелляции
    appeal_vote: float  # голосование по апелляции
    media_extra_max: float = 60  # потолок прибавки за аудио/видео

    def scaled(self, k: float) -> Timings:
        """Все интервалы ×k (для тестов и экспериментов), скорость чтения ÷k."""
        if k == 1:
            return self
        values = {f.name: getattr(self, f.name) * k for f in fields(self)}
        values["read_cps"] = self.read_cps / k
        return replace(self, **values)


PRESETS: dict[str, Timings] = {
    "fast": Timings(
        choose=30,
        press=10,
        answer=20,
        read_cps=25,
        read_min=1.5,
        read_max=6,
        give=20,
        stake=20,
        final_remove=20,
        final_bet=30,
        final_answer=45,
        all_answer=40,
        host_extra=60,
        appeal_window=40,
        appeal_vote=20,
    ),
    "normal": Timings(
        choose=45,
        press=15,
        answer=30,
        read_cps=18,
        read_min=2,
        read_max=10,
        give=30,
        stake=25,
        final_remove=30,
        final_bet=45,
        final_answer=60,
        all_answer=50,
        host_extra=90,
        appeal_window=60,
        appeal_vote=25,
    ),
    "slow": Timings(
        choose=90,
        press=25,
        answer=45,
        read_cps=12,
        read_min=3,
        read_max=15,
        give=45,
        stake=40,
        final_remove=45,
        final_bet=60,
        final_answer=90,
        all_answer=75,
        host_extra=120,
        appeal_window=90,
        appeal_vote=40,
    ),
}

PRESET_TITLES = {"fast": "быстрый", "normal": "обычный", "slow": "медленный"}
PRESET_ORDER = ["normal", "fast", "slow"]
