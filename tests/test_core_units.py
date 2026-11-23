"""Конфиг, терминология, фича-флаги, валидация и пароли — без базы данных."""

from __future__ import annotations

import pytest

from chimera.core.features import FEATURES, FeatureStatus, get_feature, is_enabled
from chimera.core.security import password_problems
from chimera.core.terms import TERM_KEYS, OrgType, StudentTerm, terms_for
from chimera.core.validation import email_problem, normalize_email, username_problem


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
