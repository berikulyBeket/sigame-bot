"""Сквозные сценарии: настоящий диспетчер aiogram и игровой движок, фейковый Telegram."""

from __future__ import annotations

import asyncio
import logging
import random

import pytest
from aiogram.types import User

from sibot.game.session import GameSession, Phase
from sibot.siq.model import Question
from sibot.tg.callbacks import LobbyCb
from tests.fakes import BOT, GROUP, Harness, until, waiting

ALICE = Harness.user(1, "Алиса")
BOB = Harness.user(2, "Боб")
CAROL = Harness.user(3, "Кэрол")
USERS = {u.id: u for u in (ALICE, BOB, CAROL)}


def lobby(s: GameSession, action: str) -> str:
    return LobbyCb(g=s.gid, a=action).pack()


async def start_game(h: Harness, players: list[User], host: User | None = None) -> GameSession:
    await h.say(ALICE, "/newgame")
    s = h.session()
    if host is not None:
        await h.click(host, lobby(s, "host"))
    for p in players:
        await h.click(p, lobby(s, "join"))
    answer = await h.click(ALICE, lobby(s, "start"))
    assert answer.text == "Поехали!", answer.text
    return s


def roles(s: GameSession) -> tuple[User, User]:
    chooser = USERS[s.chooser_id]
    other = next(USERS[uid] for uid in s.players if uid != chooser.id)
    return chooser, other


def message_with(h: Harness, needle: str, chat_id: int = GROUP) -> str:
    return next(t for t in reversed(h.texts(chat_id)) if needle in t)


async def test_join_via_private_chat(simple: Harness) -> None:
    h = simple
    h.tg.blocked.add(BOB.id)
    await h.say(ALICE, "/newgame")
    s = h.session()

    answer = await h.click(BOB, lobby(s, "join"))
    assert answer.url == f"https://t.me/{BOT.username}?start=j{s.gid}"
    assert BOB.id not in s.players

    h.tg.blocked.discard(BOB.id)  # игрок нажал «Старт» в личке
    await h.dm(BOB, f"/start j{s.gid}")
    assert BOB.id in s.players
    assert "Боб" in h.tg.messages[(GROUP, s.lobby_message_id)].text

    answer = await h.click(BOB, lobby(s, "start"))  # начинать может только создатель
    assert answer.show_alert and s.phase is Phase.LOBBY
    answer = await h.click(ALICE, lobby(s, "start"))
    assert answer.text == "Поехали!" and s.phase is Phase.PLAYING
    await h.say(ALICE, "/stop")
    await until(lambda: s.phase is Phase.FINISHED)


async def test_simple_question_flow(simple: Harness) -> None:
    h = simple
    s = await start_game(h, [ALICE, BOB])
    step = await waiting(s, "pick")
    chooser, other = roles(s)

    pick = h.button(f"P:{s.gid}:{step}:", "100").callback_data
    assert "выбирает" in (await h.click(other, pick)).text
    await h.click(chooser, pick)

    step = await waiting(s, "press")
    press = h.button(f"B:{s.gid}:{step}").callback_data
    assert "Вы отвечаете" in (await h.click(other, press)).text
    assert "Уже отвечает" in (await h.click(chooser, press)).text
    await waiting(s, f"answer:{other.id}")
    await h.say(chooser, "Париж")  # отвечает не тот, кто нажал, — игнорируется
    await h.say(other, "Лондон")

    step = await waiting(s, "press")
    press = h.button(f"B:{s.gid}:{step}").callback_data
    assert "уже отвечали" in (await h.click(other, press)).text
    await h.click(chooser, press)
    await waiting(s, f"answer:{chooser.id}")
    await h.say(chooser, "париж")

    step = await waiting(s, "pick")
    assert s.players[chooser.id].score == 100
    assert s.players[other.id].score == -100
    assert s.chooser_id == chooser.id
    reveal = message_with(h, "Ответ: <b>Париж</b>")  # был неверный ответ — ответ отдельно, с апелляцией
    assert "верно! +100" in reveal and h.buttons(f"A:{s.gid}:")
    assert "неверно. −100" in message_with(h, "Лондон")

    await h.click(chooser, h.button(f"P:{s.gid}:{step}:", "200").callback_data)  # никто не жмёт
    await until(lambda: s.phase is Phase.FINISHED)
    final = await until(lambda: next((t for t in h.texts() if "Игра окончена" in t), None))
    assert "Никто не нажал" in final and "Ответ: <b>4</b>" in final
    assert f"Победитель: <b>{chooser.first_name}</b>" in final
    assert h.registry.by_chat(GROUP) is None


async def test_skip_and_stop(simple: Harness) -> None:
    h = simple
    s = await start_game(h, [ALICE, BOB])
    step = await waiting(s, "pick")
    chooser, _ = roles(s)
    await h.click(chooser, h.button(f"P:{s.gid}:{step}:", "100").callback_data)
    await waiting(s, "press")

    await h.say(ALICE, "/skip")
    await waiting(s, "pick")
    assert "Вопрос пропущен" in message_with(h, "Выбирает")
    assert not h.buttons(f"B:{s.gid}")  # кнопка «Ответить» снята
    assert all(p.score == 0 for p in s.players.values())

    await h.say(BOB, "/stop")  # обычный игрок остановить не может
    assert s.phase is Phase.PLAYING
    await h.say(ALICE, "/stop")
    await until(lambda: s.phase is Phase.FINISHED)
    await until(lambda: any("Игра остановлена" in t for t in h.texts()))
    assert not h.buttons(f"P:{s.gid}")


async def test_special_questions_appeal_and_final(special: Harness) -> None:
    h = special
    s = await start_game(h, [ALICE, BOB, CAROL])
    step = await waiting(s, "pick")
    giver, receiver = roles(s)
    bystander = next(USERS[uid] for uid in s.players if uid not in (giver.id, receiver.id))

    # кот в мешке: отдаём сопернику, тот выбирает цену 500
    await h.click(giver, h.button(f"P:{s.gid}:{step}:", "100").callback_data)
    step = await waiting(s, "give")
    assert {b.callback_data.rsplit(":", 1)[1] for b in h.buttons(f"C:{s.gid}:{step}:give:")} == {
        str(receiver.id),
        str(bystander.id),
    }  # себе отдать нельзя
    give = h.button(f"C:{s.gid}:{step}:give:{receiver.id}").callback_data
    assert "отдаёт" in (await h.click(receiver, give)).text
    await h.click(giver, give)
    step = await waiting(s, "price")
    assert [b.text for b in h.buttons(f"C:{s.gid}:{step}:price:")] == ["100", "500"]
    await h.click(receiver, h.button(f"C:{s.gid}:{step}:price:", "500").callback_data)
    await waiting(s, f"answer:{receiver.id}")
    await h.say(receiver, "сфинкс")
    step = await waiting(s, "pick")
    assert s.players[receiver.id].score == 500 and s.chooser_id == receiver.id

    # вопрос со ставкой: ставка числом в чат, у соперников 0 очков — они пасуют автоматически
    await h.click(receiver, h.button(f"P:{s.gid}:{step}:", "200").callback_data)
    await waiting(s, f"bid:{receiver.id}")
    await h.say(receiver, "300")
    await waiting(s, f"answer:{receiver.id}")
    assert "ставка <b>300</b>" in message_with(h, "Отвечает")
    await h.say(receiver, "Нил")
    step = await waiting(s, "pick")
    assert s.players[receiver.id].score == 800

    # без риска: неверный ответ без штрафа, затем апелляция — «за» 1, «против» 0
    await h.click(receiver, h.button(f"P:{s.gid}:{step}:", "300").callback_data)
    await waiting(s, f"answer:{receiver.id}")
    await h.say(receiver, "Пушкин")
    appeal = await until(lambda: h.buttons(f"A:{s.gid}:"))
    assert s.players[receiver.id].score == 800
    assert "Апелляцию подаёт" in (await h.click(giver, appeal[0].callback_data)).text
    await h.click(receiver, appeal[0].callback_data)
    vote = await until(lambda: h.buttons(f"V:{s.gid}:"))
    assert "Голосуют остальные" in (await h.click(receiver, vote[0].callback_data)).text
    await h.click(giver, vote[0].callback_data)  # 👍
    await until(lambda: s.players[receiver.id].score == 800 + 600)
    assert "принята" in message_with(h, "Апелляция")

    # финал: игроки с нулём не проходят, финалист убирает тему, ставит и отвечает в личке
    step = await waiting(s, f"rm:{receiver.id}")
    await h.click(receiver, h.buttons(f"C:{s.gid}:{step}:rm:")[0].callback_data)
    step = await waiting(s, "bets")
    assert h.buttons(f"C:{s.gid}:{step}:bet:", chat_id=receiver.id)
    await h.dm(receiver, "5000")  # больше счёта — бот подскажет диапазон
    assert "от 1 до 1400" in h.texts(receiver.id)[-1]
    await h.dm(receiver, "1000")
    await waiting(s, "answers")
    await h.dm(receiver, s.question.right[0])
    await until(lambda: s.phase is Phase.FINISHED)
    assert s.players[receiver.id].score == 1400 + 1000
    assert s.players[giver.id].score == 0
    final = await until(lambda: next((t for t in h.texts() if "Игра окончена" in t), None))
    assert "Итоги финала" in final and f"Победитель: <b>{receiver.first_name}</b>" in final
    assert "Не прошли (нужен плюсовой счёт)" in message_with(h, "Не прошли")


async def test_human_host(simple: Harness) -> None:
    h = simple
    s = await start_game(h, [ALICE, BOB], host=CAROL)
    assert s.host is not None and s.host.id == CAROL.id
    step = await waiting(s, "pick")
    chooser, other = roles(s)
    await h.click(chooser, h.button(f"P:{s.gid}:{step}:", "100").callback_data)
    await until(lambda: any("Для ведущего" in t and "Париж" in t for t in h.texts(CAROL.id)))

    step = await waiting(s, "press")
    await h.click(other, h.button(f"B:{s.gid}:{step}").callback_data)
    step = await waiting(s, f"judge:{other.id}")
    await h.say(other, "Париж?")
    await until(lambda: any("«Париж?»" in t for t in h.texts(CAROL.id)))
    verdict = h.button(f"J:{s.gid}:{step}:{other.id}:1", chat_id=CAROL.id).callback_data
    assert (await h.click(chooser, verdict, chat_id=chooser.id)).text == "Оценивает только ведущий"
    assert s.players[other.id].score == 0
    await h.click(CAROL, verdict, chat_id=CAROL.id)
    step = await waiting(s, "pick")
    assert s.players[other.id].score == 100 and s.chooser_id == other.id

    await h.click(other, h.button(f"P:{s.gid}:{step}:", "200").callback_data)
    step = await waiting(s, "press")
    await h.click(chooser, h.button(f"B:{s.gid}:{step}").callback_data)
    step = await waiting(s, f"judge:{chooser.id}")
    await h.click(CAROL, h.button(f"J:{s.gid}:{step}:{chooser.id}:0", chat_id=CAROL.id).callback_data, chat_id=CAROL.id)
    await until(lambda: s.phase is Phase.FINISHED)
    assert s.players[chooser.id].score == -200
    assert not h.buttons(f"A:{s.gid}")  # с живым ведущим апелляций нет


async def pick_first_question(h: Harness, s: GameSession) -> int:
    step = await waiting(s, "pick")
    await h.click(USERS[s.chooser_id], h.button(f"P:{s.gid}:{step}:", "100").callback_data)
    return await waiting(s, "press")


async def test_everyone_passes(simple: Harness) -> None:
    h = simple
    s = await start_game(h, [ALICE, BOB, CAROL])
    step = await pick_first_question(h, s)
    chooser_id = s.chooser_id
    assert h.button(f"S:{s.gid}:{step}").text == "🙅 Пас"
    pass_data = h.button(f"S:{s.gid}:{step}").callback_data

    assert "Вы пасуете" in (await h.click(ALICE, pass_data)).text
    assert "уже спасовали" in (await h.click(ALICE, pass_data)).text
    await until(lambda: h.button(f"S:{s.gid}:{step}").text == "🙅 Пас · 1/3")  # счётчик на кнопке
    await h.click(BOB, pass_data)
    await h.click(CAROL, pass_data)

    await waiting(s, "pick")  # вопрос закончился сразу, не дожидаясь таймера
    board = message_with(h, "Выбирает")
    assert "Все спасовали — вопрос пропущен" in board and "Ответ: <b>Париж</b>" in board
    assert all(p.score == 0 for p in s.players.values()) and s.chooser_id == chooser_id
    await h.say(ALICE, "/stop")
    await until(lambda: s.phase is Phase.FINISHED)


async def test_pass_can_be_revoked(simple: Harness) -> None:
    h = simple
    s = await start_game(h, [ALICE, BOB, CAROL])
    step = await pick_first_question(h, s)
    await h.click(BOB, h.button(f"S:{s.gid}:{step}").callback_data)
    await h.click(CAROL, h.button(f"S:{s.gid}:{step}").callback_data)
    assert "Вы отвечаете" in (await h.click(CAROL, h.button(f"B:{s.gid}:{step}").callback_data)).text  # передумала
    assert "Уже отвечает" in (await h.click(ALICE, h.button(f"S:{s.gid}:{step}").callback_data)).text
    await waiting(s, f"answer:{CAROL.id}")
    await h.say(CAROL, "Лондон")

    step = await waiting(s, "press")  # Алиса не пасовала — кнопка открывается снова, пас Боба в силе
    assert h.button(f"S:{s.gid}:{step}").text == "🙅 Пас · 1/2"
    await h.click(ALICE, h.button(f"S:{s.gid}:{step}").callback_data)
    await waiting(s, "pick")
    assert "Остальные спасовали" in message_with(h, "Ответ: <b>Париж</b>")
    assert [s.players[u.id].score for u in (ALICE, BOB, CAROL)] == [0, 0, -100]
    await h.say(ALICE, "/stop")
    await until(lambda: s.phase is Phase.FINISHED)


async def test_passes_end_question_after_wrong_answer(simple: Harness) -> None:
    h = simple
    s = await start_game(h, [ALICE, BOB, CAROL])
    step = await pick_first_question(h, s)
    await h.click(BOB, h.button(f"S:{s.gid}:{step}").callback_data)
    await h.click(CAROL, h.button(f"S:{s.gid}:{step}").callback_data)
    await h.click(ALICE, h.button(f"B:{s.gid}:{step}").callback_data)
    await waiting(s, f"answer:{ALICE.id}")
    await h.say(ALICE, "Лондон")

    await waiting(s, "pick")  # остальные уже спасовали — кнопку заново не открываем
    reveal = message_with(h, "Ответ: <b>Париж</b>")
    assert "«Лондон» — неверно. −100" in reveal and "Остальные спасовали" in reveal
    assert not any("Кто-нибудь ещё" in t for t in h.texts())
    await h.say(ALICE, "/stop")
    await until(lambda: s.phase is Phase.FINISHED)


# ------------------------------------------------- случайные полные партии


def answer_for(q: Question | None, rng: random.Random) -> str:
    if q is None or rng.random() < 0.35:
        return "не знаю"
    return q.right[0] if q.right else "?"


async def autoplay(h: Harness, s: GameSession, rng: random.Random, host: User | None = None) -> None:
    """Играет за всех: случайно выбирает, жмёт, ошибается, торгуется, апеллирует."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + 120
    gid = s.gid
    acted: set[int] = set()
    while s.phase is not Phase.FINISHED:
        assert loop.time() < deadline, f"игра зависла на шаге {s.waiting_for}"
        label, waiter = s.waiting_for, s._waiter
        if label is None or waiter is None or waiter.step in acted:
            await asyncio.sleep(0.003)
            continue
        step = waiter.step
        acted.add(step)
        players = [USERS[uid] for uid in list(s.players)]
        if label == "pick":
            if buttons := h.buttons(f"P:{gid}:{step}:"):
                await h.click(USERS[s.chooser_id], rng.choice(buttons).callback_data)
        elif label == "press":
            press, passes, roll = h.buttons(f"B:{gid}:{step}"), h.buttons(f"S:{gid}:{step}"), rng.random()
            if roll < 0.15 and passes:  # пасуют все
                for p in players:
                    await h.click(p, passes[0].callback_data)
            elif roll < 0.9 and press and passes:  # кто-то пасует, кто-то жмёт (возможно, передумав)
                for p in rng.sample(players, k=rng.randint(0, len(players) - 1)):
                    await h.click(p, passes[0].callback_data)
                await h.click(rng.choice(players), press[0].callback_data)
        elif label.startswith(("answer:", "judge:")):
            uid = int(label.split(":")[1])
            if rng.random() < 0.9:
                if buttons := h.buttons(f"C:{gid}:{step}:opt:"):
                    await h.click(USERS[uid], rng.choice(buttons).callback_data)
                else:
                    await h.say(USERS[uid], answer_for(s.question, rng))
            judging = host is not None and label.startswith("judge:") and rng.random() < 0.9
            if judging and (buttons := h.buttons(f"J:{gid}:{step}:{uid}:", chat_id=host.id)):
                await h.click(host, rng.choice(buttons).callback_data, chat_id=host.id)
        elif label in ("give", "price") or label.startswith(("bid:", "rm:")):
            kind = label.split(":")[0]
            if buttons := h.buttons(f"C:{gid}:{step}:{kind}:"):
                data = rng.choice(buttons).callback_data
                for p in players:  # кто именно должен ходить — решит игра
                    await h.click(p, data)
        elif label == "bets":
            for p in players:
                if buttons := h.buttons(f"C:{gid}:{step}:bet:", chat_id=p.id):
                    await h.click(p, rng.choice(buttons).callback_data, chat_id=p.id)
        elif label == "answers":
            for p in players:
                if buttons := h.buttons(f"C:{gid}:{step}:opt:", chat_id=p.id):
                    await h.click(p, rng.choice(buttons).callback_data, chat_id=p.id)
                else:
                    await h.dm(p, answer_for(s.question, rng))
        elif label == "judge_all" and host is not None:
            for button in h.buttons(f"J:{gid}:{step}:", chat_id=host.id):
                await h.click(host, button.callback_data, chat_id=host.id)
        for button in h.buttons(f"A:{gid}:")[:1]:
            if rng.random() < 0.3:
                await h.click(rng.choice(players), button.callback_data)
        for button in h.buttons(f"V:{gid}:"):
            if rng.random() < 0.5:
                await h.click(rng.choice(players), button.callback_data)


@pytest.mark.parametrize("seed", [1, 2, 3])
async def test_random_full_game(tmp_path, caplog: pytest.LogCaptureFixture, seed: int) -> None:
    caplog.set_level(logging.WARNING)
    h = Harness(tmp_path, time_scale=0.01)
    s = await start_game(h, [ALICE, BOB, CAROL])
    await autoplay(h, s, random.Random(seed))
    final = await until(lambda: next((t for t in h.texts() if "Игра окончена" in t), None))
    assert all(p.name in final for p in s.players.values())
    assert s.question_no == 50  # все вопросы двух раундов сыграны
    problems = [r for r in caplog.records if r.levelno >= logging.WARNING]
    assert not problems, [r.getMessage() for r in problems]


async def test_random_full_game_with_host(tmp_path, caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.WARNING)
    h = Harness(tmp_path, time_scale=0.01)
    s = await start_game(h, [ALICE, BOB], host=CAROL)
    await autoplay(h, s, random.Random(7), host=CAROL)
    await until(lambda: any("Игра окончена" in t for t in h.texts()))
    problems = [r for r in caplog.records if r.levelno >= logging.WARNING]
    assert not problems, [r.getMessage() for r in problems]


async def test_abandoned_lobby_expires(simple: Harness, monkeypatch: pytest.MonkeyPatch) -> None:
    from sibot.game import session as session_module

    monkeypatch.setattr(session_module, "LOBBY_TTL", 0.05)
    h = simple
    await h.say(ALICE, "/newgame")
    s = h.session()
    await until(lambda: s.phase is Phase.FINISHED)
    assert h.registry.by_chat(GROUP) is None
    await until(lambda: "Набор закрыт" in h.tg.messages[(GROUP, s.lobby_message_id)].text)
    assert h.tg.messages[(GROUP, s.lobby_message_id)].kb is None
