"""Проверка оболочки в Edge на отдельном SQLite-стенде этапа 02."""

import sys
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Thread

from playwright.sync_api import Page, expect, sync_playwright
from sqlalchemy import select
from werkzeug.serving import make_server

from chimera.core.config import Settings
from chimera.core.db import Database
from chimera.core.permissions import Role
from chimera.core.security import hash_password
from chimera.core.tenancy import bind_organization
from chimera.models import Membership, Organization, User
from chimera.web import create_app
from scripts import dev_test_env as dev
from scripts.dev_test_fixtures import COLLEGE
from tests.browser.stage03_smoke import (
    ROUTES,
    WIDTHS,
    check_backup_actions,
    check_contrast,
    check_keyboard_and_filters,
    check_layout,
    check_login_wordmark,
    check_sidebar_focus,
    check_sidebar_wordmark,
    check_themes,
    open_route,
)
from tests.test_navigation import MANAGEMENT, SYSTEM

ACCOUNTS = (
    ("orgadmin", "Администратор"),
    ("head", "Руководитель подразделения"),
    ("curator1", "Куратор"),
    ("starosta", "Староста"),
    ("viewer", "Наблюдатель"),
    ("admin", "Администратор"),
)


def add_accounts(settings: Settings) -> None:
    assert settings.admin_default_password
    database = Database.from_settings(settings)
    try:
        with database.transaction() as session:
            college = session.scalars(
                select(Organization).where(Organization.name == COLLEGE)
            ).one()
            university = session.scalars(
                select(Organization).where(Organization.short_name == "Образец")
            ).one()
            password_hash = hash_password(settings.admin_default_password)
            for username, name, role in (
                ("orgadmin", "Тестовый администратор организации", Role.ADMIN),
                ("starosta", "Тестовый староста", Role.STAROSTA),
            ):
                bind_organization(session, college.id)
                user = User(username=username, full_name=name, password_hash=password_hash)
                session.add(user)
                session.flush()
                session.add(Membership(user_id=user.id, organization_id=college.id, role=role))
                session.flush()
                if role is Role.ADMIN:
                    bind_organization(session, university.id)
                    session.add(
                        Membership(user_id=user.id, organization_id=university.id, role=Role.VIEWER)
                    )
                    session.flush()
    finally:
        database.dispose()


def sign_in(page: Page, base: str, username: str, password: str) -> None:
    open_route(page, base, "/login")
    page.locator("#username").fill(username)
    page.locator("#password").fill(password)
    page.get_by_role("button", name="Войти").click()
    if username == "admin":
        expect(page).to_have_url(base + "/password")
        expect(page.locator("#app-sidebar")).to_have_count(0)
        page.locator("#current_password").fill(password)
        page.locator("#new_password").fill(password + "Stage04")
        page.locator("#new_password_confirm").fill(password + "Stage04")
        page.get_by_role("button", name="Сохранить пароль").click()
    expect(page).to_have_url(base + "/")
    page.wait_for_load_state("load")
    expect(page.locator("[data-theme-toggle]")).to_be_visible()


def check_routes(page: Page, base: str, management: bool, system: bool) -> None:
    open_route(page, base, "/")
    for routes, allowed in ((MANAGEMENT, management), (SYSTEM, system)):
        for route in routes:
            expect(page.locator(f'#app-sidebar a[href="{route}"]')).to_have_count(int(allowed))
    for routes, allowed in ((MANAGEMENT, management), (SYSTEM, system)):
        for route in routes:
            response = page.goto(base + route)
            assert response is not None and response.status == (200 if allowed else 403), route
            if not allowed:
                expect(page.locator("main h1")).to_have_text("Доступ запрещён")


def check_roles(base: str, password: str, screenshots: Path) -> None:
    with sync_playwright() as pw:
        browser = pw.chromium.launch(channel="msedge", timeout=20_000)
        screenshots.mkdir(parents=True, exist_ok=True)
        public = browser.new_page(reduced_motion="reduce")
        for theme in ("light", "dark"):
            open_route(public, base, "/login")
            public.evaluate("(theme) => window.ChimeraTheme.set(theme)", theme)
            check_contrast(public)
            for width in WIDTHS:
                public.set_viewport_size({"width": width, "height": 900})
                for route in ("/login", "/register"):
                    open_route(public, base, route)
                    check_layout(public)
                    check_login_wordmark(public, theme)
        public.close()
        for username, role in ACCOUNTS:
            page = browser.new_page(
                viewport={"width": 1440, "height": 900}, reduced_motion="reduce"
            )
            errors: list[str] = []
            page.on("pageerror", lambda exc, found=errors: found.append(str(exc)))
            sign_in(page, base, username, password)
            switch = page.locator("#org-switcher")
            if username == "orgadmin":
                switch.select_option(label=COLLEGE)
                expect(page.locator(".page-header p")).to_have_text(role)
            check_routes(page, base, username in {"admin", "orgadmin"}, username == "admin")
            for theme in ("light", "dark"):
                open_route(page, base, "/")
                page.evaluate("(theme) => window.ChimeraTheme.set(theme)", theme)
                if username == "admin":
                    check_sidebar_focus(page, base)
                for width in WIDTHS:
                    open_route(page, base, "/")
                    page.set_viewport_size({"width": width, "height": 900})
                    check_layout(page)
                    expect(page.locator(".page-header p")).to_have_text(role)
                    menu = page.get_by_label("Меню пользователя")
                    menu.click()
                    expect(page.get_by_role("link", name="Профиль")).to_be_visible()
                    check_layout(page)
                    page.keyboard.press("Escape")
                    expect(menu).to_be_focused()
                    expect(menu).to_have_attribute("aria-expanded", "false")
                    if username == "admin":
                        if width > 900:
                            check_sidebar_wordmark(page)
                        for route in ROUTES:
                            if route not in {"/login", "/register"}:
                                open_route(page, base, route)
                                check_layout(page)
            if username == "orgadmin":
                switch.select_option(label="Образец")
                expect(page.locator(".page-header p")).to_have_text("Наблюдатель")
                check_routes(page, base, False, False)
                switch.select_option(label=COLLEGE)
                expect(page.locator(".page-header p")).to_have_text("Администратор")
            if username == "admin":
                check_themes(page, base)
                check_keyboard_and_filters(page, base)
                check_backup_actions(page, base)
                page.set_viewport_size({"width": 1440, "height": 900})
                page.evaluate("window.ChimeraTheme.set('dark')")
                open_route(page, base, "/admin/users")
                expect(page.locator('#app-sidebar a[aria-current="page"]')).to_have_text(
                    "Пользователи"
                )
                toggle = page.locator("[data-sidebar-toggle]")
                toggle.click()
                page.reload()
                expect(toggle).to_have_attribute("aria-expanded", "false")
                expect(page.locator("#app-sidebar")).to_have_js_property("inert", True)
                toggle.click()
                page.screenshot(path=screenshots / "management-desktop-dark.png")
                open_route(page, base, "/admin/backups")
                page.evaluate("window.ChimeraTheme.set('light')")
                page.screenshot(path=screenshots / "system-desktop-light.png")
                if switch.locator("option:checked").inner_text() != COLLEGE:
                    switch.select_option(label=COLLEGE)
                    expect(page).to_have_url(base + "/")
                open_route(page, base, "/admin/act-as")
                page.locator("#curator_id").select_option(label="Тестовый куратор колледжа")
                page.get_by_role("button", name="Включить режим").click()
                expect(page.locator(".role-banner")).to_be_visible()
                check_routes(page, base, False, False)
                open_route(page, base, "/")
                page.set_viewport_size({"width": 375, "height": 900})
                page.screenshot(path=screenshots / "acting-mobile-light.png")
                page.get_by_role("button", name="Вернуться").click()
                expect(page.locator(".role-banner")).to_be_hidden()
                open_route(page, base, "/admin/users")
                toggle.click()
                expect(page.locator(".nav-brand")).to_be_focused()
                page.screenshot(path=screenshots / "management-mobile-light.png")
                page.keyboard.press("Escape")
                menu = page.get_by_label("Меню пользователя")
                account = page.get_by_role("navigation", name="Действия с учётной записью")
                menu.click()
                account.get_by_role("link", name="Профиль").click()
                expect(page).to_have_url(base + "/profile")
                menu.click()
                account.get_by_role("link", name="Сменить пароль").click()
                expect(page).to_have_url(base + "/password")
                menu.click()
                account.get_by_role("button", name="Выйти").click()
                expect(page).to_have_url(base + "/login")
            assert not errors, errors
            page.close()
            print(f"PASS {username}: доступ, четыре ширины, две темы")
        browser.close()


def main() -> None:
    screenshots = Path(sys.argv[1] if len(sys.argv) > 1 else "docs/planning/stage-04/screenshots")
    with TemporaryDirectory(prefix="stage04-browser-", dir=".chimera-test-envs") as root:
        dev.STATE_ROOT = Path(root).resolve()
        with dev.locked_stand("sqlite", create=True) as path:
            settings = dev.prepare_or_status(path, "sqlite", prepare=True)
            add_accounts(settings)
            assert settings.admin_default_password
            app = create_app(settings)
            server = make_server("127.0.0.1", 0, app, threaded=True)
            thread = Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                check_roles(
                    f"http://127.0.0.1:{server.server_port}",
                    settings.admin_default_password,
                    screenshots,
                )
            finally:
                server.shutdown()
                server.server_close()
                thread.join()
                app.extensions["chimera.db"].dispose()


if __name__ == "__main__":
    main()
