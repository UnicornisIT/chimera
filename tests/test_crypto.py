"""Шифрование полей и слепой индекс."""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from cryptography.fernet import Fernet
from sqlalchemy import Column, Integer, MetaData, String, Table, select, text
from sqlalchemy.orm import Session

from chimera.core import crypto
from chimera.core.crypto import (
    CryptoError,
    EncryptedString,
    FieldCipher,
    normalize_digits,
)
from chimera.core.db import Database

KEY = Fernet.generate_key().decode()


def test_roundtrip_and_randomised_ciphertext() -> None:
    cipher = FieldCipher(KEY)
    first, second = cipher.encrypt("123-456-789 01"), cipher.encrypt("123-456-789 01")
    assert first != second
    assert cipher.decrypt(first) == "123-456-789 01"


def test_wrong_key_is_rejected() -> None:
    token = FieldCipher(KEY).encrypt("секрет")
    with pytest.raises(CryptoError):
        FieldCipher(Fernet.generate_key().decode()).decrypt(token)


def test_invalid_key_format() -> None:
    with pytest.raises(CryptoError):
        FieldCipher("not-a-key")
    with pytest.raises(CryptoError):
        FieldCipher("")


def test_key_rotation_keeps_old_values_readable() -> None:
    old = FieldCipher(KEY)
    token = old.encrypt("паспорт 4510 123456")
    new_key = Fernet.generate_key().decode()
    rotated = FieldCipher(f"{new_key},{KEY}")
    assert rotated.decrypt(token) == "паспорт 4510 123456"
    fresh = rotated.rotate(token)
    assert FieldCipher(new_key).decrypt(fresh) == "паспорт 4510 123456"


def test_blind_index_is_deterministic_and_normalised() -> None:
    cipher = FieldCipher(KEY)
    a = cipher.blind_index("123-456-789 01", normalize_digits)
    b = cipher.blind_index("12345678901", normalize_digits)
    assert a == b
    assert len(a) == crypto.BLIND_INDEX_LENGTH
    assert cipher.blind_index("Иванов") == cipher.blind_index("  иванов ")


def test_blind_index_depends_on_key() -> None:
    other = FieldCipher(Fernet.generate_key().decode())
    assert FieldCipher(KEY).blind_index("4510123456") != other.blind_index("4510123456")


def test_explicit_blind_index_key_survives_encryption_key_rotation() -> None:
    a = FieldCipher(KEY, blind_index_key="index-key")
    b = FieldCipher(Fernet.generate_key().decode(), blind_index_key="index-key")
    assert a.blind_index("x") == b.blind_index("x")


def test_blind_index_helper_ignores_empty() -> None:
    crypto.configure(KEY)
    assert crypto.blind_index(None) is None
    assert crypto.blind_index("   ") is None


@pytest.fixture
def secret_table(database: Database) -> Iterator[Table]:
    metadata = MetaData()
    table = Table(
        "test_encrypted",
        metadata,
        Column("id", Integer, primary_key=True),
        Column("snils", EncryptedString()),
        Column("snils_index", String(64), index=True),
    )
    metadata.create_all(database.engine)
    yield table
    metadata.drop_all(database.engine)


def test_encrypted_column_in_database(database: Database, secret_table: Table) -> None:
    crypto.configure(KEY)
    value = "123-456-789 01"
    with Session(database.engine) as s:
        s.execute(
            secret_table.insert().values(
                snils=value, snils_index=crypto.blind_index(value, normalize_digits)
            )
        )
        s.commit()
        raw = s.execute(text("SELECT snils FROM test_encrypted")).scalar_one()
        assert value not in raw
        found = s.execute(
            select(secret_table.c.snils).where(
                secret_table.c.snils_index == crypto.blind_index("12345678901", normalize_digits)
            )
        ).scalar_one()
        assert found == value
