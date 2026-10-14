"""Медиа из пакетов, загрузка своего .siq и ведущий, которого сняли посреди оценки."""

from __future__ import annotations

import asyncio
import io
import zipfile
from pathlib import Path

from aiogram.methods import SendAudio, SendPhoto

from sibot.game.session import Phase
from sibot.siq import Content, Kind
from sibot.tg.ui import TelegramUI
from tests.fakes import GROUP, Harness, until, waiting
from tests.test_game import ALICE, BOB, CAROL, lobby, roles, start_game

ROOT = Path(__file__).resolve().parent.parent
PNG = (ROOT / "packages" / "demo" / "Images" / "japan.png").read_bytes()

MEDIA_XML = """<?xml version="1.0" encoding="utf-8"?>
<package name="Медиа" version="4" xmlns="http://vladimirkhil.com/ygpackage3.0.xsd">
  <rounds><round name="Раунд"><themes><theme name="Картинки"><questions>
    <question price="100"><scenario>
      <atom>Чей флаг?</atom><atom type="image">@флаг японии.png</atom>
      <atom type="marker"/><atom type="voice">@гимн.mp3</atom>
    </scenario><right><answer>Япония</answer></right></question>
  </questions></theme></themes></round></rounds>
</package>
"""


def media_siq() -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("content.xml", MEDIA_XML)
        zf.writestr("Images/%D1%84%D0%BB%D0%B0%D0%B3%20%D1%8F%D0%BF%D0%BE%D0%BD%D0%B8%D0%B8.png", PNG)
        zf.writestr("Audio/%D0%B3%D0%B8%D0%BC%D0%BD.mp3", b"ID3fake-mp3")
    return buf.getvalue()


async def test_upload_package_and_play_media(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    await h.say(ALICE, "/newgame")
    s = h.session()
    await h.send_document(BOB, "media.siq", media_siq())  # не создатель — отказ
    assert "создатель" in h.texts()[-1] and s.entry.title == "Демо-пакет"

    await h.send_document(ALICE, "media.siq", media_siq())
    assert "✅ Пакет «Медиа» загружен" in h.texts()[-1]
    assert s.entry.title == "Медиа" and "Медиа" in h.tg.messages[(GROUP, s.lobby_message_id)].text
    await h.send_document(ALICE, "broken.siq", b"not a zip")
    assert "Это не пакет SIGame" in h.texts()[-1] and s.entry.title == "Медиа"

    for player in (ALICE, BOB):
        await h.click(player, lobby(s, "join"))
    await h.click(ALICE, lobby(s, "start"))
    step = await waiting(s, "pick")
    chooser, _ = roles(s)
    await h.click(chooser, h.button(f"P:{s.gid}:{step}:", "100").callback_data)

    step = await waiting(s, "press")
    photo = next(m for m in h.tg.order if m.kind == "photo")
    assert "Чей флаг?" in photo.text  # картинка с подписью-вопросом
    assert photo.kb and photo.kb.inline_keyboard[0][0].callback_data == f"B:{s.gid}:{step}"
    await h.click(chooser, photo.kb.inline_keyboard[0][0].callback_data)
    await waiting(s, "answer")
    await h.say(chooser, "Япония")
    await until(lambda: s.phase is Phase.FINISHED)
    audio = next(m for m in h.tg.order if m.kind == "audio")  # медиа после маркера — вместе с ответом
    assert "Ответ: <b>Япония</b>" in audio.text


async def test_media_is_uploaded_once(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    entry = h.library.default()
    ui = TelegramUI(h.bot, GROUP, None, h.library)
    item = Content(Kind.IMAGE, "japan.png", ref=True)
    first, _ = await ui.send_content("раз", [item], entry)
    second, _ = await ui.send_content("два", [item], entry)
    photos = [r for r in h.tg.requests if isinstance(r, SendPhoto)]
    assert first and second and len(photos) == 2
    assert not isinstance(photos[0].photo, str)  # первый раз — загрузка файла
    assert photos[1].photo == f"file{first}"  # дальше — file_id из кэша

    missing, _ = await ui.send_content("три", [Content(Kind.AUDIO, "нет.mp3", ref=True)], entry)
    assert "медиафайл недоступен" in h.tg.messages[(GROUP, missing)].text
    assert not [r for r in h.tg.requests if isinstance(r, SendAudio)]


async def test_kicking_host_during_judging(simple: Harness) -> None:
    h = simple
    s = await start_game(h, [ALICE, BOB], host=CAROL)
    step = await waiting(s, "pick")
    chooser, other = roles(s)
    await h.click(chooser, h.button(f"P:{s.gid}:{step}:", "100").callback_data)
    step = await waiting(s, "press")
    await h.click(other, h.button(f"B:{s.gid}:{step}").callback_data)
    await waiting(s, f"judge:{other.id}")
    await h.say(other, "Париж")

    loop = asyncio.get_running_loop()
    started = loop.time()
    await s.kick(CAROL.id)
    await waiting(s, "pick")
    assert loop.time() - started < s.t.host_extra  # не ждали решения ушедшего ведущего
    assert s.host is None and s.players[other.id].score == 100  # проверил бот
    assert "больше не ведущий" in "\n".join(h.texts())
    await h.say(ALICE, "/stop")
    await until(lambda: s.phase is Phase.FINISHED)
