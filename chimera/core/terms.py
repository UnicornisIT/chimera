"""Единый словарь терминологии по типу организации (вуз / колледж).

В шаблонах и коде эти слова не пишутся напрямую: `terms_for(...)` возвращает
набор `Terms`, в Jinja он доступен как `t` (`{{ t.division }}`,
`{{ t.students.gen_pl }}`, `{{ t.division.lower }}`).
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from types import MappingProxyType


class OrgType(StrEnum):
    UNIVERSITY = "university"
    COLLEGE = "college"


class StudentTerm(StrEnum):
    """Как называть обучающихся; настраивается в реквизитах организации."""

    STUDENT = "student"
    LEARNER = "learner"


ORG_TYPE_LABELS = MappingProxyType({OrgType.UNIVERSITY: "Вуз", OrgType.COLLEGE: "Колледж (СУЗ)"})
STUDENT_TERM_LABELS = MappingProxyType(
    {StudentTerm.STUDENT: "студент", StudentTerm.LEARNER: "обучающийся"}
)


@dataclass(frozen=True, slots=True)
class Term:
    """Формы слова: именительный ед./мн., родительный ед./мн."""

    nom: str
    nom_pl: str
    gen: str
    gen_pl: str

    def __str__(self) -> str:
        return self.nom

    @property
    def lower(self) -> str:
        return self.nom[:1].lower() + self.nom[1:]

    @property
    def pl(self) -> str:
        return self.nom_pl


def _t(nom: str, nom_pl: str, gen: str, gen_pl: str) -> Term:
    return Term(nom, nom_pl, gen, gen_pl)


_BY_TYPE: MappingProxyType[OrgType, MappingProxyType[str, Term]] = MappingProxyType(
    {
        OrgType.UNIVERSITY: MappingProxyType(
            {
                "division": _t("Факультет", "Факультеты", "факультета", "факультетов"),
                "department": _t("Кафедра", "Кафедры", "кафедры", "кафедр"),
                "program": _t(
                    "Направление подготовки",
                    "Направления подготовки",
                    "направления подготовки",
                    "направлений подготовки",
                ),
                "assessment": _t(
                    "Экзаменационная сессия",
                    "Экзаменационные сессии",
                    "экзаменационной сессии",
                    "экзаменационных сессий",
                ),
                "division_head": _t("Декан", "Деканы", "декана", "деканов"),
                "department_head": _t(
                    "Заведующий кафедрой",
                    "Заведующие кафедрами",
                    "заведующего кафедрой",
                    "заведующих кафедрами",
                ),
                "organization": _t("Вуз", "Вузы", "вуза", "вузов"),
            }
        ),
        OrgType.COLLEGE: MappingProxyType(
            {
                "division": _t("Отделение", "Отделения", "отделения", "отделений"),
                "department": _t(
                    "Цикловая комиссия",
                    "Цикловые комиссии",
                    "цикловой комиссии",
                    "цикловых комиссий",
                ),
                "program": _t("Специальность", "Специальности", "специальности", "специальностей"),
                "assessment": _t(
                    "Промежуточная аттестация",
                    "Промежуточные аттестации",
                    "промежуточной аттестации",
                    "промежуточных аттестаций",
                ),
                "division_head": _t(
                    "Заведующий отделением",
                    "Заведующие отделениями",
                    "заведующего отделением",
                    "заведующих отделениями",
                ),
                "department_head": _t(
                    "Председатель цикловой комиссии",
                    "Председатели цикловых комиссий",
                    "председателя цикловой комиссии",
                    "председателей цикловых комиссий",
                ),
                "organization": _t("Колледж", "Колледжи", "колледжа", "колледжей"),
            }
        ),
    }
)

_STUDENT: MappingProxyType[StudentTerm, Term] = MappingProxyType(
    {
        StudentTerm.STUDENT: _t("Студент", "Студенты", "студента", "студентов"),
        StudentTerm.LEARNER: _t("Обучающийся", "Обучающиеся", "обучающегося", "обучающихся"),
    }
)

DEFAULT_STUDENT_TERM = MappingProxyType(
    {OrgType.UNIVERSITY: StudentTerm.STUDENT, OrgType.COLLEGE: StudentTerm.STUDENT}
)

TERM_KEYS = (
    "division",
    "department",
    "program",
    "assessment",
    "division_head",
    "department_head",
    "organization",
    "student",
)


@dataclass(frozen=True, slots=True)
class Terms:
    org_type: OrgType
    division: Term
    department: Term
    program: Term
    assessment: Term
    division_head: Term
    department_head: Term
    organization: Term
    student: Term

    def __getitem__(self, key: str) -> Term:
        if key not in TERM_KEYS:
            raise KeyError(key)
        value: Term = getattr(self, key)
        return value


def terms_for(org_type: OrgType | str, student_term: StudentTerm | str | None = None) -> Terms:
    kind = OrgType(org_type)
    student_kind = StudentTerm(student_term) if student_term else DEFAULT_STUDENT_TERM[kind]
    table = _BY_TYPE[kind]
    return Terms(
        org_type=kind,
        division=table["division"],
        department=table["department"],
        program=table["program"],
        assessment=table["assessment"],
        division_head=table["division_head"],
        department_head=table["department_head"],
        organization=table["organization"],
        student=_STUDENT[student_kind],
    )
