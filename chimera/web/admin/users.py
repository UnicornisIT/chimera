"""Пользователи, членства, заявки и режим «работать как»."""

from __future__ import annotations

from typing import Any

from flask import flash, redirect, render_template, request, session, url_for

from chimera.core.context import ServiceError
from chimera.core.permissions import ACTABLE_ROLES, ROLE_DESCRIPTIONS, Permission, Role
from chimera.services import impersonation
from chimera.services import organizations as org_service
from chimera.services import users as users_service
from chimera.web.admin import bp
from chimera.web.context import SESSION_ACT_AS, ctx, db, form_bool, form_int, permission_required


def _role_filter() -> Role | None:
    try:
        return Role(request.args.get("role", ""))
    except ValueError:
        return None


@bp.get("/users")
@permission_required(Permission.USERS_MANAGE)
def users() -> Any:
    current = ctx()
    return render_template(
        "admin/users.html",
        members=users_service.list_members(
            current, query=request.args.get("q"), role=_role_filter()
        ),
        roles=list(Role),
        query=request.args.get("q", ""),
        selected_role=_role_filter(),
    )


@bp.route("/users/new", methods=["GET", "POST"])
@permission_required(Permission.USERS_MANAGE)
def user_new() -> Any:
    current = ctx()
    if request.method == "POST":
        data = users_service.NewUserData(
            username=request.form.get("username", ""),
            full_name=request.form.get("full_name", ""),
            email=request.form.get("email"),
            password=request.form.get("password", ""),
            role=request.form.get("role", ""),
            division_id=form_int("division_id"),
        )
        try:
            users_service.create_member(current, data)
        except ServiceError as exc:
            db().rollback()
            flash(str(exc), "error")
        else:
            db().commit()
            flash("Пользователь добавлен. При первом входе он сменит пароль.", "success")
            return redirect(url_for("admin.users"))
    return render_template(
        "admin/user_new.html",
        roles=list(Role),
        role_descriptions=ROLE_DESCRIPTIONS,
        divisions=org_service.list_divisions(current, include_inactive=False),
        form=request.form,
    )


@bp.route("/users/<int:membership_id>", methods=["GET", "POST"])
@permission_required(Permission.USERS_MANAGE)
def user_edit(membership_id: int) -> Any:
    current = ctx()
    membership = users_service.get_membership(current, membership_id)
    if request.method == "POST":
        data = users_service.MembershipData(
            role=request.form.get("role", ""),
            division_id=form_int("division_id"),
            is_active=form_bool("is_active"),
            full_name=request.form.get("full_name", ""),
            email=request.form.get("email"),
        )
        try:
            users_service.update_member(current, membership_id, data)
        except ServiceError as exc:
            db().rollback()
            flash(str(exc), "error")
        else:
            db().commit()
            flash("Изменения сохранены.", "success")
            return redirect(url_for("admin.users"))
    return render_template(
        "admin/user_edit.html",
        membership=membership,
        roles=list(Role),
        divisions=org_service.list_divisions(current, include_inactive=False),
    )


@bp.post("/users/<int:membership_id>/password")
@permission_required(Permission.USERS_MANAGE)
def user_reset_password(membership_id: int) -> Any:
    try:
        users_service.reset_password(ctx(), membership_id, request.form.get("password", ""))
    except ServiceError as exc:
        db().rollback()
        flash(str(exc), "error")
    else:
        db().commit()
        flash("Временный пароль установлен. Пользователь сменит его при входе.", "success")
    return redirect(url_for("admin.user_edit", membership_id=membership_id))


@bp.get("/requests")
@permission_required(Permission.REQUESTS_APPROVE)
def requests() -> Any:
    current = ctx()
    return render_template(
        "admin/requests.html",
        requests=users_service.list_requests(current),
        roles=list(Role),
        divisions=org_service.list_divisions(current, include_inactive=False),
    )


@bp.post("/requests/<int:request_id>/approve")
@permission_required(Permission.REQUESTS_APPROVE)
def request_approve(request_id: int) -> Any:
    try:
        users_service.approve_request(
            ctx(), request_id, request.form.get("role", ""), form_int("division_id")
        )
    except ServiceError as exc:
        db().rollback()
        flash(str(exc), "error")
    else:
        db().commit()
        flash("Заявка одобрена.", "success")
    return redirect(url_for("admin.requests"))


@bp.post("/requests/<int:request_id>/reject")
@permission_required(Permission.REQUESTS_APPROVE)
def request_reject(request_id: int) -> Any:
    try:
        users_service.reject_request(ctx(), request_id)
    except ServiceError as exc:
        db().rollback()
        flash(str(exc), "error")
    else:
        db().commit()
        flash("Заявка отклонена.", "success")
    return redirect(url_for("admin.requests"))


@bp.route("/act-as", methods=["GET", "POST"])
@permission_required(Permission.ROLE_ACT_AS)
def act_as() -> Any:
    current = ctx()
    if request.method == "POST":
        try:
            acting = impersonation.start(
                current,
                request.form.get("role", ""),
                subject_user_id=form_int("subject_user_id"),
                division_id=form_int("division_id"),
            )
        except ServiceError as exc:
            db().rollback()
            flash(str(exc), "error")
        else:
            db().commit()
            session[SESSION_ACT_AS] = acting.to_session()
            flash(f"Вы работаете как: {acting.label}.", "info")
            return redirect(url_for("main.dashboard"))
    return render_template(
        "admin/act_as.html",
        choices=impersonation.choices(current),
        roles=ACTABLE_ROLES,
        role_descriptions=ROLE_DESCRIPTIONS,
    )
