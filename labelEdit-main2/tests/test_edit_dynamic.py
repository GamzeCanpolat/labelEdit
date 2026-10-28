"""Farklı dosyalar, canlı buffer ve beklerken kaydın korunması."""
import asyncio
from collections import Counter
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from conftest import maps_url, write_xlsx
from data_model import CaptureOutcome, PlaceRecord
from edit_data_manager import EditDataManager
from screenshot_worker import ScreenshotItem, ScreenshotWorker
from workspace_manager import workspace_mgr


def test_slow_capture_keeps_order_and_reports_only_successful_maps(tmp_path):
    async def run():
        gate = asyncio.Event()
        called = Counter()
        path = tmp_path / "map.jpg"
        path.write_bytes(b"test")

        async def capture(rec):
            called[rec.row_index] += 1
            if rec.row_index == 0:
                await gate.wait()
            if rec.row_index == 2:
                return CaptureOutcome(rec.place_key, rec.url, error="Harita bulunamadı")
            return CaptureOutcome(rec.place_key, rec.url, file_path=str(path))

        worker = ScreenshotWorker(SimpleNamespace(engine=SimpleNamespace(capture=capture)), window=3)
        records = [PlaceRecord.build(i, maps_url(i), f"Yer {i}") for i in range(3)]
        for i, rec in enumerate(records):
            rec.position, rec.total = i, 3
        await worker.start(records)
        assert await worker.get_next(wait_timeout=.02, consume_on_timeout=False) is None
        assert worker.peek_record(0).row_index == 0 and worker.queue_size() == 0
        assert worker.buffer_status() == {"ready": 0, "preparing": 1, "failed": 1, "capacity": 3}
        pending = asyncio.create_task(worker.get_next(wait_timeout=2, consume_on_timeout=False))
        await asyncio.sleep(0)
        pending.cancel()
        with pytest.raises(asyncio.CancelledError):
            await pending
        gate.set()
        first = await worker.get_next(wait_timeout=1, consume_on_timeout=False)
        assert first.row_index == 0
        assert worker.queue_size() == 1  # Başarısız üçüncü görüntü hazır sayılmaz.
        assert [p["row_index"] for p in worker.ready_previews()] == [1]
        assert (await worker.get_next()).row_index == 1
        assert (await worker.get_next()).error == "Harita bulunamadı"
        assert worker.is_empty() and called == {0: 1, 1: 1, 2: 1}
        await worker.stop()
    asyncio.run(run())


def test_header_offset_aliases_and_manual_mapping(input_dir):
    path = write_xlsx(input_dir / "dynamic_report.xlsx", {
        "Özet": [("Rapor", "Ekim"), ("Toplam", 2)],
        "Veriler": [("Hazırlayan", "Örnek"), (None,),
                    ("İşletme Adı", "Harita Linki", "Markaİsmi", "AnaKategori", "AltKategori", "POL_STATE", "Özel Alan"),
                    ("Yer", maps_url(1), "A", "Ev", "Mobilya", "OPERATIONAL", "B")],
    })
    ws = workspace_mgr.create_workspace(path, "Farklı başlıklar")
    assert ws["sheet_name"] == "Veriler"
    assert "Hazırlayan" not in ws["columns"]
    assert workspace_mgr.get_edit_fields(ws["id"], 0) == {
        "brand": "A", "main_category": "Ev", "sub_category": "Mobilya", "poi_state": "OPERATIONAL"}
    workspace_mgr.update_column_mapping(ws["id"], {"brand": "Özel Alan"})
    workspace_mgr.update_action(ws["id"], 0, "keep", {"brand": "C"})
    assert workspace_mgr.get_edit_fields(ws["id"], 0)["brand"] == "C"
    workspace_mgr.undo_action(ws["id"], 0)
    workspace_mgr.update_column_mapping(ws["id"], {"brand": "Markaİsmi"})
    assert workspace_mgr.get_edit_fields(ws["id"], 0)["brand"] == "A"
    with pytest.raises(ValueError):
        workspace_mgr.update_column_mapping(ws["id"], {"sub_category": "Hayali sütun"})


def test_headerless_tab_csv_and_optional_control_column(input_dir):
    csv_path = input_dir / "headerless.csv"
    csv_path.write_text(f"Yer A\t{maps_url(1)}\nYer B\t{maps_url(2)}\n", encoding="utf-8-sig")
    ws = workspace_mgr.create_workspace(csv_path, "Başlıksız CSV")
    assert ws["stats"]["total"] == 2
    assert len(workspace_mgr.get_place_records(ws["id"], require_url=True)) == 2
    path = write_xlsx(input_dir / "no_control.xlsx", {"Veriler": [
        ("name", "Google Maps URL"), ("Yer", maps_url(3))]})
    dm = EditDataManager(path.name, require_status=False)
    assert dm.get_pending_count() == 1
    assert dm.get_edit_fields(0)["brand"] == ""
    dm.close()


def test_retry_next_does_not_skip_and_buffer_previews_are_versioned(input_dir, tmp_path, monkeypatch):
    import main
    path = write_xlsx(input_dir / "retry.xlsx", {"S": [
        ("name", "maps"), ("A", maps_url(1)), ("B", maps_url(2))]})
    ws = workspace_mgr.create_workspace(path, "Yeniden deneme")
    shot = tmp_path / "test.jpg"
    shot.write_bytes(b"test")
    records = workspace_mgr.get_place_records(ws["id"])

    class Worker:
        next_index = 0
        def is_empty(self): return self.next_index >= 2
        async def get_next(self, **kwargs):
            rec = records[self.next_index]
            self.next_index += 1
            return ScreenshotItem(rec.row_index, rec.position, rec.url, rec.name, str(shot), 2)
        def peek_record(self, position): return records[position] if position < 2 else None
        def queue_size(self): return 2 - self.next_index
        def buffer_status(self): return {"ready": 2 - self.next_index, "preparing": 0, "capacity": 2, "failed": 0}
        def get_stats(self): return {"queue_size": self.queue_size()}
        def ready_previews(self): return [{"row_index": 1, "name": "B", "file_path": str(shot)}]
        async def stop(self): pass

    worker = Worker()
    for key, value in {"edit_workspace_id": ws["id"], "edit_is_started": True,
                       "edit_current_item": None, "edit_current_resolved": True,
                       "edit_screenshot_worker": worker}.items():
        monkeypatch.setattr(main.state, key, value)
    with TestClient(main.app) as client:
        first = client.get("/api/edit/screenshot/next").json()
        assert client.get("/api/edit/screenshot/next").json()["row_index"] == first["row_index"]
        assert worker.next_index == 1
        status = client.get("/api/edit/status").json()
        assert status["buffer"]["ready"] == 1
        assert status["previews"][0]["file_url"] == first["file_url"]
        assert "?v=" in first["file_url"]
        client.post("/api/edit/action", json={"row_index": first["row_index"], "action": "skip"})
        assert client.get("/api/edit/screenshot/next").json()["row_index"] == 1
