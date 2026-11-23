"""Приёмка форм и сетевых ошибок на изолированном стенде этапа 02."""

import sys
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Thread

from playwright.sync_api import Page, expect, sync_playwright
from sqlalchemy import select
from werkzeug.serving import make_server

from chimera.models import Organization
from chimera.services import auth
from chimera.web import create_app
from scripts import dev_test_env as dev
from tests.browser.stage03_smoke import WIDTHS, check_layout, open_route
from tests.browser.stage04_smoke import sign_in

SCREENS = Path("docs/planning/stage-05/screenshots")


def check_forms(page: Page, base: str) -> None:
    open_route(page, base, "/admin/users/new")
    page.locator("#username").fill("stage05")
    page.locator("#full_name").fill("Тестовый сотрудник")
    page.locator("#password").fill("weak")
    page.get_by_role("button", name="Добавить", exact=True).click()
    expect(page.locator("#password-error")).to_be_visible()
    expect(page.locator("[data-error-focus]")).to_be_focused()
    expect(page.locator("#full_name")).to_have_value("Тестовый сотрудник")
    expect(page.locator("#password")).to_have_value("")
    page.screenshot(path=SCREENS / "validation-mobile.png")

    open_route(page, base, "/admin/organizations/new")
    page.locator("#name").fill("   ")
    page.locator("#short_name").fill("Проверка реквизитов")
    page.get_by_role("button", name="Создать", exact=True).click()
    expect(page.locator("#name-error")).to_be_visible()
    expect(page.locator("#short_name")).to_have_value("Проверка реквизитов")

    open_route(page, base, "/admin/divisions")
    page.locator("#name").fill("   ")
    page.locator("#short_name").fill("Сохранённое сокращение")
    page.get_by_role("button", name="Добавить", exact=True).click()
    expect(page.locator("#name-error")).to_be_visible()
    expect(page.locator("#short_name")).to_have_value("Сохранённое сокращение")


def check_backup(page: Page, base: str) -> None:
    open_route(page, base, "/admin/backups")
    page.get_by_role("button", name="Создать копию").click()
    expect(page.get_by_role("status").filter(has_text="создана")).to_be_visible()
    delete = page.get_by_role("button", name="Удалить", exact=True)
    with page.expect_download() as download:
        page.get_by_role("button", name="Скачать").click()
    assert download.value.failure() is None
    open_route(page, base, "/admin/backups")
    expect(delete).to_be_enabled()
    delete.focus()
    expect(delete).to_be_focused()
    page.keyboard.press("Space")
    dialog = page.get_by_role("dialog")
    cancel = dialog.get_by_role("button", name="Отмена")
    accept = dialog.get_by_role("button", name="Удалить")
    expect(cancel).to_be_focused()
    # Нативный dialog удерживает фокус даже при попытке сфокусировать фон.
    page.locator("[data-sidebar-toggle]").evaluate("button => button.focus()")
    expect(cancel).to_be_focused()
    page.keyboard.press("Shift+Tab")
    expect(accept).to_be_focused()
    page.keyboard.press("Tab")
    expect(cancel).to_be_focused()
    page.screenshot(path=SCREENS / "confirmation-mobile-dark.png")
    page.keyboard.press("Escape")
    expect(delete).to_be_focused()
    expect(delete).to_be_enabled()
    page.keyboard.press("Enter")
    expect(dialog).to_be_visible()
    with page.expect_request(lambda request: request.method == "POST") as submitted:
        accept.click()
    assert "confirmed=1" in (submitted.value.post_data or "")
    expect(page.get_by_role("status").filter(has_text="копия удалена")).to_be_visible()
    expect(delete).to_have_count(0)


def check_requests(page: Page, base: str) -> None:
    open_route(page, base, "/admin/requests")
    reject = page.get_by_role("button", name="Отклонить")
    if not reject.is_visible():
        switcher = page.locator("#org-switcher")
        alternative = switcher.locator("option:not(:checked)").first.get_attribute("value")
        assert alternative
        switcher.select_option(alternative)
        expect(page).to_have_url(base + "/")
        open_route(page, base, "/admin/requests")
    expect(reject).to_be_enabled()
    browser = page.context.browser
    assert browser is not None
    context = browser.new_context(
        java_script_enabled=False, storage_state=page.context.storage_state()
    )
    try:
        native = context.new_page()
        open_route(native, base, "/admin/requests")
        expect(native.get_by_role("button", name="Отклонить")).to_be_disabled()
        open_route(native, base, "/admin/divisions")
        native.locator("#name").fill("   ")
        native.locator("#short_name").fill("Без сценариев")
        native.get_by_role("button", name="Добавить", exact=True).click()
        expect(native.locator("#name-error")).to_be_visible()
        expect(native.locator("#short_name")).to_have_value("Без сценариев")
    finally:
        context.close()
    reject.click()
    dialog = page.get_by_role("dialog", name="Отклонить заявку?")
    expect(dialog.get_by_role("button", name="Отмена")).to_be_focused()
    dialog.get_by_role("button", name="Отклонить").click()
    expect(page.get_by_role("status").filter(has_text="Заявка отклонена")).to_be_visible()
    expect(reject).to_have_count(0)


def check_connection(page: Page, base: str) -> None:
    path = "/admin/divisions"
    open_route(page, base, path)
    requests = []

    def discard_response(route):
        if route.request.method == "POST":
            requests.append(route.request)
            result = route.fetch(max_redirects=0)
            assert result.status == 204
            route.abort("connectionreset")
        else:
            route.continue_()

    page.route(base + path, discard_response)
    page.locator("#name").fill("Отделение после сбоя")
    page.evaluate("""() => {
        const form = document.querySelector('main form.panel');
        const submit = form.querySelector('button[type=submit]');
        form.requestSubmit(submit);
        form.requestSubmit(submit);
    }""")
    message = page.locator("#connection-message")
    expect(message).to_contain_text("Действие могло выполниться")
    expect(message).to_be_focused()
    expect(page.locator("#name")).to_have_value("Отделение после сбоя")
    assert len(requests) == 1
    page.screenshot(path=SCREENS / "connection-mobile.png")
    message.get_by_role("link", name="Проверить страницу").click()
    expect(page.locator(".tree strong").filter(has_text="Отделение после сбоя")).to_have_count(1)
    assert len(requests) == 1
    page.unroute(base + path, discard_response)

    page.context.set_offline(True)
    try:
        expect(message).to_contain_text("Нет подключения")
    finally:
        page.context.set_offline(False)
    expect(message).to_be_hidden()


def check_post_history(page: Page, base: str) -> None:
    path = "/admin/users/new"
    open_route(page, base, path)
    page.locator("#full_name").fill("Сотрудник для проверки истории")
    submit = page.get_by_role("button", name="Добавить", exact=True)
    submit.click()
    assert page.locator("#username").evaluate("input => input.validity.valueMissing")
    expect(submit).to_be_enabled()
    page.locator("#username").fill("history.stage05")
    page.locator("#password").fill("Synthetic-history-123")
    posts = []

    def count_post(request):
        if request.method == "POST" and request.url == base + path:
            posts.append(request)

    page.on("request", count_post)
    page.keyboard.press("Enter")
    page.keyboard.press("Enter")
    expect(page).to_have_url(base + "/admin/users")
    page.reload()
    page.go_back()
    expect(page).to_have_url(base + path)
    expect(submit).to_be_enabled()
    assert len(posts) == 1
    page.remove_listener("request", count_post)


def check_restored_pending_post(page: Page, base: str) -> None:
    path = "/admin/divisions"
    open_route(page, base, path)
    requests = []

    def stall(route):
        requests.append(route.request)

    page.route(base + path, stall)
    page.locator("#name").fill("Проверка восстановления страницы")
    submit = page.get_by_role("button", name="Добавить", exact=True)
    with page.expect_request(lambda request: request.method == "POST"):
        submit.click()
    page.evaluate("window.dispatchEvent(new PageTransitionEvent('pageshow', {persisted: true}))")
    message = page.locator("#connection-message")
    expect(message).to_contain_text("Действие могло выполниться")
    expect(submit).to_be_disabled()
    page.locator("#name").press("Enter")
    assert len(requests) == 1
    page.unroute(base + path, stall)
    message.get_by_role("link", name="Проверить страницу").click()
    expect(page.locator(".tree strong").filter(has_text="Проверка восстановления")).to_have_count(0)


def check_unavailable_and_timeout(page: Page, base: str) -> None:
    path = "/admin/divisions"
    url = base + path
    open_route(page, base, path)
    attempts = []

    def error_response(route):
        attempts.append(route.request)
        route.fulfill(status=500, body="synthetic upstream error")

    page.route(url, error_response, times=1)
    page.locator("#name").fill("Запись при недоступности")
    page.get_by_role("button", name="Добавить", exact=True).click()
    message = page.locator("#connection-message")
    expect(message).to_contain_text("Не удалось получить ответ от приложения")
    assert len(attempts) == 1
    message.get_by_role("link", name="Проверить страницу").click()
    expect(page.locator(".tree strong").filter(has_text="Запись при недоступности")).to_have_count(
        0
    )

    page.clock.install()
    page.route(url, lambda route: None, times=1)
    page.locator("#name").fill("Запись при тайм-ауте")
    with page.expect_request(lambda request: request.url == url and request.method == "POST"):
        page.get_by_role("button", name="Добавить", exact=True).click()
    page.clock.fast_forward(31_000)
    expect(message).to_contain_text("Приложение не ответило вовремя")
    message.get_by_role("link", name="Проверить страницу").click()
    expect(page.locator(".tree strong").filter(has_text="Запись при тайм-ауте")).to_have_count(0)


def check_browser(base: str, password: str) -> None:
    SCREENS.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(channel="msedge")
        page = browser.new_page(viewport={"width": 375, "height": 900}, accept_downloads=True)
        sign_in(page, base, "admin", password)
        if "--network-only" in sys.argv:
            check_unavailable_and_timeout(page, base)
            browser.close()
            print("PASS Stage 05 Edge network")
            return
        themes = () if "--actions-only" in sys.argv else ("light", "dark", "system")
        for theme in themes:
            page.evaluate("(theme) => window.ChimeraTheme.set(theme)", theme)
            for width in WIDTHS:
                page.set_viewport_size({"width": width, "height": 900})
                for route in (
                    "/admin/users",
                    "/admin/audit",
                    "/admin/requests",
                    "/admin/organizations",
                    "/admin/divisions",
                    "/admin/backups",
                ):
                    open_route(page, base, route)
                    check_layout(page)
        page.set_viewport_size({"width": 1440, "height": 900})
        page.evaluate("window.ChimeraTheme.set('light')")
        open_route(page, base, "/admin/users")
        page.locator("#q").fill("точного совпадения нет")
        page.get_by_role("button", name="Найти").click()
        expect(page.get_by_text("Ничего не найдено.")).to_be_visible()
        page.get_by_role("link", name="Сбросить фильтры").click()
        expect(page.locator("#q")).to_have_value("")
        page.screenshot(path=SCREENS / "users-desktop.png")

        page.set_viewport_size({"width": 375, "height": 900})
        check_forms(page, base)
        page.evaluate("window.ChimeraTheme.set('dark')")
        check_backup(page, base)
        page.evaluate("window.ChimeraTheme.set('light')")
        check_requests(page, base)
        check_connection(page, base)
        check_post_history(page, base)
        check_restored_pending_post(page, base)
        check_unavailable_and_timeout(page, base)
        browser.close()
    print("PASS Stage 05 Edge")


def main() -> None:
    with TemporaryDirectory(prefix="stage05-edge-", dir=".chimera-test-envs") as root:
        dev.STATE_ROOT = Path(root).resolve()
        with dev.locked_stand("sqlite", create=True) as path:
            settings = dev.prepare_or_status(path, "sqlite", prepare=True)
            assert settings.admin_default_password
            app = create_app(settings)
            with app.extensions["chimera.db"].transaction() as session:
                org_id = session.scalar(select(Organization.id).order_by(Organization.id))
                assert org_id is not None
                auth.register(
                    session,
                    username="pending.stage05",
                    full_name="Тестовый заявитель",
                    email=None,
                    password=settings.admin_default_password,
                    organization_id=org_id,
                    comment=None,
                    ip=None,
                )
            server = make_server("127.0.0.1", 0, app, threaded=True)
            thread = Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                check_browser(
                    f"http://127.0.0.1:{server.server_port}", settings.admin_default_password
                )
            finally:
                server.shutdown()
                server.server_close()
                thread.join()
                app.extensions["chimera.db"].dispose()


if __name__ == "__main__":
    main()
