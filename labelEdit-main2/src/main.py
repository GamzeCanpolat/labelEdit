"""
Proje V1 — FastAPI Ana Uygulama
REST API + static file serving.

Özellikler:
  • Çoklu Çalışma Ortamları (Workspaces): Kullanıcı istediği Excel (.xlsx, .xlsm, .csv)
    dosyasını yükleyip dinamik bir çalışma alanı açabilir.
  • SQLite Tabanlı Çalışma Tablosu: Tüm etiketleme ve düzenleme doğrudan SQLite üzerinden
    yapılır; kaynak Excel kilitlenmez veya bozulmaz.
  • Dinamik Sütun Eşleştirme: Excel'deki sütun adları ne olursa olsun (A, B vs.),
    Google Maps URL, Mekan Adı, Kategori ve Kontrol rolleri arayüzden eşleştirilir.
  • Sütun Ekleme: Çalışma ortamına anında yeni sütunlar (Kategori, Notlar vb.) eklenebilir.
  • Excel Dışa Aktarma: Güncel SQLite tablosu temiz bir Excel dosyası olarak indirilir.
  • Eş zamanlı Playwright CaptureEngine ile hızlı screenshot ön yükleme (prefetch).
"""
import logging
import os
import sys
import tempfile
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from typing import Optional, Dict, Any, List

import uvicorn
from fastapi import FastAPI, HTTPException, UploadFile, File, Form
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.background import BackgroundTask
from pydantic import BaseModel, Field

# src/ klasörünü path'e ekle
sys.path.insert(0, str(Path(__file__).parent))

from config import SCREENSHOTS_DIR, STATIC_DIR, HOST, PORT, BUFFER_SIZE, list_input_files
from data_manager import DataManager
from data_model import ColumnDetectionError
from edit_data_manager import EditDataManager, export_excel
from excel_schema import scan_input_dir
from session_manager import SessionManager, current_engine
from screenshot_worker import ScreenshotWorker, ScreenshotItem
from workspace_manager import workspace_mgr

# ── Loglama ──────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger(__name__)


# ── Uygulama Durumu (Singleton) ───────────────────────────────────────────────
class AppState:
    # -- Çalışma Ortamı (Workspace)
    active_workspace_id: Optional[str] = None

    # -- Etiketleme Akışı
    data_manager: Optional[DataManager] = None
    session_manager: Optional[SessionManager] = None
    screenshot_worker: Optional[ScreenshotWorker] = None
    current_position: int = 0
    current_item: Optional[ScreenshotItem] = None
    is_started: bool = False

    # -- Düzenle Akışı
    edit_data_manager: Optional[EditDataManager] = None
    edit_session_manager: Optional[SessionManager] = None
    edit_screenshot_worker: Optional[ScreenshotWorker] = None
    edit_current_position: int = 0
    edit_current_item: Optional[ScreenshotItem] = None
    edit_is_started: bool = False


state = AppState()


async def _stop_label_flow(close_data: bool) -> None:
    if state.screenshot_worker:
        await state.screenshot_worker.stop()
        state.screenshot_worker = None
    if state.session_manager:
        await run_in_threadpool(state.session_manager.stop)
        state.session_manager = None
    if close_data and state.data_manager:
        await run_in_threadpool(state.data_manager.close)
        state.data_manager = None
    state.is_started = False


async def _stop_edit_flow(close_data: bool) -> None:
    if state.edit_screenshot_worker:
        await state.edit_screenshot_worker.stop()
        state.edit_screenshot_worker = None
    if state.edit_session_manager:
        await run_in_threadpool(state.edit_session_manager.stop)
        state.edit_session_manager = None
    if state.edit_data_manager:
        state.edit_data_manager.flush()
        if close_data:
            state.edit_data_manager.close()
            state.edit_data_manager = None
    state.edit_is_started = False


# ── Lifespan (startup/shutdown) ───────────────────────────────────────────────
@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("Uygulama başlatılıyor...")
    # Başlangıçta kayıtlı workspace yoksa ve input/ içinde hazır Excel varsa otomatik içe aktar
    try:
        existing = await run_in_threadpool(workspace_mgr.list_workspaces)
        if not existing:
            files = list_input_files()
            if files:
                first = files[0]
                logger.info(f"Mevcut Excel dosyası çalışma ortamı olarak aktarılıyor: {first.name}")
                await run_in_threadpool(workspace_mgr.create_workspace, first, first.stem)
    except Exception as e:
        logger.warning(f"Başlangıç workspace kontrolü uyarısı: {e}")

    yield
    await _stop_label_flow(close_data=True)
    await _stop_edit_flow(close_data=True)
    logger.info("Uygulama kapatıldı.")


# ── FastAPI ───────────────────────────────────────────────────────────────────
app = FastAPI(
    title="Proje V1 — Google Maps Etiketleme",
    version="2.1.0",
    lifespan=lifespan,
)


@app.middleware("http")
async def disable_ui_cache(request, call_next):
    """Geliştirme sırasında eski arayüz dosyalarının tarayıcıda kalmasını önler."""
    response = await call_next(request)
    if request.url.path in ("/", "/edit", "/islem", "/label") or request.url.path.startswith("/static/"):
        response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
        response.headers["Pragma"] = "no-cache"
        response.headers["Expires"] = "0"
    return response


# ── Pydantic Modeller ─────────────────────────────────────────────────────────
class LabelRequest(BaseModel):
    row_index: int
    category: str
    extra_fields: Optional[Dict[str, str]] = None


class SessionStartRequest(BaseModel):
    start_index: int = Field(default=0, ge=0)
    source_file: Optional[str] = None
    workspace_id: Optional[str] = None


class SessionStartResponse(BaseModel):
    status: str
    total: int
    pending: int
    buffer_size: int
    start_index: int
    source_file: Optional[str] = None
    workspace_id: Optional[str] = None
    columns: Optional[Dict[str, str]] = None
    warnings: list[str] = []


class EditActionRequest(BaseModel):
    row_index: int
    action: str   # "delete" | "keep" | "skip"


class EditUndoRequest(BaseModel):
    row_index: int


class ColumnMappingUpdateRequest(BaseModel):
    mapping: Dict[str, str]


class ColumnAddRequest(BaseModel):
    name: str
    default_value: Optional[str] = ""


class SheetChangeRequest(BaseModel):
    sheet_name: str


def _item_response(item: ScreenshotItem, worker: ScreenshotWorker) -> dict:
    resp = {
        "status": "ok",
        "row_index": item.row_index,
        "name": item.name,
        "url": item.url,
        "position": item.position,
        "total": item.total,
        "queue_size": worker.queue_size(),
        "error": item.error,
        "from_cache": item.from_cache,
    }
    if item.file_path and Path(item.file_path).exists():
        resp["file_url"] = f"/screenshots/{Path(item.file_path).name}"
    else:
        resp["file_url"] = None
    return resp


def _cleanup_file(file_path: str) -> None:
    p = Path(file_path)
    if p.exists():
        p.unlink(missing_ok=True)


# ── Çalışma Ortamı (Workspace) API Endpoint'leri ──────────────────────────────

@app.get("/api/workspaces")
async def list_workspaces():
    """Tüm çalışma ortamlarını, satır sayılarını ve durumlarını listeler."""
    return JSONResponse(await run_in_threadpool(workspace_mgr.list_workspaces))


@app.get("/api/workspaces/active")
async def get_active_workspace():
    """Şu anda aktif olan çalışma ortamını döndürür."""
    active_id = await run_in_threadpool(workspace_mgr.get_active_workspace_id)
    if not active_id:
        return JSONResponse({"active": None})
    ws = await run_in_threadpool(workspace_mgr.get_workspace, active_id)
    return JSONResponse({"active": ws})


@app.post("/api/workspaces/upload")
async def upload_workspace(
    file: UploadFile = File(...),
    name: Optional[str] = Form(None),
    sheet_name: Optional[str] = Form(None),
):
    """
    Kullanıcının seçtiği Excel (.xlsx, .xlsm, .csv) dosyasını yükler,
    SQLite çalışma tablosunu açar ve önerilen sütun eşleştirmesini döndürür.
    """
    suffix = Path(file.filename or "").suffix.lower()
    if suffix not in (".xlsx", ".xlsm", ".csv"):
        raise HTTPException(status_code=400, detail="Yalnızca .xlsx, .xlsm ve .csv dosyaları desteklenir.")

    fd, tmp_path_str = tempfile.mkstemp(suffix=suffix)
    os.close(fd)
    tmp_path = Path(tmp_path_str)

    try:
        content = await file.read()
        tmp_path.write_bytes(content)

        ws_name = name or Path(file.filename).stem
        ws_info = await run_in_threadpool(
            workspace_mgr.create_workspace,
            tmp_path,
            name=ws_name,
            sheet_name=sheet_name,
        )
        return JSONResponse(ws_info)
    except Exception as e:
        logger.error(f"Workspace yükleme hatası: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Yükleme hatası: {str(e)}")
    finally:
        _cleanup_file(str(tmp_path))


@app.get("/api/workspaces/{ws_id}")
async def get_workspace_detail(ws_id: str):
    """Belirli bir çalışma ortamının tüm detaylarını ve sütunlarını döndürür."""
    ws = await run_in_threadpool(workspace_mgr.get_workspace, ws_id)
    if not ws:
        raise HTTPException(status_code=404, detail="Çalışma ortamı bulunamadı.")
    return JSONResponse(ws)


@app.post("/api/workspaces/{ws_id}/activate")
async def activate_workspace(ws_id: str):
    """Seçilen çalışma ortamını aktif yapar."""
    ws = await run_in_threadpool(workspace_mgr.get_workspace, ws_id)
    if not ws:
        raise HTTPException(status_code=404, detail="Çalışma ortamı bulunamadı.")
    await run_in_threadpool(workspace_mgr.set_active_workspace_id, ws_id)
    state.active_workspace_id = ws_id
    return JSONResponse({"status": "activated", "workspace": ws})


@app.post("/api/workspaces/{ws_id}/mapping")
async def update_workspace_mapping(ws_id: str, request: ColumnMappingUpdateRequest):
    """Dinamik sütun eşleştirmesini kaydeder (A sütununu Google Maps URL'ye vb. eşler)."""
    try:
        updated = await run_in_threadpool(workspace_mgr.update_column_mapping, ws_id, request.mapping)
        return JSONResponse({"status": "ok", "workspace": updated})
    except Exception as e:
        logger.error(f"Mapping güncelleme hatası: {e}")
        raise HTTPException(status_code=400, detail=str(e))


@app.post("/api/workspaces/{ws_id}/columns/add")
async def add_workspace_column(ws_id: str, request: ColumnAddRequest):
    """Çalışma ortamının SQLite tablosuna yeni bir sütun ekler (örn: 'Kategori', 'Notlar')."""
    try:
        updated = await run_in_threadpool(workspace_mgr.add_column, ws_id, request.name, request.default_value or "")
        return JSONResponse({"status": "ok", "workspace": updated})
    except Exception as e:
        logger.error(f"Sütun ekleme hatası: {e}")
        raise HTTPException(status_code=400, detail=str(e))


@app.post("/api/workspaces/{ws_id}/change-sheet")
async def change_workspace_sheet(ws_id: str, request: SheetChangeRequest):
    """Çalışma ortamının verilerini Excel dosyasındaki başka bir sayfadan yeniden yükler."""
    try:
        updated = await run_in_threadpool(workspace_mgr.reload_sheet, ws_id, request.sheet_name)
        return JSONResponse({"status": "ok", "workspace": updated})
    except Exception as e:
        logger.error(f"Sayfa değiştirme hatası: {e}")
        raise HTTPException(status_code=400, detail=str(e))


@app.delete("/api/workspaces/{ws_id}")
async def delete_workspace(ws_id: str):
    """Çalışma ortamını ve ilişkili SQLite veritabanını siler."""
    try:
        success = await run_in_threadpool(workspace_mgr.delete_workspace, ws_id)
        if state.active_workspace_id == ws_id:
            state.active_workspace_id = None
        return JSONResponse({"status": "deleted", "success": success})
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/workspaces/{ws_id}/export")
def export_workspace_excel(ws_id: str):
    """SQLite tablosundaki güncel verileri temiz bir Excel dosyası olarak indirir."""
    ws = workspace_mgr.get_workspace(ws_id)
    if not ws:
        raise HTTPException(status_code=404, detail="Çalışma ortamı bulunamadı.")

    fd, tmp = tempfile.mkstemp(suffix=".xlsx")
    os.close(fd)
    try:
        workspace_mgr.export_to_excel(ws_id, Path(tmp))
        stem = Path(ws["source_file"]).stem
        filename = f"{stem}_guncel_{datetime.now():%Y%m%d_%H%M%S}.xlsx"
        return FileResponse(
            tmp,
            filename=filename,
            media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            background=BackgroundTask(_cleanup_file, tmp),
        )
    except Exception as e:
        _cleanup_file(tmp)
        logger.error(f"Excel export hatası: {e}")
        raise HTTPException(status_code=500, detail=str(e))


# ── API Endpoint'leri — Etiketleme Akışı ──────────────────────────────────────

@app.post("/api/session/start", response_model=SessionStartResponse)
async def start_session(request: Optional[SessionStartRequest] = None):
    """
    Oturumu başlatır:
    1. Varsa seçilen çalışma ortamını (SQLite) yükler.
    2. Paylaşılan CaptureEngine'i başlatır.
    3. ScreenshotWorker önündeki kayıtları eş zamanlı hazırlar.
    """
    request = request or SessionStartRequest()
    await _stop_label_flow(close_data=True)

    try:
        start_index = request.start_index
        ws_id = request.workspace_id

        if ws_id:
            ws = await run_in_threadpool(workspace_mgr.get_workspace, ws_id)
            if not ws:
                raise HTTPException(status_code=404, detail=f"Çalışma ortamı bulunamadı: {ws_id}")

            state.active_workspace_id = ws_id
            all_records = await run_in_threadpool(workspace_mgr.get_place_records, ws_id, start_index)
            pending_count = len(all_records)
            total_count = ws["stats"]["total"]

            if pending_count == 0:
                raise HTTPException(
                    status_code=404,
                    detail=f"{start_index}. indeksten itibaren işlenecek kayıt bulunamadı.",
                )

            state.session_manager = SessionManager()
            await run_in_threadpool(state.session_manager.start)
            state.screenshot_worker = ScreenshotWorker(state.session_manager)
            await state.screenshot_worker.start(all_records)

            state.current_position = 0
            state.is_started = True
            logger.info(
                f"Workspace oturumu başlatıldı ({ws['name']}). Başlangıç indeksi: {start_index}, "
                f"Toplam: {total_count}, Bekleyen: {pending_count}"
            )
            return SessionStartResponse(
                status="started",
                total=total_count,
                pending=pending_count,
                buffer_size=BUFFER_SIZE,
                start_index=start_index,
                source_file=ws["source_file"],
                workspace_id=ws_id,
                columns=ws["column_mapping"],
                warnings=[],
            )

        # Geriye dönük uyumluluk: DataManager fallback
        state.active_workspace_id = None
        state.data_manager = await run_in_threadpool(DataManager, request.source_file)
        total_count = state.data_manager.get_total_count()

        all_records = state.data_manager.get_place_records(start_index)
        pending_count = len(all_records)
        if pending_count == 0:
            raise HTTPException(
                status_code=404,
                detail=f"{start_index}. indeksten itibaren işlenecek kayıt bulunamadı.",
            )

        state.session_manager = SessionManager()
        await run_in_threadpool(state.session_manager.start)
        state.screenshot_worker = ScreenshotWorker(state.session_manager)
        await state.screenshot_worker.start(all_records)

        state.current_position = 0
        state.is_started = True
        schema = state.data_manager.schema
        return SessionStartResponse(
            status="started",
            total=total_count,
            pending=pending_count,
            buffer_size=BUFFER_SIZE,
            start_index=start_index,
            source_file=schema.file,
            columns={k: m.column for k, m in schema.matches.items()},
            warnings=schema.warnings,
        )

    except (FileNotFoundError, ColumnDetectionError, ValueError) as e:
        raise HTTPException(status_code=422, detail=str(e))
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Oturum başlatma hatası: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Başlatma hatası: {str(e)}")


@app.get("/api/screenshot/next")
async def get_next_screenshot():
    """Sıradaki screenshot'ı döndürür (hazırsa anında)."""
    if not state.is_started or not state.screenshot_worker:
        raise HTTPException(status_code=400, detail="Oturum başlatılmamış. /api/session/start çağrın.")

    if state.screenshot_worker.is_empty():
        return JSONResponse({"status": "completed", "message": "Tüm kayıtlar işlendi!"})

    item = await state.screenshot_worker.get_next()
    if item is None:
        raise HTTPException(status_code=504, detail="Screenshot alınamadı (timeout).")

    state.current_item = item
    state.current_position += 1
    return JSONResponse(_item_response(item, state.screenshot_worker))


@app.post("/api/label")
async def save_label(request: LabelRequest):
    """Kullanıcının girdiği kategoriyi doğrudan SQLite tablosuna kaydeder."""
    has_category = bool(request.category and request.category.strip())
    has_extra = bool(
        request.extra_fields
        and any(v.strip() for v in request.extra_fields.values() if v)
    )
    if not has_category and not has_extra:
        raise HTTPException(status_code=422, detail="En az bir alan doldurulmalıdır.")

    try:
        if state.active_workspace_id:
            await run_in_threadpool(
                workspace_mgr.update_label,
                state.active_workspace_id,
                request.row_index,
                request.category.strip() if request.category else "",
                extra_fields=request.extra_fields,
            )
            return JSONResponse({"status": "saved", "row_index": request.row_index,
                                 "category": request.category.strip() if request.category else ""})

        if not state.data_manager:
            raise HTTPException(status_code=400, detail="DataManager veya Çalışma Ortamı hazır değil.")

        state.data_manager.save_category(
            request.row_index,
            request.category.strip() if request.category else "",
            extra_fields=request.extra_fields,
        )
        return JSONResponse({"status": "saved", "row_index": request.row_index,
                             "category": request.category.strip() if request.category else ""})
    except Exception as e:
        logger.error(f"Kaydetme hatası — row:{request.row_index}: {e}")
        raise HTTPException(status_code=500, detail=f"Kaydetme hatası: {str(e)}")


@app.get("/api/status")
async def get_status():
    """Worker ve buffer durumunu döndürür."""
    if not state.is_started:
        return JSONResponse({"status": "not_started"})

    stats = state.screenshot_worker.get_stats() if state.screenshot_worker else {}
    pending = 0
    if state.active_workspace_id:
        ws_stats = workspace_mgr.get_workspace_stats(state.active_workspace_id)
        pending = ws_stats.get("pending", 0)
    elif state.data_manager:
        pending = state.data_manager.get_pending_count()

    return JSONResponse({
        "status": "running",
        "current_position": state.current_position,
        "pending": pending,
        "worker_stats": stats,
    })


@app.post("/api/session/stop")
async def stop_session():
    """Oturumu durdurur ve kaynakları serbest bırakır."""
    await _stop_label_flow(close_data=True)
    return JSONResponse({"status": "stopped"})


# ── Analiz / izleme uç noktaları ─────────────────────────────────────────────

@app.get("/api/input/files")
async def list_input_analysis():
    """input/ klasöründeki dosyaların sayfa, kolon ve tespit edilen rol bilgileri."""
    return JSONResponse(await run_in_threadpool(scan_input_dir))


@app.get("/api/capture/stats")
async def capture_stats():
    engine = current_engine()
    if not engine:
        return JSONResponse({"status": "not_running"})
    return JSONResponse({"status": "running", "engine": engine.stats(), "store": engine.store.stats()})


@app.get("/api/capture/failures")
async def capture_failures(limit: int = 100):
    engine = current_engine()
    if not engine:
        return JSONResponse({"status": "not_running", "failures": []})
    return JSONResponse({"status": "running", "failures": engine.store.failures(limit)})


# ── Screenshot dosyalarını serve et ──────────────────────────────────────────
_MEDIA = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg"}


@app.get("/screenshots/{filename}")
async def serve_screenshot(filename: str):
    file_path = (SCREENSHOTS_DIR / filename).resolve()
    if file_path.parent != SCREENSHOTS_DIR.resolve() or not file_path.is_file():
        raise HTTPException(status_code=404, detail="Screenshot bulunamadı.")
    return FileResponse(str(file_path), media_type=_MEDIA.get(file_path.suffix.lower(), "application/octet-stream"))


# ── Static files + Ana sayfa ─────────────────────────────────────────────────
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


@app.get("/")
async def root():
    return FileResponse(str(STATIC_DIR / "index.html"))


@app.get("/islem")
async def islem_page():
    return FileResponse(str(STATIC_DIR / "edit.html"))


@app.get("/edit")
async def edit_page():
    return FileResponse(str(STATIC_DIR / "edit.html"))


@app.get("/label")
async def label_page():
    return FileResponse(str(STATIC_DIR / "label.html"))


# ── Düzenle Akışı API Endpoint'leri ──────────────────────────────────────────

@app.post("/api/edit/session/start", response_model=SessionStartResponse)
async def edit_start_session(request: Optional[SessionStartRequest] = None):
    request = request or SessionStartRequest()
    await _stop_edit_flow(close_data=True)

    try:
        start_index = request.start_index
        ws_id = request.workspace_id

        if ws_id:
            ws = await run_in_threadpool(workspace_mgr.get_workspace, ws_id)
            if not ws:
                raise HTTPException(status_code=404, detail=f"Çalışma ortamı bulunamadı: {ws_id}")

            state.active_workspace_id = ws_id
            all_records = await run_in_threadpool(workspace_mgr.get_place_records, ws_id, start_index)
            pending_count = len(all_records)
            if pending_count == 0:
                raise HTTPException(status_code=404, detail="Kayıt bulunamadı.")

            state.edit_session_manager = SessionManager()
            await run_in_threadpool(state.edit_session_manager.start)
            state.edit_screenshot_worker = ScreenshotWorker(state.edit_session_manager)
            await state.edit_screenshot_worker.start(all_records)

            state.edit_current_position = 0
            state.edit_is_started = True

            return SessionStartResponse(
                status="started",
                total=ws["stats"]["total"],
                pending=pending_count,
                buffer_size=BUFFER_SIZE,
                start_index=start_index,
                source_file=ws["source_file"],
                workspace_id=ws_id,
                columns=ws["column_mapping"],
                warnings=[],
            )

        # Fallback to EditDataManager
        state.active_workspace_id = None
        state.edit_data_manager = await run_in_threadpool(EditDataManager, request.source_file)
        all_records = await run_in_threadpool(state.edit_data_manager.get_place_records, start_index)
        pending_count = len(all_records)
        if pending_count == 0:
            raise HTTPException(status_code=404, detail="Kayıt bulunamadı.")

        state.edit_session_manager = SessionManager()
        await run_in_threadpool(state.edit_session_manager.start)
        state.edit_screenshot_worker = ScreenshotWorker(state.edit_session_manager)
        await state.edit_screenshot_worker.start(all_records)

        state.edit_current_position = 0
        state.edit_is_started = True
        schema = state.edit_data_manager.schema
        return SessionStartResponse(
            status="started",
            total=state.edit_data_manager.get_total_count(),
            pending=pending_count,
            buffer_size=BUFFER_SIZE,
            start_index=start_index,
            source_file=state.edit_data_manager.source_path.name,
            columns={k: m.column for k, m in schema.matches.items()},
            warnings=schema.warnings,
        )
    except HTTPException:
        raise
    except FileNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except (ColumnDetectionError, ValueError) as e:
        raise HTTPException(status_code=422, detail=str(e))
    except Exception as e:
        logger.error(f"Düzenle oturumu başlatma hatası: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/edit/screenshot/next")
async def edit_get_next_screenshot():
    worker = state.edit_screenshot_worker
    if not state.edit_is_started or not worker:
        raise HTTPException(status_code=400, detail="Oturum başlatılmamış.")
    if worker.is_empty():
        return JSONResponse({"status": "completed", "message": "Tüm kayıtlar işlendi!"})
    item = await worker.get_next()
    if item is None:
        raise HTTPException(status_code=504, detail="Screenshot alınamadı.")
    state.edit_current_item = item
    state.edit_current_position += 1
    rec = worker.peek_record(item.position)
    nxt = worker.peek_record(item.position + 1)
    resp = _item_response(item, worker)
    resp["kontrol_value"] = rec.status_value if rec else ""
    resp["next_name"] = nxt.name if nxt else None
    return JSONResponse(resp)


@app.post("/api/edit/action")
async def edit_action(request: EditActionRequest):
    action = request.action.strip().lower()
    if action not in ("delete", "keep", "skip"):
        raise HTTPException(status_code=422, detail="Geçersiz action.")

    try:
        if state.active_workspace_id:
            await run_in_threadpool(workspace_mgr.update_action, state.active_workspace_id, request.row_index, action)
            status_name = "deleted" if action == "delete" else ("skipped" if action == "skip" else "kept")
            return JSONResponse({"status": status_name, "row_index": request.row_index})

        if not state.edit_data_manager:
            raise HTTPException(status_code=400, detail="EditDataManager veya Çalışma Ortamı hazır değil.")

        if action == "delete":
            state.edit_data_manager.delete_row(request.row_index)
            return JSONResponse({"status": "deleted", "row_index": request.row_index})
        elif action == "skip":
            state.edit_data_manager.skip_row(request.row_index)
            return JSONResponse({"status": "skipped", "row_index": request.row_index})
        else:
            state.edit_data_manager.keep_row(request.row_index)
            return JSONResponse({"status": "kept", "row_index": request.row_index})
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/edit/action/undo")
async def edit_undo_action(request: EditUndoRequest):
    try:
        if state.active_workspace_id:
            undone = await run_in_threadpool(workspace_mgr.undo_action, state.active_workspace_id, request.row_index)
            return JSONResponse({"status": "ok", "undone_action": undone, "row_index": request.row_index})

        if not state.edit_data_manager:
            raise HTTPException(status_code=400, detail="EditDataManager veya Çalışma Ortamı hazır değil.")

        undone = state.edit_data_manager.undo_action(request.row_index)
        return JSONResponse({"status": "ok", "undone_action": undone, "row_index": request.row_index})
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/edit/session/stop")
async def edit_stop_session():
    await _stop_edit_flow(close_data=False)
    return JSONResponse({"status": "stopped"})


@app.get("/api/edit/status")
async def get_edit_status():
    if not state.edit_is_started:
        return JSONResponse({"status": "not_started"})

    stats = state.edit_screenshot_worker.get_stats() if state.edit_screenshot_worker else {}
    pending = 0
    if state.active_workspace_id:
        ws_stats = workspace_mgr.get_workspace_stats(state.active_workspace_id)
        pending = ws_stats.get("pending", 0)
    elif state.edit_data_manager:
        pending = state.edit_data_manager.get_pending_count()

    return JSONResponse({
        "status": "running",
        "current_position": state.edit_current_position,
        "pending": pending,
        "worker_stats": stats,
    })


@app.get("/api/edit/export")
def edit_export_excel(source_file: Optional[str] = None, workspace_id: Optional[str] = None):
    """Veritabanındaki güncel hali (silinenler hariç) Excel olarak indirir."""
    ws_id = workspace_id or (state.active_workspace_id if not source_file and not state.edit_data_manager else None)
    fd, tmp = tempfile.mkstemp(suffix=".xlsx")
    os.close(fd)

    if ws_id:
        try:
            workspace_mgr.export_to_excel(ws_id, Path(tmp))
            ws = workspace_mgr.get_workspace(ws_id)
            stem = Path(ws["source_file"]).stem if ws else "workspace"
            filename = f"{stem}_guncel_{datetime.now():%Y%m%d_%H%M%S}.xlsx"
            return FileResponse(
                tmp,
                filename=filename,
                media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                background=BackgroundTask(_cleanup_file, tmp),
            )
        except Exception as e:
            _cleanup_file(tmp)
            raise HTTPException(status_code=500, detail=str(e))

    if source_file is None and state.edit_data_manager:
        source_file = state.edit_data_manager.source_path.name

    try:
        export_excel(Path(tmp), source_file)
    except FileNotFoundError as e:
        _cleanup_file(tmp)
        raise HTTPException(status_code=404, detail=str(e))
    except Exception as e:
        _cleanup_file(tmp)
        raise HTTPException(status_code=500, detail=str(e))

    stem = Path(source_file).stem if source_file else "duzenle"
    filename = f"{stem}_guncel_{datetime.now():%Y%m%d_%H%M%S}.xlsx"
    return FileResponse(
        tmp,
        filename=filename,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        background=BackgroundTask(_cleanup_file, tmp),
    )


# ── Giriş noktası ─────────────────────────────────────────────────────────────
if __name__ == "__main__":
    uvicorn.run(
        "main:app",
        host=HOST,
        port=PORT,
        reload=False,
        log_level="info",
    )
