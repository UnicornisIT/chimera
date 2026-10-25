"""Организации и подразделения."""

from __future__ import annotations

from typing import Any

from flask import flash, redirect, render_template, request, url_for

from chimera.core.context import ServiceError
from chimera.core.permissions import Permission, SystemPermission
from chimera.core.terms import ORG_TYPE_LABELS, STUDENT_TERM_LABELS, OrgType
from chimera.models import DivisionKind
from chimera.services import organizations as org_service
from chimera.web.admin import bp
from chimera.web.context import ctx, db, form_bool, form_int, permission_required, system_required


def _organization_form() -> org_service.OrganizationData:
    form = request.form
    return org_service.OrganizationData(
        name=form.get("name", ""),
        short_name=form.get("short_name", ""),
        org_type=form.get("org_type", ""),
        student_term=form.get("student_term") or None,
        founder_name=form.get("founder_name"),
        address=form.get("address"),
        head_full_name=form.get("head_full_name"),
        head_position=form.get("head_position"),
        document_logo_path=form.get("document_logo_path"),
        is_active=form_bool("is_active"),
    )


def _render_org_form(organization: Any, *, creating: bool) -> str:
    return render_template(
        "admin/organization_form.html",
        organization=organization,
        creating=creating,
        org_types=ORG_TYPE_LABELS,
        student_terms=STUDENT_TERM_LABELS,
        form=request.form,
    )


@bp.get("/organizations")
@permission_required(Permission.ORG_MANAGE)
def organizations() -> Any:
    current = ctx()
    return render_template(
        "admin/organizations.html",
        organizations=org_service.list_organizations(current),
        can_create=current.can_system(SystemPermission.ORGANIZATIONS_CREATE),
        org_types=ORG_TYPE_LABELS,
    )


@bp.route("/organizations/new", methods=["GET", "POST"])
@system_required(SystemPermission.ORGANIZATIONS_CREATE)
def organization_new() -> Any:
    if request.method == "POST":
        try:
            organization = org_service.create_organization(ctx(), _organization_form())
        except ServiceError as exc:
            db().rollback()
            flash(str(exc), "error")
        else:
            db().commit()
            flash(f"Организация «{organization.short_name}» создана.", "success")
            return redirect(url_for("admin.organizations"))
    return _render_org_form(None, creating=True)


@bp.route("/organizations/<int:organization_id>", methods=["GET", "POST"])
@permission_required(Permission.ORG_MANAGE)
def organization_edit(organization_id: int) -> Any:
    organization = org_service.get_organization(ctx(), organization_id)
    if request.method == "POST":
        try:
            org_service.update_organization(ctx(), organization_id, _organization_form())
        except ServiceError as exc:
            db().rollback()
            flash(str(exc), "error")
        else:
            db().commit()
            flash("Реквизиты сохранены.", "success")
            return redirect(url_for("admin.organization_edit", organization_id=organization_id))
    return _render_org_form(organization, creating=False)


def _division_form() -> org_service.DivisionData:
    return org_service.DivisionData(
        name=request.form.get("name", ""),
        kind=request.form.get("kind", ""),
        short_name=request.form.get("short_name"),
        parent_id=form_int("parent_id"),
        is_active=form_bool("is_active"),
    )


@bp.route("/divisions", methods=["GET", "POST"])
@permission_required(Permission.ORG_MANAGE)
def divisions() -> Any:
    current = ctx()
    if request.method == "POST":
        try:
            org_service.create_division(current, _division_form())
        except ServiceError as exc:
            db().rollback()
            flash(str(exc), "error")
        else:
            db().commit()
            flash("Подразделение добавлено.", "success")
            return redirect(url_for("admin.divisions"))
    all_divisions = org_service.list_divisions(current)
    return render_template(
        "admin/divisions.html",
        tree=org_service.division_tree(current),
        parents=[d for d in all_divisions if d.kind is DivisionKind.DIVISION],
        kinds=DivisionKind,
        is_university=current_org_type() is OrgType.UNIVERSITY,
    )


def current_org_type() -> OrgType | None:
    from chimera.web.context import state

    organization = state().organization
    return organization.org_type if organization else None


@bp.route("/divisions/<int:division_id>", methods=["GET", "POST"])
@permission_required(Permission.ORG_MANAGE)
def division_edit(division_id: int) -> Any:
    current = ctx()
    division = org_service.get_division(current, division_id)
    if request.method == "POST":
        try:
            org_service.update_division(current, division_id, _division_form())
        except ServiceError as exc:
            db().rollback()
            flash(str(exc), "error")
        else:
            db().commit()
            flash("Подразделение сохранено.", "success")
            return redirect(url_for("admin.divisions"))
    parents = [
        d
        for d in org_service.list_divisions(current)
        if d.kind is DivisionKind.DIVISION and d.id != division.id
    ]
    return render_template(
        "admin/division_form.html", division=division, parents=parents, kinds=DivisionKind
    )


@bp.post("/divisions/<int:division_id>/delete")
@permission_required(Permission.ORG_MANAGE)
def division_delete(division_id: int) -> Any:
    try:
        org_service.delete_division(ctx(), division_id)
    except ServiceError as exc:
        db().rollback()
        flash(str(exc), "error")
    else:
        db().commit()
        flash("Подразделение удалено.", "success")
    return redirect(url_for("admin.divisions"))
