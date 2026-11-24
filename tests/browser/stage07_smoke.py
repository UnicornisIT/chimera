"""Проверка экрана первого запуска в Edge на отдельном профиле."""

from __future__ import annotations

import secrets
import socket
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Thread

from playwright.sync_api import expect, sync_playwright
from werkzeug.serving import make_server

from chimera.core.config import LOOPBACK, load_settings
from chimera.core.local_profile import own_local_profile
from chimera.web import create_app

SCREENS = Path("docs/planning/stage-07/screenshots")


def main() -> None:
    with TemporaryDirectory() as temporary, socket.socket() as probe:
        probe.bind((LOOPBACK, 0))
        port = probe.getsockname()[1]
        probe.close()
        settings = load_settings(
            {"CHIMERA_MODE": "desktop-local", "CHIMERA_DATA_DIR": temporary, "APP_PORT": str(port)}
        )
        token = secrets.token_urlsafe(32)
        origin = f"http://{LOOPBACK}:{port}"
        with own_local_profile(settings):
            app = create_app(settings, setup_token=token)
            with make_server(LOOPBACK, port, app) as server:
                worker = Thread(target=server.serve_forever)
                worker.start()
                try:
                    with sync_playwright() as playwright:
                        browser = playwright.chromium.launch(channel="msedge", headless=True)
                        try:
                            SCREENS.mkdir(parents=True, exist_ok=True)
                            for theme, width in (("light", 1024), ("dark", 360)):
                                page = browser.new_page(
                                    viewport={"width": width, "height": 780}, color_scheme=theme
                                )
                                page.goto(origin + "/local-setup")
                                submit = page.get_by_role("button", name="Создать администратора")
                                expect(submit).to_be_disabled()
                                page.goto("about:blank")
                                page.goto(origin + "/local-setup#" + token)
                                password = page.get_by_label("Пароль", exact=True)
                                confirm = page.get_by_label("Повторите пароль")
                                expect(password).to_be_focused()
                                expect(password).to_have_attribute("type", "password")
                                expect(submit).to_be_enabled()
                                assert (
                                    page.evaluate("document.documentElement.scrollWidth") == width
                                )
                                password.press("Tab")
                                expect(confirm).to_be_focused()
                                page.screenshot(path=SCREENS / f"setup-{theme}.png")
                                password.fill("Synthetic-Local-456")
                                confirm.fill("Synthetic-Local-789")
                                submit.click()
                                expect(page.get_by_role("alert")).to_have_text(
                                    "Пароли не совпадают."
                                )
                                expect(password).to_be_focused()
                                expect(submit).to_be_enabled()
                                expect(password).to_have_value("")
                                expect(confirm).to_have_value("")
                                if theme == "dark":
                                    password.fill("Synthetic-Local-456")
                                    confirm.fill("Synthetic-Local-456")
                                    submit.click()
                                    expect(page).to_have_url(origin + "/login")
                                    expect(
                                        page.get_by_text("Администратор создан.", exact=False)
                                    ).to_be_visible()
                                page.close()
                        finally:
                            browser.close()
                finally:
                    server.shutdown()
                    worker.join()
                    app.extensions["chimera.db"].dispose()


if __name__ == "__main__":
    main()
