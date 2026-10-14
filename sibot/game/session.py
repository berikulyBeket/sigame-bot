"""Игровая сессия одного чата: лобби и сценарий игры.

Сценарий — обычная корутина (`_run`), которая ждёт действий игроков через
`_listen`. Обработчики Telegram передают действия в `submit`: проверка и
изменение состояния выполняются синхронно, без await, поэтому из двух
одновременных нажатий «Ответить» засчитывается ровно одно.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import random
import secrets
from collections.abc import Callable, Coroutine, Iterator
from dataclasses import dataclass, field
from enum import StrEnum
from typing import TYPE_CHECKING, Any

from aiogram import Bot
from aiogram.types import InlineKeyboardMarkup

from ..siq.model import SECRET_TYPES, Kind, NumberSet, QType, Question, Round, Theme
from ..tg import render as R
from ..tg.ui import TelegramUI
from . import checker
from .timings import PRESET_ORDER, PRESETS

if TYPE_CHECKING:
    from ..library import Library, PackageEntry
    from ..registry import Registry

log = logging.getLogger(__name__)

STALE = "Эта кнопка уже неактуальна"
LOBBY_TTL = 30 * 60  # набор без старта закрывается через 30 минут


class Phase(StrEnum):
    LOBBY = "lobby"
    PLAYING = "playing"
    FINISHED = "finished"


@dataclass(eq=False, slots=True)
class Player:
    id: int
    name: str
    score: int = 0


@dataclass(slots=True)
class Event:
    """Действие игрока: нажатие кнопки или сообщение."""

    kind: str  # pick, press, text, opt, give, price, bid, rm, bet, judge
    user_id: int
    data: Any = None
    step: int | None = None  # номер шага, для которого нарисована кнопка
    private: bool = False  # пришло из лички с ботом


Check = Callable[[Event], "str | None"]


class _Waiter:
    """Ожидание действий на одном шаге игры."""

    def __init__(self, check: Check, single: bool, step: int, label: str) -> None:
        self.check = check
        self.single = single  # после первого принятого события шаг закрывается
        self.step = step
        self.label = label  # что сейчас ждём (для логов и тестов)
        self.queue: asyncio.Queue[Event] = asyncio.Queue()
        self.closed = False

    async def get(self, timeout: float = 0, *, until: float | None = None) -> Event | None:
        if until is not None:
            timeout = until - asyncio.get_running_loop().time()
        try:
            return await asyncio.wait_for(self.queue.get(), max(timeout, 0))
        except TimeoutError:  # событие могло прийти в последний момент
            return None if self.queue.empty() else self.queue.get_nowait()


@dataclass(slots=True)
class BidOptions:
    can_pass: bool
    min_bid: int | None  # минимальная обычная ставка; None — только пас или ва-банк
    max_bid: int  # счёт игрока, он же сумма ва-банка
    allin: bool
    presets: list[int]


@dataclass(slots=True)
class Miss:
    """Незасчитанный ответ — по нему можно подать апелляцию."""

    player_id: int
    given: str
    penalty: int  # сколько сняли
    reward: int  # сколько дали бы за верный ответ


@dataclass(slots=True)
class AppealCase:
    question: Question
    misses: dict[int, Miss]
    deadline: float
    message_id: int | None = None
    used: set[int] = field(default_factory=set)


@dataclass(slots=True)
class Vote:
    miss: Miss
    voters: set[int]
    yes: set[int] = field(default_factory=set)
    no: set[int] = field(default_factory=set)
    done: asyncio.Event = field(default_factory=asyncio.Event)
    message_id: int | None = None


def _to_int(value: Any) -> int | None:
    try:
        return int(str(value).strip().replace(" ", "").replace(" ", ""))
    except ValueError:
        return None


def _now() -> float:
    return asyncio.get_running_loop().time()


class GameSession:
    def __init__(
        self,
        *,
        bot: Bot,
        chat_id: int,
        thread_id: int | None,
        chat_title: str,
        creator_id: int,
        library: Library,
        registry: Registry,
        bot_username: str,
        max_players: int = 12,
        max_upload_mb: int = 20,
        time_scale: float = 1.0,
    ) -> None:
        self.gid = secrets.token_hex(4)
        self.chat_id = chat_id
        self.chat_title = chat_title
        self.creator_id = creator_id
        self.library = library
        self.registry = registry
        self.bot_username = bot_username
        self.max_players = max_players
        self.max_upload_mb = max_upload_mb
        self.time_scale = time_scale
        self.ui = TelegramUI(bot, chat_id, thread_id, library, on_lost=self._lost, on_migrate=self._migrated)

        self.phase = Phase.LOBBY
        self.players: dict[int, Player] = {}
        self.host: Player | None = None
        self.entry: PackageEntry | None = library.default()
        self.preset = "normal"
        self.t = PRESETS[self.preset].scaled(time_scale)
        self.appeals = True
        self.privacy_warning = False
        self.lobby_message_id: int | None = None
        self._menu = "main"
        self._lobby_timer: asyncio.Task[None] | None = None

        self.round: Round | None = None
        self.round_no = 0
        self.played: set[tuple[int, int]] = set()
        self.chooser_id: int | None = None
        self.question_no = 0
        self.question: Question | None = None  # текущий вопрос
        self._answering: Player | None = None
        self._carry: list[str] = []  # текст, который уйдёт в начале следующего сообщения
        self._waiter: _Waiter | None = None
        self._step = 0
        self._task: asyncio.Task[None] | None = None
        self._started = False
        self._qtask: asyncio.Future[None] | None = None
        self._stop_reason = ""
        self._appeal_cases: dict[int, AppealCase] = {}
        self._votes: dict[int, Vote] = {}
        self._vote_seq = 0
        self._bg: set[asyncio.Task[None]] = set()

    @property
    def waiting_for(self) -> str | None:
        w = self._waiter
        return None if w is None or w.closed else w.label

    def involves(self, user_id: int) -> bool:
        return user_id in self.players or (self.host is not None and self.host.id == user_id)

    def player_by_index(self, n: int) -> Player | None:
        players = list(self.players.values())
        return players[n - 1] if 1 <= n <= len(players) else None

    # ================================================================== лобби

    def _lobby_view(self):
        if self._menu == "packs":
            return R.packs_text(self.max_upload_mb), R.packs_kb(self, self.library.available(self.chat_id))
        return R.lobby_text(self), R.lobby_kb(self)

    async def open_lobby(self) -> None:
        text, kb = self._lobby_view()
        self.lobby_message_id = await self.ui.send(text, kb, track=False)
        if self._lobby_timer is None:
            self._lobby_timer = asyncio.create_task(self._expire_lobby(), name=f"lobby-{self.gid}")

    async def _expire_lobby(self) -> None:
        await asyncio.sleep(LOBBY_TTL)
        if self.phase is Phase.LOBBY:
            self.abort("набор истёк")
            await self.ui.edit(self.lobby_message_id, "⌛ Набор закрыт: игру так и не начали. Новая игра: /newgame")

    async def repost_lobby(self) -> None:
        """Переслать лобби вниз чата (повторный /newgame)."""
        old = self.lobby_message_id
        self._menu = "main"
        await self.open_lobby()
        await self.ui.delete(old)

    async def refresh_lobby(self) -> None:
        if self.phase is Phase.LOBBY:
            text, kb = self._lobby_view()
            await self.ui.edit(self.lobby_message_id, text, kb)

    def join_error(self, user_id: int) -> str | None:
        if self.phase is Phase.FINISHED:
            return "Игра уже закончилась"
        if user_id in self.players:
            return "Вы уже в игре"
        if self.host is not None and self.host.id == user_id:
            return "Вы ведущий. Чтобы играть, сначала откажитесь от роли ведущего"
        if len(self.players) >= self.max_players:
            return f"Мест нет: максимум {self.max_players} игроков"
        return None

    async def join(self, user_id: int, name: str) -> str | None:
        if err := self.join_error(user_id):
            return err
        player = Player(user_id, name)
        self.players[user_id] = player
        if self.phase is Phase.LOBBY:
            await self.refresh_lobby()
        else:
            await self.ui.send(R.joined_midgame(player))
        return None

    async def leave(self, user_id: int) -> str | None:
        player = self.players.pop(user_id, None)
        if player is None:
            return "Вы не в игре"
        if self.phase is Phase.LOBBY:
            await self.refresh_lobby()
        elif self.phase is Phase.PLAYING:
            await self.ui.send(R.left_game(player))
            if not self.players:
                self.abort("все игроки вышли")
        return None

    def host_error(self, user_id: int) -> str | None:
        if self.phase is not Phase.LOBBY:
            return "Ведущего можно выбрать только до начала игры"
        if self.host is not None:
            return "Вы уже ведущий" if self.host.id == user_id else f"Ведущий уже есть: {self.host.name}"
        return None

    async def set_host(self, user_id: int, name: str) -> str | None:
        if err := self.host_error(user_id):
            return err
        self.players.pop(user_id, None)
        self.host = Player(user_id, name)
        await self.refresh_lobby()
        return None

    async def drop_host(self) -> None:
        host, self.host = self.host, None
        if self.phase is Phase.LOBBY:
            await self.refresh_lobby()
        elif host is not None:
            waiter = self._waiter
            if waiter is not None and not waiter.closed and waiter.label.startswith("judge"):
                waiter.queue.put_nowait(Event("host_gone", host.id))  # не ждать решения ушедшего ведущего
            await self.ui.send(f"🎙 {R.bold(host)} больше не ведущий — дальше ответы проверяет бот.")

    async def show_packs(self, on: bool) -> None:
        self._menu = "packs" if on else "main"
        await self.refresh_lobby()

    async def choose_package(self, key: str) -> str | None:
        entry = self.library.get(key)
        if entry is None or entry not in self.library.available(self.chat_id):
            return "Пакет не найден"
        await self.set_package(entry)
        return None

    async def set_package(self, entry: PackageEntry) -> None:
        self.entry = entry
        self._menu = "main"
        await self.refresh_lobby()

    async def cycle_tempo(self) -> None:
        self.preset = PRESET_ORDER[(PRESET_ORDER.index(self.preset) + 1) % len(PRESET_ORDER)]
        self.t = PRESETS[self.preset].scaled(self.time_scale)
        await self.refresh_lobby()

    async def toggle_appeals(self) -> None:
        self.appeals = not self.appeals
        await self.refresh_lobby()

    async def start(self) -> str | None:
        if self.phase is not Phase.LOBBY:
            return "Игра уже идёт"
        if self.entry is None:
            return "Сначала выберите пакет"
        if not self.players:
            return "Нужен хотя бы один игрок"
        self.phase = Phase.PLAYING
        if self._lobby_timer is not None:
            self._lobby_timer.cancel()
        await self.ui.edit(self.lobby_message_id, R.lobby_started(self), None)
        self._task = asyncio.create_task(self._run(), name=f"sigame-{self.gid}")
        return None

    async def stop(self, reason: str) -> None:
        if self.phase is Phase.LOBBY:
            self.abort(reason)
            await self.ui.edit(self.lobby_message_id, R.lobby_cancelled(), None)
        else:
            self.abort(reason)

    def abort(self, reason: str) -> asyncio.Task[None] | None:
        """Прервать игру; возвращает её задачу, чтобы можно было дождаться прощания."""
        if self.phase is Phase.LOBBY:
            self.phase = Phase.FINISHED
            self.registry.remove(self)
            if self._lobby_timer is not None and self._lobby_timer is not asyncio.current_task():
                self._lobby_timer.cancel()
            return None
        task = self._task
        if task is None or task.done():
            return None
        self._stop_reason = self._stop_reason or reason
        if self._started:  # отмена ещё не начавшейся задачи пропустила бы её finally
            task.cancel()
        return task

    def skip(self) -> bool:
        """Пропустить текущий вопрос (команда /skip)."""
        if self._qtask is not None and not self._qtask.done():
            self._qtask.cancel()
            return True
        return False

    async def kick(self, user_id: int) -> str | None:
        if self.host is not None and self.host.id == user_id:
            await self.drop_host()
            return None
        return await self.leave(user_id)

    async def set_score(self, user_id: int, value: int) -> str | None:
        player = self.players.get(user_id)
        if player is None:
            return "Такого игрока нет"
        player.score = value
        await self.ui.send(f"✏️ Счёт игрока {R.bold(player)} исправлен: <b>{R.pts(value)}</b>")
        return None

    def theme_info(self, round_no: int, theme: int) -> str:
        if self.round is None or round_no != self.round_no or not 0 <= theme < len(self.round.themes):
            return "Табло уже обновилось"
        return R.theme_info(self.round.themes[theme])

    def _lost(self) -> None:
        self.abort("бот потерял доступ к чату")

    def _migrated(self, new_chat_id: int) -> None:
        self.registry.move(self, new_chat_id)

    # ========================================================= приём действий

    def submit(self, ev: Event) -> str | None:
        """Принять действие игрока. None — принято; строка — причина отказа ("" — отказать молча)."""
        w = self._waiter
        if w is None or w.closed:
            return self._idle_reason(ev)
        if ev.step is not None and ev.step != w.step:
            return STALE
        err = w.check(ev)
        if err is not None:
            return err
        if w.single:
            w.closed = True
        w.queue.put_nowait(ev)
        return None

    def _idle_reason(self, ev: Event) -> str:
        if ev.kind == "text":
            return ""
        if self.phase is not Phase.PLAYING:
            return "Игра не идёт"
        if ev.step is not None and ev.step != self._step:
            return STALE
        if ev.kind in ("press", "pass") and self._answering is not None:
            return f"Уже отвечает {self._answering.name}"
        return "Сейчас это действие недоступно"

    @contextlib.contextmanager
    def _listen(self, label: str, check: Check, *, single: bool = True) -> Iterator[_Waiter]:
        self._step += 1
        waiter = _Waiter(check, single, self._step, label)
        self._waiter = waiter
        try:
            yield waiter
        finally:
            waiter.closed = True
            if self._waiter is waiter:
                self._waiter = None

    # ============================================================ ход игры

    async def _run(self) -> None:
        self._started = True
        completed = False
        try:
            if not self._stop_reason:  # игру могли остановить ещё до первого шага
                completed = await self._play()
        except asyncio.CancelledError:
            log.info("Игра %s остановлена: %s", self.gid, self._stop_reason)
            if (task := asyncio.current_task()) is not None:
                task.uncancel()
        except Exception:
            log.exception("Игра %s завершилась с ошибкой", self.gid)
            self._stop_reason = "внутренняя ошибка"
        finally:
            self.phase = Phase.FINISHED
            self._waiter = None
            self.registry.remove(self)
        try:
            await self._finish(completed)
        except Exception:
            log.exception("Не удалось завершить игру %s", self.gid)

    async def _play(self) -> bool:
        await self._intro()
        for no, rnd in enumerate(self.entry.package.rounds, 1):
            if not self.players:
                break
            self.round_no = no
            if rnd.final:
                await self._final_round(rnd)
            else:
                await self._regular_round(rnd)
        return bool(self.players)

    async def _finish(self, completed: bool) -> None:
        await self.ui.clear_tracked()
        await self._close_appeals()
        text = self._take_carry() + R.game_over(list(self.players.values()), completed, self._stop_reason)
        await self.ui.send(text, track=False)
        if self.host is not None:
            await self.ui.dm(self.host.id, "🏁 Игра окончена. Спасибо, что вели её!")

    async def _intro(self) -> None:
        text = R.game_intro(self.entry, self.host, self.players.values())
        logo = self.library.logo(self.entry)
        if logo is not None:
            await self.ui.send_content(text, [logo], self.entry)
        else:
            await self.ui.send(text)
        self.chooser_id = random.choice(list(self.players))
        await asyncio.sleep(self.t.read_min)

    async def _regular_round(self, rnd: Round) -> None:
        self.round, self.played = rnd, set()
        if self.round_no > 1 or self.chooser_id not in self.players:
            self.chooser_id = self._lowest().id  # по правилам новый раунд начинает отстающий
        await self.ui.send(self._take_carry() + R.round_intro(rnd, self.round_no))
        await asyncio.sleep(self.t.read_min)
        while self.players:
            available = [
                (ti, qi)
                for ti, theme in enumerate(rnd.themes)
                for qi in range(len(theme.questions))
                if (ti, qi) not in self.played
            ]
            if not available:
                break
            ti, qi = await self._select(rnd, available)
            self.played.add((ti, qi))
            theme = rnd.themes[ti]
            await self._guarded(self._play_question(theme, theme.questions[qi]), theme.questions[qi])
        self._carry.append(R.round_end(rnd, self.players.values()))

    async def _select(self, rnd: Round, available: list[tuple[int, int]]) -> tuple[int, int]:
        chooser = self._chooser()
        compact = R.board_is_compact(rnd, self.played)
        base = self._take_carry() + R.board_text(rnd, self.players.values(), self.played, compact)

        def check(ev: Event) -> str | None:
            if ev.kind != "pick":
                return ""
            if ev.user_id != chooser.id:
                return f"Сейчас выбирает {chooser.name}"
            if ev.data not in available:
                return "Этот вопрос уже сыгран"
            return None

        with self._listen("pick", check) as w:
            kb = R.board_kb(self.gid, w.step, rnd, self.played, compact, self.round_no)
            mid = await self.ui.send(f"{base}\n\n{R.board_prompt(chooser, self.t.choose)}", kb)
            ev = await w.get(self.t.choose)
        ti, qi = ev.data if ev else random.choice(available)
        theme = rnd.themes[ti]
        chosen = R.board_chosen(chooser, theme, theme.questions[qi].price, auto=ev is None)
        await self.ui.edit(mid, f"{base}\n\n{chosen}", None)
        return ti, qi

    async def _guarded(self, coro: Coroutine[Any, Any, None], q: Question) -> None:
        """Сыграть вопрос так, чтобы /skip прерывал только его, а не всю игру."""
        self._qtask = asyncio.ensure_future(coro)
        try:
            await self._qtask
        except asyncio.CancelledError:
            current = asyncio.current_task()
            if current is not None and current.cancelling():
                raise  # останавливают всю игру
            await self.ui.clear_tracked()
            self._carry.append(f"{R.skipped_line()}\n{R.answer_block(q, self._display)}")
        finally:
            self._qtask = None
            self._answering = None

    async def _play_question(self, theme: Theme, q: Question) -> None:
        self.question_no += 1
        self.question = q
        chooser = self._chooser()
        if q.type in SECRET_TYPES:
            await self._q_secret(theme, q, chooser)
        elif q.type is QType.STAKE:
            await self._q_stake(theme, q, chooser)
        elif q.type is QType.NO_RISK:
            await self._q_norisk(theme, q, chooser)
        elif q.type in (QType.FOR_ALL, QType.STAKE_ALL):
            await self._q_for_all(theme, q)
        else:
            await self._q_simple(theme, q)

    # ------------------------------------------------------- обычный вопрос

    async def _q_simple(self, theme: Theme, q: Question) -> None:
        price = q.price
        mid, media_time = await self._show(R.question_header(theme.name, price), q)
        await self._brief_host(theme.name, price, q)
        await asyncio.sleep(self._read_delay(q))

        tried: set[int] = set()  # уже отвечали
        passed: set[int] = set()  # спасовали: пас держится до конца вопроса, отменяется нажатием «Ответить»
        misses: list[Miss] = []
        lines: list[str] = []
        pending: str | None = None  # вердикт, который уйдёт вместе с повторной кнопкой
        window = self.t.press + min(media_time, self.t.media_extra_max)
        winner = everyone_passed = False

        def remaining() -> list[int]:
            return [pid for pid in self.players if pid not in tried]

        def all_passed() -> bool:
            left = remaining()
            return bool(left) and all(pid in passed for pid in left)

        def buttons(step: int) -> InlineKeyboardMarkup:
            left = remaining()
            return R.press_kb(self.gid, step, sum(pid in passed for pid in left), len(left))

        def check(ev: Event) -> str | None:
            if ev.kind not in ("press", "pass"):
                return ""
            if ev.user_id not in self.players:
                return "Вы не участвуете в этой игре"
            if ev.user_id in tried:
                return "Вы уже отвечали на этот вопрос"
            if self._answering is not None:
                return f"Уже отвечает {self._answering.name}"
            if ev.kind == "pass":
                if ev.user_id in passed:
                    return "Вы уже спасовали"
                passed.add(ev.user_id)
                return None
            passed.discard(ev.user_id)  # передумал и всё-таки отвечает
            self._answering = self.players[ev.user_id]  # остальные нажатия теперь отклоняются
            return None

        while remaining():
            if all_passed():  # пасы остались с прошлого окна — открывать кнопку незачем
                everyone_passed = True
                break
            with self._listen("press", check, single=False) as w:
                if pending is None:
                    target = mid
                    await self.ui.set_kb(target, buttons(w.step))
                else:
                    target = await self.ui.send(pending + R.press_more(window), buttons(w.step))
                    pending = None
                deadline = _now() + window
                while (ev := await w.get(until=deadline)) is not None and ev.kind == "pass":
                    if all_passed():
                        break
                    await self.ui.set_kb(target, buttons(w.step))  # обновить счётчик пасов
            await self.ui.set_kb(target, None)
            if ev is None:
                break
            if ev.kind == "pass":  # спасовали все, кто ещё мог ответить
                everyone_passed = True
                break
            player = self.players.get(ev.user_id)
            if player is None:  # нажал и сразу вышел из игры
                self._answering = None
                continue
            tried.add(player.id)
            ok, given = await self._answer(player, q, theme.name, price, self.t.answer)
            if ok:
                self._add(player, price)
                self.chooser_id = player.id
                lines.append(R.right_line(player, self._display(q, given), price))
                winner = True
                break
            self._add(player, -price)
            if given:
                misses.append(Miss(player.id, given, price, price))
            pending = R.wrong_line(player, self._display(q, given), -price)
            window = self.t.press

        if pending:
            lines.append(pending)
        if not winner:
            lines.append(R.passed_line(bool(tried)) if everyone_passed else R.nobody_line(bool(tried)))
        await self._reveal(q, lines, misses)

    async def _answer(
        self, player: Player, q: Question, theme_name: str, price: int | None, timeout: float
    ) -> tuple[bool, str | None]:
        """Ответ одного игрока — сообщением в чат или кнопкой варианта. Решает бот или ведущий."""
        self._answering = player
        try:
            if self.host is not None:
                return await self._answer_hosted(player, q, theme_name, price, timeout)

            def check(ev: Event) -> str | None:
                if ev.user_id != player.id or ev.private:
                    return ""
                return None if ev.kind == "text" or (ev.kind == "opt" and q.answer_type == "select") else ""

            with self._listen(f"answer:{player.id}", check) as w:
                kb = R.options_kb(self.gid, w.step, q) if q.answer_type == "select" else None
                text = R.answer_prompt(player, timeout, q, hosted=False, reply_hint=self.privacy_warning)
                prompt = await self.ui.send(text, kb)
                ev = await w.get(timeout)
            if kb is not None:
                await self.ui.set_kb(prompt, None)
            if ev is None:
                return False, None
            given = str(ev.data)
            return checker.check_answer(q, given), given
        finally:
            self._answering = None

    async def _answer_hosted(
        self, player: Player, q: Question, theme_name: str, price: int | None, timeout: float
    ) -> tuple[bool, str | None]:
        host = self.host
        assert host is not None
        given: str | None = None
        verdict: bool | None = None

        def check(ev: Event) -> str | None:
            answering = ev.kind == "text" or (ev.kind == "opt" and q.answer_type == "select")
            if answering and ev.user_id == player.id and not ev.private:
                return None
            if ev.kind == "judge":
                if self.host is None or ev.user_id != self.host.id:
                    return "Оценивает только ведущий"
                return None if ev.data[0] == player.id else STALE
            return ""

        with self._listen(f"judge:{player.id}", check, single=False) as w:
            kb = R.options_kb(self.gid, w.step, q) if q.answer_type == "select" else None
            text = R.answer_prompt(player, timeout, q, hosted=True, reply_hint=self.privacy_warning)
            prompt = await self.ui.send(text, kb)
            judge_kb = R.judge_kb(self.gid, w.step, player.id)
            dm = await self.ui.dm(host.id, R.judge_text(player, theme_name, price, q, None), judge_kb)
            deadline = _now() + timeout + (self.t.host_extra if dm else 0)
            while True:
                ev = await w.get(until=deadline)
                if ev is None or ev.kind == "host_gone":
                    break
                if ev.kind == "judge":
                    verdict = bool(ev.data[1])
                    break
                given = str(ev.data)
                if dm is None:  # ведущему не написать — проверяет бот
                    break
                shown = self._display(q, given)
                await self.ui.dm_edit(host.id, dm, R.judge_text(player, theme_name, price, q, shown), judge_kb)
        if kb is not None:
            await self.ui.set_kb(prompt, None)
        auto = verdict is None
        if verdict is None:
            verdict = checker.check_answer(q, given)
        text = R.judge_text(player, theme_name, price, q, self._display(q, given)) + R.judge_done(verdict, auto)
        await self.ui.dm_edit(host.id, dm, text, None)
        return verdict, given

    async def _single_result(
        self, player: Player, q: Question, ok: bool, given: str | None, gain: int, loss: int
    ) -> None:
        """Итог вопроса, на который отвечал один назначенный игрок."""
        if ok:
            self._add(player, gain)
            self.chooser_id = player.id
            lines = [R.right_line(player, self._display(q, given), gain)]
        else:
            self._add(player, -loss)
            lines = [R.wrong_line(player, self._display(q, given), -loss)]
        misses = [Miss(player.id, given, loss, gain)] if not ok and given else []
        await self._reveal(q, lines, misses)

    # ---------------------------------------------------------- кот в мешке

    async def _q_secret(self, theme: Theme, q: Question, chooser: Player) -> None:
        prices = (q.secret_price or NumberSet(q.price, q.price)).options(self.round.prices if self.round else [])
        cat_theme = q.secret_theme.strip() or theme.name
        public = q.type is QType.SECRET_PUBLIC
        gift = q.type is QType.SECRET_NO_QUESTION
        everyone = list(self.players.values())
        if q.selection == "any":
            candidates = everyone
        elif q.selection == "current":
            candidates = [chooser]
        else:
            candidates = [p for p in everyone if p is not chooser] or [chooser]

        mid = None
        if len(candidates) == 1:
            receiver, how = candidates[0], "only"
        else:
            ids = {p.id for p in candidates}

            def check(ev: Event) -> str | None:
                if ev.kind != "give":
                    return ""
                if ev.user_id != chooser.id:
                    return f"Кота в мешке отдаёт {chooser.name}"
                target = _to_int(ev.data)
                return None if target in ids and target in self.players else "Этому игроку отдать нельзя"

            with self._listen("give", check) as w:
                text = R.secret_give(chooser, cat_theme if public else None, prices if public else None, self.t.give)
                mid = await self.ui.send(text, R.give_kb(self.gid, w.step, candidates))
                ev = await w.get(self.t.give)
            picked = self.players.get(_to_int(ev.data)) if ev else None
            if picked is not None:
                receiver, how = picked, "chosen"
            else:
                receiver, how = random.choice([p for p in candidates if p.id in self.players] or [chooser]), "timeout"
        given_text = R.secret_given(chooser, receiver, cat_theme, how, gift)
        if mid is not None:
            await self.ui.edit(mid, given_text, None)
        else:
            await self.ui.send(given_text)

        price = prices[0]
        if len(prices) > 1:

            def check_price(ev: Event) -> str | None:
                if ev.kind != "price":
                    return ""
                if ev.user_id != receiver.id:
                    return f"Стоимость выбирает {receiver.name}"
                return None if _to_int(ev.data) in prices else STALE

            with self._listen("price", check_price) as w:
                price_mid = await self.ui.send(
                    R.secret_price(receiver, prices, self.t.give), R.price_kb(self.gid, w.step, prices)
                )
                ev = await w.get(self.t.give)
            price = _to_int(ev.data) if ev else prices[0]
            await self.ui.edit(price_mid, R.secret_price_chosen(receiver, price, auto=ev is None), None)

        if gift:
            self._add(receiver, price)
            self._carry.append(R.secret_gift(receiver, price))
            return
        tag = "🐱 Кот в мешке"
        _, media_time = await self._show(R.question_header(cat_theme, price, tag), q)
        await self._brief_host(cat_theme, price, q, tag)
        ok, given = await self._answer(receiver, q, cat_theme, price, self._single_timeout(q, media_time))
        await self._single_result(receiver, q, ok, given, gain=price, loss=price)

    # ------------------------------------------------------- вопрос со ставкой

    async def _q_stake(self, theme: Theme, q: Question, chooser: Player) -> None:
        nominal = q.price
        step = self._bid_step()
        order = self._order_from(chooser)
        stake, leader, allin = 0, None, False
        passed: set[int] = set()
        history: list[str] = []
        mid: int | None = None
        turn = 0
        while True:
            alive = [p for p in order if p.id in self.players and p.id not in passed]
            if not alive or (leader is not None and alive == [leader]):
                break
            current = order[turn % len(order)]
            turn += 1
            if current.id not in self.players or current.id in passed or current is leader:
                continue
            opts = self._bid_options(current, stake, leader is None, nominal, allin, step)
            if opts is None:
                passed.add(current.id)
                history.append(R.stake_log(current, "pass", 0) + " — не хватает очков")
                continue

            def check(ev: Event, current: Player = current, opts: BidOptions = opts) -> str | None:
                if ev.kind not in ("bid", "text") or ev.private:
                    return ""
                if ev.user_id != current.id:
                    return "" if ev.kind == "text" else f"Сейчас ход {current.name}"
                parsed = self._parse_bid(str(ev.data), opts)
                return parsed if isinstance(parsed, str) else None

            with self._listen(f"bid:{current.id}", check) as w:
                text = R.stake_text(theme.name, nominal, stake, leader, current, history, self.t.stake)
                kb = R.stake_kb(self.gid, w.step, opts)
                if mid is None:
                    mid = await self.ui.send(text, kb)
                else:
                    await self.ui.edit(mid, text, kb)
                ev = await w.get(self.t.stake)
            if ev is None:  # молчание — пас; первую ставку делаем за игрока минимальной
                action, amount = ("bid", opts.min_bid or nominal) if leader is None else ("pass", 0)
            else:
                parsed = self._parse_bid(str(ev.data), opts)
                action, amount = parsed if isinstance(parsed, tuple) else ("pass", 0)
            if action == "pass":
                passed.add(current.id)
            else:
                stake, leader = amount, current
                allin = allin or action == "allin"
            history.append(R.stake_log(current, action, amount, auto=ev is None))

        if leader is None:
            leader, stake = chooser, nominal
        done = R.stake_done(theme.name, nominal, leader, stake, history)
        if mid is not None:
            await self.ui.edit(mid, done, None)
        else:
            await self.ui.send(done)
        if leader.id not in self.players:
            await self._reveal(q, [R.nobody_line(False)], [])
            return
        tag = "💰 Вопрос со ставкой"
        _, media_time = await self._show(R.question_header(theme.name, stake, tag), q)
        await self._brief_host(theme.name, stake, q, tag)
        ok, given = await self._answer(leader, q, theme.name, stake, self._single_timeout(q, media_time))
        await self._single_result(leader, q, ok, given, gain=stake, loss=stake)

    def _bid_step(self) -> int:
        prices = [p for p in (self.round.prices if self.round else []) if p > 0]
        return min(100, max(1, min(prices, default=100)))

    @staticmethod
    def _bid_options(
        p: Player, stake: int, first: bool, nominal: int, allin_mode: bool, step: int
    ) -> BidOptions | None:
        """Что может игрок в торгах. None — ходить нечем, автоматический пас."""
        score = p.score
        if first:
            if score <= nominal:  # очков мало — играет по номиналу
                return BidOptions(can_pass=False, min_bid=nominal, max_bid=nominal, allin=False, presets=[nominal])
            presets = [v for v in (nominal, nominal + step, nominal + 2 * step, nominal + 5 * step) if v < score]
            return BidOptions(False, nominal, score, True, presets)
        if allin_mode:  # ва-банк перебивается только бо́льшим ва-банком
            return BidOptions(True, None, score, True, []) if score > stake else None
        low = stake + step
        if score >= low:
            presets = [v for v in (low, low + step, low + 3 * step) if v < score]
            return BidOptions(True, low, score, True, presets)
        return BidOptions(True, None, score, True, []) if score > stake else None

    @staticmethod
    def _parse_bid(raw: str, o: BidOptions) -> tuple[str, int] | str:
        s = raw.strip().casefold().replace(" ", "").replace(" ", "")
        if s in ("pass", "пас"):
            return ("pass", 0) if o.can_pass else "Первая ставка обязательна"
        if s in ("allin", "ва-банк", "вабанк", "all-in", "va-bank"):
            return ("allin", o.max_bid) if o.allin else "Ва-банк сейчас недоступен"
        if not s.isdigit():
            return ""
        n = int(s)
        if o.allin and n == o.max_bid:
            return "allin", n
        if o.min_bid is None:
            return "Можно только спасовать или пойти ва-банк"
        if not o.min_bid <= n <= o.max_bid:
            return f"Ставка — от {o.min_bid} до {o.max_bid}"
        return "bid", n

    # ---------------------------------------------------- без риска / для всех

    async def _q_norisk(self, theme: Theme, q: Question, chooser: Player) -> None:
        tag = R.norisk_tag(chooser, q.price)
        _, media_time = await self._show(R.question_header(theme.name, q.price, tag), q)
        await self._brief_host(theme.name, q.price, q, "🛡 Вопрос без риска")
        ok, given = await self._answer(chooser, q, theme.name, q.price, self._single_timeout(q, media_time))
        await self._single_result(chooser, q, ok, given, gain=2 * q.price, loss=0)

    async def _q_for_all(self, theme: Theme, q: Question) -> None:
        stake_all = q.type is QType.STAKE_ALL
        part = [p for p in self.players.values() if p.score > 0] if stake_all else list(self.players.values())
        if not part:  # ставить некому — играем как обычный «вопрос для всех»
            stake_all, part = False, list(self.players.values())
        await self.ui.send(R.all_intro(theme.name, q.price, part, stake_all))
        bets = await self._collect_bets(theme.name, part) if stake_all else {p.id: q.price for p in part}
        tag = "💰 Ставки для всех" if stake_all else "👥 Вопрос для всех"
        _, media_time = await self._show(R.question_header(theme.name, None if stake_all else q.price, tag), q)
        await self._brief_host(theme.name, q.price, q, tag)
        timeout = self.t.all_answer + self._read_delay(q) + min(media_time, self.t.media_extra_max)
        answers = await self._collect_answers(theme.name, q, part, timeout, final=False)
        verdicts = await self._judge_all(theme.name, q, part, answers)
        lines, misses = [], []
        for p in part:
            if p.id not in self.players:
                continue
            ok, given, amount = verdicts.get(p.id, False), answers.get(p.id), bets.get(p.id, q.price)
            self._add(p, amount if ok else -amount)
            lines.append(R.many_line(p, self._display(q, given), ok, amount))
            if not ok and given:
                misses.append(Miss(p.id, given, amount, amount))
        await self._reveal(q, [R.many_results(lines)], misses)

    async def _collect_bets(self, theme_name: str, part: list[Player]) -> dict[int, int]:
        """Тайные ставки в личке: кнопками или числом."""
        ids = {p.id for p in part}
        taken: set[int] = set()
        bets: dict[int, int] = {}

        def check(ev: Event) -> str | None:
            if ev.user_id not in ids or ev.kind not in ("bet", "text") or (ev.kind == "text" and not ev.private):
                return ""
            if ev.user_id in taken:
                return "Ставка уже принята"
            player = self.players.get(ev.user_id)
            if player is None:
                return ""
            n = _to_int(ev.data)
            if n is None or not 1 <= n <= player.score:
                return f"Ставка — целое число от 1 до {player.score}"
            taken.add(ev.user_id)
            return None

        dms: dict[int, int | None] = {}
        with self._listen("bets", check, single=False) as w:
            for p in part:
                dms[p.id] = await self.ui.dm(
                    p.id, R.bet_prompt(theme_name, p.score, self.t.final_bet), R.bet_kb(self.gid, w.step, p.score)
                )
            missing = [p for p in part if dms[p.id] is None]
            status = await self.ui.send(R.bets_wait(part, missing, self.t.final_bet, self.bot_username))
            deadline = _now() + self.t.final_bet
            while len(bets) < len(part):
                ev = await w.get(until=deadline)
                if ev is None:
                    break
                bets[ev.user_id] = _to_int(ev.data) or 1
                if dms.get(ev.user_id):
                    await self.ui.dm_edit(ev.user_id, dms[ev.user_id], R.bet_done(bets[ev.user_id]), None)
                else:
                    await self.ui.dm(ev.user_id, R.bet_done(bets[ev.user_id]))
        for p in part:
            if p.id not in bets:
                bets[p.id] = 1
                await self.ui.dm_edit(p.id, dms.get(p.id), R.bet_default(), None)
        await self.ui.edit(status, R.bets_done(part), None)
        return bets

    async def _collect_answers(
        self, theme_name: str, q: Question, part: list[Player], timeout: float, final: bool
    ) -> dict[int, str]:
        """Тайные ответы в личке (финал, «вопрос для всех»). Принимается первый ответ."""
        ids = {p.id for p in part}
        taken: set[int] = set()
        answers: dict[int, str] = {}

        def check(ev: Event) -> str | None:
            if ev.user_id not in ids or not ev.private:
                return ""
            if not (ev.kind == "text" or (ev.kind == "opt" and q.answer_type == "select")):
                return ""
            if ev.user_id in taken:
                return "Ответ уже принят — изменить его нельзя"
            taken.add(ev.user_id)
            return None

        dms: dict[int, int | None] = {}
        with self._listen("answers", check, single=False) as w:
            for p in part:
                kb = R.options_kb(self.gid, w.step, q) if q.answer_type == "select" else None
                dms[p.id] = await self.ui.dm(p.id, R.private_question(theme_name, q, timeout, final), kb)
            missing = [p for p in part if dms[p.id] is None]
            await self.ui.send(R.answers_wait(part, missing, timeout, self.bot_username))
            deadline = _now() + timeout
            while len(answers) < len(part):
                ev = await w.get(until=deadline)
                if ev is None:
                    break
                answers[ev.user_id] = str(ev.data)
                await self.ui.dm(ev.user_id, R.answer_received(self._display(q, answers[ev.user_id]) or ""))
        if q.answer_type == "select":
            for uid, mid in dms.items():
                await self.ui.set_kb(mid, None, chat_id=uid)
        for p in part:
            if p.id not in answers and dms.get(p.id):
                await self.ui.dm(p.id, "⏱ Время на ответ вышло.")
        return answers

    async def _judge_all(
        self, theme_name: str, q: Question, part: list[Player], answers: dict[int, str]
    ) -> dict[int, bool]:
        """Проверка тайных ответов: ведущим (кнопками в личке) или автоматически."""
        verdicts = {p.id: False for p in part if not answers.get(p.id)}
        todo = [p for p in part if answers.get(p.id)]
        host = self.host
        msgs: dict[int, int | None] = {}
        if host is not None and todo:
            pending = {p.id for p in todo}

            def check(ev: Event) -> str | None:
                if ev.kind != "judge":
                    return ""
                if self.host is None or ev.user_id != self.host.id:
                    return "Оценивает только ведущий"
                if ev.data[0] not in pending:
                    return "Этот ответ уже оценён"
                pending.discard(ev.data[0])
                return None

            with self._listen("judge_all", check, single=False) as w:
                for p in todo:
                    text = R.judge_text(p, theme_name, None, q, self._display(q, answers[p.id]))
                    msgs[p.id] = await self.ui.dm(host.id, text, R.judge_kb(self.gid, w.step, p.id))
                if all(m is not None for m in msgs.values()):
                    await self.ui.send("🎙 Ведущий проверяет ответы…")
                    deadline = _now() + self.t.host_extra
                    while any(p.id not in verdicts for p in todo):
                        ev = await w.get(until=deadline)
                        if ev is None or ev.kind == "host_gone":
                            break
                        pid, value = ev.data
                        verdicts[pid] = bool(value)
                        p = next(x for x in todo if x.id == pid)
                        text = R.judge_text(p, theme_name, None, q, self._display(q, answers[pid]))
                        await self.ui.dm_edit(host.id, msgs[pid], text + R.judge_done(bool(value), False), None)
        for p in todo:
            if p.id not in verdicts:
                verdicts[p.id] = checker.check_answer(q, answers[p.id])
                if host is not None:
                    text = R.judge_text(p, theme_name, None, q, self._display(q, answers[p.id]))
                    await self.ui.dm_edit(host.id, msgs.get(p.id), text + R.judge_done(verdicts[p.id], True), None)
        return verdicts

    # ------------------------------------------------------------------- финал

    async def _final_round(self, rnd: Round) -> None:
        self.round, self.played = rnd, set()
        finalists = [p for p in self.players.values() if p.score > 0]
        excluded = [p for p in self.players.values() if p.score <= 0]
        await self.ui.send(self._take_carry() + R.final_intro(rnd, finalists, excluded))
        if not finalists:
            return
        await asyncio.sleep(self.t.read_min)
        theme = await self._final_theme(rnd.themes, finalists)
        self.question = q = theme.questions[0]
        await self._guarded(self._final_play(theme, q, finalists), q)

    async def _final_theme(self, themes: list[Theme], finalists: list[Player]) -> Theme:
        """Финалисты по очереди убирают темы, пока не останется одна."""
        remaining = list(range(len(themes)))
        order = sorted(finalists, key=lambda p: p.score)
        mid: int | None = None
        turn = 0
        while len(remaining) > 1:
            present = [p for p in order if p.id in self.players]
            if not present:
                break
            current = present[turn % len(present)]
            turn += 1

            def check(ev: Event, current: Player = current) -> str | None:
                if ev.kind != "rm":
                    return ""
                if ev.user_id != current.id:
                    return f"Сейчас убирает тему {current.name}"
                return None if _to_int(ev.data) in remaining else "Эта тема уже убрана"

            with self._listen(f"rm:{current.id}", check) as w:
                text = R.final_themes(themes, remaining, current, self.t.final_remove)
                kb = R.final_themes_kb(self.gid, w.step, themes, remaining)
                if mid is None:
                    mid = await self.ui.send(text, kb)
                else:
                    await self.ui.edit(mid, text, kb)
                ev = await w.get(self.t.final_remove)
            removed = _to_int(ev.data) if ev else None
            remaining.remove(removed if removed in remaining else random.choice(remaining))
        theme = themes[remaining[0]]
        if mid is not None:
            await self.ui.edit(mid, R.final_theme_left(theme), None)
        else:
            await self.ui.send(R.final_theme_left(theme))
        return theme

    async def _final_play(self, theme: Theme, q: Question, finalists: list[Player]) -> None:
        finalists = [p for p in finalists if p.id in self.players]
        bets = await self._collect_bets(theme.name, finalists)
        tag = "🏆 Финал"
        _, media_time = await self._show(R.question_header(theme.name, None, tag), q)
        await self._brief_host(theme.name, None, q, tag)
        timeout = self.t.final_answer + self._read_delay(q) + min(media_time, self.t.media_extra_max)
        answers = await self._collect_answers(theme.name, q, finalists, timeout, final=True)
        verdicts = await self._judge_all(theme.name, q, finalists, answers)
        lines = []
        for p in sorted(finalists, key=lambda p: p.score):
            if p.id not in self.players:
                continue
            ok, given, amount = verdicts.get(p.id, False), answers.get(p.id), bets.get(p.id, 1)
            self._add(p, amount if ok else -amount)
            lines.append(R.many_line(p, self._display(q, given), ok, amount))
        await self._reveal(q, [R.final_results(lines)], [])

    # --------------------------------------------------------------- апелляции

    def request_appeal(self, no: int, user_id: int) -> str | None:
        case = self._appeal_cases.get(no)
        if case is None or _now() > case.deadline:
            return "Время для апелляции истекло"
        miss = case.misses.get(user_id)
        if miss is None:
            return "Апелляцию подаёт игрок, чей ответ не засчитали"
        if user_id in case.used:
            return "Вы уже подали апелляцию по этому вопросу"
        case.used.add(user_id)
        self._spawn(self._vote(case, miss), f"vote-{self.gid}")
        return None

    async def _vote(self, case: AppealCase, miss: Miss) -> None:
        player = self.players.get(miss.player_id)
        if player is None:
            return
        voters = {pid for pid in self.players if pid != player.id}
        vote = Vote(miss, voters)
        self._vote_seq += 1
        vid = self._vote_seq
        self._votes[vid] = vote
        try:
            if voters:
                answer = self._display(case.question, case.question.answer) or ""
                vote.message_id = await self.ui.send(
                    R.vote_text(player, miss.given, answer, self.t.appeal_vote),
                    R.vote_kb(self.gid, vid, 0, 0),
                    track=False,
                )
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(vote.done.wait(), self.t.appeal_vote)
            vote.done.set()
            accepted = not voters or len(vote.yes) > len(vote.no)
            gain = miss.penalty + miss.reward
            if accepted and player.id in self.players:
                self._add(player, gain)
            result = R.vote_result(player, miss.given, accepted, gain, len(vote.yes), len(vote.no), len(voters))
            if vote.message_id is not None:
                await self.ui.edit(vote.message_id, result, None)
            else:
                await self.ui.send(result, track=False)
        finally:
            self._votes.pop(vid, None)

    async def cast_vote(self, vid: int, user_id: int, yes: bool) -> str | None:
        vote = self._votes.get(vid)
        if vote is None or vote.done.is_set():
            return "Голосование уже закончилось"
        if user_id not in vote.voters:
            return "Голосуют остальные игроки"
        (vote.yes if yes else vote.no).add(user_id)
        (vote.no if yes else vote.yes).discard(user_id)
        if vote.yes | vote.no >= vote.voters:
            vote.done.set()
        else:
            kb = R.vote_kb(self.gid, vid, len(vote.yes), len(vote.no))
            await self.ui.set_kb(vote.message_id, kb, track=False)
        return None

    async def _expire_appeal(self, no: int, case: AppealCase) -> None:
        await asyncio.sleep(max(0.0, case.deadline - _now()))
        if self._appeal_cases.get(no) is case:
            del self._appeal_cases[no]
            await self.ui.set_kb(case.message_id, None, track=False)

    async def _close_appeals(self) -> None:
        for case in self._appeal_cases.values():
            await self.ui.set_kb(case.message_id, None, track=False)
        self._appeal_cases.clear()
        votes = [t for t in self._bg if t.get_name().startswith("vote-")]
        if votes:  # даём идущим голосованиям закончиться — они меняют итоговый счёт
            await asyncio.wait(votes, timeout=self.t.appeal_vote + 5)
        for task in list(self._bg):
            task.cancel()

    # ---------------------------------------------------------------- утилиты

    async def _reveal(self, q: Question, lines: list[str], misses: list[Miss]) -> None:
        """Показать правильный ответ. Если нет медиа и апелляций, текст уходит в следующее табло."""
        text = "\n".join([*lines, R.answer_block(q, self._display)])
        kb, case = None, None
        if misses and self.appeals and self.host is None:
            case = AppealCase(q, {m.player_id: m for m in misses}, _now() + self.t.appeal_window)
            self._appeal_cases[self.question_no] = case
            kb = R.appeal_kb(self.gid, self.question_no)
        has_media = any(c.is_media or (c.kind is Kind.HTML and c.ref) for c in q.answer_content)
        if not has_media and kb is None:
            self._carry.append(text)
            return
        await self._flush_carry()
        mid, _ = await self.ui.send_content(text, q.answer_content, self.entry, kb, track=False)
        if case is not None:
            case.message_id = mid
            self._spawn(self._expire_appeal(self.question_no, case), f"expire-{self.gid}")

    async def _show(self, header: str, q: Question) -> tuple[int | None, float]:
        await self._flush_carry()
        return await self.ui.send_content(R.question_text(header, q), q.content, self.entry)

    async def _brief_host(self, theme_name: str, price: int | None, q: Question, tag: str | None = None) -> None:
        if self.host is not None:
            await self.ui.dm(self.host.id, R.host_brief(theme_name, price, q, tag))

    def _read_delay(self, q: Question) -> float:
        """Пауза на чтение перед кнопкой «Ответить»."""
        chars = len(q.text) + sum(len(text) for _, text in q.options)
        images = sum(1 for c in q.content if c.kind is Kind.IMAGE)
        delay = chars / self.t.read_cps + images * self.t.read_min
        return min(max(delay, self.t.read_min), self.t.read_max)

    def _single_timeout(self, q: Question, media_time: float) -> float:
        return self.t.answer + self._read_delay(q) + min(media_time, self.t.media_extra_max)

    @staticmethod
    def _display(q: Question, given: str | None) -> str | None:
        """Как показать ответ: для вопросов с вариантами — «C) текст варианта»."""
        if given and q.answer_type == "select" and q.options:
            label = checker.resolve_option(q, given)
            if label is not None:
                return f"{label}) {dict(q.options)[label]}"
        return given

    def _add(self, player: Player, delta: int) -> None:
        player.score += delta

    def _lowest(self) -> Player:
        low = min(p.score for p in self.players.values())
        return random.choice([p for p in self.players.values() if p.score == low])

    def _chooser(self) -> Player:
        player = self.players.get(self.chooser_id) if self.chooser_id is not None else None
        if player is None:
            player = self._lowest()
            self.chooser_id = player.id
        return player

    def _order_from(self, first: Player) -> list[Player]:
        players = list(self.players.values())
        i = players.index(first) if first in players else 0
        return players[i:] + players[:i]

    def _take_carry(self) -> str:
        text = "\n\n".join(self._carry)
        self._carry.clear()
        return f"{text}\n\n" if text else ""

    async def _flush_carry(self) -> None:
        if self._carry:
            await self.ui.send(self._take_carry().rstrip())

    def _spawn(self, coro: Coroutine[Any, Any, None], name: str) -> None:
        task = asyncio.create_task(coro, name=name)
        self._bg.add(task)
        task.add_done_callback(self._bg_done)

    def _bg_done(self, task: asyncio.Task[None]) -> None:
        self._bg.discard(task)
        if not task.cancelled() and (exc := task.exception()) is not None:
            log.error("Фоновая задача игры %s упала", self.gid, exc_info=exc)
