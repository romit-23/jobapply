"""LangGraph node callables."""

from __future__ import annotations

import json
import traceback
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sqlmodel import Session

from jobapply.agents.cover_letter import write_cover_letter
from jobapply.agents.fit_scorer import score_fit
from jobapply.agents.networking import write_networking
from jobapply.agents.resume_tailor import tailor_resume
from jobapply.agents.search import search_jobs
from jobapply.config import load_config
from jobapply.graph_state import GraphState
from jobapply.jd_extract import extract_application_hints
from jobapply.ledger import (
    JobLedgerEntry,
    get_engine,
    init_db,
    should_skip,
    update_status,
    upsert_pending,
)
from jobapply.llm import create_chat_model
from jobapply.models import (
    JobArtifacts,
    JobRecord,
    JobSearchInput,
    JobsIndex,
    LedgerStatus,
    RawJob,
)
from jobapply.nodes.persist import upsert_job_record, write_job_json
from jobapply.nodes.render import (
    fill_cover_letter_tex,
    fill_resume_tex,
    md_to_pdf,
    render_cover_markdown,
    render_resume_markdown,
    slug_from_paths,
    tex_to_pdf,
)
from jobapply.profile import (
    Profile,
    ProfileLoadError,
    load_profile,
    profile_skill_list,
    profile_to_text,
)
from jobapply.run_meta import read_meta, write_meta
from jobapply.utils import profile_hash as profile_hash_fn
from jobapply.utils import slugify
from jobapply.workspace import Workspace


def _template_index_from_state(state: GraphState) -> JobsIndex:
    """Build the empty JobsIndex header used to seed jobs.json."""
    inp = JobSearchInput.model_validate(state["search_input"])
    return JobsIndex(
        run_id=state["run_id"],
        search=inp,
        profile_path=state["profile_path"],
        provider=state["provider"],
        model=state["model"],
        jobs=[],
    )


def search_node(state: GraphState) -> dict[str, Any]:
    if state.get("skip_search"):
        return {}
    inp = JobSearchInput.model_validate(state["search_input"])
    jobs = search_jobs(inp)
    raw = [j.model_dump(mode="json") for j in jobs]
    run_dir = Path(state["run_dir"])
    meta = read_meta(run_dir)
    meta.update(
        {
            "run_id": state["run_id"],
            "search_input": state["search_input"],
            "profile_path": str(Path(state["profile_path"]).resolve()),
            "provider": state["provider"],
            "model": state["model"],
            "min_fit": state["min_fit"],
            "with_networking": state.get("with_networking", False),
            "no_pdf": state.get("no_pdf", False),
            "jobs_raw": raw,
        },
    )
    write_meta(run_dir, meta)
    return {"jobs_raw": raw, "log": [f"search: found {len(raw)} jobs"]}


def _open_workspace(state: GraphState) -> Workspace | None:
    """Return a :class:`Workspace` when the run is in workspace mode."""
    ws_path = state.get("workspace_path")
    if not isinstance(ws_path, str) or not ws_path:
        return None
    return Workspace.open(Path(ws_path))


def dedupe_node(state: GraphState) -> dict[str, Any]:
    ledger_path = Path(state["ledger_db_path"])
    engine = get_engine(ledger_path)
    init_db(engine)
    ph = state["profile_hash"]
    force = bool(state.get("force"))
    run_dir = Path(state["run_dir"])
    template_index = _template_index_from_state(state)
    workspace = _open_workspace(state)
    workspace_search_id = state.get("workspace_search_id") if workspace else None

    raw_dicts = state.get("jobs_raw") or []
    queue: list[dict[str, Any]] = []
    cached_records: list[dict[str, Any]] = []

    for d in raw_dicts:
        job = RawJob.model_validate(d)
        # Backfill application hints for jobs that came in without
        # them (e.g. legacy state restored by ``jobapply resume``);
        # ``iter_search_jobs`` already sets this for fresh batches.
        if job.application is None and job.description:
            hints = extract_application_hints(job.description)
            if hints.has_any:
                job.application = hints
        # Persist a per-job JSON for every fetched job so users can hand
        # the path to `jobapply tailor --job <path>` later. We do this
        # for cached jobs too — they're the ones most likely to be
        # tailored ad-hoc since the run flow won't process them again.
        _job_dir = slug_from_paths(slugify(job.title, job.company, job.job_id), run_dir)
        write_job_json(_job_dir, job.model_dump(mode="json"))

        # Workspace-level dedupe. Independent from the global ledger so
        # users can populate a workspace from a different profile and
        # still skip duplicates. Falls through to the ledger checks
        # below when the workspace doesn't know about the job yet.
        if workspace is not None and not force and workspace.is_seen(job.job_id):
            existing = workspace.get(job.job_id)
            cached_rec = existing or JobRecord(
                job_id=job.job_id,
                title=job.title,
                company=job.company,
                location=job.location,
                description=(job.description or "")[:2000],
                job_url=job.job_url,
                apply_url=job.apply_url,
                site=job.site,
                status=LedgerStatus.cached,
                application=job.application,
                error="Already in workspace; pass --force to re-process.",
                processed_at=datetime.now(UTC),
            )
            # Touch last_seen so workspace history reflects the latest run.
            workspace.upsert_job(cached_rec, search_id=workspace_search_id)
            upsert_job_record(run_dir, template_index, cached_rec)
            cached_records.append(cached_rec.model_dump(mode="json"))
            continue

        if not force and should_skip(engine, job.job_id, ph, skip_if_done=True):
            with Session(engine) as session:
                row = session.get(JobLedgerEntry, job.job_id)
            paths_json: dict[str, Any] = {}
            prior_status = LedgerStatus.cached
            if row is not None:
                if row.paths_json:
                    try:
                        paths_json = json.loads(row.paths_json)
                    except json.JSONDecodeError:
                        paths_json = {}
                if row.status == LedgerStatus.skipped.value:
                    prior_status = LedgerStatus.skipped
            note = (
                f"Already processed in {row.run_id} (status={row.status}); pass --force to re-run."
                if row is not None
                else "Already in ledger; pass --force to re-run."
            )
            cached_rec = JobRecord(
                job_id=job.job_id,
                title=job.title,
                company=job.company,
                location=job.location,
                description=(job.description or "")[:2000],
                job_url=job.job_url,
                apply_url=job.apply_url,
                site=job.site,
                status=prior_status,
                application=job.application,
                artifacts=JobArtifacts.model_validate(
                    {k: v for k, v in paths_json.items() if v},
                ),
                error=note,
                processed_at=datetime.now(UTC),
            )
            upsert_job_record(run_dir, template_index, cached_rec)
            cached_records.append(cached_rec.model_dump(mode="json"))
            continue
        upsert_pending(
            engine,
            job_id=job.job_id,
            profile_hash=ph,
            site=job.site,
            company=job.company,
            title=job.title,
            location=job.location,
            apply_url=job.apply_url,
            job_url=job.job_url,
            run_id=state["run_id"],
        )
        queue.append(job.model_dump(mode="json"))

    log: list[str] = [f"dedupe: {len(queue)} jobs to process"]
    if cached_records:
        log.append(
            f"dedupe: {len(cached_records)} jobs already in ledger "
            "(written as cached; use --force to re-run them)",
        )
    return {
        "queue": queue,
        "results": cached_records,
        "log": log,
    }


def process_one_node(state: GraphState) -> dict[str, Any]:
    q = state.get("queue") or []
    if not q:
        return {}
    job = RawJob.model_validate(q[0])
    new_queue = q[1:]
    run_dir = Path(state["run_dir"])
    ledger_path = Path(state["ledger_db_path"])
    engine = get_engine(ledger_path)
    init_db(engine)
    workspace = _open_workspace(state)
    workspace_search_id = state.get("workspace_search_id") if workspace else None
    profile_text = state["profile_text"]
    inp = JobSearchInput.model_validate(state["search_input"])
    min_fit = float(state.get("min_fit", 0.35))
    slug = slugify(job.title, job.company, job.job_id)
    job_dir = slug_from_paths(slug, run_dir)
    template_index = _template_index_from_state(state)
    write_job_json(job_dir, job.model_dump(mode="json"))

    try:
        llm = create_chat_model(state["provider"], state["model"], cfg=load_config())
        fit = score_fit(llm, profile_text=profile_text, job=job, skills=inp.skills)
        if fit.score < min_fit:
            rec = JobRecord(
                job_id=job.job_id,
                title=job.title,
                company=job.company,
                location=job.location,
                description=job.description[:5000],
                job_url=job.job_url,
                apply_url=job.apply_url,
                site=job.site,
                status=LedgerStatus.skipped,
                fit=fit,
                application=job.application,
                processed_at=datetime.now(UTC),
            )
            upsert_job_record(run_dir, template_index, rec)
            update_status(engine, job.job_id, LedgerStatus.skipped, run_id=state["run_id"])
            if workspace is not None:
                workspace.upsert_job(rec, search_id=workspace_search_id)
            return {
                "queue": new_queue,
                "results": [rec.model_dump(mode="json")],
                "log": [f"skipped low fit: {job.title} ({fit.score:.2f})"],
            }

        # Best-effort load of the structured profile so the tailor
        # agent can fall back to the source experience / projects
        # when the LLM drops them. ``profile_path`` is set by
        # ``cli.run`` at run-start; if the file is gone or malformed
        # by the time we reach this job we just skip the safety net
        # and let the LLM output stand.
        struct_profile: Profile | None = None
        prof_path_str = state.get("profile_path")
        if isinstance(prof_path_str, str) and prof_path_str:
            try:
                struct_profile = load_profile(Path(prof_path_str))
            except (ProfileLoadError, OSError):
                struct_profile = None
        resume = tailor_resume(
            llm,
            profile_text=profile_text,
            job=job,
            skills=inp.skills,
            profile_skills=list(state.get("profile_skills") or []),
            profile=struct_profile,
        )
        update_status(engine, job.job_id, LedgerStatus.tailored, run_id=state["run_id"])

        cover = write_cover_letter(llm, profile_text=profile_text, job=job, resume=resume)
        networking = None
        if state.get("with_networking"):
            networking = write_networking(llm, profile_text=profile_text, job=job)

        md_resume = render_resume_markdown(resume)
        md_cover = render_cover_markdown(cover)
        (job_dir / "resume.md").write_text(md_resume, encoding="utf-8")
        (job_dir / "cover_letter.md").write_text(md_cover, encoding="utf-8")
        tex = fill_resume_tex(resume)
        (job_dir / "resume.tex").write_text(tex, encoding="utf-8")
        cover_tex = fill_cover_letter_tex(
            cover,
            contact=resume.contact,
            name=resume.document_title or "Candidate",
            role=job.title,
        )
        (job_dir / "cover_letter.tex").write_text(cover_tex, encoding="utf-8")

        artifacts: dict[str, str | None] = {
            "job_json": str((job_dir / "job.json").resolve()),
            "resume_md": str((job_dir / "resume.md").resolve()),
            "resume_tex": str((job_dir / "resume.tex").resolve()),
            "cover_letter_md": str((job_dir / "cover_letter.md").resolve()),
            "cover_letter_tex": str((job_dir / "cover_letter.tex").resolve()),
            "resume_pdf": None,
            "resume_latex_pdf": None,
            "cover_letter_pdf": None,
            "cover_letter_latex_pdf": None,
            "networking_json": None,
        }
        if networking is not None:
            np = job_dir / "networking.json"
            np.write_text(json.dumps(networking.model_dump(), indent=2), encoding="utf-8")
            artifacts["networking_json"] = str(np.resolve())

        if not state.get("no_pdf", False):
            rp = job_dir / "resume.pdf"
            if md_to_pdf(job_dir / "resume.md", rp):
                artifacts["resume_pdf"] = str(rp.resolve())
            cp = job_dir / "cover_letter.pdf"
            if md_to_pdf(job_dir / "cover_letter.md", cp):
                artifacts["cover_letter_pdf"] = str(cp.resolve())
            lp = tex_to_pdf(job_dir / "resume.tex", job_dir)
            if lp and lp.is_file():
                artifacts["resume_latex_pdf"] = str(lp.resolve())
            clp = tex_to_pdf(job_dir / "cover_letter.tex", job_dir)
            if clp and clp.is_file():
                artifacts["cover_letter_latex_pdf"] = str(clp.resolve())

        rec = JobRecord(
            job_id=job.job_id,
            title=job.title,
            company=job.company,
            location=job.location,
            description=job.description[:8000],
            job_url=job.job_url,
            apply_url=job.apply_url,
            site=job.site,
            status=LedgerStatus.done,
            fit=fit,
            application=job.application,
            tailored_resume=resume,
            cover_letter=cover,
            networking=networking,
            artifacts=JobArtifacts.model_validate({k: v for k, v in artifacts.items() if v}),
            processed_at=datetime.now(UTC),
        )
        upsert_job_record(run_dir, template_index, rec)
        update_status(
            engine,
            job.job_id,
            LedgerStatus.done,
            paths={k: v for k, v in artifacts.items() if v},
            run_id=state["run_id"],
        )
        if workspace is not None:
            workspace.upsert_job(rec, search_id=workspace_search_id)
        return {
            "queue": new_queue,
            "results": [rec.model_dump(mode="json")],
            "log": [f"done: {job.title}"],
        }
    except Exception as e:  # noqa: BLE001
        tb = traceback.format_exc()
        rec = JobRecord(
            job_id=job.job_id,
            title=job.title,
            company=job.company,
            location=job.location,
            description=(job.description or "")[:2000],
            job_url=job.job_url,
            apply_url=job.apply_url,
            site=job.site,
            status=LedgerStatus.failed,
            application=job.application,
            error=f"{e}\n{tb}",
            processed_at=datetime.now(UTC),
        )
        upsert_job_record(run_dir, template_index, rec)
        update_status(engine, job.job_id, LedgerStatus.failed, run_id=state["run_id"])
        if workspace is not None:
            workspace.upsert_job(rec, search_id=workspace_search_id)
        return {
            "queue": new_queue,
            "results": [rec.model_dump(mode="json")],
            "log": [f"failed: {job.title}: {e}"],
        }


def bootstrap_resume_state(run_dir: Path, ledger_db_path: Path) -> dict[str, Any]:
    """Build initial graph state from meta.json for `jobapply resume`.

    Loads the structured ``profile.json`` referenced by the run meta and
    derives both the agent-facing ``profile_text`` and the canonical
    ``profile_skills`` list from it. Older runs that pointed at a
    legacy ``profile.md`` are no longer supported — re-run
    ``jobapply init`` to regenerate the JSON profile.
    """
    meta = read_meta(run_dir)
    if not meta:
        raise FileNotFoundError(f"No meta.json in {run_dir}")
    profile_path = Path(meta["profile_path"])
    profile = load_profile(profile_path)
    profile_text = profile_to_text(profile)
    return {
        "run_id": meta["run_id"],
        "run_dir": str(run_dir.resolve()),
        "profile_path": str(profile_path.resolve()),
        "profile_text": profile_text,
        "profile_hash": profile_hash_fn(profile_text),
        "profile_skills": profile_skill_list(profile),
        "provider": meta["provider"],
        "model": meta["model"],
        "min_fit": float(meta.get("min_fit", 0.35)),
        "with_networking": bool(meta.get("with_networking", False)),
        "no_pdf": bool(meta.get("no_pdf", False)),
        "force": False,
        "ledger_db_path": str(ledger_db_path.resolve()),
        "search_input": meta["search_input"],
        "jobs_raw": meta.get("jobs_raw", []),
        "queue": [],
        "skip_search": True,
    }
