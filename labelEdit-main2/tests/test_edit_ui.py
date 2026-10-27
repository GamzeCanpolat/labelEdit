"""İşlem alanlarında yazmak karar kısayollarını tetiklememeli."""
import json
import os
from pathlib import Path

from playwright.sync_api import sync_playwright, expect


def test_edit_form_keyboard_actions_and_back_navigation():
    root = Path(__file__).resolve().parent.parent
    actions = []
    next_index = 0
    workspace = {"id": "test", "name": "Test ortamı", "source_file": "test.xlsx",
                 "stats": {"total": 3, "pending": 3}, "column_mapping": {}}

    def respond(route):
        nonlocal next_index
        path = route.request.url.split("labeledit.test", 1)[-1]
        if path.startswith("/static/"):
            name = path.split("?", 1)[0].split("/")[-1]
            route.fulfill(path=str(root / "static" / name))
            return
        if path == "/islem":
            route.fulfill(path=str(root / "static" / "edit.html"), content_type="text/html")
            return
        if path.startswith("/map.svg"):
            route.fulfill(content_type="image/svg+xml", body='<svg xmlns="http://www.w3.org/2000/svg" width="960" height="640"><rect width="960" height="640" fill="#e5e7eb"/><text x="300" y="300" font-size="24">Test harita görüntüsü</text></svg>')
            return
        if path == "/api/workspaces":
            data = [workspace]
        elif path == "/api/workspaces/active":
            data = {"active": workspace}
        elif path == "/api/edit/session/start":
            data = {"status": "started", "pending": 3, "start_index": 0}
        elif path == "/api/edit/field-options":
            data = {"brand": ["Marka A", "Marka B"], "main_category": ["Ev"], "sub_category": ["Mobilya"]}
        elif path == "/api/edit/screenshot/next":
            data = {"status": "ok", "row_index": next_index, "name": f"Mağaza {next_index}",
                    "url": "https://www.google.com/maps/place/Test", "file_url": "/map.svg",
                    "position": next_index, "total": 3, "fields": {"brand": "Marka A",
                    "main_category": "Ev", "sub_category": "Mobilya",
                    "poi_state": "OPERATIONAL" if next_index == 0 else "CLOSED_TEMPORARILY"}}
            next_index += 1
        elif path == "/api/edit/action":
            actions.append(route.request.post_data_json)
            data = {"status": "ok"}
        elif path == "/api/edit/action/undo":
            data = {"status": "ok"}
        else:
            route.abort()
            return
        route.fulfill(content_type="application/json", body=json.dumps(data))

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page(viewport={"width": 1440, "height": 1000})
        page.route("**/*", respond)
        page.goto("http://labeledit.test/islem")
        page.get_by_role("button", name="Oturumu Başlat").click()
        expect(page.locator("#edit-poi-state")).to_have_text("Açık")
        page.locator("#brand-select-btn").click()
        page.keyboard.press("Escape")
        brand = page.locator("#edit-brand")
        brand.fill("Marka B")
        brand.press("Enter")
        brand.press("ArrowRight")
        expect(page.locator("#business-name")).to_have_text("Mağaza 0")
        assert actions == []
        page.locator("#edit-main-category").fill("Alışveriş")
        page.locator("#edit-sub-category").fill("Dekorasyon")
        screenshot = os.environ.get("LABELEDIT_UI_SCREENSHOT")
        if screenshot:
            expect(page.locator("#screenshot-img")).to_have_class("loaded")
            page.screenshot(path=screenshot, animations="disabled")
        page.locator("#keep-btn").click()
        expect(page.locator("#business-name")).to_have_text("Mağaza 1")
        expect(page.locator("#edit-poi-state")).to_have_text("Geçici olarak kapalı")
        assert actions[0]["fields"] == {"brand": "Marka B", "main_category": "Alışveriş", "sub_category": "Dekorasyon"}
        page.locator("#back-btn").click()
        expect(page.locator("#business-name")).to_have_text("Mağaza 0")
        expect(brand).to_have_value("Marka B")
        page.locator("#skip-btn").click()
        expect(page.locator("#business-name")).to_have_text("Mağaza 1")
        assert next_index == 2   # Geri dönüşte sıradaki kayıt kaybolmaz.
        assert actions[-1]["fields"] == {}
        page.set_viewport_size({"width": 390, "height": 844})
        assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
        expect(page.locator("#keep-btn")).to_be_in_viewport()
        browser.close()
