"""Настройки из переменных окружения (и файла .env в корне проекта)."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def load_dotenv(path: Path) -> None:
    """Минимальный загрузчик .env: KEY=value, комментарии через #."""
    if not path.is_file():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip().strip("'\""))


def _path(name: str, default: Path) -> Path:
    value = os.environ.get(name, "").strip()
    return (ROOT / value if not Path(value).is_absolute() else Path(value)) if value else default


@dataclass(frozen=True, slots=True)
class Config:
    token: str = ""
    packages_dir: Path = ROOT / "packages"
    data_dir: Path = ROOT / "data"
    max_players: int = 12
    max_upload_mb: int = 20
    time_scale: float = 1.0
    api_url: str | None = None  # свой Bot API сервер (снимает лимит 20 МБ на скачивание)
    api_local: bool = False

    @classmethod
    def from_env(cls) -> Config:
        load_dotenv(ROOT / ".env")
        token = os.environ.get("BOT_TOKEN", "").strip()
        if not token:
            raise SystemExit("Не задан BOT_TOKEN. Скопируйте .env.example в .env и впишите токен от @BotFather.")
        api_url = os.environ.get("BOT_API_URL", "").strip() or None
        return cls(
            token=token,
            packages_dir=_path("PACKAGES_DIR", ROOT / "packages"),
            data_dir=_path("DATA_DIR", ROOT / "data"),
            max_players=int(os.environ.get("MAX_PLAYERS", "12")),
            max_upload_mb=int(os.environ.get("MAX_UPLOAD_MB", "2000" if api_url else "20")),
            time_scale=float(os.environ.get("TIME_SCALE", "1")),
            api_url=api_url,
            api_local=os.environ.get("BOT_API_LOCAL", "").strip().lower() in ("1", "true", "yes"),
        )
