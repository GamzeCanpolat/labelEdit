"""
Proje V1 — Çalışma Ortamları ve SQLite Tabanlı Veri Yönetimi (WorkspaceManager)

Kullanıcının herhangi bir Excel (.xlsx, .xlsm) veya CSV dosyasını yükleyip bağımsız bir
çalışma ortamı (workspace) oluşturmasını, sütunları dinamik eşleştirmesini (column mapping),
yeni sütunlar eklemesini (add column) ve tüm okuma/yazma/düzenleme işlemlerini doğrudan
SQLite çalışma tablosu üzerinden yapmasını sağlar.

İşlemler bittiğinde güncel SQLite tablosu temiz bir Excel dosyası olarak dışa aktarılır.
"""
from __future__ import annotations

import csv
import json
import logging
import os
import re
import sqlite3
import threading
import time
import uuid
import xml.etree.ElementTree as ET
from zipfile import ZipFile
from contextlib import contextmanager
from datetime import date, datetime, time as dtime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import openpyxl
from openpyxl.styles import Font, PatternFill, Alignment, Border, Side

from config import DATA_DIR, INPUT_DIR, EXTRA_ALLOWED_MAPS_HOSTS
from data_model import (
    ColumnRole,
    PlaceRecord,
    SheetSchema,
    clean_cell,
    parse_maps_url,
)
from excel_schema import detect_columns, detect_header_row, normalize_header, unique_columns
from edit_fields import field_columns, read_fields, read_options, write_fields

logger = logging.getLogger(__name__)

WORKSPACES_DB = DATA_DIR / "workspaces.db"
UPLOADS_DIR = DATA_DIR / "uploads"


def _safe_sql_col(name: str) -> str:
    """SQL sütun adını tırnaklar."""
    return '"' + str(name).replace('"', '""') + '"'


def _to_sql_value(v: Any) -> Any:
    if v is None:
        return None
    if isinstance(v, float) and v != v:
        return None
    if isinstance(v, datetime):
        return v.isoformat(sep=" ")
    if isinstance(v, (date, dtime)):
        return v.isoformat()
    if isinstance(v, str) and v == "":
        return None
    if hasattr(v, "item"):
        return v.item()
    return str(v) if not isinstance(v, (int, float, bool)) else v


@contextmanager
def _connection(path):
    """Transaction bittikten sonra Windows dosya kilidini de serbest bırakır."""
    conn = sqlite3.connect(path, timeout=30.0, check_same_thread=False)
    try:
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        with conn:
            yield conn
    finally:
        conn.close()


class WorkspaceManager:
    """Tüm çalışma ortamlarını, SQLite tablolarını ve sütun eşleştirmelerini yönetir."""

    def __init__(self, db_path: Path = WORKSPACES_DB) -> None:
        self.db_path = db_path
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        UPLOADS_DIR.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._init_registry()

    def _get_registry_conn(self):
        return _connection(self.db_path)

    def _init_registry(self) -> None:
        with self._lock, self._get_registry_conn() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS workspaces (
                    id TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    source_file TEXT NOT NULL,
                    stored_file_path TEXT NOT NULL,
                    db_file_path TEXT NOT NULL,
                    sheet_name TEXT,
                    total_rows INTEGER DEFAULT 0,
                    processed_rows INTEGER DEFAULT 0,
                    column_mapping TEXT NOT NULL,
                    extra_columns TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )
            # Aktif çalışma ortamını tutan ayar tablosu
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS settings (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                )
                """
            )
        self._sync_existing_sqlite_dbs()

    def _sync_existing_sqlite_dbs(self) -> None:
        """
        data/ altındaki edit_*.db SQLite dosyalarını tarar ve henüz
        kaydedilmemiş olanları workspaces tablosuna otomatik ekler.
        Böylece SQLite üzerinde var olan tüm Excel dosyaları doğrudan okunur.
        """
        with self._lock, self._get_registry_conn() as conn:
            existing_paths = {row["db_file_path"] for row in conn.execute("SELECT db_file_path FROM workspaces").fetchall()}
            now_iso = datetime.now().isoformat()

            for edit_file in sorted(DATA_DIR.glob("edit_*.db")):
                abs_path = str(edit_file.resolve())
                if abs_path in existing_paths:
                    continue

                try:
                    with _connection(edit_file) as econ:
                        econ.row_factory = sqlite3.Row
                        cur = econ.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='mekanlar'")
                        if not cur.fetchone():
                            continue

                        meta_dict = {}
                        cur_meta = econ.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='meta'")
                        if cur_meta.fetchone():
                            meta_dict = dict(econ.execute("SELECT key, value FROM meta").fetchall())

                        source_file = meta_dict.get("source_file") or (edit_file.stem.replace("edit_", "") + ".xlsx")
                        sheet_name = meta_dict.get("sheet_name") or "Mekanlar"
                        name = edit_file.stem.replace("edit_", "")

                        mapping = {}
                        schema_raw = meta_dict.get("schema")
                        if schema_raw:
                            try:
                                schema_obj = json.loads(schema_raw)
                                matches = schema_obj.get("matches", {})
                                for role, match in matches.items():
                                    if isinstance(match, dict) and "column" in match:
                                        mapping[role] = match["column"]
                            except Exception:
                                pass

                        cols = [r["name"] for r in econ.execute("PRAGMA table_info(mekanlar)").fetchall() if not r["name"].startswith("_") and r["name"] != "id"]
                        if not mapping.get("maps_url"):
                            for c in cols:
                                if "maps" in c.lower() or "url" in c.lower():
                                    mapping["maps_url"] = c
                                    break
                            for c in cols:
                                if "isim" in c.lower() or "ad" in c.lower() or "name" in c.lower():
                                    mapping["name"] = c
                                    break
                            for c in cols:
                                if "kontrol" in c.lower() or "durum" in c.lower():
                                    mapping["status"] = c
                                    break

                        total = econ.execute("SELECT COUNT(*) FROM mekanlar").fetchone()[0]
                        ws_id = "ws_" + uuid.uuid4().hex[:12]

                        conn.execute(
                            """
                            INSERT INTO workspaces (
                                id, name, source_file, stored_file_path, db_file_path,
                                sheet_name, total_rows, processed_rows, column_mapping,
                                extra_columns, created_at, updated_at
                            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                            """,
                            (
                                ws_id,
                                name,
                                source_file,
                                abs_path,
                                abs_path,
                                sheet_name,
                                total,
                                0,
                                json.dumps(mapping, ensure_ascii=False),
                                json.dumps([], ensure_ascii=False),
                                now_iso,
                                now_iso,
                            ),
                        )
                        conn.commit()
                        existing_paths.add(abs_path)
                        logger.info(f"Mevcut SQLite Excel veritabanı çalışma ortamı olarak eklendi: {name} ({total} satır)")
                except Exception as e:
                    logger.warning(f"{edit_file.name} senkronize edilirken hata: {e}")

    def get_active_workspace_id(self) -> Optional[str]:
        with self._lock, self._get_registry_conn() as conn:
            row = conn.execute("SELECT value FROM settings WHERE key='active_workspace_id'").fetchone()
            if row:
                ws = conn.execute("SELECT id FROM workspaces WHERE id=?", (row[0],)).fetchone()
                if ws:
                    return ws[0]
            # Kayıtlı herhangi bir workspace varsa ilkini seç
            first = conn.execute("SELECT id FROM workspaces ORDER BY updated_at DESC LIMIT 1").fetchone()
            return first[0] if first else None

    def set_active_workspace_id(self, ws_id: str) -> None:
        with self._lock, self._get_registry_conn() as conn:
            conn.execute("INSERT OR REPLACE INTO settings (key, value) VALUES ('active_workspace_id', ?)", (ws_id,))
            conn.commit()

    def list_workspaces(self) -> List[Dict[str, Any]]:
        with self._lock, self._get_registry_conn() as conn:
            rows = conn.execute("SELECT * FROM workspaces ORDER BY updated_at DESC").fetchall()
            active_id = self.get_active_workspace_id()
            result = []
            for r in rows:
                ws_id = r["id"]
                stats = self.get_workspace_stats(ws_id)
                mapping = json.loads(r["column_mapping"]) if r["column_mapping"] else {}
                extra = json.loads(r["extra_columns"]) if r["extra_columns"] else []
                result.append({
                    "id": ws_id,
                    "name": r["name"],
                    "source_file": r["source_file"],
                    "sheet_name": r["sheet_name"],
                    "total_rows": stats["total"],
                    "pending_rows": stats["pending"],
                    "processed_rows": stats["processed"],
                    "deleted_rows": stats["deleted"],
                    "kept_rows": stats["kept"],
                    "column_mapping": mapping,
                    "extra_columns": extra,
                    "created_at": r["created_at"],
                    "updated_at": r["updated_at"],
                    "is_active": (ws_id == active_id),
                })
            return result

    def get_workspace(self, ws_id: str) -> Optional[Dict[str, Any]]:
        with self._lock, self._get_registry_conn() as conn:
            row = conn.execute("SELECT * FROM workspaces WHERE id=?", (ws_id,)).fetchone()
            if not row:
                return None
            stats = self.get_workspace_stats(ws_id)
            mapping = json.loads(row["column_mapping"]) if row["column_mapping"] else {}
            extra = json.loads(row["extra_columns"]) if row["extra_columns"] else []
            columns = self.get_workspace_columns(ws_id)
            active_id = self.get_active_workspace_id()

            stored_path = Path(row["stored_file_path"])
            available_sheets = []
            if stored_path.exists() and stored_path.suffix.lower() in (".xlsx", ".xlsm"):
                try:
                    # Yalnızca küçük çalışma kitabı metadatası okunur. openpyxl burada
                    # büyük sharedStrings.xml dosyasını da açıp tüm istekleri bekletiyordu.
                    with ZipFile(stored_path) as archive:
                        book = ET.fromstring(archive.read("xl/workbook.xml"))
                    available_sheets = [sheet.attrib["name"] for sheet in book.findall("{*}sheets/{*}sheet")]
                except Exception as e:
                    logger.warning(f"Sheets listelenirken hata: {e}")

            return {
                "id": ws_id,
                "name": row["name"],
                "source_file": row["source_file"],
                "stored_file_path": row["stored_file_path"],
                "db_file_path": row["db_file_path"],
                "sheet_name": row["sheet_name"],
                "available_sheets": available_sheets,
                "stats": stats,
                "column_mapping": mapping,
                "extra_columns": extra,
                "columns": columns,
                "created_at": row["created_at"],
                "updated_at": row["updated_at"],
                "is_active": (ws_id == active_id),
            }

    def _get_workspace_db_path(self, ws_id: str) -> Path:
        with self._lock, self._get_registry_conn() as conn:
            row = conn.execute("SELECT db_file_path FROM workspaces WHERE id=?", (ws_id,)).fetchone()
            if row and row[0] and Path(row[0]).exists():
                return Path(row[0])
        return DATA_DIR / f"workspace_{ws_id}.db"

    def _get_table_info(self, conn: sqlite3.Connection) -> Tuple[str, str, str]:
        """(table_name, row_index_col, status_col) döndürür."""
        cur = conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='records'")
        if cur.fetchone():
            return "records", "_row_index", "_status"
        cur = conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='mekanlar'")
        if cur.fetchone():
            return "mekanlar", "id", "_durum"
        return "records", "_row_index", "_status"

    def _get_workspace_conn(self, ws_id: str):
        return _connection(self._get_workspace_db_path(ws_id))

    def get_workspace_columns(self, ws_id: str) -> List[str]:
        """Kayıtlar tablosundaki tüm kullanıcı sütunlarını döndürür (_ ile başlayan meta sütunlar hariç)."""
        with self._lock, self._get_workspace_conn(ws_id) as conn:
            table_name, _, _ = self._get_table_info(conn)
            cursor = conn.execute(f"PRAGMA table_info({_safe_sql_col(table_name)})")
            cols = [r["name"] for r in cursor.fetchall() if not r["name"].startswith("_") and r["name"] != "id"]
            return cols

    def get_workspace_stats(self, ws_id: str) -> Dict[str, int]:
        db_file = self._get_workspace_db_path(ws_id)
        if not db_file.exists():
            return {"total": 0, "pending": 0, "processed": 0, "deleted": 0, "kept": 0}

        try:
            with self._get_workspace_conn(ws_id) as conn:
                table_name, row_idx_col, status_col = self._get_table_info(conn)
                cur = conn.execute(f"SELECT 1 FROM sqlite_master WHERE type='table' AND name='{table_name}'")
                if not cur.fetchone():
                    return {"total": 0, "pending": 0, "processed": 0, "deleted": 0, "kept": 0}

                counts = dict(conn.execute(
                    f"SELECT {_safe_sql_col(status_col)}, COUNT(*) FROM {_safe_sql_col(table_name)} "
                    f"GROUP BY {_safe_sql_col(status_col)}").fetchall())

                return {
                    "total": sum(counts.values()),
                    "pending": counts.get("bekliyor", 0),
                    "processed": counts.get("islendi", 0),
                    "deleted": counts.get("silindi", 0),
                    "kept": counts.get("tutuldu", 0),
                }
        except Exception as e:
            logger.warning(f"Stats okuma hatası {ws_id}: {e}")
            return {"total": 0, "pending": 0, "processed": 0, "deleted": 0, "kept": 0}

    # ── Excel / CSV Yükleme ve Çalışma Alanı Oluşturma ───────────────────────────

    def create_workspace(
        self,
        file_path: Path,
        name: Optional[str] = None,
        sheet_name: Optional[str] = None,
        custom_mapping: Optional[Dict[str, str]] = None,
    ) -> Dict[str, Any]:
        """
        Excel veya CSV dosyasını okur, SQLite çalışma tablosunu oluşturur,
        otomatik kolon tespiti yapar ve yeni çalışma ortamını kaydeder.
        """
        if not file_path.exists():
            raise FileNotFoundError(f"Dosya bulunamadı: {file_path}")

        ws_id = "ws_" + uuid.uuid4().hex[:12]
        display_name = name.strip() if name and name.strip() else file_path.stem

        # Dosyayı uploads/ klasörüne kopyala / taşı
        dest_file = UPLOADS_DIR / f"{ws_id}_{file_path.name}"
        if file_path != dest_file:
            import shutil
            shutil.copy2(file_path, dest_file)

        ws_db_path = self._get_workspace_db_path(ws_id)

        # 1. Dosya içeriğini analiz et ve satırları oku
        columns, raw_rows, detected_sheet = self._read_file_data(dest_file, sheet_name)

        if not columns:
            raise ValueError("Dosyada geçerli bir sütun başlığı bulunamadı.")
        if not raw_rows:
            raise ValueError("Dosyada işlenecek veri satırı bulunamadı.")

        # 2. SQLite veritabanı ve records tablosunu oluştur
        with _connection(ws_db_path) as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            # Güvenli sütun tanımları
            col_defs = ", ".join(f"{_safe_sql_col(c)} TEXT" for c in columns)
            create_sql = f"""
                CREATE TABLE records (
                    _id INTEGER PRIMARY KEY AUTOINCREMENT,
                    _row_index INTEGER NOT NULL,
                    _status TEXT NOT NULL DEFAULT 'bekliyor',
                    _created_at TEXT,
                    _updated_at TEXT,
                    {col_defs}
                )
            """
            conn.execute(create_sql)
            conn.execute("CREATE INDEX idx_rec_row ON records(_row_index)")
            conn.execute("CREATE INDEX idx_rec_status ON records(_status)")

            # Satırları aktar
            now_str = datetime.now().isoformat()
            placeholders = ", ".join(["?"] * (len(columns) + 4))
            col_names_sql = ", ".join(["_row_index", "_status", "_created_at", "_updated_at"] + [_safe_sql_col(c) for c in columns])
            insert_sql = f"INSERT INTO records ({col_names_sql}) VALUES ({placeholders})"

            batch = []
            for idx, r_vals in enumerate(raw_rows):
                # r_vals columns uzunluğunda olmalı
                padded = list(r_vals) + [None] * (len(columns) - len(r_vals))
                batch.append([idx, "bekliyor", now_str, now_str] + [_to_sql_value(v) for v in padded[:len(columns)]])
                if len(batch) >= 2000:
                    conn.executemany(insert_sql, batch)
                    batch = []
            if batch:
                conn.executemany(insert_sql, batch)

        # 3. Otomatik sütun eşleştirmesi öner
        mapping = self._suggest_mapping(columns, raw_rows)
        if custom_mapping:
            mapping.update(custom_mapping)

        # 4. Registry'e kaydet
        now_iso = datetime.now().isoformat()
        total_rows = len(raw_rows)
        with self._lock, self._get_registry_conn() as conn:
            conn.execute(
                """
                INSERT INTO workspaces (
                    id, name, source_file, stored_file_path, db_file_path,
                    sheet_name, total_rows, processed_rows, column_mapping,
                    extra_columns, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    ws_id,
                    display_name,
                    file_path.name,
                    str(dest_file),
                    str(ws_db_path),
                    detected_sheet,
                    total_rows,
                    0,
                    json.dumps(mapping, ensure_ascii=False),
                    json.dumps([], ensure_ascii=False),
                    now_iso,
                    now_iso,
                ),
            )
            conn.execute("INSERT OR REPLACE INTO settings (key, value) VALUES ('active_workspace_id', ?)", (ws_id,))
            conn.commit()

        logger.info(f"Yeni çalışma ortamı oluşturuldu: {display_name} ({ws_id}), {total_rows} satır.")
        return self.get_workspace(ws_id)

    def _detect_best_sheet(self, wb: openpyxl.Workbook) -> str:
        """
        Çok sayfalı Excel dosyalarında gerçek veri sayfasını (Google Maps URL içeren) tespit eder.
        Özet, Rapor, Dağılım gibi yan sayfaları eler.
        """
        candidates = []
        for name in wb.sheetnames:
            ws = wb[name]
            max_r = ws.max_row or 0
            max_c = ws.max_column or 0
            if max_r < 2 or max_c < 2:
                continue

            score = 0
            has_maps_url = False
            has_maps_header = False

            # İlk 25 satırı tara
            for row_vals in ws.iter_rows(max_row=min(25, max_r), values_only=True):
                for v in row_vals:
                    if v is None:
                        continue
                    v_str = str(v)
                    norm = normalize_header(v_str)
                    if any(k in norm for k in ["maps url", "maps_url", "google maps", "gmaps", "harita link"]):
                        has_maps_header = True
                    if ("google." in v_str and ("maps" in v_str or "search" in v_str)) or "goo.gl" in v_str or "maps.app" in v_str:
                        has_maps_url = True

            if has_maps_url:
                score += 2000
            if has_maps_header:
                score += 1000

            norm_title = normalize_header(name)
            if any(k in norm_title for k in ["mekan", "poi", "data", "veri", "kayit", "place"]):
                score += 300
            elif any(k in norm_title for k in ["ozet", "rapor", "dagilim", "krilim", "istatistik", "summary"]):
                score -= 500

            score += min(max_r, 1000)
            candidates.append((score, max_r, name))

        if candidates:
            candidates.sort(key=lambda x: (x[0], x[1]), reverse=True)
            chosen = candidates[0][2]
            logger.info(f"Otomatik seçilen sayfa: '{chosen}' (Tüm adaylar: {candidates})")
            return chosen

        return wb.sheetnames[0]

    def _read_file_data(
        self, file_path: Path, preferred_sheet: Optional[str]
    ) -> Tuple[List[str], List[List[Any]], Optional[str]]:
        """Dosyayı okuyup sütun listesi ve satır değerlerini döndürür."""
        suffix = file_path.suffix.lower()
        if suffix in (".xlsx", ".xlsm"):
            wb = openpyxl.load_workbook(file_path, data_only=True, read_only=True)
            sheet_name = preferred_sheet if preferred_sheet and preferred_sheet in wb.sheetnames else self._detect_best_sheet(wb)
            ws = wb[sheet_name]

            head = list(ws.iter_rows(max_row=min(40, ws.max_row), values_only=True))
            detected = detect_header_row(head)
            header_row_idx = detected + 1
            headers = list(head[detected]) if detected >= 0 else [f"Kolon_{i+1}" for i in range(ws.max_column)]
            unique_cols = unique_columns(headers)

            data_rows = []
            for row in ws.iter_rows(min_row=header_row_idx + 1, values_only=True):
                if any(v is not None and str(v).strip() != "" for v in row):
                    data_rows.append(list(row))

            wb.close()
            return unique_cols, data_rows, sheet_name

        elif suffix == ".csv":
            encodings = ["utf-8-sig", "utf-8", "cp1254", "iso-8859-9", "latin1"]
            for enc in encodings:
                try:
                    with open(file_path, "r", encoding=enc) as f:
                        sample = f.read(4096)
                        try:
                            delimiter = csv.Sniffer().sniff(sample, delimiters=",;\t|").delimiter
                        except csv.Error:
                            delimiter = ";" if sample.count(";") > sample.count(",") else ","
                        f.seek(0)
                        reader = csv.reader(f, delimiter=delimiter)
                        rows = list(reader)
                        if rows:
                            detected = detect_header_row(rows[:40])
                            headers = rows[detected] if detected >= 0 else [f"Kolon_{i+1}" for i in range(max(map(len, rows)))]
                            unique_cols = unique_columns(headers)
                            data_rows = rows[detected + 1:]
                            return unique_cols, data_rows, None
                except Exception:
                    continue
            raise ValueError(f"CSV dosyası desteklenen kodlamalarla açılamadı: {file_path.name}")
        else:
            raise ValueError(f"Desteklenmeyen dosya biçimi: {suffix}")

    def reload_sheet(self, ws_id: str, sheet_name: str) -> Dict[str, Any]:
        """
        Mevcut çalışma ortamındaki verileri seçilen yeni Excel sayfasından tekrar yükler
        ve sütunları yeniden eşleştirir.
        """
        ws = self.get_workspace(ws_id)
        if not ws:
            raise KeyError(f"Çalışma ortamı bulunamadı: {ws_id}")

        stored_file = Path(ws["stored_file_path"])
        if not stored_file.exists():
            raise FileNotFoundError(f"Yüklenen kaynak dosya bulunamadı: {stored_file}")

        columns, raw_rows, detected_sheet = self._read_file_data(stored_file, sheet_name)
        if not columns or not raw_rows:
            raise ValueError(f"'{sheet_name}' sayfasında işlenecek veri bulunamadı.")

        ws_db_path = self._get_workspace_db_path(ws_id)

        with _connection(ws_db_path) as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("DROP TABLE IF EXISTS records")

            col_defs = ", ".join(f"{_safe_sql_col(c)} TEXT" for c in columns)
            create_sql = f"""
                CREATE TABLE records (
                    _id INTEGER PRIMARY KEY AUTOINCREMENT,
                    _row_index INTEGER NOT NULL,
                    _status TEXT NOT NULL DEFAULT 'bekliyor',
                    _created_at TEXT,
                    _updated_at TEXT,
                    {col_defs}
                )
            """
            conn.execute(create_sql)
            conn.execute("CREATE INDEX idx_rec_row ON records(_row_index)")
            conn.execute("CREATE INDEX idx_rec_status ON records(_status)")

            now_str = datetime.now().isoformat()
            placeholders = ", ".join(["?"] * (len(columns) + 4))
            col_names_sql = ", ".join(["_row_index", "_status", "_created_at", "_updated_at"] + [_safe_sql_col(c) for c in columns])
            insert_sql = f"INSERT INTO records ({col_names_sql}) VALUES ({placeholders})"

            batch = []
            for idx, r_vals in enumerate(raw_rows):
                padded = list(r_vals) + [None] * (len(columns) - len(r_vals))
                batch.append([idx, "bekliyor", now_str, now_str] + [_to_sql_value(v) for v in padded[:len(columns)]])
                if len(batch) >= 2000:
                    conn.executemany(insert_sql, batch)
                    batch = []
            if batch:
                conn.executemany(insert_sql, batch)
            conn.commit()

        mapping = self._suggest_mapping(columns, raw_rows)
        now_iso = datetime.now().isoformat()
        total_rows = len(raw_rows)
        with self._lock, self._get_registry_conn() as conn:
            conn.execute(
                """
                UPDATE workspaces SET
                    sheet_name=?, total_rows=?, processed_rows=0,
                    column_mapping=?, extra_columns=?, updated_at=?
                WHERE id=?
                """,
                (
                    detected_sheet,
                    total_rows,
                    json.dumps(mapping, ensure_ascii=False),
                    json.dumps([], ensure_ascii=False),
                    now_iso,
                    ws_id,
                )
            )
            conn.commit()

        logger.info(f"Workspace {ws_id} sayfası '{detected_sheet}' olarak güncellendi ({total_rows} satır).")
        return self.get_workspace(ws_id)

    def _suggest_mapping(self, columns: List[str], sample_rows: List[List[Any]]) -> Dict[str, str]:
        """
        Sütun isimleri ve hücre içeriklerinden akıllı öneri üretir.
        Gerçek Google Maps URL'si içeren sütunları içerik doğrulamasıyla garanti altına alır.
        """
        mapping: Dict[str, str] = {
            "maps_url": "",
            "name": "",
            "status": "",
            "category": "",
        }

        col_count = len(columns)

        # 1. URL tespiti: Önce hücre içeriklerini puanla (kesin doğruluk)
        url_scores = [0] * col_count
        for r in sample_rows[:100]:
            for i in range(min(col_count, len(r))):
                val = str(r[i] or "")
                if ("google." in val and ("maps" in val or "search" in val)) or "goo.gl" in val or "maps.app" in val:
                    url_scores[i] += 1

        best_content_idx = max(range(col_count), key=lambda i: url_scores[i]) if col_count > 0 else -1
        if best_content_idx >= 0 and url_scores[best_content_idx] > 0:
            mapping["maps_url"] = columns[best_content_idx]
        else:
            # Başlıklardan tespit et
            for col in columns:
                norm = normalize_header(col)
                if any(k in norm for k in ["maps url", "maps_url", "gmaps", "google maps", "harita link", "maps", "harita"]):
                    mapping["maps_url"] = col
                    break

        # 2. İsim / Başlık tespiti
        for col in columns:
            norm = normalize_header(col)
            if any(k in norm for k in ["isim", "ismi", "ad", "adi", "name", "unvan", "title", "mekan", "firma"]):
                if col != mapping.get("maps_url"):
                    mapping["name"] = col
                    break

        # 3. Durum / Kontrol tespiti
        for col in columns:
            norm = normalize_header(col)
            if any(k in norm for k in ["kontrol", "status", "durum", "statu", "state", "check"]):
                if col not in (mapping.get("maps_url"), mapping.get("name")):
                    mapping["status"] = col
                    break

        # 4. Kategori tespiti
        for col in columns:
            norm = normalize_header(col)
            if any(k in norm for k in ["kategori", "category", "sektor", "tur", "type"]):
                if col not in (mapping.get("maps_url"), mapping.get("name"), mapping.get("status")):
                    mapping["category"] = col
                    break

        return mapping

    # ── Sütun Eşleştirme ve Sütun Ekleme ─────────────────────────────────────────

    def update_column_mapping(self, ws_id: str, new_mapping: Dict[str, str]) -> Dict[str, Any]:
        """
        Kullanıcının seçtiği dinamik sütun eşleştirmesini kaydeder.
        """
        ws = self.get_workspace(ws_id)
        if not ws:
            raise KeyError(f"Çalışma ortamı bulunamadı: {ws_id}")

        existing_cols = ws["columns"]
        for role, column in new_mapping.items():
            if column and column not in existing_cols:
                raise ValueError(f"Seçilen sütun tabloda mevcut değil: {column}")
        combined = {**ws["column_mapping"], **new_mapping}
        selected = [v for k, v in combined.items() if v and k in ("brand", "main_category", "sub_category", "poi_state", "maps_url", "name")]
        if len(selected) != len(set(selected)):
            raise ValueError("Her alan için farklı bir sütun seçin.")
        # URL kolonu var mı kontrolü
        maps_col = new_mapping.get("maps_url")
        if maps_col and maps_col not in existing_cols:
            raise ValueError(f"Seçilen Maps URL sütunu '{maps_col}' tabloda mevcut değil.")

        current_mapping = ws["column_mapping"]
        current_mapping.update(new_mapping)

        now_iso = datetime.now().isoformat()
        with self._lock, self._get_registry_conn() as conn:
            conn.execute(
                "UPDATE workspaces SET column_mapping=?, updated_at=? WHERE id=?",
                (json.dumps(current_mapping, ensure_ascii=False), now_iso, ws_id),
            )

        logger.info(f"Sütun eşleştirmesi güncellendi {ws_id}: {current_mapping}")
        return self.get_workspace(ws_id)

    def add_column(self, ws_id: str, column_name: str, default_val: str = "") -> Dict[str, Any]:
        """
        Çalışma ortamının SQLite tablosuna yeni bir sütun ekler (örn: 'Kategori', 'Notlar').
        """
        clean_name = column_name.strip()
        if not clean_name:
            raise ValueError("Sütun adı boş olamaz.")

        ws = self.get_workspace(ws_id)
        if not ws:
            raise KeyError(f"Çalışma ortamı bulunamadı: {ws_id}")

        if clean_name in ws["columns"]:
            return ws  # Zaten mevcut

        with self._lock, self._get_workspace_conn(ws_id) as conn:
            sql = f"ALTER TABLE records ADD COLUMN {_safe_sql_col(clean_name)} TEXT"
            conn.execute(sql)
            if default_val:
                conn.execute(f"UPDATE records SET {_safe_sql_col(clean_name)} = ?", (default_val,))
            conn.commit()

        extra_cols = list(ws["extra_columns"])
        if clean_name not in extra_cols:
            extra_cols.append(clean_name)

        now_iso = datetime.now().isoformat()
        with self._lock, self._get_registry_conn() as conn:
            conn.execute(
                "UPDATE workspaces SET extra_columns=?, updated_at=? WHERE id=?",
                (json.dumps(extra_cols, ensure_ascii=False), now_iso, ws_id),
            )

        logger.info(f"Çalışma ortamına yeni sütun eklendi: '{clean_name}' (ws: {ws_id})")
        return self.get_workspace(ws_id)

    def delete_workspace(self, ws_id: str) -> bool:
        with self._lock:
            with self._get_registry_conn() as conn:
                conn.execute("DELETE FROM workspaces WHERE id=?", (ws_id,))
                # Eğer silinen aktifse, ayarı temizle
                conn.execute("DELETE FROM settings WHERE key='active_workspace_id' AND value=?", (ws_id,))

            # Veritabanı ve yüklenen dosyayı temizle
            db_file = self._get_workspace_db_path(ws_id)
            if db_file.exists():
                db_file.unlink(missing_ok=True)
            for f in DATA_DIR.glob(f"workspace_{ws_id}.db*"):
                f.unlink(missing_ok=True)
            return True

    # ── Kayıt Okuma & Güncelleme (SQLite Üzerinden) ──────────────────────────────

    def get_place_records(
        self,
        ws_id: str,
        start_index: int = 0,
        limit: Optional[int] = None,
        status_filter: Optional[str] = None,
        require_url: bool = False,
        assignee: Optional[str] = None,
    ) -> List[PlaceRecord]:
        """
        Oturum motorunun işleyeceği PlaceRecord nesnelerini SQLite tablosundan çeker.
        Dinamik sütun eşleştirmesini kullanır.
        """
        ws = self.get_workspace(ws_id)
        if not ws:
            raise KeyError(f"Çalışma ortamı bulunamadı: {ws_id}")

        mapping = ws["column_mapping"]
        maps_col = mapping.get("maps_url")
        name_col = mapping.get("name")
        status_col = mapping.get("status")

        if not maps_col or maps_col not in ws["columns"]:
            raise ValueError(
                f"Google Maps URL sütunu belirlenmemiş veya tabloda yok! "
                f"Lütfen önce sütun eşleştirmesi yapın. Mevcut sütunlar: {ws['columns']}"
            )

        records: List[PlaceRecord] = []
        with self._get_workspace_conn(ws_id) as conn:
            table_name, row_idx_col, status_col_name = self._get_table_info(conn)
            selected = list(dict.fromkeys(c for c in [row_idx_col, maps_col, name_col, status_col, status_col_name] if c))
            query = f"SELECT {', '.join(_safe_sql_col(c) for c in selected)} FROM {_safe_sql_col(table_name)} WHERE {_safe_sql_col(row_idx_col)} >= ?"
            params: List[Any] = [start_index]
            if assignee:
                assignee_col = self._assignee_column(ws)
                if not assignee_col:
                    raise ValueError("Bu dosyada Kontrol Eden / Sorumlu sütunu bulunamadı.")
                query += f" AND TRIM(CAST({_safe_sql_col(assignee_col)} AS TEXT)) = ?"
                params.append(assignee.strip())
            if require_url:
                query += f" AND TRIM(COALESCE({_safe_sql_col(maps_col)}, '')) != ''"

            if status_filter:
                query += f" AND {_safe_sql_col(status_col_name)} = ?"
                params.append(status_filter)

            query += f" ORDER BY {_safe_sql_col(row_idx_col)} ASC"
            if limit is not None:
                query += " LIMIT ?"
                params.append(limit)

            rows = conn.execute(query, params).fetchall()
            total_records = len(rows)

            for pos, r in enumerate(rows):
                r_dict = dict(r)
                row_idx = int(r[row_idx_col])
                raw_url = r_dict.get(maps_col) or ""
                raw_name = r_dict.get(name_col) if name_col else ""
                raw_status = r_dict.get(status_col) if status_col else r_dict.get(status_col_name, "")

                rec = PlaceRecord.build(
                    row_index=row_idx,
                    raw_url=raw_url,
                    name=raw_name,
                    status=raw_status,
                    extra_hosts=EXTRA_ALLOWED_MAPS_HOSTS,
                )
                rec.position = pos
                rec.total = total_records
                records.append(rec)

        return records

    @staticmethod
    def _assignee_column(ws: dict) -> Optional[str]:
        explicit = ws["column_mapping"].get("assignee")
        if explicit in ws["columns"]:
            return explicit
        aliases = {"kontrol eden", "kontrol eden kisi", "sorumlu", "atanan", "atanan kisi",
                   "assigned to", "assignee", "reviewer", "operator", "kullanici"}
        return next((c for c in ws["columns"] if normalize_header(c) in aliases), None)

    def get_assignee_options(self, ws_id: str) -> dict:
        ws = self.get_workspace(ws_id)
        if not ws:
            raise KeyError("Çalışma ortamı bulunamadı.")
        column = self._assignee_column(ws)
        if not column:
            return {"column": None, "choices": []}
        with self._get_workspace_conn(ws_id) as conn:
            table, _, status = self._get_table_info(conn)
            value = f"TRIM(CAST({_safe_sql_col(column)} AS TEXT))"
            rows = conn.execute(
                f"SELECT {value}, COUNT(*), SUM(CASE WHEN {_safe_sql_col(status)}='bekliyor' THEN 1 ELSE 0 END) "
                f"FROM {_safe_sql_col(table)} WHERE COALESCE({value}, '')<>'' GROUP BY {value} ORDER BY {value}"
            ).fetchall()
        return {"column": column, "choices": [{"value": r[0], "total": r[1], "pending": r[2]} for r in rows]}

    def get_assignee_pending_count(self, ws_id: str, assignee: str) -> int:
        ws = self.get_workspace(ws_id)
        column = self._assignee_column(ws) if ws else None
        if not column:
            return 0
        with self._get_workspace_conn(ws_id) as conn:
            table, _, status = self._get_table_info(conn)
            return conn.execute(
                f"SELECT COUNT(*) FROM {_safe_sql_col(table)} WHERE {_safe_sql_col(status)}='bekliyor' "
                f"AND TRIM(CAST({_safe_sql_col(column)} AS TEXT))=?", (assignee.strip(),)).fetchone()[0]

    def update_label(
        self,
        ws_id: str,
        row_index: int,
        category: str,
        extra_fields: Optional[Dict[str, str]] = None,
    ) -> None:
        """
        SQLite'taki kaydı günceller (Kategori ve ek sütunlar).
        Excel'e anlık yazma yapılmaz; doğrudan SQLite tablosu güncellenir.
        """
        ws = self.get_workspace(ws_id)
        if not ws:
            raise KeyError(f"Çalışma ortamı bulunamadı: {ws_id}")

        mapping = ws["column_mapping"]
        cat_col = mapping.get("category")

        # Kategori kolonu henüz yoksa otomatik oluştur
        if not cat_col:
            cat_col = "Kategori"
            self.add_column(ws_id, cat_col)
            self.update_column_mapping(ws_id, {"category": cat_col})
            ws = self.get_workspace(ws_id)
        elif cat_col not in ws["columns"]:
            self.add_column(ws_id, cat_col)
            ws = self.get_workspace(ws_id)

        updates = {
            cat_col: category.strip() if category else "",
            "_status": "islendi",
            "_updated_at": datetime.now().isoformat(),
        }

        # Ek alanlar varsa tabloya ekle ve güncelle
        if extra_fields:
            for k, v in extra_fields.items():
                if k not in ws["columns"]:
                    self.add_column(ws_id, k)
                updates[k] = v.strip() if v else ""

        set_clauses = [f"{_safe_sql_col(k)} = ?" for k in updates.keys()]
        values = list(updates.values()) + [row_index]

        with self._lock, self._get_workspace_conn(ws_id) as conn:
            conn.execute(
                f"UPDATE records SET {', '.join(set_clauses)} WHERE _row_index = ?",
                values,
            )

        # Registry'de son güncelleme tarihini tazele
        with self._lock, self._get_registry_conn() as conn:
            conn.execute("UPDATE workspaces SET updated_at=? WHERE id=?", (datetime.now().isoformat(), ws_id))

    def get_edit_fields(self, ws_id: str, row_index: int) -> dict:
        ws = self.get_workspace(ws_id)
        if not ws:
            raise KeyError("Çalışma ortamı bulunamadı.")
        with self._lock, self._get_workspace_conn(ws_id) as conn:
            table, index_col, _ = self._get_table_info(conn)
            return read_fields(conn, table, index_col, ws["columns"], row_index, ws["column_mapping"])

    def get_edit_options(self, ws_id: str) -> dict:
        ws = self.get_workspace(ws_id)
        if not ws:
            raise KeyError("Çalışma ortamı bulunamadı.")
        with self._lock, self._get_workspace_conn(ws_id) as conn:
            table, _, _ = self._get_table_info(conn)
            return read_options(conn, table, ws["columns"], ws["column_mapping"])

    def update_action(self, ws_id: str, row_index: int, action: str, fields: Optional[dict] = None) -> None:
        """
        Düzenle akışı aksiyonu (tut, sil, atla) doğrudan SQLite tablosuna yazılır.
        """
        ws = self.get_workspace(ws_id)
        if not ws:
            raise KeyError(f"Çalışma ortamı bulunamadı: {ws_id}")

        mapping = ws["column_mapping"]
        status_col = mapping.get("status")

        action_clean = action.strip().lower()
        if action_clean == "delete":
            db_status = "silindi"
        elif action_clean == "keep":
            db_status = "tutuldu"
        elif action_clean == "skip":
            db_status = "bekliyor"
        else:
            raise ValueError(f"Geçersiz aksiyon: {action}")

        now_iso = datetime.now().isoformat()
        with self._lock, self._get_workspace_conn(ws_id) as conn:
            table_name, row_idx_col, status_col_name = self._get_table_info(conn)
            read_fields(conn, table_name, row_idx_col, ws["columns"], row_index)
            if action_clean == "keep":
                added = write_fields(conn, table_name, row_idx_col, ws["columns"], row_index, fields or {}, ws["column_mapping"])
                # Eski edit veritabanları dışa aktarma için sütun listesini meta'da tutar.
                if added and table_name == "mekanlar":
                    conn.execute("INSERT OR REPLACE INTO meta VALUES (?,?)",
                                 ("columns", json.dumps(ws["columns"] + added, ensure_ascii=False)))
            cur_cols = [r["name"] for r in conn.execute(f"PRAGMA table_info({_safe_sql_col(table_name)})").fetchall()]
            has_updated_at = "_updated_at" in cur_cols

            set_parts = [f"{_safe_sql_col(status_col_name)}=?"]
            vals = [db_status]
            # Kaynak kontrol/açık-kapalı bilgisi kararın dahili durumundan bağımsızdır.
            if has_updated_at:
                set_parts.append("_updated_at=?")
                vals.append(now_iso)
            vals.append(row_index)

            sql = f"UPDATE {_safe_sql_col(table_name)} SET {', '.join(set_parts)} WHERE {_safe_sql_col(row_idx_col)}=?"
            conn.execute(sql, tuple(vals))

        with self._lock, self._get_registry_conn() as conn:
            conn.execute("UPDATE workspaces SET updated_at=? WHERE id=?", (now_iso, ws_id))

    def undo_action(self, ws_id: str, row_index: int) -> str:
        """Önceki aksiyonu geri alır ve durumu 'bekliyor' yapar."""
        now_iso = datetime.now().isoformat()
        with self._lock, self._get_workspace_conn(ws_id) as conn:
            table_name, row_idx_col, status_col_name = self._get_table_info(conn)
            cur_cols = [r["name"] for r in conn.execute(f"PRAGMA table_info({_safe_sql_col(table_name)})").fetchall()]
            has_updated_at = "_updated_at" in cur_cols
            if has_updated_at:
                conn.execute(f"UPDATE {_safe_sql_col(table_name)} SET {_safe_sql_col(status_col_name)}='bekliyor', _updated_at=? WHERE {_safe_sql_col(row_idx_col)}=?", (now_iso, row_index))
            else:
                conn.execute(f"UPDATE {_safe_sql_col(table_name)} SET {_safe_sql_col(status_col_name)}='bekliyor' WHERE {_safe_sql_col(row_idx_col)}=?", (row_index,))
        return "undone"

    # ── Excel Dışa Aktarma (SQLite Tablosundan Temiz Excel Üretimi) ──────────────

    def export_to_excel(
        self,
        ws_id: str,
        output_path: Path,
        include_deleted: bool = False,
    ) -> Path:
        """
        SQLite'taki güncel çalışma tablosunu tüm orijinal ve sonradan eklenen sütunlarla
        birlikte şık, formatlı bir Excel (.xlsx) dosyasına aktarır.
        """
        ws = self.get_workspace(ws_id)
        if not ws:
            raise KeyError(f"Çalışma ortamı bulunamadı: {ws_id}")

        columns = ws["columns"]

        with self._get_workspace_conn(ws_id) as conn:
            table_name, row_idx_col, status_col_name = self._get_table_info(conn)
            query = f"SELECT * FROM {_safe_sql_col(table_name)}"
            if not include_deleted:
                query += f" WHERE {_safe_sql_col(status_col_name)} != 'silindi'"
            query += f" ORDER BY {_safe_sql_col(row_idx_col)} ASC"
            rows = conn.execute(query).fetchall()

        wb = openpyxl.Workbook()
        sheet = wb.active
        sheet.title = (ws.get("name") or "Veriler")[:30]

        # Başlık formatları
        header_font = Font(name="Calibri", size=11, bold=True, color="FFFFFF")
        header_fill = PatternFill(start_color="1F2937", end_color="1F2937", fill_type="solid")
        header_align = Alignment(horizontal="center", vertical="center", wrap_text=True)

        border_side = Side(border_style="thin", color="E5E7EB")
        thin_border = Border(left=border_side, right=border_side, top=border_side, bottom=border_side)

        # 1. Başlık satırını yaz
        for col_idx, col_name in enumerate(columns, start=1):
            cell = sheet.cell(row=1, column=col_idx, value=col_name)
            cell.font = header_font
            cell.fill = header_fill
            cell.alignment = header_align
            cell.border = thin_border

        sheet.row_dimensions[1].height = 28

        # 2. Veri satırlarını yaz
        for row_idx, r in enumerate(rows, start=2):
            r_dict = dict(r)
            for col_idx, col_name in enumerate(columns, start=1):
                val = r_dict.get(col_name)
                c = sheet.cell(row=row_idx, column=col_idx, value=val)
                c.border = thin_border
                c.alignment = Alignment(vertical="center")

        # Otomatik sütun genişliği
        for col in sheet.columns:
            max_len = max(len(str(cell.value or "")) for cell in col[:100])
            col_letter = col[0].column_letter
            sheet.column_dimensions[col_letter].width = min(max(max_len + 3, 12), 50)

        output_path.parent.mkdir(parents=True, exist_ok=True)
        wb.save(output_path)
        logger.info(f"Excel başarıyla dışa aktarıldı ({len(rows)} satır): {output_path.name}")
        return output_path


# Global Singleton
workspace_mgr = WorkspaceManager()
