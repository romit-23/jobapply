"""Centralized SQLite-backed workspace for ``jobapply search`` / ``jobapply run``.

A *workspace* is a single directory the user passes via ``--workspace``
that accumulates every job ever searched / run into one place. It
replaces the timestamped ``output/search-<ts>`` and ``output/run-<ts>``
folders for users who want a long-lived, dedupe-aware checkout.

Layout::

    <workspace>/
    ├── workspace.db          # SQLite (workspace_jobs + workspace_searches)
    ├── jobs.json             # Master index, regenerated from DB
    ├── jobs.csv              # Master CSV, regenerated from DB
    ├── meta.json             # Latest search snapshot + counts
    ├── checkpoint.sqlite     # LangGraph checkpoint (used by `run`)
    └── jobs/<slug>/...       # Per-job artifacts (job.json, resume.md, ...)

The DB is the source of truth — ``jobs.json`` / ``jobs.csv`` are
re-rendered from it after every operation so the on-disk files always
reflect the full state of the workspace, not just the current run.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sqlalchemy.engine import Engine
from sqlmodel import Field, Session, SQLModel, col, create_engine, select

from jobapply.models import (
    ApplicationHints,
    FitScore,
    JobArtifacts,
    JobRecord,
    JobSearchInput,
    JobsIndex,
    LedgerStatus,
)
from jobapply.nodes.persist import write_jobs_csv
from jobapply.utils import atomic_write_json

WORKSPACE_DB_FILENAME = "workspace.db"
WORKSPACE_META_FILENAME = "meta.json"


class WorkspaceJobEntry(SQLModel, table=True):
    """One row per unique job ever observed in this workspace.

    The full :class:`JobRecord` is stored as JSON in ``record_json`` so
    we can round-trip every field (including nested ``TailoredResume``,
    ``CoverLetter``, etc.) without flattening to columns. The denormalized
    columns (``title``, ``company``, …) exist purely so SQLite ``WHERE``
    queries are cheap when users filter the DB by hand.
    """

    __tablename__ = "workspace_jobs"

    job_id: str = Field(primary_key=True, max_length=64)
    title: str = ""
    company: str = ""
    location: str = ""
    site: str = ""
    job_url: str | None = None
    apply_url: str | None = None
    status: str = Field(default=LedgerStatus.pending.value, index=True)
    fit_score: float | None = None
    record_json: str = Field(
        default="{}",
        description="Full JobRecord serialized as JSON for round-tripping.",
    )
    first_search_id: int | None = Field(default=None, index=True)
    last_search_id: int | None = Field(default=None, index=True)
    first_seen_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    last_seen_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    processed_at: datetime | None = None


class WorkspaceSearchEntry(SQLModel, table=True):
    """One row per ``search`` / ``run`` invocation against this workspace.

    Lets the CLI surface a chronological history (``jobapply workspace``)
    and gives downstream tooling a way to ask "which search added this
    job to my workspace?" via the ``first_search_id`` /
    ``last_search_id`` foreign keys on :class:`WorkspaceJobEntry`.
    """

    __tablename__ = "workspace_searches"

    id: int | None = Field(default=None, primary_key=True)
    command: str = Field(max_length=16, description='"search" or "run".')
    search_input_json: str = ""
    provider: str = ""
    model: str = ""
    started_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    finished_at: datetime | None = None
    fetched: int = 0
    new_jobs: int = 0
    duplicate_jobs: int = 0


def _engine_for(path: Path) -> Engine:
    path.parent.mkdir(parents=True, exist_ok=True)
    return create_engine(f"sqlite:///{path}", echo=False)


def _row_to_record(row: WorkspaceJobEntry) -> JobRecord:
    """Hydrate a stored row back into a :class:`JobRecord`.

    Falls back to the denormalized columns when ``record_json`` is empty
    or unparseable (which only happens for hand-edited rows).
    """
    try:
        payload = json.loads(row.record_json) if row.record_json else {}
    except json.JSONDecodeError:
        payload = {}
    if isinstance(payload, dict) and payload:
        try:
            return JobRecord.model_validate(payload)
        except Exception:  # noqa: BLE001 - corrupt rows shouldn't kill the rebuild
            pass
    return JobRecord(
        job_id=row.job_id,
        title=row.title,
        company=row.company,
        location=row.location,
        site=row.site,
        job_url=row.job_url,
        apply_url=row.apply_url,
        status=LedgerStatus(row.status) if row.status else LedgerStatus.pending,
        fit=FitScore(score=row.fit_score) if row.fit_score is not None else None,
        processed_at=row.processed_at,
    )


class Workspace:
    """Handle for a workspace directory + its SQLite catalog.

    Open via :meth:`Workspace.open`; the DB is created on demand. All
    writes are wrapped in short SQLite sessions so concurrent CLI
    invocations don't trip over each other (the file is small enough
    that we don't bother with pooling).
    """

    _TABLES = [
        WorkspaceJobEntry.__table__,  # type: ignore[attr-defined]
        WorkspaceSearchEntry.__table__,  # type: ignore[attr-defined]
    ]

    def __init__(self, path: Path) -> None:
        self.path = path.resolve()
        self.path.mkdir(parents=True, exist_ok=True)
        self.db_path = self.path / WORKSPACE_DB_FILENAME
        self._engine = _engine_for(self.db_path)
        SQLModel.metadata.create_all(self._engine, tables=self._TABLES)

    @classmethod
    def open(cls, path: str | Path) -> Workspace:
        return cls(Path(path).expanduser())

    @property
    def jobs_dir(self) -> Path:
        d = self.path / "jobs"
        d.mkdir(parents=True, exist_ok=True)
        return d

    @property
    def meta_path(self) -> Path:
        return self.path / WORKSPACE_META_FILENAME

    @property
    def jobs_json_path(self) -> Path:
        return self.path / "jobs.json"

    # -------------------------------- Search log ---------------------------- #

    def start_search(
        self,
        *,
        command: str,
        search_input: JobSearchInput | dict[str, Any],
        provider: str = "",
        model: str = "",
    ) -> int:
        """Record the start of a search/run invocation. Returns search_id."""
        if isinstance(search_input, JobSearchInput):
            payload = search_input.model_dump(mode="json")
        else:
            payload = dict(search_input)
        entry = WorkspaceSearchEntry(
            command=command,
            search_input_json=json.dumps(payload, ensure_ascii=False),
            provider=provider,
            model=model,
            started_at=datetime.now(UTC),
        )
        with Session(self._engine) as s:
            s.add(entry)
            s.commit()
            s.refresh(entry)
            return int(entry.id or 0)

    def finish_search(
        self,
        search_id: int,
        *,
        fetched: int,
        new_jobs: int,
        duplicate_jobs: int,
    ) -> None:
        with Session(self._engine) as s:
            row = s.get(WorkspaceSearchEntry, search_id)
            if row is None:
                return
            row.finished_at = datetime.now(UTC)
            row.fetched = fetched
            row.new_jobs = new_jobs
            row.duplicate_jobs = duplicate_jobs
            s.add(row)
            s.commit()

    def list_searches(self, limit: int = 20) -> list[WorkspaceSearchEntry]:
        with Session(self._engine) as s:
            stmt = (
                select(WorkspaceSearchEntry)
                .order_by(WorkspaceSearchEntry.id.desc())  # type: ignore[union-attr]
                .limit(limit)
            )
            return list(s.exec(stmt))

    # -------------------------------- Job catalog --------------------------- #

    def is_seen(self, job_id: str) -> bool:
        with Session(self._engine) as s:
            return s.get(WorkspaceJobEntry, job_id) is not None

    def get(self, job_id: str) -> JobRecord | None:
        with Session(self._engine) as s:
            row = s.get(WorkspaceJobEntry, job_id)
            return _row_to_record(row) if row is not None else None

    def upsert_job(
        self,
        record: JobRecord,
        *,
        search_id: int | None = None,
    ) -> bool:
        """Insert ``record`` if new, otherwise refresh the stored row.

        Returns True when a new row was inserted (so callers can count
        "X new jobs" for the run summary). The full ``JobRecord`` is
        serialized to ``record_json`` so we can rebuild ``jobs.json``
        verbatim from the DB.
        """
        now = datetime.now(UTC)
        record_payload = record.model_dump(mode="json")
        record_blob = json.dumps(record_payload, ensure_ascii=False)
        fit_score = record.fit.score if record.fit else None
        with Session(self._engine) as s:
            existing = s.get(WorkspaceJobEntry, record.job_id)
            if existing is None:
                entry = WorkspaceJobEntry(
                    job_id=record.job_id,
                    title=record.title or "",
                    company=record.company or "",
                    location=record.location or "",
                    site=record.site or "",
                    job_url=record.job_url,
                    apply_url=record.apply_url,
                    status=str(record.status),
                    fit_score=fit_score,
                    record_json=record_blob,
                    first_search_id=search_id,
                    last_search_id=search_id,
                    first_seen_at=now,
                    last_seen_at=now,
                    processed_at=record.processed_at,
                )
                s.add(entry)
                s.commit()
                return True
            existing.title = record.title or existing.title
            existing.company = record.company or existing.company
            existing.location = record.location or existing.location
            existing.site = record.site or existing.site
            existing.job_url = record.job_url or existing.job_url
            existing.apply_url = record.apply_url or existing.apply_url
            existing.status = str(record.status)
            existing.fit_score = fit_score if fit_score is not None else existing.fit_score
            existing.record_json = record_blob
            existing.last_seen_at = now
            existing.processed_at = record.processed_at or existing.processed_at
            if search_id is not None:
                existing.last_search_id = search_id
                if existing.first_search_id is None:
                    existing.first_search_id = search_id
            s.add(existing)
            s.commit()
            return False

    def all_records(self) -> list[JobRecord]:
        """Hydrate every stored row back into a :class:`JobRecord`."""
        with Session(self._engine) as s:
            stmt = select(WorkspaceJobEntry).order_by(col(WorkspaceJobEntry.last_seen_at).desc())
            return [_row_to_record(r) for r in s.exec(stmt)]

    def count(self) -> int:
        with Session(self._engine) as s:
            return len(list(s.exec(select(WorkspaceJobEntry.job_id))))

    # ----------------------------- Disk artifacts --------------------------- #

    def flush_files(
        self,
        *,
        run_id: str,
        search_input: JobSearchInput | dict[str, Any] | None = None,
        profile_path: str = "",
        provider: str = "",
        model: str = "",
    ) -> tuple[Path, Path]:
        """Re-render ``jobs.json`` + ``jobs.csv`` from the DB.

        Always reflects the *full* workspace contents — every job ever
        added — so users get a stable spreadsheet to triage in.
        """
        if isinstance(search_input, JobSearchInput):
            search_model = search_input
        elif isinstance(search_input, dict):
            search_model = JobSearchInput.model_validate(search_input)
        else:
            search_model = JobSearchInput(titles=["workspace"])
        idx = JobsIndex(
            run_id=run_id,
            search=search_model,
            profile_path=profile_path,
            provider=provider,
            model=model,
            jobs=self.all_records(),
        )
        atomic_write_json(self.jobs_json_path, idx.model_dump(mode="json"))
        csv_path = write_jobs_csv(self.path, idx)
        return self.jobs_json_path, csv_path

    def write_meta(self, payload: dict[str, Any]) -> None:
        """Persist a human-readable meta.json with last-search context."""
        atomic_write_json(self.meta_path, payload)

    def read_meta(self) -> dict[str, Any]:
        if not self.meta_path.is_file():
            return {}
        try:
            data = json.loads(self.meta_path.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        except (OSError, json.JSONDecodeError):
            return {}


__all__ = [
    "Workspace",
    "WorkspaceJobEntry",
    "WorkspaceSearchEntry",
    "WORKSPACE_DB_FILENAME",
]


# ---- Small helpers used by the CLI ----------------------------------------- #


def record_from_entry_dump(payload: dict[str, Any]) -> JobRecord:
    """Best-effort hydrate a JobRecord from any dict shape we've stored.

    Tolerant of missing fields (older DB rows) so workspace upgrades
    are non-destructive.
    """
    safe = dict(payload)
    if "artifacts" not in safe:
        safe["artifacts"] = JobArtifacts().model_dump(mode="json")
    if "application" in safe and isinstance(safe["application"], dict):
        try:
            ApplicationHints.model_validate(safe["application"])
        except Exception:  # noqa: BLE001
            safe["application"] = None
    return JobRecord.model_validate(safe)
