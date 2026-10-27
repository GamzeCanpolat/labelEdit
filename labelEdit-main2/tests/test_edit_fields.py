"""İşlem sayfası alanlarının kayıt ve dışa aktarma sözleşmeleri."""
import io

import openpyxl
import pytest
from fastapi.testclient import TestClient

from conftest import maps_url, write_xlsx
from edit_data_manager import EditDataManager, export_excel
from edit_fields import field_columns


def source(input_dir, name, with_fields=True):
    headers = ["KONTROL", "isim", "maps_url"]
    row = ["İncele", "Mağaza", maps_url(1)]
    if with_fields:
        headers += ["brand_name", "mainCategory", "Alt Kategori", "Pol State"]
        row += ["Eski Marka", "Alışveriş", "Mobilya", "CLOSED_TEMPORARILY"]
    return write_xlsx(input_dir / name, {"Mekanlar": [headers, row]})


@pytest.mark.parametrize("with_fields", [True, False])
def test_edit_fields_persist_and_export(input_dir, tmp_path, with_fields):
    p = source(input_dir, f"cleanpoi_cevre_fields_{with_fields}.xlsx", with_fields)
    dm = EditDataManager(p.name)
    row = dm.get_place_records()[0].row_index
    assert dm.get_edit_fields(row)["brand"] == ("Eski Marka" if with_fields else "")
    fields = {"brand": "Yeni Marka", "main_category": "Ev", "sub_category": "Dekorasyon"}
    dm.keep_row(row, fields)
    dm.undo_action(row)
    assert dm.get_edit_fields(row) == {**fields, "poi_state": "CLOSED_TEMPORARILY" if with_fields else ""}
    dm.close()
    dm = EditDataManager(p.name)
    assert dm.get_edit_fields(row)["brand"] == "Yeni Marka"
    dm.close()
    out = tmp_path / "export.xlsx"
    export_excel(out, p.name)
    sheet = openpyxl.load_workbook(out).active
    columns = [c.value for c in sheet[1]]
    mapping = field_columns(columns)
    assert sheet.cell(2, columns.index(mapping["brand"]) + 1).value == "Yeni Marka"
    assert sheet.cell(2, columns.index(mapping["sub_category"]) + 1).value == "Dekorasyon"
    if with_fields:
        assert not ({"Marka", "Ana Kategori"} & set(columns))


def test_workspace_edit_api_keeps_fields_and_preserves_poi_state(input_dir, tmp_path, monkeypatch):
    import main
    from workspace_manager import workspace_mgr

    p = source(input_dir, "workspace_edit_fields.xlsx")
    ws = workspace_mgr.create_workspace(p, "İşlem alanları")
    ws_id = ws["id"]
    workspace_mgr.update_column_mapping(ws_id, {"status": "Pol State"})
    monkeypatch.setattr(main.state, "edit_workspace_id", ws_id)
    # Etiketleme sayfasının aktif ortamı farklı olsa da işlem kaydı doğru yere gider.
    monkeypatch.setattr(main.state, "active_workspace_id", "another-workspace")
    with TestClient(main.app) as client:
        options = client.get("/api/edit/field-options")
        assert options.json()["brand"] == ["Eski Marka"]
        fields = {"brand": 'Marka "A"', "main_category": "Ev", "sub_category": "Dekorasyon"}
        response = client.post("/api/edit/action", json={"row_index": 0, "action": "keep", "fields": fields})
        assert response.status_code == 200, response.text
        assert workspace_mgr.get_edit_fields(ws_id, 0) == {**fields, "poi_state": "CLOSED_TEMPORARILY"}
        assert client.post("/api/edit/action/undo", json={"row_index": 0}).status_code == 200
        assert client.post("/api/edit/action", json={"row_index": 0, "action": "skip", "fields": {"brand": "Kaydedilmemeli"}}).status_code == 200
        assert workspace_mgr.get_edit_fields(ws_id, 0)["brand"] == 'Marka "A"'
        assert client.post("/api/edit/action", json={"row_index": 0, "action": "keep", "fields": {"_status": "silindi"}}).status_code == 422
        assert client.post("/api/edit/action", json={"row_index": 999, "action": "keep", "fields": fields}).status_code == 404
        result = client.get("/api/edit/export")
        assert result.status_code == 200
        sheet = openpyxl.load_workbook(io.BytesIO(result.content)).active
        columns = [c.value for c in sheet[1]]
        assert sheet.cell(2, columns.index("brand_name") + 1).value == 'Marka "A"'


def test_workspace_adds_missing_fields(input_dir):
    from workspace_manager import workspace_mgr
    p = source(input_dir, "workspace_without_fields.xlsx", False)
    ws = workspace_mgr.create_workspace(p, "Eksik alanlar")
    workspace_mgr.update_action(ws["id"], 0, "keep", {"brand": "Yeni", "sub_category": "Alt"})
    assert workspace_mgr.get_edit_fields(ws["id"], 0)["brand"] == "Yeni"
    assert {"Marka", "Alt Kategori"} <= set(workspace_mgr.get_workspace(ws["id"])["columns"])
