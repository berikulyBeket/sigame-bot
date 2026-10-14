from __future__ import annotations

import zipfile
from pathlib import Path

import pytest

from sibot.siq import Kind, NumberSet, PackageError, QType, load_package, parse_content, parse_number_set
from tests.conftest import SPECIAL_XML

V5_XML = """<?xml version="1.0" encoding="utf-8"?>
<package name="Пять" version="5" logo="@logo.png" xmlns="https://github.com/VladimirKhil/SI/blob/master/assets/siq_5.xsd">
  <info><authors><author>Автор</author></authors></info>
  <rounds>
    <round name="Р1">
      <themes>
        <theme name="Т1">
          <questions>
            <question price="100">
              <params>
                <param name="question" type="content">
                  <item placement="replic">Слушаем:</item>
                  <item type="audio" isRef="True" placement="background">звук 1.mp3</item>
                </param>
                <param name="answer" type="content"><item type="image" isRef="True">ответ.png</item></param>
              </params>
              <right><answer>Ответ</answer></right>
            </question>
            <question price="-1"><params /><right /></question>
            <question price="200" type="secretPublicPrice">
              <params>
                <param name="theme">Секрет</param>
                <param name="price" type="numberSet"><numberSet minimum="0" maximum="0" step="0" /></param>
                <param name="selectionMode">any</param>
                <param name="question" type="content"><item>Вопрос кота</item></param>
              </params>
              <right><answer>Кот</answer></right>
            </question>
            <question price="300">
              <params>
                <param name="question" type="content"><item>Сколько?</item></param>
                <param name="answerType">number</param>
                <param name="answerDeviation">5</param>
              </params>
              <right><answer>100</answer></right>
            </question>
            <question price="400" type="forAll">
              <params>
                <param name="question" type="content"><item>Выберите</item></param>
                <param name="answerType">select</param>
                <param name="answerOptions" type="group">
                  <param name="A" type="content"><item>Да</item></param>
                  <param name="B" type="content"><item type="image" isRef="True">b.png</item></param>
                </param>
              </params>
              <right><answer>A</answer></right>
            </question>
          </questions>
        </theme>
      </themes>
    </round>
  </rounds>
</package>
"""


def test_v4_types_are_upgraded_like_sigame() -> None:
    pkg = parse_content(SPECIAL_XML.encode())
    cat, auction, sponsored = pkg.rounds[0].themes[0].questions
    assert cat.type is QType.SECRET and cat.secret_theme == "Кошки"
    assert cat.secret_price == NumberSet(100, 500, 400) and cat.selection == "exceptCurrent"
    assert auction.type is QType.STAKE and sponsored.type is QType.NO_RISK
    final = pkg.rounds[1]
    assert final.final and [t.name for t in final.themes] == ["Химия", "Физика"]


def test_v4_marker_moves_content_to_answer() -> None:
    xml = """<package name="x" version="4"><rounds><round name="r"><themes><theme name="t"><questions>
      <question price="100"><scenario>
        <atom>Текст</atom><atom type="say">Реплика</atom><atom type="voice">@a.mp3</atom>
        <atom type="marker"/><atom type="image">@img.png</atom><atom type="video">https://e.com/v.mp4</atom>
      </scenario><right><answer>О</answer></right></question>
    </questions></theme></themes></round></rounds></package>"""
    q = parse_content(xml.encode()).rounds[0].themes[0].questions[0]
    assert [(c.kind, c.value, c.ref, c.replic) for c in q.content] == [
        (Kind.TEXT, "Текст", False, False),
        (Kind.TEXT, "Реплика", False, True),
        (Kind.AUDIO, "a.mp3", True, False),
    ]
    assert [(c.kind, c.value, c.ref) for c in q.answer_content] == [
        (Kind.IMAGE, "img.png", True),
        (Kind.VIDEO, "https://e.com/v.mp4", False),
    ]


def test_v5_package() -> None:
    pkg = parse_content(V5_XML.encode())
    assert pkg.logo == "logo.png" and pkg.authors == ["Автор"]
    questions = pkg.rounds[0].themes[0].questions
    assert [q.price for q in questions] == [100, 200, 300, 400]  # пустая ячейка -1 пропущена
    media, secret, number, select = questions
    assert media.content[0].replic and media.content[1].kind is Kind.AUDIO and media.content[1].ref
    assert media.answer_content[0].value == "ответ.png"
    assert secret.type is QType.SECRET_PUBLIC and secret.selection == "any"
    assert secret.secret_price.options([100, 200, 500]) == [100, 500]  # 0/0 — минимум или максимум раунда
    assert number.answer_type == "number" and number.deviation == 5
    assert select.type is QType.FOR_ALL and select.options == [("A", "Да"), ("B", "[изображение]")]


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("500", [500]),
        ("[100;500]", [100, 500]),
        ("[100;1000]/200", [100, 300, 500, 700, 900]),
    ],
)
def test_number_sets(raw: str, expected: list[int]) -> None:
    assert parse_number_set(raw).options([100, 200]) == expected


def test_number_set_is_thinned_out() -> None:
    options = NumberSet(100, 5000, 100).options([])
    assert len(options) == 12 and options[0] == 100 and options[-1] == 5000


def test_siq_archive_with_encoded_names(tmp_path: Path) -> None:
    path = tmp_path / "p.siq"
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("content.xml", V5_XML)
        zf.writestr("Audio/%D0%B7%D0%B2%D1%83%D0%BA%201.mp3", b"ID3audio")  # имена в .siq часто URL-кодированы
        zf.writestr("Images/logo.png", b"PNG")
    pkg = load_package(path)
    assert pkg.media.read(Kind.AUDIO, "звук 1.mp3") == b"ID3audio"
    assert pkg.media.read(Kind.IMAGE, "LOGO.png") == b"PNG"  # регистр не важен
    assert pkg.media.read(Kind.IMAGE, "нет.png") is None


def test_directory_package(tmp_path: Path) -> None:
    (tmp_path / "Images").mkdir()
    (tmp_path / "Images" / "logo.png").write_bytes(b"PNG")
    (tmp_path / "content.xml").write_text(V5_XML, encoding="utf-8")
    pkg = load_package(tmp_path)
    assert pkg.media.read(Kind.IMAGE, "logo.png") == b"PNG"
    assert pkg.media.read(Kind.IMAGE, "../content.xml") is None


def test_demo_package_is_valid() -> None:
    pkg = load_package(Path(__file__).resolve().parent.parent / "packages" / "demo")
    assert pkg.question_count == 51 and pkg.has_final
    assert pkg.media.read(Kind.IMAGE, "japan.png").startswith(b"\x89PNG")


@pytest.mark.parametrize(
    "content",
    [
        b"not xml",
        b"<root/>",
        b'<?xml version="1.0"?><!DOCTYPE p [<!ENTITY a "aaaa">]><package name="x"><rounds/></package>',
        b'<package name="x" version="4"><rounds><round name="r"/></rounds></package>',
    ],
)
def test_bad_packages(content: bytes) -> None:
    with pytest.raises(PackageError):
        parse_content(content)


def test_not_a_zip(tmp_path: Path) -> None:
    path = tmp_path / "x.siq"
    path.write_bytes(b"hello")
    with pytest.raises(PackageError):
        load_package(path)
