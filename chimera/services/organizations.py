"""Организации, их реквизиты и дерево подразделений."""

from __future__ import annotations

from dataclasses import dataclass, field

from sqlalchemy import select

from chimera.core import audit
from chimera.core.context import NotFound, ServiceContext, ServiceError
from chimera.core.permissions import Permission, SystemPermission
from chimera.core.terms import OrgType, StudentTerm
from chimera.core.validation import clean_text, optional_text
from chimera.models import Division, DivisionKind, Organization


@dataclass(slots=True)
class OrganizationData:
    name: str
    short_name: str
    org_type: str
    student_term: str | None = None
    founder_name: str | None = None
    address: str | None = None
    head_full_name: str | None = None
    head_position: str | None = None
    document_logo_path: str | None = None
    is_active: bool = True

    def validated(self) -> dict[str, object]:
        problems: list[str] = []
        name = clean_text(self.name, max_length=500)
        short_name = clean_text(self.short_name, max_length=150) or name[:150]
        if not name:
            problems.append("Укажите полное название.")
        try:
            org_type = OrgType(self.org_type)
        except ValueError:
            problems.append("Выберите тип организации.")
            org_type = OrgType.COLLEGE
        student_term: StudentTerm | None = None
        if self.student_term:
            try:
                student_term = StudentTerm(self.student_term)
            except ValueError:
                problems.append("Неизвестный вариант термина «студент».")
        if problems:
            raise ServiceError(" ".join(problems))
        return {
            "name": name,
            "short_name": short_name,
            "org_type": org_type,
            "student_term": student_term,
            "founder_name": optional_text(self.founder_name, max_length=500),
            "address": optional_text(self.address),
            "head_full_name": optional_text(self.head_full_name, max_length=300),
            "head_position": optional_text(self.head_position, max_length=300),
            "document_logo_path": optional_text(self.document_logo_path, max_length=500),
            "is_active": bool(self.is_active),
        }


def list_organizations(ctx: ServiceContext) -> list[Organization]:
    stmt = select(Organization).order_by(Organization.short_name)
    if not ctx.can_system(SystemPermission.ORGANIZATIONS_CREATE):
        stmt = stmt.where(Organization.id == ctx.require_organization())
    return list(ctx.session.scalars(stmt))


def get_organization(ctx: ServiceContext, organization_id: int) -> Organization:
    if organization_id != ctx.organization_id and not ctx.can_system(
        SystemPermission.ORGANIZATIONS_CREATE
    ):
        raise NotFound("Организация не найдена")
    organization = ctx.session.get(Organization, organization_id)
    if organization is None:
        raise NotFound("Организация не найдена")
    return organization


def create_organization(ctx: ServiceContext, data: OrganizationData) -> Organization:
    ctx.require_system(SystemPermission.ORGANIZATIONS_CREATE)
    organization = Organization(**data.validated())
    ctx.session.add(organization)
    ctx.session.flush()
    audit.record(
        ctx,
        "organization.created",
        entity=organization,
        after=audit.snapshot(organization),
        organization_id=organization.id,
    )
    return organization


def update_organization(
    ctx: ServiceContext, organization_id: int, data: OrganizationData
) -> Organization:
    ctx.require(Permission.ORG_MANAGE)
    organization = get_organization(ctx, organization_id)
    values = data.validated()
    if organization.id == ctx.organization_id and not values["is_active"]:
        raise ServiceError("Нельзя отключить организацию, в которой вы сейчас работаете.")
    before = audit.snapshot(organization)
    for key, value in values.items():
        setattr(organization, key, value)
    ctx.session.flush()
    audit.record(
        ctx,
        "organization.updated",
        entity=organization,
        before=before,
        after=audit.snapshot(organization),
        organization_id=organization.id,
    )
    return organization


# --- Подразделения -----------------------------------------------------------------


@dataclass(slots=True)
class DivisionNode:
    division: Division
    children: list[DivisionNode] = field(default_factory=list)


def list_divisions(ctx: ServiceContext, *, include_inactive: bool = True) -> list[Division]:
    ctx.require_organization()
    stmt = select(Division).order_by(Division.kind, Division.name)
    if not include_inactive:
        stmt = stmt.where(Division.is_active.is_(True))
    return list(ctx.session.scalars(stmt))


def division_tree(ctx: ServiceContext) -> list[DivisionNode]:
    nodes = {d.id: DivisionNode(d) for d in list_divisions(ctx)}
    roots: list[DivisionNode] = []
    for node in nodes.values():
        parent = nodes.get(node.division.parent_id) if node.division.parent_id else None
        (parent.children if parent else roots).append(node)
    return roots


def get_division(ctx: ServiceContext, division_id: int) -> Division:
    ctx.require_organization()
    division = ctx.session.scalar(select(Division).where(Division.id == division_id))
    if division is None:
        raise NotFound("Подразделение не найдено")
    return division


@dataclass(slots=True)
class DivisionData:
    name: str
    kind: str
    short_name: str | None = None
    parent_id: int | None = None
    is_active: bool = True


def _validate_division(
    ctx: ServiceContext, data: DivisionData, *, current: Division | None = None
) -> dict[str, object]:
    problems: list[str] = []
    name = clean_text(data.name, max_length=300)
    if not name:
        problems.append("Укажите название.")
    try:
        kind = DivisionKind(data.kind)
    except ValueError:
        problems.append("Неизвестный вид подразделения.")
        kind = DivisionKind.DIVISION
    parent: Division | None = None
    if data.parent_id:
        try:
            parent = get_division(ctx, data.parent_id)
        except NotFound:
            problems.append("Вышестоящее подразделение не найдено.")
    if kind is DivisionKind.DIVISION and parent is not None:
        problems.append("Подразделение верхнего уровня не может быть вложенным.")
    if parent is not None and parent.kind is not DivisionKind.DIVISION:
        problems.append("Вложенные подразделения создаются только внутри верхнего уровня.")
    if current is not None and parent is not None and parent.id == current.id:
        problems.append("Подразделение не может быть вложено само в себя.")
    if current is not None and kind is not current.kind and current.children:
        problems.append("Нельзя сменить вид подразделения, у которого есть вложенные.")
    duplicate = ctx.session.scalar(
        select(Division.id).where(
            Division.name == name,
            Division.parent_id.is_(None) if parent is None else Division.parent_id == parent.id,
        )
    )
    if duplicate is not None and (current is None or duplicate != current.id):
        problems.append("Подразделение с таким названием уже есть на этом уровне.")
    if problems:
        raise ServiceError(" ".join(problems))
    return {
        "name": name,
        "kind": kind,
        "short_name": optional_text(data.short_name, max_length=100),
        "parent_id": parent.id if parent else None,
        "is_active": bool(data.is_active),
    }


def create_division(ctx: ServiceContext, data: DivisionData) -> Division:
    ctx.require(Permission.ORG_MANAGE)
    division = Division(**_validate_division(ctx, data))
    ctx.session.add(division)
    ctx.session.flush()
    audit.record(ctx, "division.created", entity=division, after=audit.snapshot(division))
    return division


def update_division(ctx: ServiceContext, division_id: int, data: DivisionData) -> Division:
    ctx.require(Permission.ORG_MANAGE)
    division = get_division(ctx, division_id)
    values = _validate_division(ctx, data, current=division)
    before = audit.snapshot(division)
    for key, value in values.items():
        setattr(division, key, value)
    ctx.session.flush()
    audit.record(
        ctx, "division.updated", entity=division, before=before, after=audit.snapshot(division)
    )
    return division


def delete_division(ctx: ServiceContext, division_id: int) -> None:
    ctx.require(Permission.ORG_MANAGE)
    division = get_division(ctx, division_id)
    if division.children:
        raise ServiceError("Сначала удалите или перенесите вложенные подразделения.")
    before = audit.snapshot(division)
    ctx.session.delete(division)
    ctx.session.flush()
    audit.record(
        ctx, "division.deleted", entity_type="Division", entity_id=division_id, before=before
    )
