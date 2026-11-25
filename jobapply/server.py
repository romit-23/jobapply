"""FastAPI app powering the JobApply web UI.

The server is a thin orchestration layer over the existing CLI
primitives (``Workspace``, ``iter_search_jobs``, ``score_fit``,
``tailor_for_job_description``, ``draft_application_email``) so the UI
and CLI never drift apart. All long-running operations are dispatched
through :mod:`jobapply.tasks` so the UI can poll progress instead of
blocking on multi-minute LLM batches.

The frontend (React + Vite, see ``web/``) is built into
``jobapply/web_dist/`` and served as static files. Any path that
doesn't match an ``/api`` route falls through to ``index.html`` so the
SPA's client-side router can handle it.
"""

from __future__ import annotations

import contextlib
import json
import threading
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from starlette.requests import Request

from jobapply.agents.email_drafter import draft_application_email
from jobapply.agents.fit_scorer import score_fit
from jobapply.agents.search import iter_search_jobs
from jobapply.config import (
    DEFAULT_MODELS,
    PROVIDER_NAMES,
    AppConfig,
    apply_latex_api_env,
    get_api_key,
    load_config,
    load_dotenv_if_present,
)
from jobapply.jd_extract import extract_application_hints
from jobapply.llm import create_chat_model
from jobapply.models import (
    ApplicationHints,
    FitScore,
    JobArtifacts,
    JobRecord,
    JobSearchInput,
    LedgerStatus,
    RawJob,
)
from jobapply.nodes.render import probe_tex_pdf_backend, tex_to_pdf
from jobapply.profile import (
    Profile,
    ProfileLoadError,
    load_profile,
    profile_skill_list,
    profile_to_text,
)
from jobapply.tailor_one import (
    tailor_for_job_description,
)
from jobapply.tasks import TaskHandle, TaskManager, get_default_manager
from jobapply.utils import slugify, stable_job_id
from jobapply.workspace import Workspace

DEFAULT_WEB_WORKSPACE = "output/web"
WEB_DIST_DIRNAME = "web_dist"


# --------------------------- Request / response models ---------------------- #


class SearchRequest(BaseModel):
    titles: list[str] = Field(..., min_length=1)
    skills: list[str] = Field(default_factory=list)
    location: str | None = None
    remote: bool = False
    results_wanted: int | None = None
    sites: list[str] | None = None
    score: bool = False
    provider: str | None = None
    model: str | None = None
    linkedin_descriptions: bool = True
    force: bool = False


class RunRequest(BaseModel):
    """Tailor a set of *already-cataloged* jobs in the workspace.

    The frontend selects jobs from the list view and POSTs their
    ``job_id`` values; the server runs the tailor pipeline against
    each (skipping the live search step). Set ``force=True`` to
    re-tailor jobs whose status is already ``done``.
    """

    job_ids: list[str] = Field(..., min_length=1)
    provider: str | None = None
    model: str | None = None
    no_pdf: bool = False
    force: bool = False


class TailorOneRequest(BaseModel):
    """Tailor a single job already in the workspace catalog."""

    provider: str | None = None
    model: str | None = None
    no_pdf: bool = False


class FreeformRequest(BaseModel):
    """Paste a job description and add a tailored entry to the workspace."""

    description: str = Field(..., min_length=20)
    title: str | None = None
    company: str | None = None
    location: str | None = None
    skills: list[str] = Field(default_factory=list)
    provider: str | None = None
    model: str | None = None
    no_pdf: bool = False


class EmailRequest(BaseModel):
    """Draft an application email for a job already in the workspace."""

    recipient: str = Field(..., min_length=3)
    additional_info: str = ""
    provider: str | None = None
    model: str | None = None


class ArtifactSaveRequest(BaseModel):
    """Body for ``PUT /api/jobs/{job_id}/artifacts/{name}``.

    The UI uses this to save edits made in the in-browser LaTeX
    editor. We deliberately ship the content as a single string
    rather than as a multipart upload — these files are small (well
    under 100 KB each) and a JSON round-trip composes nicely with
    React Query's mutation cache.
    """

    content: str


class StatusResponse(BaseModel):
    workspace: str
    workspace_total_jobs: int
    profile_path: str | None
    profile_loaded: bool
    profile_name: str
    provider: str
    model: str
    sites: list[str]
    results_wanted: int
    web_dist_present: bool


# ----------------------------- App-state container -------------------------- #


class ServerContext:
    """Bundle of long-lived dependencies the route handlers reach for.

    Keeping everything on a single object makes it easy to override in
    tests (just construct a new ``ServerContext`` with stubs) and
    avoids module-level globals that would leak between test cases.
    """

    def __init__(
        self,
        *,
        cwd: Path,
        cfg: AppConfig,
        workspace_path: Path,
        task_manager: TaskManager,
    ) -> None:
        self.cwd = cwd
        self.cfg = cfg
        self.workspace_path = workspace_path
        self.task_manager = task_manager
        self._workspace: Workspace | None = None
        # The SPA fires several API calls at once on first load; without
        # this lock two threadpool workers can both run ``create_all``
        # and the loser fails with "table workspace_jobs already exists".
        self._workspace_lock = threading.Lock()

    @property
    def workspace(self) -> Workspace:
        if self._workspace is None:
            with self._workspace_lock:
                if self._workspace is None:
                    self._workspace = Workspace.open(self.workspace_path)
        return self._workspace

    def reset_workspace(self) -> None:
        """Force a fresh ``Workspace`` handle on the next access.

        Tests use this when they swap out the underlying directory
        between runs; production code never needs it.
        """
        self._workspace = None


# ----------------------------- Helper functions ----------------------------- #


def _load_profile(ctx: ServerContext) -> tuple[Profile, str, list[str], Path]:
    """Resolve and load ``profile.json``.

    Returns ``(profile, profile_text, skills, abspath)``. Raises an
    HTTP 400 when the profile is missing — the UI surfaces this as a
    "configure profile.json" prompt.
    """
    raw = ctx.cfg.profile_path or "profile.json"
    p = Path(raw).expanduser()
    if not p.is_absolute():
        p = ctx.cwd / p
    p = p.resolve()
    if not p.is_file():
        raise HTTPException(
            status_code=400,
            detail=(
                f"profile.json not found at {p}. Run `jobapply init --resume " "<path>` first."
            ),
        )
    try:
        profile = load_profile(p)
    except ProfileLoadError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return profile, profile_to_text(profile), profile_skill_list(profile), p


def _record_from_raw_job(
    job: RawJob,
    *,
    fit: FitScore | None = None,
    error: str | None = None,
) -> JobRecord:
    """Mirror :func:`jobapply.cli._record_from_raw_job`.

    Keeping a copy here avoids importing the CLI module (which imports
    Typer / questionary at import time and would balloon FastAPI's
    cold-start cost).
    """
    return JobRecord(
        job_id=job.job_id,
        title=job.title,
        company=job.company,
        location=job.location,
        description=(job.description or "")[:5000],
        job_url=job.job_url,
        apply_url=job.apply_url,
        site=job.site,
        status=LedgerStatus.pending,
        fit=fit,
        application=job.application,
        error=error,
        processed_at=datetime.now(UTC),
    )


def _persist_job_dir(workspace: Workspace, record: JobRecord) -> Path:
    """Per-job folder + ``job.json`` mirror of the CLI ``search`` flow."""
    slug = slugify(record.title or "", record.company or "", record.job_id)
    job_dir = workspace.jobs_dir / slug
    job_dir.mkdir(parents=True, exist_ok=True)
    (job_dir / "job.json").write_text(
        json.dumps(record.model_dump(mode="json"), indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    return job_dir


def _resolve_job_dir(workspace: Workspace, record: JobRecord) -> Path:
    """Stable per-job folder path (matches CLI slugify rules)."""
    slug = slugify(record.title or "", record.company or "", record.job_id)
    return workspace.jobs_dir / slug


def _serialize_record(record: JobRecord, workspace: Workspace) -> dict[str, Any]:
    """JobRecord -> JSON-friendly dict the frontend consumes.

    Adds workspace-relative artifact paths so the UI can build
    ``download`` links without exposing absolute filesystem paths.
    """
    payload = record.model_dump(mode="json")
    job_dir = _resolve_job_dir(workspace, record)
    artifacts: dict[str, str | None] = {}
    if job_dir.is_dir():
        for name in (
            "resume.md",
            "resume.pdf",
            "resume.tex",
            "cover_letter.md",
            "cover_letter.pdf",
            "cover_letter.tex",
            "email.txt",
            "job.json",
        ):
            if (job_dir / name).is_file():
                artifacts[name] = name
    payload["available_artifacts"] = artifacts
    payload["job_dir_relative"] = str(job_dir.relative_to(workspace.path))
    return payload


def _resolve_provider_model(
    cfg: AppConfig, *, provider: str | None, model: str | None
) -> tuple[str, str]:
    prov = (provider or cfg.provider or "").lower().strip()
    if not prov:
        raise HTTPException(
            status_code=400, detail="No provider configured. Run `jobapply config`."
        )
    mdl = (model or cfg.resolved_model(prov) or "").strip()
    if not mdl:
        raise HTTPException(status_code=400, detail=f"No model configured for provider '{prov}'.")
    return prov, mdl


def _build_synthetic_raw_job(payload: FreeformRequest) -> RawJob:
    """Pasted-JD case: synthesize a :class:`RawJob` from user input.

    The job_id is a stable hash so re-pasting the same JD lands in the
    same workspace slug instead of creating duplicates.
    """
    title = (payload.title or "").strip() or "Tailored Application"
    company = (payload.company or "").strip() or "Unknown Company"
    location = (payload.location or "").strip()
    jid = stable_job_id(
        site="freeform",
        company=company,
        title=title,
        location=location,
        apply_url=None,
        job_url=None,
    )
    hints = extract_application_hints(payload.description)
    return RawJob(
        job_id=jid,
        title=title,
        company=company,
        location=location,
        description=payload.description,
        site="freeform",
        application=hints if hints.has_any else None,
    )


def _record_to_raw_job(record: JobRecord) -> RawJob:
    """Hydrate a stored :class:`JobRecord` back into a :class:`RawJob`."""
    return RawJob(
        job_id=record.job_id,
        title=record.title or "",
        company=record.company or "",
        location=record.location or "",
        description=record.description or "",
        job_url=record.job_url,
        apply_url=record.apply_url,
        site=record.site or "",
        application=record.application,
    )


# ---------------------------- App factory ----------------------------------- #


def create_app(
    *,
    cwd: Path | None = None,
    workspace_path: Path | None = None,
    task_manager: TaskManager | None = None,
    web_dist: Path | None = None,
    enable_cors: bool = True,
) -> FastAPI:
    """Construct the FastAPI app.

    The factory pattern lets tests inject a fresh ``cwd`` /
    ``task_manager`` per test case without globals.
    """
    load_dotenv_if_present()
    root = (cwd or Path.cwd()).resolve()
    cfg = load_config(root)
    apply_latex_api_env(cfg)
    ws_path = workspace_path
    if ws_path is None:
        ws_path = root / DEFAULT_WEB_WORKSPACE
    ws_path = ws_path.expanduser()
    if not ws_path.is_absolute():
        ws_path = (root / ws_path).resolve()
    ctx = ServerContext(
        cwd=root,
        cfg=cfg,
        workspace_path=ws_path,
        task_manager=task_manager or get_default_manager(),
    )

    app = FastAPI(title="JobApply", version="0.1.0")
    if enable_cors:
        # The Vite dev server runs on :5173 and proxies /api → :8000.
        # Allowing localhost:* keeps that flow working without
        # special-casing ports.
        app.add_middleware(
            CORSMiddleware,
            allow_origins=[
                "http://localhost:5173",
                "http://127.0.0.1:5173",
            ],
            allow_credentials=True,
            allow_methods=["*"],
            allow_headers=["*"],
        )
    app.state.ctx = ctx

    _register_routes(app)

    # Serve the pre-built React bundle when present so production users
    # can run `jobapply ui` and have one URL serving both API + UI.
    dist = web_dist or (Path(__file__).parent / WEB_DIST_DIRNAME)
    if dist.is_dir() and (dist / "index.html").is_file():
        _mount_static(app, dist)
    else:
        _register_dev_root(app, dist)

    return app


# --------------------------- Static / dev fallback -------------------------- #


def _mount_static(app: FastAPI, dist: Path) -> None:
    """Mount the Vite build output and add a SPA catch-all."""
    assets_dir = dist / "assets"
    if assets_dir.is_dir():
        app.mount("/assets", StaticFiles(directory=str(assets_dir)), name="static-assets")

    index_html = dist / "index.html"

    @app.get("/", include_in_schema=False)
    async def _index() -> FileResponse:
        return FileResponse(index_html)

    @app.get("/{full_path:path}", include_in_schema=False)
    async def _spa_catchall(full_path: str, request: Request) -> FileResponse:
        # Don't intercept the API namespace.
        if full_path.startswith("api/") or full_path == "api":
            raise HTTPException(status_code=404, detail="Not found")
        # Serve any concrete file under the dist dir directly (favicon,
        # robots.txt, etc.). Otherwise fall through to index.html so the
        # client-side router can render the requested route.
        candidate = dist / full_path
        if candidate.is_file():
            return FileResponse(candidate)
        return FileResponse(index_html)


def _register_dev_root(app: FastAPI, expected_dist: Path) -> None:
    """Friendly "frontend not built" page when ``web_dist`` is missing."""

    @app.get("/", include_in_schema=False)
    async def _dev_root() -> JSONResponse:
        return JSONResponse(
            {
                "message": (
                    "JobApply API is running, but no built frontend was found. "
                    "Build the React app and rerun, or use the Vite dev server."
                ),
                "expected_dist_dir": str(expected_dist),
                "dev_steps": [
                    "cd web",
                    "npm install",
                    "npm run dev      # http://localhost:5173 (proxies /api here)",
                    "# OR: npm run build → outputs to ../jobapply/web_dist",
                ],
                "api_root": "/api",
            }
        )


# ------------------------------ Routes -------------------------------------- #


def _register_routes(app: FastAPI) -> None:
    @app.get("/api/status", response_model=StatusResponse)
    def status(request: Request) -> StatusResponse:
        ctx: ServerContext = request.app.state.ctx
        cfg = ctx.cfg
        # Best-effort profile detection — never fail the status route
        # on a missing profile, the UI uses this to decide what to show.
        prof_path = ctx.cwd / (cfg.profile_path or "profile.json")
        profile_loaded = prof_path.is_file()
        profile_name = ""
        if profile_loaded:
            try:
                profile_name = load_profile(prof_path).name
            except ProfileLoadError:
                profile_loaded = False
        web_dist = (Path(__file__).parent / WEB_DIST_DIRNAME / "index.html").is_file()
        return StatusResponse(
            workspace=str(ctx.workspace.path),
            workspace_total_jobs=ctx.workspace.count(),
            profile_path=str(prof_path) if profile_loaded else None,
            profile_loaded=profile_loaded,
            profile_name=profile_name,
            provider=cfg.provider,
            model=cfg.resolved_model(cfg.provider),
            sites=list(cfg.sites),
            results_wanted=cfg.results_wanted,
            web_dist_present=web_dist,
        )

    @app.get("/api/profile")
    def profile(request: Request) -> dict[str, Any]:
        ctx: ServerContext = request.app.state.ctx
        try:
            profile_obj, _, _, path = _load_profile(ctx)
        except HTTPException as exc:
            return {"loaded": False, "error": str(exc.detail)}
        return {
            "loaded": True,
            "path": str(path),
            "profile": profile_obj.model_dump(mode="json"),
        }

    @app.get("/api/providers")
    def providers(request: Request) -> dict[str, Any]:
        """List every provider known to the CLI plus the user's configured
        defaults. The frontend uses this to populate the provider/model
        picker on the tailor + email actions, pre-selecting whatever is
        active in ``jobapply.toml``.

        Each entry includes:

        * ``name`` – canonical provider key.
        * ``configured`` – ``True`` when ``jobapply.toml`` has a
          ``[providers.<name>]`` block (i.e. ``jobapply config`` ran for it).
        * ``has_credentials`` – ``True`` when an API key is reachable
          (toml first, env vars second). Lets the UI grey out providers
          the user can't actually use yet.
        * ``default_model`` – the user's per-provider model override
          when present, else the bundled :data:`DEFAULT_MODELS` value.
        """
        ctx: ServerContext = request.app.state.ctx
        cfg = ctx.cfg
        items: list[dict[str, Any]] = []
        for name in PROVIDER_NAMES:
            pcfg = cfg.provider_config(name)
            items.append(
                {
                    "name": name,
                    "configured": name in cfg.providers,
                    "has_credentials": (name == "ollama" or bool(get_api_key(cfg, name))),
                    "default_model": pcfg.model or DEFAULT_MODELS.get(name, ""),
                    "fallback_model": DEFAULT_MODELS.get(name, ""),
                }
            )
        return {
            "active_provider": cfg.provider,
            "active_model": cfg.resolved_model(cfg.provider),
            "providers": items,
        }

    @app.get("/api/jobs")
    def list_jobs(request: Request) -> dict[str, Any]:
        ctx: ServerContext = request.app.state.ctx
        ws = ctx.workspace
        records = ws.all_records()
        jobs = [_serialize_record(r, ws) for r in records]
        return {
            "workspace": str(ws.path),
            "total": len(jobs),
            "jobs": jobs,
        }

    @app.get("/api/jobs/{job_id}")
    def get_job(job_id: str, request: Request) -> dict[str, Any]:
        ctx: ServerContext = request.app.state.ctx
        ws = ctx.workspace
        record = ws.get(job_id)
        if record is None:
            raise HTTPException(status_code=404, detail=f"Unknown job: {job_id}")
        return _serialize_record(record, ws)

    @app.get("/api/jobs/{job_id}/artifacts/{name}")
    def get_artifact(job_id: str, name: str, request: Request) -> FileResponse:
        ctx: ServerContext = request.app.state.ctx
        ws = ctx.workspace
        record = ws.get(job_id)
        if record is None:
            raise HTTPException(status_code=404, detail=f"Unknown job: {job_id}")
        # Whitelist the artifact filenames so users can't path-traverse
        # out of the per-job directory.
        allowed = {
            "resume.md",
            "resume.pdf",
            "resume.tex",
            "cover_letter.md",
            "cover_letter.pdf",
            "cover_letter.tex",
            "email.txt",
            "job.json",
            "tailor_meta.json",
        }
        if name not in allowed:
            raise HTTPException(status_code=404, detail="Artifact not allowed")
        path = _resolve_job_dir(ws, record) / name
        if not path.is_file():
            raise HTTPException(status_code=404, detail=f"Artifact not found: {name}")
        return FileResponse(path)

    # ------------------------------------------------------------------
    # Live LaTeX editing
    # ------------------------------------------------------------------
    # The UI lets users tweak the model-generated ``resume.tex`` /
    # ``cover_letter.tex`` and then recompile to PDF without rerunning
    # the entire tailor pipeline. Editing is gated to the LaTeX
    # sources (and the markdown fallbacks) — touching ``job.json`` or
    # ``email.txt`` from the browser would silently desync workspace
    # state, which is more pain than it's worth.
    EDITABLE_ARTIFACTS = {
        "resume.tex",
        "cover_letter.tex",
        "resume.md",
        "cover_letter.md",
    }
    COMPILABLE_ARTIFACTS = {"resume.tex", "cover_letter.tex"}

    @app.put("/api/jobs/{job_id}/artifacts/{name}")
    def save_artifact(
        job_id: str,
        name: str,
        payload: ArtifactSaveRequest,
        request: Request,
    ) -> dict[str, Any]:
        ctx: ServerContext = request.app.state.ctx
        ws = ctx.workspace
        record = ws.get(job_id)
        if record is None:
            raise HTTPException(status_code=404, detail=f"Unknown job: {job_id}")
        if name not in EDITABLE_ARTIFACTS:
            raise HTTPException(
                status_code=400,
                detail=(
                    f"Artifact '{name}' is not editable. Editable: "
                    f"{sorted(EDITABLE_ARTIFACTS)}."
                ),
            )
        # Sanity-check the body length so a hung paste doesn't blow up
        # the server. 1 MB is a generous limit for LaTeX sources.
        if len(payload.content) > 1_000_000:
            raise HTTPException(status_code=413, detail="Artifact content too large (>1 MB).")
        job_dir = _resolve_job_dir(ws, record)
        job_dir.mkdir(parents=True, exist_ok=True)
        path = job_dir / name
        path.write_text(payload.content, encoding="utf-8")
        st = path.stat()
        return {
            "name": name,
            "bytes": st.st_size,
            "mtime": st.st_mtime,
            "saved": str(path.relative_to(ws.path)),
        }

    @app.post("/api/jobs/{job_id}/artifacts/{name}/compile")
    def compile_artifact(job_id: str, name: str, request: Request) -> dict[str, Any]:
        ctx: ServerContext = request.app.state.ctx
        ws = ctx.workspace
        record = ws.get(job_id)
        if record is None:
            raise HTTPException(status_code=404, detail=f"Unknown job: {job_id}")
        if name not in COMPILABLE_ARTIFACTS:
            raise HTTPException(
                status_code=400,
                detail=(
                    f"Artifact '{name}' cannot be compiled to PDF. "
                    f"Compilable: {sorted(COMPILABLE_ARTIFACTS)}."
                ),
            )
        job_dir = _resolve_job_dir(ws, record)
        tex_path = job_dir / name
        if not tex_path.is_file():
            raise HTTPException(
                status_code=404,
                detail=f"LaTeX source not found: {name}. Save it first.",
            )
        backend = probe_tex_pdf_backend()
        if not backend:
            raise HTTPException(
                status_code=503,
                detail=(
                    "No LaTeX → PDF backend available. Install "
                    "`tectonic` or `pdflatex`, or enable the "
                    "latex_api in jobapply.toml."
                ),
            )
        # ``tex_to_pdf`` walks the configured backends and silently
        # returns ``None`` on failure — we don't get a build log out of
        # any of them. A 500 with the backend name is the most
        # actionable thing we can surface today.
        pdf_path = tex_to_pdf(tex_path, job_dir)
        if pdf_path is None or not pdf_path.is_file():
            raise HTTPException(
                status_code=502,
                detail=(
                    f"LaTeX compile via '{backend}' failed. The source "
                    "likely has a syntax error — re-run the tailor "
                    "pipeline or fix the .tex by hand."
                ),
            )
        st = pdf_path.stat()
        return {
            "name": pdf_path.name,
            "size": st.st_size,
            "mtime": st.st_mtime,
            "backend": backend,
        }

    @app.delete("/api/jobs/{job_id}")
    def delete_job(job_id: str, request: Request) -> dict[str, Any]:
        ctx: ServerContext = request.app.state.ctx
        ws = ctx.workspace
        record = ws.get(job_id)
        if record is None:
            raise HTTPException(status_code=404, detail=f"Unknown job: {job_id}")
        # Best-effort artifact cleanup — leaving stale files behind is
        # cosmetic, not destructive.
        job_dir = _resolve_job_dir(ws, record)
        if job_dir.is_dir():
            for child in job_dir.iterdir():
                with contextlib.suppress(OSError):
                    child.unlink()
            with contextlib.suppress(OSError):
                job_dir.rmdir()
        from sqlmodel import Session

        from jobapply.workspace import WorkspaceJobEntry

        with Session(ws._engine) as s:  # noqa: SLF001 - scoped helper
            row = s.get(WorkspaceJobEntry, job_id)
            if row is not None:
                s.delete(row)
                s.commit()
        ws.flush_files(run_id="delete", profile_path=str(ws.path))
        return {"deleted": job_id, "workspace_total_jobs": ws.count()}

    @app.get("/api/searches")
    def list_searches(request: Request, limit: int = 25) -> dict[str, Any]:
        ctx: ServerContext = request.app.state.ctx
        ws = ctx.workspace
        rows = ws.list_searches(limit=limit)
        return {
            "workspace": str(ws.path),
            "searches": [
                {
                    "id": r.id,
                    "command": r.command,
                    "search_input": json.loads(r.search_input_json or "{}"),
                    "provider": r.provider,
                    "model": r.model,
                    "started_at": r.started_at.isoformat() if r.started_at else None,
                    "finished_at": (r.finished_at.isoformat() if r.finished_at else None),
                    "fetched": r.fetched,
                    "new_jobs": r.new_jobs,
                    "duplicate_jobs": r.duplicate_jobs,
                }
                for r in rows
            ],
        }

    # --------- task endpoints ----------

    @app.get("/api/tasks")
    def list_tasks(request: Request, limit: int = 25) -> dict[str, Any]:
        ctx: ServerContext = request.app.state.ctx
        return {"tasks": [r.to_dict() for r in ctx.task_manager.list_recent(limit=limit)]}

    @app.get("/api/tasks/{task_id}")
    def get_task(task_id: str, request: Request) -> dict[str, Any]:
        ctx: ServerContext = request.app.state.ctx
        record = ctx.task_manager.get(task_id)
        if record is None:
            raise HTTPException(status_code=404, detail=f"Unknown task: {task_id}")
        return record.to_dict()

    @app.post("/api/tasks/{task_id}/cancel")
    def cancel_task(task_id: str, request: Request) -> dict[str, Any]:
        ctx: ServerContext = request.app.state.ctx
        ok = ctx.task_manager.cancel(task_id)
        if not ok:
            raise HTTPException(status_code=409, detail="Task already finished or unknown.")
        return {"cancelled": task_id}

    # --------- search ----------

    @app.post("/api/search")
    def post_search(payload: SearchRequest, request: Request) -> dict[str, Any]:
        ctx: ServerContext = request.app.state.ctx
        ws = ctx.workspace
        cfg = ctx.cfg

        site_list = payload.sites or list(cfg.sites)
        results_wanted = payload.results_wanted or cfg.results_wanted
        search_input = JobSearchInput(
            titles=payload.titles,
            skills=payload.skills,
            location=payload.location,
            remote=payload.remote,
            results_wanted=results_wanted,
            hours_old=cfg.hours_old,
            site_names=site_list,
            linkedin_fetch_description=payload.linkedin_descriptions,
        )

        prov = ""
        mdl = ""
        profile_text = ""
        skill_list = list(payload.skills)
        if payload.score:
            prov, mdl = _resolve_provider_model(cfg, provider=payload.provider, model=payload.model)
            _, profile_text, _, _ = _load_profile(ctx)

        search_id = ws.start_search(
            command="search",
            search_input=search_input,
            provider=prov,
            model=mdl,
        )

        def _job(handle: TaskHandle) -> dict[str, Any]:
            handle.set_label("Searching job boards…")
            handle.set_percent(5.0)
            new_count = 0
            duplicate_count = 0
            fetched = 0
            llm = None
            if payload.score:
                llm = create_chat_model(prov, mdl, cfg=cfg)
            try:
                for raw in iter_search_jobs(search_input):
                    if handle.is_cancelled():
                        handle.log("Cancelled by user.")
                        break
                    fetched += 1
                    record = _record_from_raw_job(raw)
                    if ws.is_seen(raw.job_id) and not payload.force:
                        duplicate_count += 1
                        handle.log(f"dup: {raw.title} @ {raw.company}")
                    else:
                        if payload.score and llm is not None:
                            try:
                                fit = score_fit(
                                    llm,
                                    profile_text=profile_text,
                                    job=raw,
                                    skills=skill_list,
                                )
                                record = _record_from_raw_job(raw, fit=fit)
                            except Exception as exc:  # noqa: BLE001
                                record = _record_from_raw_job(raw, error=f"score failed: {exc}")
                        inserted = ws.upsert_job(record, search_id=search_id)
                        if inserted:
                            new_count += 1
                            handle.log(f"new: {raw.title} @ {raw.company}")
                        else:
                            duplicate_count += 1
                            handle.log(f"refresh: {raw.title} @ {raw.company}")
                        _persist_job_dir(ws, record)
                    handle.set_label(
                        f"Searching… {fetched} fetched ({new_count} new, "
                        f"{duplicate_count} duplicate)"
                    )
                    # We can't know JobSpy's total upfront; fake gradual
                    # progress to keep the UI's bar moving.
                    handle.set_percent(min(95.0, 5.0 + fetched * 1.0))
            finally:
                ws.finish_search(
                    search_id,
                    fetched=fetched,
                    new_jobs=new_count,
                    duplicate_jobs=duplicate_count,
                )
                ws.flush_files(
                    run_id=f"search-{search_id}",
                    search_input=search_input,
                    profile_path="",
                    provider=prov,
                    model=mdl,
                )
            handle.set_percent(100.0)
            handle.set_label(f"Done: {new_count} new, {duplicate_count} duplicate")
            return {
                "search_id": search_id,
                "fetched": fetched,
                "new_jobs": new_count,
                "duplicate_jobs": duplicate_count,
            }

        record = ctx.task_manager.submit(
            "search",
            _job,
            metadata={
                "search_id": search_id,
                "titles": payload.titles,
                "score": payload.score,
            },
        )
        return record.to_dict()

    # --------- run (tailor batch) ----------

    @app.post("/api/run")
    def post_run(payload: RunRequest, request: Request) -> dict[str, Any]:
        ctx: ServerContext = request.app.state.ctx
        ws = ctx.workspace
        cfg = ctx.cfg

        prov, mdl = _resolve_provider_model(cfg, provider=payload.provider, model=payload.model)
        profile_obj, profile_text, profile_skills, profile_path = _load_profile(ctx)

        # Validate every requested job_id up front so the task surfaces
        # bogus IDs as a 400 instead of failing midway through.
        records: list[JobRecord] = []
        for jid in payload.job_ids:
            rec = ws.get(jid)
            if rec is None:
                raise HTTPException(status_code=404, detail=f"Unknown job: {jid}")
            records.append(rec)

        search_id = ws.start_search(
            command="run",
            search_input={"selected_job_ids": payload.job_ids},
            provider=prov,
            model=mdl,
        )

        def _job(handle: TaskHandle) -> dict[str, Any]:
            llm = create_chat_model(prov, mdl, cfg=cfg)
            done = 0
            failed = 0
            skipped = 0
            total = len(records)
            for i, record in enumerate(records, start=1):
                if handle.is_cancelled():
                    break
                handle.set_label(f"Tailoring {i}/{total}: {record.title}")
                handle.set_percent(((i - 1) / max(1, total)) * 100.0)
                if (
                    not payload.force
                    and record.status == LedgerStatus.done
                    and record.tailored_resume is not None
                ):
                    handle.log(f"skip already-done: {record.title}")
                    skipped += 1
                    continue
                try:
                    new_record = _tailor_record(
                        ws=ws,
                        record=record,
                        llm=llm,
                        profile=profile_obj,
                        profile_text=profile_text,
                        profile_skills=profile_skills,
                        profile_path=profile_path,
                        no_pdf=payload.no_pdf,
                    )
                    ws.upsert_job(new_record, search_id=search_id)
                    done += 1
                    handle.log(f"done: {new_record.title}")
                except Exception as exc:  # noqa: BLE001
                    failed += 1
                    handle.log(f"failed: {record.title}: {exc}")
                    failed_record = record.model_copy(
                        update={
                            "status": LedgerStatus.failed,
                            "error": str(exc),
                            "processed_at": datetime.now(UTC),
                        }
                    )
                    ws.upsert_job(failed_record, search_id=search_id)
            ws.finish_search(
                search_id,
                fetched=total,
                new_jobs=done,
                duplicate_jobs=skipped,
            )
            ws.flush_files(
                run_id=f"run-{search_id}",
                profile_path=str(profile_path),
                provider=prov,
                model=mdl,
            )
            handle.set_percent(100.0)
            handle.set_label(f"Done: {done} tailored, {skipped} skipped, {failed} failed")
            return {
                "search_id": search_id,
                "tailored": done,
                "failed": failed,
                "skipped": skipped,
            }

        record = ctx.task_manager.submit("run", _job, metadata={"job_ids": payload.job_ids})
        return record.to_dict()

    # --------- single tailor ----------

    @app.post("/api/jobs/{job_id}/tailor")
    def post_tailor(job_id: str, payload: TailorOneRequest, request: Request) -> dict[str, Any]:
        ctx: ServerContext = request.app.state.ctx
        ws = ctx.workspace
        cfg = ctx.cfg
        record = ws.get(job_id)
        if record is None:
            raise HTTPException(status_code=404, detail=f"Unknown job: {job_id}")
        if not (record.description or "").strip():
            raise HTTPException(
                status_code=400,
                detail=(
                    "Saved job has no description; re-run search with "
                    "LinkedIn descriptions enabled or paste the JD via "
                    "the Freeform route."
                ),
            )
        prov, mdl = _resolve_provider_model(cfg, provider=payload.provider, model=payload.model)
        profile_obj, profile_text, profile_skills, profile_path = _load_profile(ctx)

        def _job(handle: TaskHandle) -> dict[str, Any]:
            handle.set_label(f"Tailoring {record.title}…")
            handle.set_percent(10.0)
            llm = create_chat_model(prov, mdl, cfg=cfg)
            new_record = _tailor_record(
                ws=ws,
                record=record,
                llm=llm,
                profile=profile_obj,
                profile_text=profile_text,
                profile_skills=profile_skills,
                profile_path=profile_path,
                no_pdf=payload.no_pdf,
            )
            ws.upsert_job(new_record)
            ws.flush_files(
                run_id="tailor-one",
                profile_path=str(profile_path),
                provider=prov,
                model=mdl,
            )
            handle.set_percent(100.0)
            handle.set_label("Tailored.")
            return {"job": _serialize_record(new_record, ws)}

        record_h = ctx.task_manager.submit("tailor", _job, metadata={"job_id": job_id})
        return record_h.to_dict()

    # --------- freeform paste ----------

    @app.post("/api/freeform")
    def post_freeform(payload: FreeformRequest, request: Request) -> dict[str, Any]:
        ctx: ServerContext = request.app.state.ctx
        ws = ctx.workspace
        cfg = ctx.cfg
        prov, mdl = _resolve_provider_model(cfg, provider=payload.provider, model=payload.model)
        profile_obj, profile_text, profile_skills, profile_path = _load_profile(ctx)

        def _job(handle: TaskHandle) -> dict[str, Any]:
            handle.set_label("Tailoring pasted JD…")
            handle.set_percent(10.0)
            llm = create_chat_model(prov, mdl, cfg=cfg)
            raw = _build_synthetic_raw_job(payload)
            seed = _record_from_raw_job(raw)
            ws.upsert_job(seed)
            new_record = _tailor_record(
                ws=ws,
                record=seed,
                llm=llm,
                profile=profile_obj,
                profile_text=profile_text,
                profile_skills=profile_skills,
                profile_path=profile_path,
                no_pdf=payload.no_pdf,
                target_skills=payload.skills,
            )
            ws.upsert_job(new_record)
            ws.flush_files(
                run_id="freeform",
                profile_path=str(profile_path),
                provider=prov,
                model=mdl,
            )
            handle.set_percent(100.0)
            handle.set_label("Done.")
            return {"job": _serialize_record(new_record, ws)}

        record_h = ctx.task_manager.submit("freeform", _job, metadata={"title": payload.title})
        return record_h.to_dict()

    # --------- email drafting ----------

    @app.post("/api/jobs/{job_id}/email")
    def post_email(job_id: str, payload: EmailRequest, request: Request) -> dict[str, Any]:
        ctx: ServerContext = request.app.state.ctx
        ws = ctx.workspace
        cfg = ctx.cfg
        record = ws.get(job_id)
        if record is None:
            raise HTTPException(status_code=404, detail=f"Unknown job: {job_id}")
        # Either tailored artifacts OR a non-empty JD body is enough
        # for the drafter to produce a grounded email. We bail only
        # when both are missing so we don't ask the model to make up
        # the role from thin air.
        has_artifacts = record.tailored_resume is not None and record.cover_letter is not None
        if not has_artifacts and not (record.description or "").strip():
            raise HTTPException(
                status_code=400,
                detail=(
                    "This job has neither a tailored resume nor a job "
                    "description on file. Tailor it first or paste a JD "
                    "via the Freeform route."
                ),
            )
        prov, mdl = _resolve_provider_model(cfg, provider=payload.provider, model=payload.model)
        _, profile_text, _, _ = _load_profile(ctx)

        def _job(handle: TaskHandle) -> dict[str, Any]:
            handle.set_label("Drafting email…")
            handle.set_percent(20.0)
            llm = create_chat_model(prov, mdl, cfg=cfg)
            sender = ""
            if record.tailored_resume is not None:
                sender = record.tailored_resume.document_title or ""
            email = draft_application_email(
                llm,
                profile_text=profile_text,
                job=_record_to_raw_job(record),
                resume=record.tailored_resume,
                cover=record.cover_letter,
                recipient_email=payload.recipient.strip(),
                additional_info=payload.additional_info,
                sender_name=sender,
            )
            job_dir = _resolve_job_dir(ws, record)
            job_dir.mkdir(parents=True, exist_ok=True)
            email_path = job_dir / "email.txt"
            email_path.write_text(email.as_text(), encoding="utf-8")
            handle.set_percent(100.0)
            handle.set_label("Drafted.")
            return {
                "to": email.to,
                "subject": email.subject,
                "body": email.body,
                "text": email.as_text(),
                "saved_to": "email.txt",
            }

        record_h = ctx.task_manager.submit("email", _job, metadata={"job_id": job_id})
        return record_h.to_dict()

    @app.get("/api/jobs/{job_id}/email-hint")
    def email_hint(job_id: str, request: Request) -> dict[str, Any]:
        """Return the recipient/subject parsed from the JD body so the
        frontend's email modal can pre-fill its inputs."""
        ctx: ServerContext = request.app.state.ctx
        record = ctx.workspace.get(job_id)
        if record is None:
            raise HTTPException(status_code=404, detail=f"Unknown job: {job_id}")
        hints: ApplicationHints = (
            record.application
            if record.application is not None
            else extract_application_hints(record.description or "")
        )
        return {
            "primary_email": hints.primary_email,
            "subject_line": hints.subject_line,
            "instructions": list(hints.instructions),
        }


# --------------------------- Tailor orchestration --------------------------- #


def _tailor_record(
    *,
    ws: Workspace,
    record: JobRecord,
    llm: Any,
    profile: Profile,
    profile_text: str,
    profile_skills: list[str],
    profile_path: Path,
    no_pdf: bool,
    target_skills: list[str] | None = None,
) -> JobRecord:
    """Run the tailor pipeline against a workspace job and return the
    fully-populated :class:`JobRecord` (with TailoredResume + CoverLetter
    + artifact paths). The caller persists the result back into the
    workspace.

    The implementation reuses :func:`tailor_for_job_description` by
    writing a temporary ``job.json`` so we get the exact same on-disk
    artifact layout (resume.md, resume.tex, cover_letter.md, …) that
    ``jobapply tailor --job <path>`` produces.
    """
    job_dir = _resolve_job_dir(ws, record)
    job_dir.mkdir(parents=True, exist_ok=True)
    job_json = job_dir / "job.json"
    # Persist the latest snapshot so the tailor agent reads the exact
    # text we just upserted (handles freeform → DB roundtrip too).
    job_json.write_text(
        json.dumps(record.model_dump(mode="json"), indent=2, ensure_ascii=False),
        encoding="utf-8",
    )

    # ``tailor_for_job_description`` writes artifacts into
    # ``<output_root>/jobs/<slug>/`` (it appends the ``jobs/``
    # segment internally via :func:`slug_from_paths`). We must hand
    # it the workspace *root* — passing ``ws.jobs_dir`` doubles up
    # the ``jobs/`` segment and lands the resume under
    # ``output/web/jobs/jobs/<slug>/``, while ``job.json`` for the
    # same record lives under ``output/web/jobs/<slug>/``.
    outputs = tailor_for_job_description(
        llm,
        jd_path=job_json,
        profile_text=profile_text,
        profile_skills=profile_skills,
        output_root=ws.path,
        target_skills=list(target_skills or []),
        no_pdf=no_pdf,
        email=None,
        profile=profile,
    )

    artifacts = JobArtifacts.model_validate(
        {
            "job_json": str(job_json.resolve()),
            "resume_md": str(outputs.resume_md.resolve()),
            "resume_tex": str(outputs.resume_tex.resolve()),
            "resume_pdf": str(outputs.resume_pdf.resolve()) if outputs.resume_pdf else None,
            "resume_latex_pdf": (
                str(outputs.resume_latex_pdf.resolve()) if outputs.resume_latex_pdf else None
            ),
            "cover_letter_md": str(outputs.cover_md.resolve()),
            "cover_letter_tex": str(outputs.cover_tex.resolve()),
            "cover_letter_pdf": str(outputs.cover_pdf.resolve()) if outputs.cover_pdf else None,
            "cover_letter_latex_pdf": (
                str(outputs.cover_latex_pdf.resolve()) if outputs.cover_latex_pdf else None
            ),
        }
    )

    return JobRecord(
        job_id=record.job_id,
        title=record.title or outputs.job.title,
        company=record.company or outputs.job.company,
        location=record.location or outputs.job.location,
        description=record.description or outputs.job.description,
        job_url=record.job_url,
        apply_url=record.apply_url,
        site=record.site or outputs.job.site,
        status=LedgerStatus.done,
        fit=record.fit,
        application=record.application,
        tailored_resume=outputs.resume,
        cover_letter=outputs.cover,
        artifacts=artifacts,
        processed_at=datetime.now(UTC),
    )


__all__ = ["create_app", "DEFAULT_WEB_WORKSPACE", "WEB_DIST_DIRNAME"]
