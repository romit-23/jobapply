"""Cross-run SQLite ledger for deduplication (sqlmodel)."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sqlalchemy import text
from sqlalchemy.engine import Engine
from sqlmodel import Field, Session, SQLModel, create_engine, select

from jobapply.models import LedgerStatus


class JobLedgerEntry(SQLModel, table=True):
    __tablename__ = "jobs_ledger"

    id: str = Field(primary_key=True, max_length=64)
    profile_hash: str = Field(index=True, max_length=64)
    status: str = Field(default=LedgerStatus.pending.value, index=True)
    site: str = ""
    company: str = ""
    title: str = ""
    location: str = ""
    apply_url: str | None = None
    job_url: str | None = None
    run_id: str = ""
    paths_json: str | None = None
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


def default_ledger_path(cwd: Path | None = None) -> Path:
    """Workspace-local ledger at ``<cwd>/.jobapply/ledger.db``.

    Keeping the ledger inside the project directory means each workspace has
    its own dedupe history; collaborators / multiple checkouts won't clobber
    each other and removing the project cleans up its state. Override via
    ``ledger_path`` in ``jobapply.toml`` if you want a global ledger.
    """
    base = cwd or Path.cwd()
    p = base / ".jobapply"
    p.mkdir(parents=True, exist_ok=True)
    return p / "ledger.db"


def get_engine(db_path: Path | None = None) -> Engine:
    path = db_path or default_ledger_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    return create_engine(f"sqlite:///{path}", echo=False)


def init_db(engine: Engine) -> None:
    SQLModel.metadata.create_all(engine, tables=[JobLedgerEntry.__table__])  # type: ignore[attr-defined]


def should_skip(
    engine: Engine,
    job_id: str,
    profile_hash: str,
    *,
    skip_if_done: bool = True,
) -> bool:
    """Return True if this job+profile is already fully processed."""
    if not skip_if_done:
        return False
    with Session(engine) as session:
        row = session.get(JobLedgerEntry, job_id)
        if row is None:
            return False
        if row.profile_hash != profile_hash:
            return False
        return row.status in (LedgerStatus.done.value, LedgerStatus.skipped.value)


def upsert_pending(
    engine: Engine,
    *,
    job_id: str,
    profile_hash: str,
    site: str,
    company: str,
    title: str,
    location: str,
    apply_url: str | None,
    job_url: str | None,
    run_id: str,
) -> bool:
    """
    Insert pending row if new. Returns True if inserted, False if existed.
    """
    now = datetime.now(UTC)
    with Session(engine) as session:
        existing = session.get(JobLedgerEntry, job_id)
        if existing is None:
            session.add(
                JobLedgerEntry(
                    id=job_id,
                    profile_hash=profile_hash,
                    status=LedgerStatus.pending.value,
                    site=site,
                    company=company,
                    title=title,
                    location=location,
                    apply_url=apply_url,
                    job_url=job_url,
                    run_id=run_id,
                    created_at=now,
                    updated_at=now,
                )
            )
            session.commit()
            return True
        # Refresh metadata for cross-run visibility
        existing.updated_at = now
        existing.run_id = run_id
        session.add(existing)
        session.commit()
        return False


def update_status(
    engine: Engine,
    job_id: str,
    status: LedgerStatus,
    *,
    paths: dict[str, Any] | None = None,
    run_id: str | None = None,
) -> None:
    now = datetime.now(UTC)
    with Session(engine) as session:
        row = session.get(JobLedgerEntry, job_id)
        if row is None:
            return
        row.status = status.value
        row.updated_at = now
        if paths is not None:
            row.paths_json = json.dumps(paths)
        if run_id:
            row.run_id = run_id
        session.add(row)
        session.commit()


def list_recent(engine: Engine, limit: int = 20) -> list[JobLedgerEntry]:
    with Session(engine) as session:
        stmt = select(JobLedgerEntry).order_by(text("updated_at DESC")).limit(limit)
        return list(session.exec(stmt))
