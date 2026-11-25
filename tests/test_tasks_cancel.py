"""Cancellation semantics for the background TaskManager."""

from __future__ import annotations

import threading
import time

from jobapply.tasks import TaskHandle, TaskManager, TaskStatus


def _wait(pred, timeout: float = 5.0) -> None:
    end = time.monotonic() + timeout
    while not pred():
        if time.monotonic() > end:
            raise AssertionError("condition not met in time")
        time.sleep(0.01)


def test_cancel_keeps_running_until_worker_returns() -> None:
    """The UI must keep polling while the in-flight step finishes, so the
    work completed after the cancel request isn't hidden behind a stale
    'cancelled' snapshot."""
    mgr = TaskManager()
    in_step = threading.Event()
    release = threading.Event()
    processed: list[int] = []

    def fn(h: TaskHandle) -> dict[str, int]:
        for i in range(3):
            if h.is_cancelled():
                break
            in_step.set()
            release.wait(5)
            processed.append(i)
        return {"processed": len(processed)}

    rec = mgr.submit("run", fn)
    assert in_step.wait(5)
    assert mgr.cancel(rec.task_id) is True

    snap = mgr.get(rec.task_id)
    assert snap is not None
    assert snap.status == TaskStatus.running
    assert "Cancelling" in snap.progress.label

    release.set()
    _wait(lambda: mgr.get(rec.task_id).status == TaskStatus.cancelled)  # type: ignore[union-attr]
    final = mgr.get(rec.task_id)
    assert final is not None and final.finished_at is not None
    assert processed == [0]  # in-flight step finished, later steps skipped
    assert final.result == {"processed": 1}
    assert mgr.cancel(rec.task_id) is False  # already terminal


def test_cancel_unknown_or_finished_task() -> None:
    mgr = TaskManager()
    assert mgr.cancel("nope") is False
    rec = mgr.submit("noop", lambda _h: {"ok": 1})
    _wait(lambda: mgr.get(rec.task_id).status == TaskStatus.succeeded)  # type: ignore[union-attr]
    assert mgr.cancel(rec.task_id) is False
