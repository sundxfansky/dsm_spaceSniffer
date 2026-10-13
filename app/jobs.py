from __future__ import annotations

import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime

from .models import AnalysisRequest
from .file_index import FileIndex
from .synology import ScanCancelled, SynologyError, analyze


class JobStore:
    def __init__(self, max_workers: int = 2) -> None:
        self._jobs: dict[str, dict] = {}
        self._cancel_events: dict[str, threading.Event] = {}
        self._file_indexes: dict[str, FileIndex] = {}
        self._lock = threading.Lock()
        self._executor = ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="dsm-scan")

    @staticmethod
    def _now() -> str:
        return datetime.now(UTC).isoformat()

    def create(self, request: AnalysisRequest) -> str:
        job_id = uuid.uuid4().hex
        now = self._now()
        with self._lock:
            self._cancel_events[job_id] = threading.Event()
            self._file_indexes[job_id] = FileIndex()
            self._jobs[job_id] = {
                "id": job_id,
                "status": "queued",
                "progress": 0,
                "stage": "等待扫描",
                "created_at": now,
                "updated_at": now,
                "error": None,
                "result": None,
            }
        self._executor.submit(self._run, job_id, request)
        return job_id

    def get(self, job_id: str) -> dict | None:
        with self._lock:
            job = self._jobs.get(job_id)
            return dict(job) if job else None

    def _update(self, job_id: str, **changes: object) -> None:
        with self._lock:
            if self._jobs[job_id]["status"] in {"cancelling", "cancelled"}:
                changes.pop("stage", None)
                changes.pop("progress", None)
            self._jobs[job_id].update(changes, updated_at=self._now())

    def browse(self, job_id: str, path: str, offset: int, limit: int, query: str) -> dict | None:
        with self._lock:
            job = self._jobs.get(job_id)
            index = self._file_indexes.get(job_id)
            result = job.get("result") if job else None
        if job is None:
            return None
        if result is None or index is None:
            raise ValueError("任务尚未生成文件索引")
        return index.browse(result, path, offset, limit, query)

    def cancel(self, job_id: str) -> dict | None:
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None:
                return None
            if job["status"] in {"queued", "running", "cancelling"}:
                self._cancel_events[job_id].set()
                queued = job["status"] == "queued"
                job.update(status="cancelled" if queued else "cancelling",
                           stage="分析已停止" if queued else "正在停止分析，保留已读取结果",
                           updated_at=self._now())
            return dict(job)

    def _finish(self, job_id: str, **changes: object) -> None:
        # Completion and cancellation are serialized so a late finish cannot undo a stop.
        with self._lock:
            job = self._jobs[job_id]
            if self._cancel_events[job_id].is_set():
                result = changes.get("result", job["result"])
                if result is not None:
                    result = {**result, "complete": False, "scan_finished": False}
                changes = dict(status="cancelled", stage="分析已停止（保留部分结果）", error=None, result=result)
            job.update(changes, updated_at=self._now())

    def _run(self, job_id: str, request: AnalysisRequest) -> None:
        with self._lock:
            cancel_event = self._cancel_events[job_id]
            if cancel_event.is_set():
                return
            self._jobs[job_id].update(status="running", progress=2, stage="连接群晖", updated_at=self._now())

        def progress(value: int, stage: str) -> None:
            self._update(job_id, progress=min(max(value, 0), 99), stage=stage)

        try:
            result = analyze(request, progress, lambda result: self._update(job_id, result=result),
                             cancel_event=cancel_event, on_file=self._file_indexes[job_id].add)
            self._finish(
                job_id,
                status="completed",
                progress=100,
                stage="分析完成" if result.get("complete", True) else "分析结束（部分目录未读取）",
                result=result,
            )
        except ScanCancelled:
            self._finish(job_id, status="cancelled", stage="分析已停止", error=None)
        except (SynologyError, ValueError) as exc:
            self._finish(job_id, status="failed", stage="分析失败", error=str(exc))
        except Exception:
            self._finish(job_id, status="failed", stage="分析失败", error="服务内部错误，请查看服务日志")

    def shutdown(self) -> None:
        with self._lock:
            job_ids = list(self._jobs)
        for job_id in job_ids:
            self.cancel(job_id)
        self._executor.shutdown(wait=False, cancel_futures=True)
