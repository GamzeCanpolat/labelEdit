"""Tema seçimi gezinme ve yenilemede kalmalı; resmi logo her iki temada görünmeli."""
import json
from pathlib import Path
from urllib.parse import urlsplit

from playwright.sync_api import sync_playwright, expect


def test_theme_survives_navigation_and_header_shortcut_is_removed():
    root = Path(__file__).resolve().parent.parent

    def respond(route):
        url = urlsplit(route.request.url)
        if url.hostname != "mapin.test":
            route.abort()
            return
        if url.path.startswith("/static/"):
            route.fulfill(path=str(root / "static" / Path(url.path).name))
        elif url.path in ("/", "/islem", "/label"):
            name = {"/": "index.html", "/islem": "edit.html", "/label": "label.html"}[url.path]
            route.fulfill(path=str(root / "static" / name), content_type="text/html")
        else:
            data = [] if url.path == "/api/workspaces" else {"active": None}
            route.fulfill(content_type="application/json", body=json.dumps(data))

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page(viewport={"width": 1440, "height": 900})
        page.route("**/*", respond)
        page.goto("http://mapin.test/")
        expect(page.locator('header .brand-logo img')).to_be_visible()
        page.wait_for_function("document.querySelector('header .brand-logo img').naturalWidth > 0")
        expect(page.locator('header a[href="/islem"]')).to_have_count(0)
        expect(page.locator('#btn-start-process')).to_be_visible()
        expect(page.locator('#home-selected-file-badge')).not_to_be_visible()
        page.get_by_role("button", name="Açık", exact=True).click()
        expect(page.locator('html')).to_have_attribute('data-theme', 'light')
        page.goto("http://mapin.test/islem")
        expect(page.locator('html')).to_have_attribute('data-theme', 'light')
        expect(page.get_by_role('button', name='Açık', exact=True)).to_have_attribute('aria-pressed', 'true')
        assert page.evaluate("getComputedStyle(document.body).backgroundColor") == 'rgb(238, 242, 248)'
        page.get_by_role('button', name='Koyu', exact=True).click()
        assert page.evaluate("getComputedStyle(document.body).backgroundColor") == 'rgb(26, 40, 71)'
        page.reload()
        expect(page.locator('html')).to_have_attribute('data-theme', 'dark')
        page.set_viewport_size({"width": 390, "height": 844})
        assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
        expect(page.get_by_role('button', name='Açık', exact=True)).to_be_in_viewport()
        page.goto("http://mapin.test/label")
        expect(page.locator('html')).to_have_attribute('data-theme', 'dark')
        expect(page.locator('header .brand-logo img')).to_be_visible()
        browser.close()
