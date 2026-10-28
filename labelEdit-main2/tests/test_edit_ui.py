"""İşlem alanlarında yazmak karar kısayollarını tetiklememeli."""
import json
import os
from collections import Counter
from pathlib import Path
import pytest

from playwright.sync_api import sync_playwright, expect


@pytest.mark.parametrize('initial_state, initial_selection, chosen_state', [
    ('', '', 'CLOSED_TEMPORARILY'),
    ('OPERATIONAL', 'OPERATIONAL', None),
    ('UNKNOWN_SOURCE', 'UNKNOWN_SOURCE', None),
    ('Bilgi yok', '', 'OPERATIONAL'),
])
def test_edit_form_keyboard_actions_and_back_navigation(initial_state, initial_selection, chosen_state):
    root = Path(__file__).resolve().parent.parent
    actions = []
    next_index = 0
    map_requests = Counter()
    mappings = []
    preparing_responses = [True]
    schema = {"workspace_id": "test", "columns": ["name", "maps", "Marka", "Ana Kategori", "Alt Kategori", "Pol State", "Özel Marka"],
              "mapping": {"name": "name", "maps_url": "maps", "brand": "Marka", "main_category": "Ana Kategori", "sub_category": "Alt Kategori", "poi_state": "Pol State"}}
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
            map_requests[path] += 1
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
        elif path.startswith("/api/edit/schema"):
            data = schema
        elif path == "/api/edit/status":
            data = {"status": "running", "buffer": {"ready": 2, "preparing": 0},
                    "previews": [{"row_index": 1, "name": "Mağaza 1", "file_url": "/map.svg?v=1"}]}
        elif path == "/api/workspaces/test/mapping":
            mappings.append(route.request.post_data_json["mapping"])
            schema["mapping"].update(mappings[-1])
            data = {"status": "ok"}
        elif path.startswith("/api/edit/fields/"):
            data = {"brand": "Özel Marka Değeri", "main_category": "Ev", "sub_category": "Mobilya", "poi_state": initial_state}
        elif path == "/api/edit/screenshot/next":
            if preparing_responses:
                preparing_responses.pop()
                route.fulfill(content_type="application/json", body=json.dumps({"status": "preparing", "buffer": {"ready": 0, "preparing": 3}}))
                return
            data = {"status": "ok", "row_index": next_index, "name": f"Mağaza {next_index}",
                    "url": "https://www.google.com/maps/place/Test", "file_url": f"/map.svg?v={next_index}",
                    "position": next_index, "total": 3, "fields": {"brand": "Marka A",
                    "main_category": "Ev", "sub_category": "Mobilya",
                    "poi_state": initial_state if next_index == 0 else "CLOSED_TEMPORARILY"}}
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
        expect(page.locator("#edit-poi-state")).to_have_value(initial_selection)
        if chosen_state:
            expect(page.locator('#edit-poi-state')).to_be_visible()
            expect(page.locator('#edit-poi-state')).to_be_enabled()
        else:
            expect(page.locator('#edit-poi-state')).not_to_be_visible()
            expect(page.locator('#poi-state-display')).to_be_visible()
        expect(page.locator("#buffer-text")).to_have_text("2 harita hazır")
        page.wait_for_function("performance.getEntriesByName('http://labeledit.test/map.svg?v=1').length > 0")
        if chosen_state:
            page.locator("#edit-poi-state").select_option(chosen_state)
        page.locator("#column-mapping-btn").click()
        expect(page.locator('select[data-role="maps_url"]')).to_be_disabled()
        page.locator('select[data-role="brand"]').select_option("Özel Marka")
        page.locator("#mapping-save-btn").click()
        expect(page.locator("#mapping-dialog")).not_to_be_visible()
        expect(page.locator("#edit-brand")).to_have_value("Özel Marka Değeri")
        expect(page.locator("#edit-poi-state")).to_have_value(chosen_state or initial_selection)
        assert mappings[-1]["brand"] == "Özel Marka"
        page.locator("#brand-select-btn").click()
        page.keyboard.press("Escape")
        brand = page.locator("#edit-brand")
        brand.fill("Marka B")
        expect(page.locator("#save-state")).to_have_text("Kaydedilmemiş değişiklikler")
        brand.press("Enter")
        brand.press("ArrowRight")
        expect(page.locator("#business-name")).to_have_text("Mağaza 0")
        assert actions == []
        page.locator("#edit-main-category").fill("Alışveriş")
        page.locator("#edit-sub-category").fill("Dekorasyon")
        if chosen_state:
            page.locator("#edit-poi-state").select_option(chosen_state)
            page.locator("#edit-poi-state").press("ArrowRight")
            assert actions == []  # Seçim alanındaki ok tuşu kayıt ilerletmez.
            page.locator("#edit-poi-state").select_option(chosen_state)
        screenshot = os.environ.get("LABELEDIT_UI_SCREENSHOT")
        if screenshot:
            expect(page.locator("#screenshot-img")).to_have_class("loaded")
            page.screenshot(path=screenshot, animations="disabled")
        page.locator("#keep-btn").click()
        expect(page.locator("#business-name")).to_have_text("Mağaza 1")
        expect(page.locator("#edit-poi-state")).to_have_value("CLOSED_TEMPORARILY")
        expect(page.locator('#edit-poi-state')).not_to_be_visible()
        expect(page.locator('#poi-state-display')).to_have_text('Geçici olarak kapalı')
        expect(page.locator("#save-state")).to_have_text("Son işlem veritabanına kaydedildi.")
        assert map_requests["/map.svg?v=1"] == 1  # Önceden yüklenen görsel ikinci kez indirilmez.
        expected = {"brand": "Marka B", "main_category": "Alışveriş", "sub_category": "Dekorasyon"}
        if chosen_state:
            expected['poi_state'] = chosen_state
        assert actions[0]["fields"] == expected
        page.locator("#back-btn").click()
        expect(page.locator("#business-name")).to_have_text("Mağaza 0")
        expect(brand).to_have_value("Marka B")
        expect(page.locator("#edit-poi-state")).to_have_value(chosen_state or initial_selection)
        expect(page.locator('#edit-poi-state')).not_to_be_visible()
        page.locator("#skip-btn").click()
        expect(page.locator("#business-name")).to_have_text("Mağaza 1")
        assert next_index == 2   # Geri dönüşte sıradaki kayıt kaybolmaz.
        assert actions[-1]["fields"] == {}
        page.set_viewport_size({"width": 390, "height": 844})
        assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
        expect(page.locator("#keep-btn")).to_be_in_viewport()
        browser.close()
