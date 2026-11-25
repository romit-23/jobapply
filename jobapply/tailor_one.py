"""Single-job tailoring pipeline.

The ``jobapply tailor`` command exists for the case where the user already
knows which role they want to apply to and just hands us the job
description. We skip the search / dedupe / ledger machinery used by
``jobapply run`` and execute the resume + cover-letter agents inline,
plus an optional email drafter for users who want a ready-to-paste
application email.

The output layout mirrors the per-job folder produced by ``jobapply run``
so downstream tooling (PDF backends, Sheets export, etc.) can read it
the same way.
"""

from __future__ import annotations

import contextlib
import json
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from langchain_core.language_models.chat_models import BaseChatModel

from jobapply.agents.cover_letter import write_cover_letter
from jobapply.agents.email_drafter import draft_application_email
from jobapply.agents.jd_parser import parse_job_description
from jobapply.agents.resume_tailor import tailor_resume
from jobapply.jd_backfill import JdBackfillError, fetch_description_from_url
from jobapply.jd_extract import extract_application_hints
from jobapply.models import (
    ApplicationHints,
    CoverLetter,
    EmailDraft,
    JobDescriptionMeta,
    RawJob,
    TailoredResume,
)
from jobapply.nodes.render import (
    fill_cover_letter_tex,
    fill_resume_tex,
    md_to_pdf,
    render_cover_markdown,
    render_resume_markdown,
    slug_from_paths,
    tex_to_pdf,
)
from jobapply.profile import Profile
from jobapply.profile_import import (
    SUPPORTED_SUFFIXES,
    ResumeImportError,
    extract_text_from_resume,
)
from jobapply.utils import slugify, stable_job_id

JD_SUPPORTED_SUFFIXES: tuple[str, ...] = SUPPORTED_SUFFIXES

#: Suffix used by the per-job JSON files written by ``jobapply search`` /
#: ``jobapply run`` (see ``cli._persist_job_record`` and
#: ``graph_nodes.dedupe_node``). Passing one of these into
#: ``jobapply tailor --job <path>`` short-circuits the LLM JD parser
#: because we already have structured title / company / location.
JOB_JSON_SUFFIX = ".json"

#: Full set of suffixes ``jobapply tailor --job <path>`` accepts. The CLI
#: validates against this; ``read_job_description_text`` still rejects
#: ``.json`` because that path goes through the structured loader below.
TAILOR_INPUT_SUFFIXES: tuple[str, ...] = (*JD_SUPPORTED_SUFFIXES, JOB_JSON_SUFFIX)


class JobDescriptionReadError(RuntimeError):
    """Raised when the JD path can't be read or returns no text."""


@dataclass
class TailorEmailRequest:
    """Caller-supplied email parameters for ``tailor_for_job_description``.

    ``recipient`` is the only required field — ``additional_info`` is the
    optional free-form context the user wants the model to weave in
    (e.g. referrals, availability, recent contact).
    """

    recipient: str
    additional_info: str = ""


@dataclass
class TailorOutputs:
    """Bundle of artifacts written by :func:`tailor_for_job_description`.

    Paths are absolute. ``email`` is ``None`` when the caller didn't pass
    a :class:`TailorEmailRequest`. ``resume_pdf`` / ``cover_letter_pdf``
    are populated only when their respective backends succeeded.
    """

    job_dir: Path
    job: RawJob
    resume: TailoredResume
    cover: CoverLetter
    email: EmailDraft | None

    resume_md: Path
    resume_tex: Path
    cover_md: Path
    cover_tex: Path
    resume_pdf: Path | None = None
    resume_latex_pdf: Path | None = None
    cover_pdf: Path | None = None
    cover_latex_pdf: Path | None = None
    email_path: Path | None = None


def read_job_description_text(path: Path) -> str:
    """Read a JD file (txt / md / pdf / docx) and return its plain text.

    We reuse :func:`jobapply.profile_import.extract_text_from_resume`
    because it already covers the four formats users commonly paste a JD
    in. ``ResumeImportError`` is rewrapped as :class:`JobDescriptionReadError`
    so the CLI can present a JD-specific message.
    """
    if not path.is_file():
        raise JobDescriptionReadError(f"No such file: {path}")
    if path.suffix.lower() not in JD_SUPPORTED_SUFFIXES:
        raise JobDescriptionReadError(
            f"Unsupported job description format '{path.suffix}'. "
            f"Use one of: {', '.join(JD_SUPPORTED_SUFFIXES)}.",
        )
    try:
        text = extract_text_from_resume(path)
    except ResumeImportError as exc:
        raise JobDescriptionReadError(str(exc)) from exc
    if not (text or "").strip():
        raise JobDescriptionReadError(
            f"Could not extract any text from {path}. If it's a scanned "
            "PDF, paste the text into a .txt file and retry.",
        )
    return text


def _resolve_meta(
    *,
    raw_text: str,
    title_override: str | None,
    company_override: str | None,
    location_override: str | None,
    llm: BaseChatModel | None,
) -> JobDescriptionMeta:
    """Combine CLI overrides with LLM-extracted JD metadata.

    CLI overrides always win; we only invoke the LLM when at least one
    field is missing AND ``llm`` was supplied. When ``llm`` is ``None``
    (e.g. unit tests) we skip the call entirely and return whatever
    overrides we have.
    """
    title = (title_override or "").strip()
    company = (company_override or "").strip()
    location = (location_override or "").strip()

    if title and company and location:
        return JobDescriptionMeta(title=title, company=company, location=location)
    if llm is None:
        return JobDescriptionMeta(title=title, company=company, location=location)

    parsed = parse_job_description(llm, text=raw_text)
    return JobDescriptionMeta(
        title=title or parsed.title.strip(),
        company=company or parsed.company.strip(),
        location=location or parsed.location.strip(),
    )


def _backfill_description(
    data: dict[str, object],
    *,
    fetcher: Callable[[str], str | None] = fetch_description_from_url,
) -> str | None:
    """Try ``apply_url`` then ``job_url`` to recover a missing JD body.

    Split out from :func:`_load_job_from_json` so tests can inject a
    deterministic fetcher. Returns the first non-empty result or
    ``None``. Network/parse failures raised by the underlying
    ``httpx`` call are swallowed by the fetcher (returns ``None``);
    only :class:`~jobapply.jd_backfill.JdBackfillError` (raised on
    truly malformed URLs) propagates as ``None`` here.
    """
    for key in ("apply_url", "job_url"):
        candidate = data.get(key)
        if not isinstance(candidate, str) or not candidate.strip():
            continue
        try:
            text = fetcher(candidate)
        except JdBackfillError:
            continue
        if text and text.strip():
            return text
    return None


def _load_job_from_json(
    path: Path,
    *,
    fetcher: Callable[[str], str | None] | None = None,
) -> RawJob:
    """Load a per-job JSON written by ``jobapply search`` / ``jobapply run``.

    Tolerant of both the bare :class:`RawJob` shape (what
    ``graph_nodes.dedupe_node`` writes) and the richer
    :class:`~jobapply.models.JobRecord` shape (what
    ``cli._persist_job_record`` writes from ``search`` — that one also
    carries a ``fit`` block, which we ignore). Missing optional fields
    fall back to sensible defaults so partially-populated files still
    work.

    When the saved JSON has an empty ``description`` we attempt a
    best-effort backfill from ``apply_url`` / ``job_url`` (LinkedIn,
    Indeed, etc.). On success we rewrite the file in place so future
    tailor runs are instant; on failure we raise the same
    :class:`JobDescriptionReadError` the user used to see, with a
    pointer to ``--no-linkedin-descriptions`` / pasting the JD as a
    workaround.
    """
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise JobDescriptionReadError(
            f"Could not read saved job JSON at {path}: {exc}",
        ) from exc
    if not isinstance(data, dict):
        raise JobDescriptionReadError(
            f"Saved job JSON at {path} must be a JSON object, got {type(data).__name__}.",
        )

    description = str(data.get("description") or "")
    description_was_recovered = False
    if not description.strip():
        backfill_fn = fetcher or fetch_description_from_url
        recovered = _backfill_description(data, fetcher=backfill_fn)
        if recovered:
            description = recovered
            data["description"] = recovered
            description_was_recovered = True
        else:
            raise JobDescriptionReadError(
                f"Saved job JSON at {path} has no `description` field and "
                "the URL backfill couldn't recover it (LinkedIn auth wall, "
                "page gone, or a non-public board). Re-run `jobapply "
                "search` (LinkedIn descriptions are fetched by default "
                "now), or pass a JD file (.md/.txt/.docx/.pdf) with "
                "`jobapply tailor --job <file>` instead.",
            )

    # Application-hint resolution: prefer the persisted block when
    # present (fresh search runs already ran the extractor), but
    # always re-extract when the description was recovered via URL
    # backfill since that text wasn't seen by the search-side
    # extractor. Legacy job.json files (no `application` field) get
    # extraction here too so old artifacts benefit from the feature
    # without requiring a re-search.
    application: ApplicationHints | None = None
    raw_app = data.get("application")
    if isinstance(raw_app, dict) and not description_was_recovered:
        try:
            application = ApplicationHints.model_validate(raw_app)
        except (ValueError, TypeError):
            application = None
    if application is None and description.strip():
        hints = extract_application_hints(description)
        if hints.has_any:
            application = hints

    # Re-write the JSON when we changed it (recovered description
    # and/or freshly-extracted hints) so subsequent runs are instant
    # and downstream tooling sees the same data.
    needs_rewrite = description_was_recovered or (
        application is not None and not isinstance(raw_app, dict)
    )
    if needs_rewrite:
        if application is not None:
            data["application"] = application.model_dump(mode="json")
        # Best-effort: failing to persist isn't fatal — the
        # tailor run still gets the recovered text in-memory.
        with contextlib.suppress(OSError):
            path.write_text(
                json.dumps(data, indent=2, ensure_ascii=False),
                encoding="utf-8",
            )

    return RawJob(
        job_id=str(data.get("job_id") or ""),
        title=str(data.get("title") or ""),
        company=str(data.get("company") or ""),
        location=str(data.get("location") or ""),
        description=description,
        job_url=data.get("job_url") if isinstance(data.get("job_url"), str) else None,
        apply_url=data.get("apply_url") if isinstance(data.get("apply_url"), str) else None,
        site=str(data.get("site") or ""),
        date_posted=data.get("date_posted") if isinstance(data.get("date_posted"), str) else None,
        application=application,
    )


def peek_application_hints(jd_path: Path) -> ApplicationHints:
    """Cheaply extract application hints from ``jd_path`` without LLM calls.

    The CLI calls this *before* prompting for ``--email-to`` /
    ``--email-context`` so it can pre-fill those prompts with a
    recipient + subject the recruiter embedded in the JD body. The
    function never raises: any IO / parse failure yields an empty
    :class:`ApplicationHints` so the CLI degrades to its old
    "ask the user" behaviour.

    Both supported ``--job`` shapes are handled:

    * Saved per-job ``job.json``: prefer the persisted ``application``
      block, fall back to extracting from ``description``.
    * Raw JD files (``.md`` / ``.txt`` / ``.docx`` / ``.pdf``): read
      the text and run the extractor.
    """
    try:
        if jd_path.suffix.lower() == JOB_JSON_SUFFIX:
            data = json.loads(jd_path.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                return ApplicationHints()
            raw_app = data.get("application")
            if isinstance(raw_app, dict):
                try:
                    cached = ApplicationHints.model_validate(raw_app)
                except (ValueError, TypeError):
                    cached = ApplicationHints()
                if cached.has_any:
                    return cached
            return extract_application_hints(str(data.get("description") or ""))
        text = read_job_description_text(jd_path)
    except (OSError, ValueError, JobDescriptionReadError):
        return ApplicationHints()
    return extract_application_hints(text)


def _build_raw_job(text: str, meta: JobDescriptionMeta) -> RawJob:
    """Wrap the JD text + metadata in a synthetic :class:`RawJob`.

    The downstream agents (`tailor_resume`, `write_cover_letter`,
    `draft_application_email`) all consume ``RawJob`` instances, so we
    fabricate one. ``site`` is hard-coded to ``"local"`` and ``job_id``
    is derived from the stable hash so re-runs land in the same slug.
    Application hints are extracted from the JD body so
    ``--with-email`` benefits the same way the search/JSON paths do.
    """
    title = meta.title or "Tailored Application"
    company = meta.company or "Unknown Company"
    location = meta.location or ""
    job_id = stable_job_id(
        site="local",
        company=company,
        title=title,
        location=location,
        apply_url=None,
        job_url=None,
    )
    hints = extract_application_hints(text)
    return RawJob(
        job_id=job_id,
        title=title,
        company=company,
        location=location,
        description=text,
        site="local",
        application=hints if hints.has_any else None,
    )


def _maybe_render_pdfs(job_dir: Path, *, no_pdf: bool) -> dict[str, Path | None]:
    """Run the markdown / LaTeX PDF pipelines if ``no_pdf`` isn't set.

    Mirrors the behaviour of ``graph_nodes.process_one_node`` so the
    artifact set on disk looks identical between ``run`` and
    ``tailor`` outputs. PDF failures degrade silently — they're
    reported back as ``None`` so the caller can show a friendly hint.
    """
    out: dict[str, Path | None] = {
        "resume_pdf": None,
        "resume_latex_pdf": None,
        "cover_pdf": None,
        "cover_latex_pdf": None,
    }
    if no_pdf:
        return out

    resume_md_path = job_dir / "resume.md"
    cover_md_path = job_dir / "cover_letter.md"
    resume_tex_path = job_dir / "resume.tex"
    cover_tex_path = job_dir / "cover_letter.tex"

    rp = job_dir / "resume.pdf"
    if md_to_pdf(resume_md_path, rp):
        out["resume_pdf"] = rp.resolve()
    cp = job_dir / "cover_letter.pdf"
    if md_to_pdf(cover_md_path, cp):
        out["cover_pdf"] = cp.resolve()

    lp = tex_to_pdf(resume_tex_path, job_dir)
    if lp and lp.is_file():
        out["resume_latex_pdf"] = lp.resolve()
    clp = tex_to_pdf(cover_tex_path, job_dir)
    if clp and clp.is_file():
        out["cover_latex_pdf"] = clp.resolve()
    return out


def tailor_for_job_description(
    llm: BaseChatModel,
    *,
    jd_path: Path,
    profile_text: str,
    profile_skills: list[str],
    output_root: Path,
    target_skills: list[str] | None = None,
    title_override: str | None = None,
    company_override: str | None = None,
    location_override: str | None = None,
    no_pdf: bool = False,
    email: TailorEmailRequest | None = None,
    profile: Profile | None = None,
) -> TailorOutputs:
    """Tailor a resume + cover letter (and optionally an email) for one JD.

    ``output_root`` is typically the run's output directory; we write
    everything under ``output_root / <slug>/``. Returns a
    :class:`TailorOutputs` describing every file produced so the CLI can
    print a nice summary table.

    ``jd_path`` accepts two shapes:

    * A free-form JD file (``.md`` / ``.txt`` / ``.docx`` / ``.pdf``).
      The LLM JD parser fills in title / company / location.
    * A saved per-job ``job.json`` (written by ``jobapply search`` or
      ``jobapply run``). Title / company / location are taken straight
      from the file, skipping the JD-parser LLM call entirely.
    """
    if jd_path.suffix.lower() == JOB_JSON_SUFFIX:
        job = _load_job_from_json(jd_path)
        # CLI overrides still win (lets users tweak metadata without
        # editing the JSON).
        if title_override and title_override.strip():
            job.title = title_override.strip()
        if company_override and company_override.strip():
            job.company = company_override.strip()
        if location_override and location_override.strip():
            job.location = location_override.strip()
    else:
        raw_text = read_job_description_text(jd_path)
        meta = _resolve_meta(
            raw_text=raw_text,
            title_override=title_override,
            company_override=company_override,
            location_override=location_override,
            llm=llm,
        )
        job = _build_raw_job(raw_text, meta)

    slug = slugify(job.title, job.company, job.job_id)
    job_dir = slug_from_paths(slug, output_root)

    resume = tailor_resume(
        llm,
        profile_text=profile_text,
        job=job,
        skills=list(target_skills or []),
        profile_skills=profile_skills,
        profile=profile,
    )
    cover = write_cover_letter(llm, profile_text=profile_text, job=job, resume=resume)

    md_resume = render_resume_markdown(resume)
    md_cover = render_cover_markdown(cover)
    resume_md = job_dir / "resume.md"
    cover_md = job_dir / "cover_letter.md"
    resume_tex = job_dir / "resume.tex"
    cover_tex = job_dir / "cover_letter.tex"
    resume_md.write_text(md_resume, encoding="utf-8")
    cover_md.write_text(md_cover, encoding="utf-8")
    resume_tex.write_text(fill_resume_tex(resume), encoding="utf-8")
    cover_tex.write_text(
        fill_cover_letter_tex(
            cover,
            contact=resume.contact,
            name=resume.document_title or "Candidate",
            role=job.title,
        ),
        encoding="utf-8",
    )

    pdf_paths = _maybe_render_pdfs(job_dir, no_pdf=no_pdf)

    email_draft: EmailDraft | None = None
    email_path: Path | None = None
    if email is not None and email.recipient.strip():
        email_draft = draft_application_email(
            llm,
            profile_text=profile_text,
            job=job,
            resume=resume,
            cover=cover,
            recipient_email=email.recipient.strip(),
            additional_info=email.additional_info,
            sender_name=resume.document_title,
        )
        email_path = job_dir / "email.txt"
        email_path.write_text(email_draft.as_text(), encoding="utf-8")

    # Persist a compact metadata file so re-runs / debugging can see what
    # the LLM thought the role was without re-parsing the JD.
    meta_path = job_dir / "tailor_meta.json"
    _write_meta(
        meta_path,
        job=job,
        jd_path=jd_path,
        email=email_draft,
    )

    return TailorOutputs(
        job_dir=job_dir,
        job=job,
        resume=resume,
        cover=cover,
        email=email_draft,
        resume_md=resume_md.resolve(),
        resume_tex=resume_tex.resolve(),
        cover_md=cover_md.resolve(),
        cover_tex=cover_tex.resolve(),
        resume_pdf=pdf_paths.get("resume_pdf"),
        resume_latex_pdf=pdf_paths.get("resume_latex_pdf"),
        cover_pdf=pdf_paths.get("cover_pdf"),
        cover_latex_pdf=pdf_paths.get("cover_latex_pdf"),
        email_path=email_path.resolve() if email_path else None,
    )


def _write_meta(
    path: Path,
    *,
    job: RawJob,
    jd_path: Path,
    email: EmailDraft | None,
) -> None:
    """Persist a tailor_meta.json summary next to the artifacts."""
    import json

    payload = {
        "generated_at": datetime.now(UTC).isoformat(),
        "jd_source": str(jd_path.resolve()),
        "job": {
            "job_id": job.job_id,
            "title": job.title,
            "company": job.company,
            "location": job.location,
        },
        "email": email.model_dump(mode="json") if email is not None else None,
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
