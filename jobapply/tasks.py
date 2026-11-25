"""In-memory background task manager for the FastAPI server.

The web UI kicks off long-running operations (search, run, tailor) via
``POST`` endpoints that immediately return a ``task_id``. The frontend
polls ``GET /api/tasks/<task_id>`` to render progress and retrieve
results when the task completes.

We deliberately keep this in-memory rather than e.g. Celery + Redis
because the UI is a single-process developer tool: the server lives
for the duration of one ``jobapply ui`` invocation, and we'd rather
not introduce another runtime dependency users have to install. Tasks
that haven't been picked up by the polling client are discarded when
the process exits.

Concurrency: each task runs in its own ``threading.Thread`` so the
event loop stays responsive. The :class:`TaskRecord` itself is
guarded by a per-instance lock to keep ``progress`` updates and
``result`` reads consistent across threads.
"""

from __future__ import annotations

import threading
import traceback
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any


class TaskStatus(StrEnum):
    """Lifecycle of a background task as observed by the UI."""

    pending = "pending"
    running = "running"
    succeeded = "succeeded"
    failed = "failed"
    cancelled = "cancelled"


@dataclass
class TaskProgress:
    """Lightweight progress payload streamed via the polling endpoint.

    ``label`` is a short human-readable status (e.g. *"Scoring 12/30"*)
    that the UI puts in the progress card. ``percent`` is best-effort
    — many tasks just toggle between 0 and 100 without intermediate
    steps and that's fine.
    """

    label: str = ""
    percent: float = 0.0
    log: list[str] = field(default_factory=list)


@dataclass
class TaskRecord:
    """The full state of a background task, exposed via ``/api/tasks``."""

    task_id: str
    kind: str
    status: TaskStatus = TaskStatus.pending
    progress: TaskProgress = field(default_factory=TaskProgress)
    result: dict[str, Any] | None = None
    error: str | None = None
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    started_at: datetime | None = None
    finished_at: datetime | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        """Serializable snapshot. Datetimes go to ISO 8601."""
        return {
            "task_id": self.task_id,
            "kind": self.kind,
            "status": str(self.status),
            "progress": {
                "label": self.progress.label,
                "percent": self.progress.percent,
                "log": list(self.progress.log),
            },
            "result": self.result,
            "error": self.error,
            "created_at": self.created_at.isoformat(),
            "started_at": self.started_at.isoformat() if self.started_at else None,
            "finished_at": self.finished_at.isoformat() if self.finished_at else None,
            "metadata": dict(self.metadata),
        }


class TaskHandle:
    """Thin wrapper passed to task functions for progress reporting.

    Callers use :meth:`set_label`, :meth:`set_percent`, :meth:`log`,
    and :meth:`is_cancelled` to push status into the shared
    :class:`TaskRecord` without locking themselves.
    """

    def __init__(self, manager: TaskManager, record: TaskRecord) -> None:
        self._manager = manager
        self._record = record

    @property
    def task_id(self) -> str:
        return self._record.task_id

    def set_label(self, label: str) -> None:
        with self._manager._lock:
            self._record.progress.label = label

    def set_percent(self, percent: float) -> None:
        bounded = max(0.0, min(100.0, float(percent)))
        with self._manager._lock:
            self._record.progress.percent = bounded

    def log(self, line: str) -> None:
        # Keep the log bounded so a runaway task doesn't blow our memory.
        with self._manager._lock:
            self._record.progress.log.append(line)
            if len(self._record.progress.log) > 500:
                self._record.progress.log = self._record.progress.log[-500:]

    def is_cancelled(self) -> bool:
        with self._manager._lock:
            return self._record.status == TaskStatus.cancelled


class TaskManager:
    """Thread-safe registry of running and completed background tasks."""

    def __init__(self, max_records: int = 200) -> None:
        self._tasks: dict[str, TaskRecord] = {}
        self._threads: dict[str, threading.Thread] = {}
        self._lock = threading.Lock()
        self._max_records = max_records

    def submit(
        self,
        kind: str,
        fn: Callable[[TaskHandle], dict[str, Any] | None],
        *,
        metadata: dict[str, Any] | None = None,
    ) -> TaskRecord:
        """Register and start a new task. Returns a snapshot of the record.

        ``fn`` is invoked with a :class:`TaskHandle` and may return a
        dict that becomes the task's ``result``. Exceptions are
        captured into ``error`` so the UI can render them.
        """
        record = TaskRecord(
            task_id=uuid.uuid4().hex,
            kind=kind,
            metadata=dict(metadata or {}),
        )
        with self._lock:
            self._tasks[record.task_id] = record
            self._gc_old_records_locked()
        handle = TaskHandle(self, record)

        def _runner() -> None:
            with self._lock:
                record.status = TaskStatus.running
                record.started_at = datetime.now(UTC)
            try:
                result = fn(handle)
                with self._lock:
                    if record.status != TaskStatus.cancelled:
                        record.status = TaskStatus.succeeded
                        record.result = result if isinstance(result, dict) else None
            except Exception as exc:  # noqa: BLE001 - surface anything to the UI
                tb = traceback.format_exc()
                with self._lock:
                    record.status = TaskStatus.failed
                    record.error = f"{exc}\n{tb}"
            finally:
                with self._lock:
                    record.finished_at = datetime.now(UTC)

        thread = threading.Thread(target=_runner, daemon=True, name=f"task-{kind}")
        with self._lock:
            self._threads[record.task_id] = thread
        thread.start()
        return record

    def get(self, task_id: str) -> TaskRecord | None:
        with self._lock:
            return self._tasks.get(task_id)

    def cancel(self, task_id: str) -> bool:
        """Mark the task as cancelled. Cooperative — the task fn must
        check :meth:`TaskHandle.is_cancelled` to actually stop."""
        with self._lock:
            record = self._tasks.get(task_id)
            if record is None:
                return False
            if record.status in {TaskStatus.succeeded, TaskStatus.failed}:
                return False
            record.status = TaskStatus.cancelled
            return True

    def list_recent(self, limit: int = 50) -> list[TaskRecord]:
        with self._lock:
            ordered = sorted(self._tasks.values(), key=lambda r: r.created_at, reverse=True)
            return ordered[:limit]

    def _gc_old_records_locked(self) -> None:
        """Drop the oldest tasks once we exceed the cap."""
        if len(self._tasks) <= self._max_records:
            return
        ordered = sorted(self._tasks.values(), key=lambda r: r.created_at)
        keep = ordered[-self._max_records :]
        keep_ids = {r.task_id for r in keep}
        for tid in list(self._tasks.keys()):
            if tid not in keep_ids:
                self._tasks.pop(tid, None)
                self._threads.pop(tid, None)


# Process-wide singleton used by the FastAPI app. We don't expose it as
# a global because the test suite injects its own manager via dependency
# overrides.
_default_manager: TaskManager | None = None


def get_default_manager() -> TaskManager:
    global _default_manager
    if _default_manager is None:
        _default_manager = TaskManager()
    return _default_manager


__all__ = [
    "TaskHandle",
    "TaskManager",
    "TaskRecord",
    "TaskProgress",
    "TaskStatus",
    "get_default_manager",
]
