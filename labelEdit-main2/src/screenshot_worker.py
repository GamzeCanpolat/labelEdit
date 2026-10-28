"""
Proje V1 — Screenshot Worker (sıralı prefetch penceresi)

Kullanıcının önündeki BUFFER_SIZE kayıt CaptureEngine'e eş zamanlı gönderilir;
sonuçlar hangi sırayla biterse bitsin kullanıcıya her zaman kayıt sırasıyla
sunulur. Eski public API (start / stop / get_next / queue_size / is_empty /
get_stats) korunmuştur.
"""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Optional

from config import BUFFER_SIZE, GET_NEXT_TIMEOUT
from data_model import CaptureOutcome, PlaceRecord
from session_manager import SessionManager

logger = logging.getLogger(__name__)

LOOKAHEAD_FACTOR = 4


@dataclass
class ScreenshotItem:
    """Kullanıcıya sunulan tek bir ekran görüntüsü kaydı."""
    row_index: int
    position: int
    url: str
    name: str
    file_path: str
    total: int
    taken_at: float = field(default_factory=time.time)
    error: Optional[str] = None
    from_cache: bool = False


class ScreenshotWorker:
    def __init__(self, session_manager: SessionManager, window: int = BUFFER_SIZE) -> None:
        self._session = session_manager
        self._window = max(1, window)
        self._records: list[PlaceRecord] = []
        self._tasks: dict[int, asyncio.Future] = {}
        self._next_serve = 0
        self._next_schedule = 0
        self._running = False
        self._stats = {"taken": 0, "errors": 0, "cache_hits": 0, "timeouts": 0}
        self._serve_lock = asyncio.Lock()

    # Geriye dönük uyumluluk: main.py eski sürümde bu alanı okuyordu
    @property
    def _pending_records(self) -> list[dict]:
        return [r.to_dict() for r in self._records]

    # ── Başlatma / Durdurma ──────────────────────────────────────────────────
    async def start(self, records: list) -> None:
        self._records = [r if isinstance(r, PlaceRecord) else PlaceRecord.from_dict(r) for r in records]
        self._next_serve = 0
        self._next_schedule = 0
        self._running = True
        self._fill()
        logger.info(f"ScreenshotWorker başlatıldı. Toplam: {len(self._records)}, pencere: {self._window}")

    async def stop(self) -> None:
        self._running = False
        for t in self._tasks.values():
            t.remove_done_callback(self._fill)
            t.cancel()
        if self._tasks:
            await asyncio.gather(*self._tasks.values(), return_exceptions=True)
        self._tasks.clear()
        logger.info(f"ScreenshotWorker durduruldu. Stats: {self._stats}")

    def _fill(self, *_: object) -> None:
        """
        Pencere = aynı anda en fazla `window` AKTİF yakalama (sıra sayısı değil).
        Sıradaki kayıt yavaşsa (ör. zaman aşımı) hat durmaz; ileri bakış en fazla
        LOOKAHEAD_FACTOR × window sıraya kadar devam eder. Biten görevler
        callback ile pencereyi yeniden doldurur.
        """
        engine = self._session.engine
        if not self._running or engine is None:
            return
        limit = min(len(self._records), self._next_serve + self._window * LOOKAHEAD_FACTOR)
        active = sum(1 for t in self._tasks.values() if not t.done())
        self._next_schedule = max(self._next_schedule, self._next_serve)
        while self._next_schedule < limit and active < self._window:
            pos = self._next_schedule
            self._next_schedule += 1
            if pos in self._tasks:
                continue
            task = asyncio.ensure_future(engine.capture(self._records[pos]))
            task.add_done_callback(self._fill)
            self._tasks[pos] = task
            active += 1

    # ── Public API ───────────────────────────────────────────────────────────
    async def get_next(self, wait_timeout: Optional[float] = None,
                       consume_on_timeout: bool = True) -> Optional[ScreenshotItem]:
        async with self._serve_lock:
            return await self._get_next(wait_timeout, consume_on_timeout)

    async def _get_next(self, wait_timeout: Optional[float], consume_on_timeout: bool) -> Optional[ScreenshotItem]:
        if self._next_serve >= len(self._records):
            return None
        pos = self._next_serve
        rec = self._records[pos]
        task = self._tasks.get(pos)
        if task is None:   # pencere dışı (olmamalı) → doğrudan iste
            task = asyncio.ensure_future(self._session.engine.capture(rec))
            self._tasks[pos] = task
            task.add_done_callback(self._fill)
        try:
            out: CaptureOutcome = await asyncio.wait_for(
                asyncio.shield(task), timeout=GET_NEXT_TIMEOUT if wait_timeout is None else wait_timeout)
        except asyncio.TimeoutError:
            if not consume_on_timeout:
                return None  # Sıra ve yakalama görevi korunur; istemci kısa aralıkla tekrar sorar.
            # Yakalama arka planda sürer ve tamamlanınca önbelleğe yazılır
            self._stats["timeouts"] += 1
            out = CaptureOutcome(rec.place_key, rec.url,
                                 error=f"{GET_NEXT_TIMEOUT:.0f} sn içinde hazırlanamadı (arka planda deneniyor)")
        except asyncio.CancelledError:
            raise
        except Exception as e:
            out = CaptureOutcome(rec.place_key, rec.url, error=str(e)[:200])

        self._tasks.pop(pos, None)
        self._next_serve += 1
        self._fill()
        if out.ok:
            self._stats["taken"] += 1
            if out.from_cache:
                self._stats["cache_hits"] += 1
        else:
            self._stats["errors"] += 1
        return ScreenshotItem(
            row_index=rec.row_index, position=rec.position, url=rec.url, name=rec.name,
            file_path=out.file_path or "", total=rec.total, error=out.error, from_cache=out.from_cache,
        )

    def peek_record(self, position: int) -> Optional[PlaceRecord]:
        return self._records[position] if 0 <= position < len(self._records) else None

    def queue_size(self) -> int:
        """Kullanıcının önünde HAZIR bekleyen kayıt sayısı (arayüzdeki buffer noktaları)."""
        n = 0
        for pos in range(self._next_serve, min(len(self._records), self._next_serve + self._window)):
            t = self._tasks.get(pos)
            if not self._task_ok(t):
                break
            n += 1
        return n

    @staticmethod
    def _task_ok(task) -> bool:
        return bool(task and task.done() and not task.cancelled()
                    and task.exception() is None and task.result().ok)

    def buffer_status(self) -> dict:
        end = min(len(self._records), self._next_serve + self._window)
        tasks = [self._tasks.get(pos) for pos in range(self._next_serve, end)]
        return {"ready": self.queue_size(), "capacity": len(tasks),
                "preparing": sum(t is not None and not t.done() for t in tasks),
                "failed": sum(t is not None and t.done() and not self._task_ok(t) for t in tasks)}

    def ready_previews(self, limit: int = 3) -> list[dict]:
        previews = []
        for pos in range(self._next_serve, min(len(self._records), self._next_serve + limit)):
            task = self._tasks.get(pos)
            if not self._task_ok(task):
                break
            rec, outcome = self._records[pos], task.result()
            previews.append({"row_index": rec.row_index, "name": rec.name,
                             "file_path": outcome.file_path})
        return previews

    def is_empty(self) -> bool:
        return self._next_serve >= len(self._records)

    def get_stats(self) -> dict:
        engine = self._session.engine
        return {
            **self._stats,
            "queue_size": self.queue_size(),
            "buffer": self.buffer_status(),
            "fetch_index": self._next_schedule,
            "engine": engine.stats() if engine else None,
        }
