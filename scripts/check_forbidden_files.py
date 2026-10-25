"""Не пускает в Git базы, таблицы, бэкапы, ключи и персональные данные."""

from __future__ import annotations

import sys
from pathlib import PurePosixPath

ALLOWED_PATHS = {
    ".env.example",
}

FORBIDDEN_DIRS = ("_samples/", "instance/", "uploads/", "backups/")
FORBIDDEN_SUFFIXES = (
    ".db",
    ".dump",
    ".sqlite",
    ".sqlite3",
    ".xlsx",
    ".xls",
    ".csv",
    ".key",
    ".pem",
    ".pfx",
)


def normalize(raw_path: str) -> str:
    return PurePosixPath(raw_path.replace("\\", "/")).as_posix().lower()


def is_forbidden(raw_path: str) -> bool:
    path = normalize(raw_path)
    name = PurePosixPath(path).name
    if path in ALLOWED_PATHS:
        return False
    if name == ".env" or name.startswith(".env."):
        return True
    if any(path.startswith(prefix) or f"/{prefix}" in path for prefix in FORBIDDEN_DIRS):
        return True
    if name in {"claude.md", "agents.md"} or path.startswith(".claude/"):
        return True
    return name.endswith(FORBIDDEN_SUFFIXES) or ".sqlite" in name or "-wal" in name[-4:]


def main(paths: list[str]) -> int:
    forbidden = sorted({path for path in paths if is_forbidden(path)})
    if not forbidden:
        return 0
    print("Запрещённые к коммиту файлы (данные, секреты, локальные настройки):", file=sys.stderr)
    for path in forbidden:
        print(f"- {path}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
