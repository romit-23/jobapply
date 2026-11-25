"""Tests for the centralized ``--workspace`` mode on ``search`` / ``run``.

Workspace mode lets the user accumulate every searched / tailored job
into one folder backed by a SQLite catalog (``workspace.db``).
Subsequent ``--workspace`` invocations dedupe against the catalog so
the user never sees the same job twice unless they pass ``--force``.
These tests exercise the orchestration end-to-end with the search
agent stubbed (no network, no LLM).
"""

from __future__ import annotations

import csv
import json
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from jobapply import cli as cli_module
from jobapply.cli import app
from jobapply.models import FitScore, RawJob
from jobapply.workspace import Workspace


def _raw(jid: str, *, title: str = "ML Engineer", company: str = "Acme") -> RawJob:
    return RawJob(
        job_id=jid,
        title=title,
        company=company,
        location="Remote",
        description="Build ML systems at scale.",
        job_url=f"https://example.com/jobs/{jid}",
        apply_url=f"https://example.com/apply/{jid}",
        site="indeed",
    )


def _write_minimal_toml(path: Path, *, provider: str = "openai") -> None:
    path.write_text(
        f'provider = "{provider}"\n'
        f'profile_path = "profile.json"\n'
        f'output_dir = "output"\n'
        f"results_wanted = 5\n"
        f"hours_old = 24\n"
        f'sites = ["indeed"]\n'
        f"[providers.{provider}]\n"
        f'api_key = "test-key"\n'
        f'model = "test-model"\n',
        encoding="utf-8",
    )


# ---------------------------------------------------------------------------
# `jobapply search --workspace`
# ---------------------------------------------------------------------------


def test_search_workspace_creates_centralized_layout(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A first ``--workspace`` search should populate ``workspace.db``,
    ``jobs.json``, ``jobs.csv``, and per-job folders directly under the
    workspace dir (no ``search-<ts>`` subdir)."""
    monkeypatch.chdir(tmp_path)
    _write_minimal_toml(tmp_path / "jobapply.toml")
    workspace = tmp_path / "ws"

    monkeypatch.setattr(
        cli_module,
        "iter_search_jobs",
        lambda inp: iter([_raw("a1"), _raw("b2", company="Globex")]),
    )

    result = CliRunner().invoke(
        app,
        [
            "search",
            "--titles",
            "ML Engineer",
            "--workspace",
            str(workspace),
            "--yes",
        ],
    )
    assert result.exit_code == 0, result.output

    # No timestamped subdir was created — the workspace itself is the
    # output directory.
    assert (
        not list((tmp_path / "output").glob("search-*")) if (tmp_path / "output").is_dir() else True
    )
    assert (workspace / "workspace.db").is_file()
    assert (workspace / "jobs.json").is_file()
    assert (workspace / "jobs.csv").is_file()
    assert (workspace / "meta.json").is_file()

    rows = list(csv.DictReader((workspace / "jobs.csv").open(encoding="utf-8")))
    assert {r["job_id"] for r in rows} == {"a1", "b2"}

    # Workspace catalog should mirror the on-disk index.
    ws = Workspace.open(workspace)
    assert ws.count() == 2

    # Per-job JSONs land under the workspace's `jobs/` subdir.
    job_jsons = sorted((workspace / "jobs").glob("*/job.json"))
    assert len(job_jsons) == 2

    meta = json.loads((workspace / "meta.json").read_text(encoding="utf-8"))
    assert meta["new_jobs"] == 2
    assert meta["duplicate_jobs"] == 0
    assert meta["workspace"].endswith("ws")


def test_search_workspace_appends_only_new_jobs_on_second_run(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Two consecutive ``--workspace`` searches should accumulate the
    union of fetched jobs without duplicating rows in ``jobs.csv`` or
    inflating the catalog beyond the unique-job count."""
    monkeypatch.chdir(tmp_path)
    _write_minimal_toml(tmp_path / "jobapply.toml")
    workspace = tmp_path / "ws"

    # First search returns a1 + b2.
    monkeypatch.setattr(
        cli_module,
        "iter_search_jobs",
        lambda inp: iter([_raw("a1"), _raw("b2", company="Globex")]),
    )
    result = CliRunner().invoke(
        app,
        ["search", "-t", "X", "-w", str(workspace), "--yes"],
    )
    assert result.exit_code == 0, result.output

    # Second search returns a1 again (duplicate) + a fresh c3.
    monkeypatch.setattr(
        cli_module,
        "iter_search_jobs",
        lambda inp: iter([_raw("a1"), _raw("c3", title="Lead MLE")]),
    )
    result = CliRunner().invoke(
        app,
        ["search", "-t", "X", "-w", str(workspace), "--yes"],
    )
    assert result.exit_code == 0, result.output
    # The console output should announce the dedupe outcome.
    assert "1 new" in result.output
    assert "1 duplicate" in result.output

    # Final state contains exactly the union of both fetches.
    rows = list(csv.DictReader((workspace / "jobs.csv").open(encoding="utf-8")))
    assert {r["job_id"] for r in rows} == {"a1", "b2", "c3"}

    ws = Workspace.open(workspace)
    assert ws.count() == 3
    history = ws.list_searches()
    assert len(history) == 2
    # Most recent is at index 0 (descending by id).
    assert history[0].new_jobs == 1
    assert history[0].duplicate_jobs == 1
    assert history[1].new_jobs == 2
    assert history[1].duplicate_jobs == 0


def test_search_workspace_force_re_fetches_existing_jobs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With ``--force`` the dedupe check is skipped and the already-known
    job is re-upserted (useful when the JD body changed and the user
    wants to refresh the description)."""
    monkeypatch.chdir(tmp_path)
    _write_minimal_toml(tmp_path / "jobapply.toml")
    workspace = tmp_path / "ws"

    monkeypatch.setattr(cli_module, "iter_search_jobs", lambda inp: iter([_raw("a1")]))
    result = CliRunner().invoke(
        app,
        ["search", "-t", "X", "-w", str(workspace), "--yes"],
    )
    assert result.exit_code == 0, result.output

    # Re-fetch with --force; the same job_id should be processed again
    # (counted as duplicate since the row already existed) without
    # being silently skipped.
    monkeypatch.setattr(cli_module, "iter_search_jobs", lambda inp: iter([_raw("a1")]))
    result = CliRunner().invoke(
        app,
        ["search", "-t", "X", "-w", str(workspace), "--force", "--yes"],
    )
    assert result.exit_code == 0, result.output
    ws = Workspace.open(workspace)
    assert ws.count() == 1
    history = ws.list_searches()
    # Latest invocation processed the row even though it already existed.
    assert history[0].new_jobs == 0
    assert history[0].duplicate_jobs == 1


def test_search_workspace_with_score_persists_fit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``--score`` results should land in the workspace catalog so a
    follow-up ``jobapply run --workspace`` sees the fit score it would
    have computed itself."""
    monkeypatch.chdir(tmp_path)
    _write_minimal_toml(tmp_path / "jobapply.toml")
    (tmp_path / "profile.json").write_text(
        json.dumps(
            {
                "name": "Jane",
                "email": "jane@example.com",
                "experience": [{"company": "X", "role": "Eng", "bullets": ["b"]}],
                "education": [{"school": "MIT", "degree": "BS"}],
                "skills": ["Python"],
            }
        ),
        encoding="utf-8",
    )
    workspace = tmp_path / "ws"

    monkeypatch.setattr(cli_module, "iter_search_jobs", lambda inp: iter([_raw("a1")]))
    monkeypatch.setattr(cli_module, "create_chat_model", lambda *a, **kw: object())
    monkeypatch.setattr(
        cli_module,
        "score_fit",
        lambda *a, **kw: FitScore(score=0.81, rationale="strong match"),
    )

    result = CliRunner().invoke(
        app,
        [
            "search",
            "-t",
            "X",
            "-w",
            str(workspace),
            "--score",
            "--yes",
        ],
    )
    assert result.exit_code == 0, result.output

    ws = Workspace.open(workspace)
    rec = ws.get("a1")
    assert rec is not None
    assert rec.fit is not None
    assert rec.fit.score == pytest.approx(0.81)


# ---------------------------------------------------------------------------
# `jobapply workspace` summary command
# ---------------------------------------------------------------------------


def test_workspace_summary_command_shows_history(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(tmp_path)
    _write_minimal_toml(tmp_path / "jobapply.toml")
    workspace = tmp_path / "ws"
    monkeypatch.setattr(cli_module, "iter_search_jobs", lambda inp: iter([_raw("a1")]))
    CliRunner().invoke(
        app,
        ["search", "-t", "X", "-w", str(workspace), "--yes"],
    )
    result = CliRunner().invoke(app, ["workspace", str(workspace)])
    assert result.exit_code == 0, result.output
    assert "1 job" in result.output
    assert "search" in result.output


def test_workspace_summary_rejects_missing_workspace(tmp_path: Path) -> None:
    """The summary command should fail loudly when pointed at a path
    that has no ``workspace.db`` (instead of silently creating one)."""
    result = CliRunner().invoke(app, ["workspace", str(tmp_path / "nope")])
    assert result.exit_code != 0
    assert "Not a workspace" in result.output


# ---------------------------------------------------------------------------
# Workspace persistence helpers (unit tests)
# ---------------------------------------------------------------------------


def test_workspace_upsert_returns_true_only_for_new_rows(tmp_path: Path) -> None:
    """``Workspace.upsert_job`` distinguishes inserts from updates so the
    CLI can tally "X new, Y duplicate"."""
    ws = Workspace.open(tmp_path / "w")
    rec = _raw("a1")
    from jobapply.cli import _record_from_raw_job

    first = ws.upsert_job(_record_from_raw_job(rec))
    assert first is True
    second = ws.upsert_job(_record_from_raw_job(rec))
    assert second is False
    assert ws.count() == 1


def test_workspace_flush_files_round_trips_catalog(tmp_path: Path) -> None:
    """``flush_files`` should regenerate ``jobs.json`` from the DB so a
    crashed run can be rebuilt purely from ``workspace.db``."""
    ws = Workspace.open(tmp_path / "w")
    from jobapply.cli import _record_from_raw_job

    ws.upsert_job(_record_from_raw_job(_raw("a1")))
    ws.upsert_job(_record_from_raw_job(_raw("b2", company="Globex")))
    ws.flush_files(run_id="manual", profile_path="profile.json")

    data: dict[str, Any] = json.loads((tmp_path / "w" / "jobs.json").read_text(encoding="utf-8"))
    assert {j["job_id"] for j in data["jobs"]} == {"a1", "b2"}
    rows = list(csv.DictReader((tmp_path / "w" / "jobs.csv").open(encoding="utf-8")))
    assert {r["job_id"] for r in rows} == {"a1", "b2"}


# ---------------------------------------------------------------------------
# `jobapply run --workspace` (graph integration, mocked LLM)
# ---------------------------------------------------------------------------


def _fake_chat_model() -> Any:
    """Re-uses the fake LLM pattern from test_graph_offline."""
    from jobapply.agents.resume_tailor import _TailoredBullets
    from jobapply.models import CoverLetter, FitScore, TailoredResume

    class _FakeStructured:
        def __init__(self, out: Any) -> None:
            self._out = out

        def invoke(self, _msgs: Any) -> Any:
            return self._out

    class FakeChatModel:
        def with_structured_output(self, schema: type[Any]) -> Any:
            if schema.__name__ == "FitScore":
                return _FakeStructured(FitScore(score=0.9, rationale="ok"))
            # The resume-tailor agent now asks for a compact
            # ``_TailoredBullets`` payload when the graph passes a
            # structured profile (the standard production path) and
            # only falls back to the full ``TailoredResume`` schema
            # for legacy callers without a profile.
            if schema is _TailoredBullets:
                return _FakeStructured(
                    _TailoredBullets(
                        summary="s",
                        skills=["Python"],
                        experience_bullets=[],
                        project_bullets=[],
                    )
                )
            if schema.__name__ == "TailoredResume":
                return _FakeStructured(
                    TailoredResume(
                        document_title="Tester",
                        contact_line="t@e.com",
                        summary="s",
                        skills=["Python"],
                    )
                )
            if schema.__name__ == "CoverLetter":
                return _FakeStructured(CoverLetter(header="h", opening="o", body="b", closing="c"))
            raise AssertionError(f"unexpected schema {schema}")

    return FakeChatModel()


def _run_graph(initial: dict[str, Any], run_dir: Path, run_id: str) -> None:
    """Compile + invoke the graph directly (avoids CLI's PDF probes that
    transitively import weasyprint/numpy and segfault on this CI box)."""
    from jobapply.graph import compile_app

    ck = run_dir / "checkpoint.sqlite"
    if ck.is_file():
        ck.unlink()
    compiled, conn = compile_app(ck)
    try:
        compiled.invoke(initial, {"configurable": {"thread_id": run_id}})
    finally:
        conn.close()


def test_run_workspace_dedupes_already_processed_jobs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A second ``run --workspace`` against the same workspace should
    skip jobs already in ``workspace.db`` (status=cached) without
    re-invoking the tailor pipeline. Drives the graph directly so the
    CLI's PDF-backend probes (which transitively import numpy) don't
    interfere with the assertion."""
    import jobapply.graph_nodes as gn

    monkeypatch.setattr(gn, "create_chat_model", lambda *_a, **_k: _fake_chat_model())
    monkeypatch.setattr(gn, "md_to_pdf", lambda *_a, **_k: False)
    monkeypatch.setattr(gn, "tex_to_pdf", lambda *_a, **_k: None)
    monkeypatch.setattr(gn, "search_jobs", lambda inp: [_raw("a1")])

    workspace = tmp_path / "ws"
    workspace.mkdir()
    Workspace.open(workspace)  # init DB
    ledger = tmp_path / "ledger.db"
    profile = tmp_path / "profile.json"
    profile.write_text('{"name": "Me", "email": "m@e.com", "skills": ["Python"]}', encoding="utf-8")
    from jobapply.models import JobSearchInput

    inp = JobSearchInput(titles=["Python"], skills=["Python"], location="Remote")
    base_state: dict[str, Any] = {
        "run_id": "run-test",
        "run_dir": str(workspace.resolve()),
        "profile_path": str(profile.resolve()),
        "profile_text": profile.read_text(encoding="utf-8"),
        "profile_hash": "deadbeef",
        "profile_skills": ["Python"],
        "provider": "gemini",
        "model": "fake",
        "min_fit": 0.1,
        "with_networking": False,
        "no_pdf": True,
        "force": False,
        "ledger_db_path": str(ledger.resolve()),
        "search_input": inp.model_dump(mode="json"),
        "queue": [],
        "workspace_path": str(workspace.resolve()),
    }

    # First run — fetches+tailors a1.
    first_state = dict(base_state)
    first_state["jobs_raw"] = []  # let search_node fill it
    _run_graph(first_state, workspace, "run-test-1")

    ws = Workspace.open(workspace)
    rec = ws.get("a1")
    assert rec is not None, "first run should have populated workspace.db"
    assert str(rec.status) == "done"

    # Second run with the same fetched job: workspace dedupe should
    # mark it cached without re-processing through the tailor stack.
    def _spy_tailor(*_a: Any, **_kw: Any) -> Any:
        raise AssertionError("tailor_resume should not run for workspace-cached jobs")

    monkeypatch.setattr(gn, "tailor_resume", _spy_tailor)

    second_state = dict(base_state)
    second_state["run_id"] = "run-test-2"
    second_state["jobs_raw"] = []
    _run_graph(second_state, workspace, "run-test-2")

    # Final state still has exactly one job in the workspace.
    final = ws.get("a1")
    assert final is not None
    assert ws.count() == 1
