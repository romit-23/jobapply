"""Rich progress while streaming LangGraph events."""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

from rich.console import Console
from rich.progress import BarColumn, Progress, SpinnerColumn, TextColumn, TimeElapsedColumn


def stream_run(
    app: Any,
    initial_state: dict[str, Any],
    config: dict[str, Any],
    *,
    console: Console | None = None,
) -> Iterator[dict[str, Any]]:
    """Yield chunks from graph.stream while updating a progress bar.

    Total = jobs queued by ``dedupe`` + jobs already cached (we count them
    too so the bar stays meaningful when the ledger short-circuits work).
    Completed advances on every ``process_one`` node update.
    """
    c = console or Console()
    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        BarColumn(),
        TextColumn("{task.completed}/{task.total}"),
        TimeElapsedColumn(),
        console=c,
        transient=False,
    ) as progress:
        task = progress.add_task("jobapply", total=None)
        last_desc = ""
        total_known = False
        completed = 0
        total = 0
        for chunk in app.stream(initial_state, config, stream_mode="updates"):
            for node, payload in chunk.items():
                if node == "__end__":
                    continue
                if isinstance(payload, dict):
                    if node == "dedupe" and not total_known:
                        queue = payload.get("queue") or []
                        results = payload.get("results") or []
                        total = len(queue) + len(results)
                        completed = len(results)
                        total_known = True
                        progress.update(task, total=total, completed=completed)
                    elif node == "process_one" and total_known and payload.get("results"):
                        completed += 1
                        progress.update(task, completed=completed)
                desc = node
                if desc != last_desc:
                    progress.update(task, description=desc)
                    last_desc = desc
            yield chunk
        if not total_known:
            total = max(total, 1)
            completed = total
        progress.update(
            task,
            description="complete",
            completed=completed,
            total=total or completed or 1,
        )
