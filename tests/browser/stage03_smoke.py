"""Run the Stage 03 visual checks against a disposable Stage 02 loopback stand."""

import argparse
import re
from pathlib import Path

from playwright.sync_api import Page, expect, sync_playwright

ROUTES = {
    "/": "dashboard",
    "/login": "login",
    "/register": "",
    "/profile": "settings",
    "/password": "",
    "/admin/": "",
    "/admin/users": "users",
    "/admin/users/new": "",
    "/admin/organizations": "",
    "/admin/organizations/new": "",
    "/admin/divisions": "",
    "/admin/requests": "",
    "/admin/backups": "",
    "/admin/audit": "",
    "/admin/act-as": "",
}
WIDTHS = (375, 768, 1024, 1440)


def open_route(page: Page, base: str, route: str) -> None:
    response = page.goto(base + route)
    assert response is not None and response.ok, route
    assert page.url == base + route, page.url
    expect(page.locator("main h1")).to_be_visible()


def check_layout(page: Page) -> None:
    overflow = page.evaluate(
        """() => {
            const width = document.documentElement.clientWidth;
            const scroll = document.documentElement.scrollWidth;
            return {
                excess: scroll - width,
                controls: [...document.querySelectorAll('button, select, input, summary, .btn')]
                    .filter(el => !el.closest('.table-wrap, .sr-only') && el.checkVisibility({
                        visibilityProperty: true,
                    }))
                    .filter(el => {
                        const box = el.getBoundingClientRect();
                        return box.width > 1 && (box.left < -1 || box.right > width + 1);
                    })
                    .map(el => el.outerHTML.slice(0, 100))
            };
        }"""
    )
    assert overflow["excess"] <= 1 and not overflow["controls"], (
        page.url,
        page.viewport_size,
        overflow,
    )


def check_contrast(page: Page) -> None:
    failures = page.evaluate(
        """() => {
            const sample = document.createElement('span');
            document.body.append(sample);
            function luminance(token) {
                sample.style.color = token.startsWith('--') ? 'var(' + token + ')' : token;
                const [r, g, b] = getComputedStyle(sample).color.match(/\\d+/g).map(Number);
                return [r, g, b].map(v => v / 255)
                    .map(v => v <= .04045 ? v / 12.92 : ((v + .055) / 1.055) ** 2.4)
                    .reduce((sum, v, i) => sum + v * [.2126, .7152, .0722][i], 0);
            }
            const pairs = [
                ...['--text-primary', '--text-secondary', '--text-muted', '--accent'].flatMap(
                    ink => ['--bg-canvas', '--bg-surface', '--bg-elevated', '--bg-hover'].map(
                        bg => [ink, bg, 4.5])),
                ...['danger', 'warning', 'success', 'info'].map(tone =>
                    ['--' + tone, '--' + tone + '-soft', 4.5]),
                ...['--accent-control', '--accent-control-hover', '--danger-control',
                    '--danger-control-hover'].map(bg => ['--on-accent', bg, 4.5]),
                ...['--focus', '--border-control'].flatMap(ink =>
                    ['--bg-canvas', '--bg-surface', '--bg-elevated'].map(bg => [ink, bg, 3])),
                ['--sidebar-muted', '--sidebar-bg', 4.5],
                ['--sidebar-text', '--sidebar-selected', 4.5],
                ['--sidebar-focus', '--sidebar-bg', 3],
            ];
            const failures = pairs.flatMap(([ink, bg, threshold]) => {
                const [min, max] = [luminance(ink), luminance(bg)].sort((a, b) => a - b);
                const ratio = (max + .05) / (min + .05);
                return ratio < threshold ? [[ink, bg, ratio]] : [];
            });
            sample.remove();
            return failures;
        }"""
    )
    assert not failures, failures


def login(page: Page, base: str, password: str) -> None:
    open_route(page, base, "/login")
    page.locator("#username").fill("admin")
    page.locator("#password").fill("stage03-wrong-password")
    page.get_by_role("button", name="Войти").click()
    expect(page.get_by_role("alert")).to_be_visible()
    page.locator("#password").fill(password)
    page.get_by_role("button", name="Войти").click()
    changed_password = password + "Stage03"
    if page.url == base + "/login":
        page.locator("#password").fill(changed_password)
        page.get_by_role("button", name="Войти").click()
    if page.url == base + "/password":
        page.locator("#current_password").fill(password)
        page.locator("#new_password").fill(changed_password)
        page.locator("#new_password_confirm").fill(changed_password)
        page.get_by_role("button", name="Сохранить пароль").click()
        expect(page.get_by_role("status").filter(has_text="Пароль изменён")).to_be_visible()
    expect(page).to_have_url(base + "/")


def check_login_wordmark(page: Page, mode: str) -> None:
    shown = page.locator(f".login-logo.logo-{mode}")
    other = "light" if mode == "dark" else "dark"
    expect(shown).to_be_visible()
    expect(shown).to_have_js_property("naturalWidth", 1937)
    expect(page.locator(f".login-logo.logo-{other}")).to_be_hidden()


def check_sidebar_wordmark(page: Page) -> None:
    logo = page.locator(".nav-brand-logo")
    expect(logo).to_be_visible()
    expect(logo).to_have_attribute("src", re.compile(r"/chimera-logo-dark\.png$"))
    expect(logo).to_have_js_property("naturalWidth", 1937)


def check_sidebar_focus(page: Page, base: str) -> None:
    toggle = page.locator("[data-sidebar-toggle]")
    drawer = page.locator(".main-nav")
    first = drawer.locator(".nav-brand")
    submenu = drawer.locator("summary[aria-controls='nav-management']")
    system = drawer.locator("summary[aria-controls='nav-system']")
    last = drawer.locator("#nav-management a").last
    search = page.locator("#q")

    for mobile_width, desktop_width in ((375, 1024), (768, 1440)):
        page.set_viewport_size({"width": mobile_width, "height": 900})
        open_route(page, base, "/admin/users")
        expect(toggle).to_have_attribute("aria-expanded", "false")
        expect(drawer).to_have_js_property("inert", True)
        toggle.focus()
        page.keyboard.press("Enter")
        expect(first).to_be_focused()
        expect(page.locator("main")).to_have_js_property("inert", True)
        expect(drawer).to_have_js_property("inert", False)
        page.keyboard.press("Shift+Tab")
        expect(system).to_be_focused()
        page.keyboard.press("Tab")
        expect(first).to_be_focused()

        submenu.focus()
        page.keyboard.press("Enter")
        expect(submenu).to_have_attribute("aria-expanded", "false")
        expect(last).to_be_hidden()
        page.keyboard.press("Tab")
        expect(system).to_be_focused()
        page.keyboard.press("Tab")
        expect(first).to_be_focused()
        page.keyboard.press("Shift+Tab")
        expect(system).to_be_focused()

        search.focus()
        expect(system).to_be_focused()
        page.keyboard.press("Escape")
        expect(toggle).to_have_attribute("aria-expanded", "false")
        expect(toggle).to_be_focused()
        expect(page.locator("main")).to_have_js_property("inert", False)

        page.keyboard.press("Enter")
        expect(first).to_be_focused()
        page.locator("[data-sidebar-backdrop]").click(position={"x": mobile_width - 5, "y": 50})
        expect(toggle).to_have_attribute("aria-expanded", "false")
        expect(toggle).to_be_focused()

        toggle.focus()
        page.keyboard.press("Enter")
        expect(first).to_be_focused()
        toggle.click()
        expect(toggle).to_have_attribute("aria-expanded", "false")
        expect(toggle).to_be_focused()
        page.keyboard.press("Enter")
        expect(first).to_be_focused()

        page.set_viewport_size({"width": desktop_width, "height": 900})
        expect(page.locator("body")).not_to_have_class(re.compile(r"\bsidebar-open\b"))
        expect(toggle).to_have_attribute("aria-expanded", "true")
        expect(toggle).to_be_focused()
        system.focus()
        page.keyboard.press("Tab")
        expect(toggle).to_be_focused()
        page.keyboard.press("Enter")
        expect(toggle).to_have_attribute("aria-expanded", "false")
        page.keyboard.press("Enter")
        expect(toggle).to_have_attribute("aria-expanded", "true")

        first.focus()
        page.set_viewport_size({"width": mobile_width, "height": 900})
        expect(toggle).to_have_attribute("aria-expanded", "false")
        expect(toggle).to_be_focused()
        page.keyboard.press("Enter")
        expect(first).to_be_focused()
        page.keyboard.press("Escape")
        expect(toggle).to_have_attribute("aria-expanded", "false")
        expect(toggle).to_be_focused()


def check_keyboard_and_filters(page: Page, base: str) -> None:
    page.set_viewport_size({"width": 375, "height": 900})
    open_route(page, base, "/")
    page.keyboard.press("Tab")
    expect(page.locator(".skip-link")).to_be_focused()
    page.keyboard.press("Enter")
    expect(page.locator("main")).to_be_focused()
    open_route(page, base, "/")
    page.keyboard.press("Tab")
    page.keyboard.press("Tab")
    sidebar = page.locator("[data-sidebar-toggle]")
    expect(sidebar).to_be_focused()
    expect(sidebar).to_have_css("outline-style", "solid")
    page.keyboard.press("Enter")
    expect(sidebar).to_have_attribute("aria-expanded", "true")
    expect(page.locator(".main-nav .nav-brand")).to_be_focused()
    check_sidebar_wordmark(page)
    page.keyboard.press("Tab")
    expect(page.locator(".sidebar-link")).to_be_focused()
    page.keyboard.press("Escape")
    expect(sidebar).to_have_attribute("aria-expanded", "false")
    expect(sidebar).to_be_focused()

    organization = page.locator("#org-switcher")
    if organization.is_visible():
        page.keyboard.press("Tab")
        expect(organization).to_be_focused()
    page.keyboard.press("Tab")
    expect(page.locator("[data-theme-toggle]")).to_be_focused()
    page.keyboard.press("Tab")
    menu = page.get_by_label("Меню пользователя").filter(visible=True).first
    expect(menu).to_be_focused()
    page.keyboard.press("Enter")
    expect(page.get_by_role("link", name="Профиль")).to_be_visible()
    page.keyboard.press("Escape")
    expect(menu).to_be_focused()

    open_route(page, base, "/admin/users")
    table = page.get_by_role("region", name="Пользователи")
    table.focus()
    expect(table).to_have_css("outline-style", "solid")
    page.keyboard.press("ArrowRight")
    assert table.evaluate(
        """el => new Promise(resolve => {
            if (el.scrollLeft > 0) {
                resolve(true);
                return;
            }
            el.addEventListener('scroll', () => resolve(el.scrollLeft > 0), { once: true });
            setTimeout(() => resolve(false), 1000);
        })"""
    )
    page.locator("#q").fill("curator")
    page.get_by_role("button", name="Найти").click()
    expect(page).to_have_url(re.compile(r"/admin/users\?q=curator"))
    expect(page.locator("tbody tr")).to_have_count(1)
    expect(page.locator("tbody tr")).to_contain_text("Тестовый куратор")


def check_themes(page: Page, base: str) -> None:
    open_route(page, base, "/profile")
    root = page.locator("html")
    choice = page.get_by_label("Тема интерфейса")
    choice.select_option("system")
    page.emulate_media(color_scheme="dark")
    dark = root.evaluate("el => getComputedStyle(el).backgroundColor")
    page.emulate_media(color_scheme="light")
    light = root.evaluate("el => getComputedStyle(el).backgroundColor")
    assert dark != light
    choice.select_option("dark")
    expect(root).to_have_css("background-color", dark)
    page.add_init_script(
        """(() => {
            function sampleFrame() {
                if (document.querySelector('main')) {
                    window.stage03FrameColor = getComputedStyle(document.documentElement)
                        .backgroundColor;
                } else {
                    requestAnimationFrame(sampleFrame);
                }
            }
            requestAnimationFrame(sampleFrame);
        })();"""
    )
    page.reload()
    frame = page.evaluate(
        """() => new Promise(resolve => {
            requestAnimationFrame(() => resolve(window.stage03FrameColor));
        })"""
    )
    assert frame == dark, frame
    expect(choice).to_have_value("dark")
    expect(root).to_have_css("background-color", dark)
    assert page.evaluate(
        """() => {
            const script = document.querySelector('script[src$="/theme.js"]');
            const tokens = document.querySelector('link[href$="/tokens.css"]');
            return !script.defer && !script.async &&
                Boolean(script.compareDocumentPosition(tokens) & Node.DOCUMENT_POSITION_FOLLOWING);
        }"""
    )
    toggle = page.locator("[data-theme-toggle]")
    expect(toggle).to_have_accessible_name("Изменить тему. Тема: тёмная")
    toggle.click()
    expect(choice).to_have_value("system")
    expect(toggle).to_have_accessible_name("Изменить тему. Тема: системная")
    expect(root).to_have_css("background-color", light)
    page.emulate_media(color_scheme="dark")
    toggle.click()
    expect(choice).to_have_value("light")
    expect(root).to_have_css("background-color", light)


def check_backup_actions(page: Page, base: str) -> None:
    open_route(page, base, "/admin/backups")
    download = page.get_by_role("button", name="Скачать").first
    if not download.is_visible():
        page.get_by_role("button", name="Создать копию").click()
        expect(page.get_by_role("status").filter(has_text="создана")).to_be_visible()
    delete = page.get_by_role("button", name="Удалить").first
    expect(delete).to_be_enabled()
    with page.expect_download() as result:
        download.click()
    assert result.value.failure() is None
    open_route(page, base, "/admin/backups")
    delete.click()
    expect(page.get_by_role("dialog")).to_be_visible()
    expect(page.get_by_role("button", name="Отмена")).to_be_focused()
    page.keyboard.press("Escape")
    expect(page.get_by_role("dialog")).not_to_be_visible()
    expect(delete).to_be_focused()
    expect(download).to_be_visible()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=5801)
    parser.add_argument("--password-file", type=Path, required=True)
    parser.add_argument("--screenshots", type=Path)
    parser.add_argument("--browser", choices=("msedge", "chromium", "firefox"), default="msedge")
    args = parser.parse_args()
    base = f"http://127.0.0.1:{args.port}"
    if args.screenshots:
        args.screenshots.mkdir(parents=True, exist_ok=True)

    with sync_playwright() as pw:
        if args.browser == "firefox":
            browser = pw.firefox.launch()
        else:
            browser = pw.chromium.launch(channel="msedge" if args.browser == "msedge" else None)
        public = browser.new_page(reduced_motion="reduce")
        private = browser.new_page(reduced_motion="reduce")
        errors = []
        for page in (public, private):
            page.on("pageerror", lambda exc: errors.append(str(exc)))
        login(private, base, args.password_file.read_text(encoding="utf-8").strip())
        check_themes(private, base)
        check_keyboard_and_filters(private, base)
        check_backup_actions(private, base)

        for mode in ("light", "dark"):
            public.goto(base + "/login")
            private.goto(base + "/")
            for page in (public, private):
                page.evaluate("(mode) => window.ChimeraTheme.set(mode)", mode)
                check_contrast(page)
            check_sidebar_focus(private, base)
            for width in WIDTHS:
                for page in (public, private):
                    page.set_viewport_size({"width": width, "height": 900})
                for route, name in ROUTES.items():
                    page = public if route in ("/login", "/register") else private
                    open_route(page, base, route)
                    expect(page.locator("html")).to_have_attribute("data-theme", mode)
                    check_layout(page)
                    if route in ("/login", "/register"):
                        check_login_wordmark(page, mode)
                    if route == "/" and width > 900:
                        check_sidebar_wordmark(page)
                    if args.screenshots and name and width == 1440:
                        page.screenshot(
                            path=args.screenshots / f"{name}-{mode}.png", full_page=True
                        )
        private.locator(".user-menu summary").click()
        private.get_by_role("button", name="Выйти").click()
        expect(private).to_have_url(base + "/login")
        expect(private.get_by_role("status").filter(has_text="Вы вышли из системы")).to_be_visible()
        expect(private.locator("html")).to_have_attribute("data-theme", "dark")

        no_js = browser.new_page(java_script_enabled=False, color_scheme="dark")
        open_route(no_js, base, "/login")
        expect(no_js.locator("html")).to_have_css("background-color", "rgb(16, 21, 32)")
        expect(no_js.locator("[data-theme-toggle]")).to_be_hidden()
        check_login_wordmark(no_js, "dark")
        browser.close()
        assert not errors, errors
        print(f"PASS: {args.browser}, {len(ROUTES)} routes, {len(WIDTHS)} widths, two themes")


if __name__ == "__main__":
    main()
