"""Шифрование отдельных полей и «слепой индекс» для поиска по точному значению.

Ключ задаётся переменной `DATA_ENCRYPTION_KEY` (ключ Fernet). Для ротации можно
указать несколько ключей через запятую: первым шифруются новые значения,
остальные используются только для расшифровки старых.

Слепой индекс — HMAC-SHA256 от нормализованного значения. Ключ индекса берётся из
`BLIND_INDEX_KEY` или выводится из первого ключа шифрования, поэтому при ротации
ключа шифрования без явного `BLIND_INDEX_KEY` индексы придётся пересчитать.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import re
import unicodedata
from collections.abc import Callable
from typing import Any

from cryptography.fernet import Fernet, InvalidToken, MultiFernet
from sqlalchemy import String, Text
from sqlalchemy.engine import Dialect
from sqlalchemy.types import TypeDecorator

BLIND_INDEX_LENGTH = 64  # hex SHA-256
_BLIND_INDEX_LABEL = b"chimera-blind-index-v1"


class CryptoError(RuntimeError):
    """Ключ не настроен или значение не удаётся расшифровать."""


class FieldCipher:
    def __init__(self, keys: str, blind_index_key: str | None = None) -> None:
        parts = [part.strip() for part in keys.split(",") if part.strip()]
        if not parts:
            raise CryptoError("DATA_ENCRYPTION_KEY не задан")
        try:
            fernets = [Fernet(part.encode("ascii")) for part in parts]
        except (ValueError, TypeError) as exc:
            raise CryptoError("DATA_ENCRYPTION_KEY не является ключом Fernet") from exc
        self._fernet = MultiFernet(fernets)
        if blind_index_key:
            index_secret = blind_index_key.encode("utf-8")
        else:
            raw = base64.urlsafe_b64decode(parts[0].encode("ascii"))
            index_secret = hmac.new(raw, _BLIND_INDEX_LABEL, hashlib.sha256).digest()
        self._index_secret = index_secret

    def encrypt(self, value: str) -> str:
        return self._fernet.encrypt(value.encode("utf-8")).decode("ascii")

    def decrypt(self, token: str) -> str:
        try:
            return self._fernet.decrypt(token.encode("ascii")).decode("utf-8")
        except InvalidToken as exc:
            raise CryptoError("Не удалось расшифровать значение: неверный ключ") from exc

    def rotate(self, token: str) -> str:
        """Перешифровывает значение первым (текущим) ключом."""
        return self._fernet.rotate(token.encode("ascii")).decode("ascii")

    def blind_index(self, value: str, normalizer: Callable[[str], str] | None = None) -> str:
        normalized = (normalizer or normalize_default)(value)
        return hmac.new(self._index_secret, normalized.encode("utf-8"), hashlib.sha256).hexdigest()


def normalize_default(value: str) -> str:
    return unicodedata.normalize("NFKC", value).strip().casefold()


def normalize_digits(value: str) -> str:
    """Для СНИЛС, серии и номера паспорта: оставляет только цифры."""
    return re.sub(r"\D", "", value)


_cipher: FieldCipher | None = None


def configure(keys: str, blind_index_key: str | None = None) -> FieldCipher:
    global _cipher
    _cipher = FieldCipher(keys, blind_index_key)
    return _cipher


def get_cipher() -> FieldCipher:
    if _cipher is None:
        raise CryptoError("Шифрование не инициализировано: вызовите crypto.configure()")
    return _cipher


def blind_index(value: str | None, normalizer: Callable[[str], str] | None = None) -> str | None:
    if value is None or not value.strip():
        return None
    return get_cipher().blind_index(value, normalizer)


class EncryptedString(TypeDecorator[str]):
    """Строка, прозрачно шифруемая при записи в БД.

    В аудите изменения таких полей фиксируются без значений.
    """

    impl = Text
    cache_ok = True
    is_encrypted = True

    def process_bind_param(self, value: str | None, dialect: Dialect) -> str | None:
        if value is None:
            return None
        return get_cipher().encrypt(value)

    def process_result_value(self, value: Any, dialect: Dialect) -> str | None:
        if value is None:
            return None
        return get_cipher().decrypt(str(value))


def BlindIndex() -> String:
    return String(BLIND_INDEX_LENGTH)
