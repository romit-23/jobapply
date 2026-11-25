"""Pydantic models for search input, jobs, agent outputs, and persisted records."""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field


class LedgerStatus(StrEnum):
    pending = "pending"
    tailored = "tailored"
    rendered = "rendered"
    done = "done"
    skipped = "skipped"
    failed = "failed"
    cached = "cached"


class JobSearchInput(BaseModel):
    """User search parameters."""

    titles: list[str] = Field(..., min_length=1, description="One or more job titles to search.")
    skills: list[str] = Field(default_factory=list, description="Primary skills (boost search).")
    location: str | None = Field(None, description="City, country, or empty if remote-only.")
    remote: bool = Field(False, description="If true, bias toward remote-friendly queries.")
    results_wanted: int = Field(30, ge=1, le=500)
    hours_old: int = Field(720, ge=0, description="Max age of postings in hours (JobSpy).")
    site_names: list[str] = Field(
        default_factory=lambda: ["indeed", "linkedin", "google"],
        description="JobSpy site_name list.",
    )
    linkedin_fetch_description: bool = Field(
        True,
        description=(
            "Make JobSpy fetch the full LinkedIn job description page for "
            "every hit. LinkedIn's search API only returns metadata; "
            "without this flag the `description` field is blank, which "
            "breaks downstream tailoring/scoring. Disable (slower → "
            "faster) only if you're hitting LinkedIn rate limits."
        ),
    )


class ApplicationHints(BaseModel):
    """Recruiter-supplied application details parsed from the JD body.

    Recruiters routinely embed instructions like "send your resume to
    ``recruiter@acme.com``" and "mention subject line as 'Job
    Application — Skillset'" directly in the description. We extract
    them once at fetch time so ``jobapply tailor --with-email`` can
    pre-fill ``--email-to`` and ``--email-context`` instead of making
    the user copy-paste, and so ``jobs.csv`` can surface the
    recipient address as its own triage column.

    All fields default empty so the model is safe to attach
    unconditionally — a JD with no application info just yields a
    fully-default ``ApplicationHints`` rather than ``None``.
    """

    emails: list[str] = Field(
        default_factory=list,
        description="Every email address found in the JD, deduped, in source order.",
    )
    primary_email: str | None = Field(
        None,
        description=(
            "The address most likely to be the recipient (closest to a "
            "trigger phrase like 'send your resume to'). Falls back to "
            "the first email when no trigger matched. ``None`` only when "
            "no emails were found."
        ),
    )
    subject_line: str | None = Field(
        None,
        description=(
            "Subject line the recruiter asked applicants to use, if "
            "they specified one (e.g. 'Job Application - Skillset')."
        ),
    )
    instructions: list[str] = Field(
        default_factory=list,
        description=(
            "Short sentences from the JD that contain explicit "
            "application instructions (where to send, what to mention, "
            "etc.). Capped at ~6 sentences to keep email-drafter "
            "context focused."
        ),
    )

    @property
    def has_any(self) -> bool:
        """True when at least one hint field is populated.

        Lets callers cheaply skip prefill logic when the JD didn't
        carry any apply-by-email info (the common case for postings
        that go through an ATS rather than a direct recruiter
        address).
        """
        return bool(self.emails or self.primary_email or self.subject_line or self.instructions)


class RawJob(BaseModel):
    """Normalized job row from JobSpy / search layer."""

    model_config = {"extra": "allow"}

    job_id: str = Field(..., description="Stable id for ledger (hash of key fields).")
    title: str = ""
    company: str = ""
    location: str = ""
    description: str = ""
    job_url: str | None = None
    apply_url: str | None = None
    site: str = ""
    date_posted: str | None = None
    application: ApplicationHints | None = Field(
        None,
        description=(
            "Application-by-email hints parsed from ``description`` "
            "(see :class:`ApplicationHints`). Populated by "
            "``iter_search_jobs`` / ``dedupe_node`` / "
            "``tailor_one._load_job_from_json``. ``None`` for jobs "
            "fetched before this field existed."
        ),
    )
    raw: dict[str, Any] = Field(default_factory=dict, description="Original row as dict.")


class FitScore(BaseModel):
    """Structured output from fit-scoring agent."""

    score: float = Field(..., ge=0.0, le=1.0)
    rationale: str = ""
    missing_keywords: list[str] = Field(default_factory=list)
    must_haves_present: list[str] = Field(default_factory=list)


class ExperienceRole(BaseModel):
    company: str
    role: str
    dates: str = ""
    bullets: list[str] = Field(default_factory=list)


class ProjectItem(BaseModel):
    name: str
    bullets: list[str] = Field(default_factory=list)


class EducationItem(BaseModel):
    """Single education entry rendered in the resume.

    ``gpa`` and ``coursework`` are pulled out of the freeform ``details``
    string so the LaTeX template can place them in the right slot
    (GPA right of the degree, coursework on its own italic line).
    ``details`` remains for backward compatibility / freeform notes that
    don't fit the structured fields.
    """

    school: str = ""
    degree: str = ""
    location: str = Field("", description="Plain-text city/country (e.g. 'Mumbai, India').")
    dates: str = ""
    gpa: str = Field("", description="GPA value with scale, e.g. '9.6/10' or '3.85/4.0'.")
    coursework: str = Field(
        "", description="Comma-separated relevant coursework (no leading 'Course Work:')."
    )
    details: str = Field(
        "", description="Freeform extras (honors, thesis title) when GPA/coursework don't fit."
    )


class ContactInfo(BaseModel):
    """Structured contact info rendered as hyperlinked icons in the resume header.

    Each field accepts either a full URL (``https://...``) or a bare username /
    handle. The renderer normalizes both forms, derives a clean display label
    (e.g. ``github/arun2728``), and wraps the entry in ``\\href{}{...}``.
    """

    email: str = ""
    phone: str = ""
    location: str = Field("", description="Plain-text city/country; no link.")
    portfolio: str = Field("", description="Personal site URL.")
    github: str = Field("", description="Username or full GitHub URL.")
    linkedin: str = Field("", description="Username or full LinkedIn URL.")
    medium: str = Field("", description="Username or full Medium URL.")
    twitter: str = Field("", description="Username or full Twitter/X URL.")

    def has_any(self) -> bool:
        return any(getattr(self, f) for f in type(self).model_fields)


class TailoredResume(BaseModel):
    """Structured resume body (rendered to MD/LaTeX)."""

    document_title: str = Field("", description="Candidate name for PDF header.")
    contact_line: str = Field(
        "",
        description=(
            "Optional fallback contact line as plain text (used only when "
            "structured `contact` is empty)."
        ),
    )
    contact: ContactInfo = Field(
        default_factory=ContactInfo,
        description="Structured contact details rendered as hyperlinked icons.",
    )
    summary: str = ""
    skills: list[str] = Field(default_factory=list)
    experience: list[ExperienceRole] = Field(default_factory=list)
    projects: list[ProjectItem] = Field(default_factory=list)
    education: list[EducationItem] = Field(default_factory=list)


class CoverLetter(BaseModel):
    header: str = ""
    opening: str = ""
    body: str = ""
    closing: str = ""

    def as_markdown(self) -> str:
        parts = [p for p in (self.header, self.opening, self.body, self.closing) if p.strip()]
        return "\n\n".join(parts).strip() + "\n"


class OutreachMessages(BaseModel):
    referral_request: str = ""
    cold_email: str = ""


class JobDescriptionMeta(BaseModel):
    """Metadata an LLM extracts from a raw job-description blob.

    Used by the ``jobapply tailor`` flow when the user only hands us a JD
    file: we still need a job title and company name to seed the resume /
    cover-letter agents and to generate the output slug.
    """

    title: str = Field("", description="Job title (e.g. 'Senior Backend Engineer').")
    company: str = Field("", description="Hiring company name.")
    location: str = Field("", description="Office / remote location, if mentioned.")


class EmailDraft(BaseModel):
    """A ready-to-paste application email produced by the email drafter agent.

    ``subject`` and ``body`` are split so the CLI can render them separately
    (and so callers can drop them straight into a mail client). ``to`` echoes
    the recipient address the user supplied — keeping it on the model lets
    persisted artifacts (`email.txt`) be self-contained.
    """

    to: str = Field("", description="Recipient email address (echoed from the user input).")
    subject: str = Field(..., description="Concise, specific email subject line.")
    body: str = Field(..., description="Plain-text email body, ready to paste.")

    def as_text(self) -> str:
        """Render the draft as a plain-text email block.

        Format::

            To: <to>
            Subject: <subject>

            <body>
        """
        lines: list[str] = []
        if self.to.strip():
            lines.append(f"To: {self.to.strip()}")
        lines.append(f"Subject: {self.subject.strip()}")
        lines.append("")
        lines.append(self.body.strip())
        return "\n".join(lines).rstrip() + "\n"


class JobArtifacts(BaseModel):
    """Paths written under output/run-.../jobs/<slug>/."""

    job_json: str | None = None
    resume_md: str | None = None
    resume_pdf: str | None = None
    resume_tex: str | None = None
    resume_latex_pdf: str | None = None
    cover_letter_md: str | None = None
    cover_letter_pdf: str | None = None
    cover_letter_tex: str | None = None
    cover_letter_latex_pdf: str | None = None
    networking_json: str | None = None


class JobRecord(BaseModel):
    """Single job entry in jobs.json and in-memory results."""

    job_id: str
    title: str = ""
    company: str = ""
    location: str = ""
    description: str = ""
    job_url: str | None = None
    apply_url: str | None = None
    site: str = ""
    status: LedgerStatus = LedgerStatus.pending
    fit: FitScore | None = None
    application: ApplicationHints | None = None
    tailored_resume: TailoredResume | None = None
    cover_letter: CoverLetter | None = None
    networking: OutreachMessages | None = None
    artifacts: JobArtifacts = Field(default_factory=JobArtifacts)
    error: str | None = None
    processed_at: datetime | None = None

    def model_dump_for_json(self) -> dict[str, Any]:
        d = self.model_dump(mode="json")
        if isinstance(d.get("processed_at"), datetime):
            d["processed_at"] = d["processed_at"].isoformat()
        return d


class JobsIndex(BaseModel):
    """Root object for jobs.json."""

    run_id: str
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    search: JobSearchInput
    profile_path: str
    provider: str
    model: str
    jobs: list[JobRecord] = Field(default_factory=list)
