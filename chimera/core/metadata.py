"""Публичная идентичность приложения и версия (единый источник для UI и README)."""

from __future__ import annotations

from functools import cache
from importlib import metadata as importlib_metadata
from pathlib import Path
from types import MappingProxyType

APP_METADATA = MappingProxyType(
    {
        "name": "Chimera",
        "developer_name": "UnicornisIT",
        "developer_email": "dev-eva@outlook.com",
        "github_url": "https://github.com/UnicornisIT",
        "repository_url": "https://github.com/UnicornisIT/chimera",
        "telegram_url": "https://t.me/unicornis_pulse",
    }
)

_VERSION_FILE = Path(__file__).resolve().parents[2] / "VERSION"


@cache
def app_version() -> str:
    if _VERSION_FILE.is_file():
        return _VERSION_FILE.read_text(encoding="utf-8").strip()
    try:
        return importlib_metadata.version("unicornisit-chimera")
    except importlib_metadata.PackageNotFoundError:  # pragma: no cover
        return "0.0.0"
