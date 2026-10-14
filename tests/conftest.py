from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests.fakes import Harness

SIMPLE_XML = """<?xml version="1.0" encoding="utf-8"?>
<package name="Мини" version="4" xmlns="http://vladimirkhil.com/ygpackage3.0.xsd">
  <rounds>
    <round name="Раунд">
      <themes>
        <theme name="Тест">
          <questions>
            <question price="100"><scenario><atom>Столица Франции?</atom></scenario>
              <right><answer>Париж</answer></right></question>
            <question price="200"><scenario><atom>Сколько будет 2+2?</atom></scenario>
              <right><answer>4</answer></right></question>
          </questions>
        </theme>
      </themes>
    </round>
  </rounds>
</package>
"""

SPECIAL_XML = """<?xml version="1.0" encoding="utf-8"?>
<package name="Особые" version="4" xmlns="http://vladimirkhil.com/ygpackage3.0.xsd">
  <rounds>
    <round name="Раунд">
      <themes>
        <theme name="Особое">
          <questions>
            <question price="100">
              <type name="bagcat"><param name="theme">Кошки</param><param name="cost">[100;500]</param>
                <param name="self">false</param><param name="knows">after</param></type>
              <scenario><atom>Порода кошек без шерсти?</atom></scenario>
              <right><answer>Сфинкс</answer></right></question>
            <question price="200"><type name="auction" />
              <scenario><atom>Самая длинная река Африки?</atom></scenario>
              <right><answer>Нил</answer></right></question>
            <question price="300"><type name="sponsored" />
              <scenario><atom>Автор «Мастера и Маргариты»?</atom></scenario>
              <right><answer>Булгаков</answer></right></question>
          </questions>
        </theme>
      </themes>
    </round>
    <round name="Финал" type="final">
      <themes>
        <theme name="Химия"><questions><question price="0"><scenario><atom>Формула воды?</atom></scenario>
          <right><answer>H2O</answer></right></question></questions></theme>
        <theme name="Физика"><questions><question price="0"><scenario><atom>Единица силы?</atom></scenario>
          <right><answer>Ньютон</answer></right></question></questions></theme>
      </themes>
    </round>
  </rounds>
</package>
"""


def _package(root: Path, name: str, xml: str) -> Path:
    folder = root / name
    folder.mkdir(parents=True)
    (folder / "content.xml").write_text(xml, encoding="utf-8")
    return root


@pytest.fixture
def simple(tmp_path: Path) -> Harness:
    return Harness(tmp_path, packages_dir=_package(tmp_path / "packs", "simple", SIMPLE_XML))


@pytest.fixture
def special(tmp_path: Path) -> Harness:
    return Harness(tmp_path, packages_dir=_package(tmp_path / "packs", "special", SPECIAL_XML))


@pytest.fixture
def demo(tmp_path: Path) -> Harness:
    return Harness(tmp_path)
