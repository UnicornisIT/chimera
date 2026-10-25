"""Конфиг, терминология, фича-флаги, валидация и пароли — без базы данных."""

from __future__ import annotations

from pathlib import Path

import pytest
from cryptography.fernet import Fernet

from chimera.core.config import AppMode, ConfigError, load_settings
from chimera.core.features import FEATURES, FeatureStatus, get_feature, is_enabled
from chimera.core.security import password_problems
from chimera.core.terms import TERM_KEYS, OrgType, StudentTerm, terms_for
from chimera.core.validation import email_problem, normalize_email, username_problem

GOOD_ENV = {
    "SECRET_KEY": "s" * 40,
    "DATA_ENCRYPTION_KEY": Fernet.generate_key().decode(),
}


class TestConfig:
    def test_defaults_to_sqlite_in_instance(self) -> None:
        settings = load_settings(dict(GOOD_ENV))
        assert settings.mode is AppMode.SERVER
        assert settings.is_sqlite
        assert settings.database_url.endswith("instance/chimera.db")

    def test_postgres_url_is_normalised_to_psycopg(self) -> None:
        settings = load_settings({**GOOD_ENV, "DATABASE_URL": "postgresql://u:p@db/chimera"})
        assert settings.database_url == "postgresql+psycopg://u:p@db/chimera"

    def test_server_requires_strong_secret(self) -> None:
        with pytest.raises(ConfigError, match="SECRET_KEY"):
            load_settings({**GOOD_ENV, "SECRET_KEY": "short"})

    def test_server_requires_encryption_key(self) -> None:
        with pytest.raises(ConfigError, match="DATA_ENCRYPTION_KEY"):
            load_settings({"SECRET_KEY": "s" * 40})

    def test_desktop_local_generates_and_keeps_secrets(self, tmp_path: Path) -> None:
        env = {"CHIMERA_MODE": "desktop-local", "CHIMERA_DATA_DIR": str(tmp_path)}
        first = load_settings(env)
        second = load_settings(env)
        assert len(first.secret_key) >= 32
        assert first.secret_key == second.secret_key
        assert first.data_encryption_key == second.data_encryption_key
        assert first.session_cookie_secure is False

    def test_desktop_local_rejects_postgres(self, tmp_path: Path) -> None:
        with pytest.raises(ConfigError, match="SQLite"):
            load_settings(
                {
                    "CHIMERA_MODE": "desktop-local",
                    "CHIMERA_DATA_DIR": str(tmp_path),
                    "DATABASE_URL": "postgresql://x/y",
                }
            )

    @pytest.mark.parametrize(
        ("key", "value"),
        [("CHIMERA_MODE", "cloud"), ("APP_PORT", "abc"), ("APP_DEBUG", "maybe")],
    )
    def test_invalid_values(self, key: str, value: str) -> None:
        with pytest.raises(ConfigError):
            load_settings({**GOOD_ENV, key: value})

    def test_migrations_do_not_need_secrets(self) -> None:
        settings = load_settings({}, require_secrets=False)
        assert settings.database_url.startswith("sqlite")


class TestTerms:
    @pytest.mark.parametrize(
        ("org_type", "key", "expected"),
        [
            (OrgType.UNIVERSITY, "division", "Факультет"),
            (OrgType.COLLEGE, "division", "Отделение"),
            (OrgType.UNIVERSITY, "department", "Кафедра"),
            (OrgType.COLLEGE, "department", "Цикловая комиссия"),
            (OrgType.UNIVERSITY, "program", "Направление подготовки"),
            (OrgType.COLLEGE, "program", "Специальность"),
            (OrgType.UNIVERSITY, "assessment", "Экзаменационная сессия"),
            (OrgType.COLLEGE, "assessment", "Промежуточная аттестация"),
            (OrgType.UNIVERSITY, "division_head", "Декан"),
            (OrgType.COLLEGE, "division_head", "Заведующий отделением"),
        ],
    )
    def test_labels_by_org_type(self, org_type: OrgType, key: str, expected: str) -> None:
        assert str(terms_for(org_type)[key]) == expected

    def test_student_term_is_configurable(self) -> None:
        assert str(terms_for(OrgType.COLLEGE).student) == "Студент"
        learner = terms_for(OrgType.COLLEGE, StudentTerm.LEARNER).student
        assert learner.nom == "Обучающийся"
        assert learner.gen_pl == "обучающихся"

    def test_every_key_has_all_forms(self) -> None:
        for org_type in OrgType:
            terms = terms_for(org_type)
            for key in TERM_KEYS:
                term = terms[key]
                assert term.nom and term.nom_pl and term.gen and term.gen_pl
                assert term.lower[0].islower()

    def test_unknown_key(self) -> None:
        with pytest.raises(KeyError):
            terms_for("college")["faculty"]


class TestFeatures:
    def test_planned_features_are_not_available(self) -> None:
        assert not is_enabled("groups")
        assert get_feature("groups").badge == "Скоро"
        assert get_feature("dashboard").badge == ""

    def test_every_feature_has_title_and_status(self) -> None:
        for feature in FEATURES.values():
            assert feature.title
            assert isinstance(feature.status, FeatureStatus)

    def test_unknown_feature(self) -> None:
        with pytest.raises(KeyError):
            get_feature("nope")


class TestValidation:
    @pytest.mark.parametrize("email", ["ivan@example.org", "a.b+c@mail.ru", "x@пример.рф"])
    def test_valid_emails(self, email: str) -> None:
        assert email_problem(email) is None

    @pytest.mark.parametrize("email", ["ivan", "@example.org", "a..b@example.org", "a@b"])
    def test_invalid_emails(self, email: str) -> None:
        assert email_problem(email) is not None

    def test_typo_hint(self) -> None:
        assert "gmail.com" in (email_problem("ivan@gmial.com") or "")

    def test_domain_is_lowercased(self) -> None:
        assert normalize_email(" Ivan@Example.ORG ") == "Ivan@example.org"

    def test_username_rules(self) -> None:
        assert username_problem("ivan.petrov") is None
        assert username_problem("иван") is not None
        assert username_problem("ab") is not None


def test_password_policy() -> None:
    assert password_problems("Test-pass-123") == []
    assert password_problems("short1")
    assert password_problems("onlyletterslong")
    assert password_problems("ivanov12345", username="IVANOV12345")
