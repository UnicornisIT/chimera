"""Главная, переключатель организации, выход из режима «работать как»."""

from __future__ import annotations

from typing import Any

from flask import Blueprint, abort, flash, redirect, render_template, request, session, url_for

from chimera.core.permissions import Role
from chimera.services import auth as auth_service
from chimera.services import dashboard as dashboard_service
from chimera.services import impersonation
from chimera.web.context import (
    SESSION_ACT_AS,
    SESSION_ORG,
    ctx,
    current_terms,
    db,
    form_int,
    login_required,
    require_user,
    state,
)

bp = Blueprint("main", __name__)


@bp.get("/")
@login_required
def dashboard() -> Any:
    cards = dashboard_service.module_cards(ctx(), current_terms())
    return render_template("dashboard.html", cards=cards)


@bp.get("/no-organization")
def no_organization() -> Any:
    if state().user is None:
        return redirect(url_for("auth.login"))
    if ctx().organization_id is not None:
        return redirect(url_for("main.dashboard"))
    return render_template("no_organization.html")


@bp.post("/organization/switch")
@login_required
def switch_organization() -> Any:
    user = require_user()
    target = form_int("organization_id")
    if auth_service.resolve_access(db(), user, target) is None:
        abort(403)
    if ctx().is_acting:
        acting = impersonation.ActAs.from_session(session.get(SESSION_ACT_AS))
        if acting is not None:
            impersonation.stop(ctx(), acting)
        session.pop(SESSION_ACT_AS, None)
    from chimera.core import audit

    audit.record(
        ctx(),
        "organization.switched",
        entity_type="Organization",
        entity_id=target,
        organization_id=target,
    )
    db().commit()
    session[SESSION_ORG] = target
    return redirect(url_for("main.dashboard"))


@bp.post("/act-as/stop")
@login_required
def stop_acting() -> Any:
    current = ctx()
    acting = impersonation.ActAs.from_session(session.get(SESSION_ACT_AS))
    if acting is None or current.real_role is not Role.ADMIN:
        return redirect(url_for("main.dashboard"))
    impersonation.stop(current, acting)
    db().commit()
    session.pop(SESSION_ACT_AS, None)
    flash("Вы вернулись в роль администратора.", "success")
    target = request.form.get("next")
    return redirect(url_for("admin.act_as") if target == "admin" else url_for("main.dashboard"))
