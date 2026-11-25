"""Resume tailoring agent.

Historically this module asked the LLM to produce an entire
:class:`TailoredResume` — including the candidate's contact info,
education metadata, employer names, role titles, dates, GPAs and
school locations. That worked when we used premium frontier models
that strictly honour structured-output schemas, but on
OpenAI-compatible open-weights endpoints (Cloudflare Workers AI,
OpenRouter free tiers, …) the model regularly:

* hallucinated employer / school names that aren't in the profile,
* rewrote dates ("Mar 2025 – Present" → "March 2025 to Now"),
* dropped or merged "less relevant" experience rows,
* invented GPAs / coursework / locations,
* mangled the contact line (URLs shortened, phone numbers reformatted).

None of those rewrites help the candidate — the data is already
authoritative in :class:`Profile`. So this module now follows a
*deterministic where possible, AI where helpful* strategy:

* Contact info, education entries, employer / role / dates and project
  names / descriptions are copied verbatim from the profile.
* The LLM only produces a small structured payload with the parts
  that actually benefit from JD-aware rewriting: the summary, the
  skill ordering, and one rewritten bullet list per experience role
  (and per project, when the profile has projects).

The output schema for the LLM (:class:`_TailoredBullets`) is private
to this module — callers still receive a :class:`TailoredResume`
assembled deterministically from the profile + LLM bullets.

The legacy "ask the LLM for the whole TailoredResume" path is kept
for callers that don't pass a structured ``profile`` (a handful of
older tests). Real production callers — :func:`tailor_for_job_description`
and the LangGraph node — always pass one.
"""

from __future__ import annotations

from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import HumanMessage, SystemMessage
from pydantic import BaseModel, Field

from jobapply.agents._structured import invoke_structured
from jobapply.models import (
    ContactInfo,
    EducationItem,
    ExperienceRole,
    ProjectItem,
    RawJob,
    TailoredResume,
)
from jobapply.profile import Profile
from jobapply.profile_validation import merge_skills_preserving_order

# ---------------------------------------------------------------------------
# Deterministic profile → resume helpers
# ---------------------------------------------------------------------------


def _format_profile_dates(start: str, end: str) -> str:
    """Mirror the on-resume date format (e.g. ``"Mar 2025 – Present"``).

    Used everywhere a resume row needs a date range so the rendered
    output has consistent typography across roles, projects and
    education entries.
    """
    s = (start or "").strip()
    e = (end or "").strip()
    if s and e:
        return f"{s} – {e}"
    return s or e


def _contact_from_profile(profile: Profile) -> ContactInfo:
    """Verbatim mapping of profile contact fields → :class:`ContactInfo`.

    Skipping the LLM round-trip for these fields removes the most
    common class of resume-tailor hallucinations (bad emails, dropped
    LinkedIn URLs, reformatted phone numbers).
    """
    return ContactInfo(
        email=profile.email,
        phone=profile.phone,
        location=profile.location,
        portfolio=profile.portfolio,
        github=profile.github,
        linkedin=profile.linkedin,
        medium=profile.medium,
        twitter=profile.twitter,
    )


def _contact_line_from_profile(profile: Profile) -> str:
    """Build the legacy single-line contact fallback from the profile.

    The structured ``contact`` block is what the LaTeX renderer
    actually consumes, but we keep ``contact_line`` populated for the
    markdown template + downstream consumers that still read a flat
    string.
    """
    parts: list[str] = []
    if profile.email.strip():
        parts.append(profile.email.strip())
    if profile.phone.strip():
        parts.append(profile.phone.strip())
    if profile.location.strip():
        parts.append(profile.location.strip())
    if profile.linkedin.strip():
        parts.append(profile.linkedin.strip())
    if profile.github.strip():
        parts.append(profile.github.strip())
    if profile.portfolio.strip():
        parts.append(profile.portfolio.strip())
    return " | ".join(parts)


def _education_from_profile(profile: Profile) -> list[EducationItem]:
    """Translate :class:`ProfileEducation` rows into :class:`EducationItem`.

    No LLM involvement: schools, degrees, locations, dates, GPAs and
    coursework are facts, not things the model should rephrase.
    """
    out: list[EducationItem] = []
    for ed in profile.education:
        if not (ed.school or ed.degree or ed.location or ed.gpa):
            continue
        out.append(
            EducationItem(
                school=ed.school,
                degree=ed.degree,
                location=ed.location,
                dates=_format_profile_dates(ed.start_date, ed.end_date),
                gpa=ed.gpa,
                coursework=", ".join(c for c in ed.coursework if c.strip()),
                details=ed.honors,
            )
        )
    return out


def _experience_from_profile(profile: Profile) -> list[ExperienceRole]:
    """Profile experience → :class:`ExperienceRole`, bullets verbatim.

    The deterministic baseline that ``tailor_resume`` falls back to
    when the LLM fails to produce rewritten bullets for a given role.
    """
    out: list[ExperienceRole] = []
    for role in profile.experience:
        if not (role.company or role.role or role.bullets):
            continue
        out.append(
            ExperienceRole(
                company=role.company,
                role=role.role,
                dates=_format_profile_dates(role.start_date, role.end_date),
                bullets=[b for b in role.bullets if b and b.strip()],
            )
        )
    return out


def _projects_from_profile(profile: Profile) -> list[ProjectItem]:
    """Same fallback shape as :func:`_experience_from_profile`, for projects."""
    out: list[ProjectItem] = []
    for project in profile.projects:
        if not (project.name or project.bullets):
            continue
        bullets = [b for b in project.bullets if b and b.strip()]
        if project.description and project.description not in bullets:
            bullets.insert(0, project.description.strip())
        out.append(ProjectItem(name=project.name or "Project", bullets=bullets))
    return out


def _non_blank_experience(profile: Profile) -> list:
    """Profile experience rows we'll actually render — same predicate as
    the prompt-building code so LLM indices line up with our pairing
    indices.

    Returning a fresh list (rather than filtering in two places) keeps
    the "what the LLM sees" / "what we render" lists in lock-step and
    makes the pairing index unambiguous.
    """
    return [role for role in profile.experience if role.company or role.role or role.bullets]


def _non_blank_projects(profile: Profile) -> list:
    """Same predicate-matched filter for projects (see
    :func:`_non_blank_experience`)."""
    return [project for project in profile.projects if project.name or project.bullets]


def _pair_experience(profile: Profile, llm_bullets: list[list[str]]) -> list[ExperienceRole]:
    """Pair each non-blank profile role (in order) with the LLM's
    rewritten bullets.

    The LLM is asked for ``experience_bullets`` as a list-of-lists in
    the same order as the profile's non-blank experience rows. When
    the LLM omits an entry or returns an empty list for a role, we
    transparently fall back to the original profile bullets so the
    candidate's history is never lost.
    """
    rows = _non_blank_experience(profile)
    out: list[ExperienceRole] = []
    for i, role in enumerate(rows):
        rewritten = [b for b in llm_bullets[i] if b and b.strip()] if i < len(llm_bullets) else []
        bullets = rewritten or [b for b in role.bullets if b and b.strip()]
        out.append(
            ExperienceRole(
                company=role.company,
                role=role.role,
                dates=_format_profile_dates(role.start_date, role.end_date),
                bullets=bullets,
            )
        )
    return out


def _pair_projects(profile: Profile, llm_bullets: list[list[str]]) -> list[ProjectItem]:
    """Same as :func:`_pair_experience` for the ``projects`` section."""
    rows = _non_blank_projects(profile)
    out: list[ProjectItem] = []
    for i, project in enumerate(rows):
        rewritten = [b for b in llm_bullets[i] if b and b.strip()] if i < len(llm_bullets) else []
        source_bullets = [b for b in project.bullets if b and b.strip()]
        bullets = rewritten or list(source_bullets)
        # Same description-promotion rule as the legacy fallback so a
        # one-line project description still surfaces in the rendered
        # resume even when the LLM rewrote the bullet list.
        if project.description and project.description not in bullets:
            bullets.insert(0, project.description.strip())
        out.append(ProjectItem(name=project.name or "Project", bullets=bullets))
    return out


# ---------------------------------------------------------------------------
# LLM output schema
# ---------------------------------------------------------------------------


class _TailoredBullets(BaseModel):
    """Compact LLM payload — only the parts we want the model to rewrite.

    Restricting the output schema to JD-tailorable fields cuts token
    usage roughly in half (no contact / education / dates / employer
    metadata to re-emit) and removes whole categories of
    hallucinations because the model literally can't say anything
    about the immutable parts of the resume.
    """

    summary: str = Field(
        "",
        description=(
            "2-3 sentence professional summary tailored to this job. "
            "Used internally by the cover-letter agent — NOT rendered "
            "on the resume itself."
        ),
    )
    skills: list[str] = Field(
        default_factory=list,
        description=(
            "ALL skills from the candidate's profile, reordered so "
            "the most JD-relevant come first. Never drop or merge."
        ),
    )
    experience_bullets: list[list[str]] = Field(
        default_factory=list,
        description=(
            "One rewritten bullet list per role, in the same order "
            "as the candidate's profile experience. Each inner list "
            "should preserve the count of source bullets."
        ),
    )
    project_bullets: list[list[str]] = Field(
        default_factory=list,
        description=(
            "One rewritten bullet list per project, in the same "
            "order as the candidate's profile projects. Empty list "
            "when the profile has no projects."
        ),
    )


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def tailor_resume(
    llm: BaseChatModel,
    *,
    profile_text: str,
    job: RawJob,
    skills: list[str],
    profile_skills: list[str] | None = None,
    profile: Profile | None = None,
) -> TailoredResume:
    """Build a :class:`TailoredResume` for ``job``.

    With a structured ``profile`` (the standard production path), the
    resume is assembled deterministically from profile facts plus
    LLM-rewritten bullets / summary / skill ordering. Without a
    profile (``profile is None``, used only by legacy tests / older
    callers), we fall back to asking the LLM for the entire
    :class:`TailoredResume`.

    ``profile_skills`` is the canonical, deduplicated list of every
    skill on :class:`Profile`. The deterministic path uses
    ``profile.skills`` directly when this argument is omitted.
    """
    if profile is not None:
        return _tailor_with_profile(
            llm,
            profile=profile,
            profile_text=profile_text,
            job=job,
            skills=skills,
            profile_skills=profile_skills,
        )
    return _legacy_tailor_via_llm(
        llm,
        profile_text=profile_text,
        job=job,
        skills=skills,
        profile_skills=profile_skills,
    )


def _tailor_with_profile(
    llm: BaseChatModel,
    *,
    profile: Profile,
    profile_text: str,
    job: RawJob,
    skills: list[str],
    profile_skills: list[str] | None,
) -> TailoredResume:
    """The deterministic-with-LLM-bullets path."""
    canonical_skills = profile_skills or list(profile.skills)
    bullets = _request_tailored_bullets(
        llm,
        profile=profile,
        profile_text=profile_text,
        job=job,
        skills=skills,
        canonical_skills=canonical_skills,
    )

    # ``merge_skills_preserving_order`` guarantees every profile skill
    # survives even if the model dropped some — the LLM's relative
    # ordering wins where the lists overlap.
    final_skills = (
        merge_skills_preserving_order(bullets.skills, canonical_skills)
        if canonical_skills
        else list(bullets.skills)
    )

    return TailoredResume(
        document_title=profile.name or "",
        contact=_contact_from_profile(profile),
        contact_line=_contact_line_from_profile(profile),
        summary=bullets.summary or "",
        skills=final_skills,
        experience=_pair_experience(profile, bullets.experience_bullets),
        projects=_pair_projects(profile, bullets.project_bullets),
        education=_education_from_profile(profile),
    )


def _request_tailored_bullets(
    llm: BaseChatModel,
    *,
    profile: Profile,
    profile_text: str,
    job: RawJob,
    skills: list[str],
    canonical_skills: list[str],
) -> _TailoredBullets:
    """Single LLM call asking for summary + skills + bullets only."""
    jd = (job.description or "")[:12000]
    profile_skill_line = (
        ", ".join(canonical_skills) if canonical_skills else "(profile has no skills)"
    )
    # Filter blank rows up-front so the indices we show the LLM line
    # up exactly with the indices ``_pair_experience`` /
    # ``_pair_projects`` use when we later assemble the resume.
    exp_rows = _non_blank_experience(profile)
    proj_rows = _non_blank_projects(profile)
    role_lines = (
        "\n".join(
            f"  [{i}] {(r.role or '(role)')} @ {(r.company or '(company)')}"
            f" — {len([b for b in r.bullets if b.strip()])} source bullets"
            for i, r in enumerate(exp_rows)
        )
        or "  (profile has no experience entries)"
    )
    project_lines = (
        "\n".join(
            f"  [{i}] {(p.name or '(project)')}"
            f" — {len([b for b in p.bullets if b.strip()])} source bullets"
            for i, p in enumerate(proj_rows)
        )
        or "  (profile has no projects)"
    )

    sys = SystemMessage(
        content=(
            "You tailor a resume's bullet points and summary to a "
            "specific job description. Everything else (contact info, "
            "education, employer / role / dates, project names) is "
            "filled in deterministically from the candidate's profile "
            "— do NOT include any of those facts in your response and "
            "do NOT try to rewrite or reformat them.\n\n"
            "Return a single JSON object that matches the requested "
            "schema:\n"
            "- `summary`: 2-3 sentences tailored to this job.\n"
            "- `skills`: every skill from the profile's `## Skills` "
            "section, REORDERED so the most JD-relevant come first. "
            "You MUST include every skill — do not drop, summarize, "
            "or merge any. Preserve original wording (parenthesized "
            "aliases like 'Model Context Protocol (MCP)' stay).\n"
            "- `experience_bullets`: one inner list per profile role, "
            "in the SAME ORDER they appear in the profile. Rephrase "
            "each source bullet to emphasize keywords from the JD "
            "without inventing facts, metrics, or technologies that "
            "aren't already in the source bullet. Keep the same "
            "number of bullets unless two are obvious duplicates. "
            "Order within a role: same as source.\n"
            "- `project_bullets`: same shape, one inner list per "
            "project. Empty list when the profile has no projects.\n\n"
            "RULES: stay strictly truthful. Do not invent companies, "
            "roles, dates, schools, degrees, GPAs, locations, "
            "technologies, or metrics. Do not drop or merge roles or "
            "projects. Do not add commentary outside the JSON object."
        ),
    )
    user = HumanMessage(
        content=(
            f"Target skills to align with: "
            f"{', '.join(skills) if skills else '(none)'}\n\n"
            f"PROFILE SKILLS (every one of these must appear in `skills`, "
            f"reordered by JD relevance): {profile_skill_line}\n\n"
            f"PROFILE ROLES (return EXACTLY this many bullet lists in "
            f"`experience_bullets`, in this order):\n{role_lines}\n\n"
            f"PROFILE PROJECTS (return EXACTLY this many bullet lists in "
            f"`project_bullets`, in this order):\n{project_lines}\n\n"
            f"ROLE: {job.title} at {job.company}\n\nJOB DESCRIPTION:\n{jd}\n\n"
            f"BASE PROFILE (source of truth for the bullets you'll "
            f"rewrite — copy facts, rephrase wording):\n"
            f"{profile_text[:20000]}"
        ),
    )
    return invoke_structured(llm, _TailoredBullets, [sys, user])


# ---------------------------------------------------------------------------
# Legacy path (LLM produces full TailoredResume)
# ---------------------------------------------------------------------------


def _legacy_tailor_via_llm(
    llm: BaseChatModel,
    *,
    profile_text: str,
    job: RawJob,
    skills: list[str],
    profile_skills: list[str] | None,
) -> TailoredResume:
    """Older path retained for callers that don't pass a ``Profile``.

    Keeps the original "ask the LLM for the whole TailoredResume"
    behaviour so legacy tests / callers don't regress; production
    code always pairs this module with a structured profile and uses
    :func:`_tailor_with_profile` instead.
    """
    jd = (job.description or "")[:12000]
    canonical_skills: list[str] = profile_skills or []
    sys = SystemMessage(
        content=(
            "You rewrite the candidate's resume content for THIS job. "
            "Facts must stay truthful—rephrase and emphasize relevance; do not invent employers, "
            "degrees, schools, GPAs, or metrics. Output structured sections only.\n\n"
            "HEADER: Fill document_title with the candidate's real name. Populate the structured "
            "`contact` object from the profile's Header section: email, phone, location (plain "
            "text), and links. Classify each link by host into github / linkedin / medium / "
            "twitter / portfolio (anything that isn't one of the named services goes into "
            "portfolio). Copy URLs verbatim; do not shorten them. Leave a field empty if absent. "
            "Also set contact_line to the same info as a single human-readable plain-text line "
            "(used as a fallback when the renderer can't draw icons).\n\n"
            "SUMMARY: Still set `summary` (the cover-letter agent reads it for context). It is "
            "NOT rendered in the resume itself.\n\n"
            "SKILLS: The `skills` field MUST contain EVERY skill listed in the candidate's "
            "profile `## Skills` section, even ones that aren't directly relevant to this "
            "specific job. Do NOT drop, omit, summarize, filter, or merge skills based on "
            "perceived job-relevance. Preserve the candidate's original wording (including "
            "parenthesized aliases like 'Model Context Protocol (MCP)'). You SHOULD reorder "
            "the list so skills most relevant to the job description appear first; the rest "
            "follow in the candidate's original order. You MAY add a few skills clearly "
            "evidenced by the experience/projects bullets if they're missing — but never "
            "drop any.\n\n"
            "EXPERIENCE: The `experience` field MUST include EVERY role from the candidate's "
            "profile `## Experience` section — do not drop, merge, or omit any role under any "
            "circumstance. For each role, copy `company`, `role`, and the date range exactly "
            "as written; populate `bullets` from the profile's bullets, optionally rephrasing "
            "to emphasize keywords from the job description (without inventing facts, metrics, "
            "or technologies that aren't in the source bullet). Keep ALL bullets — never "
            "truncate the list of bullets to 'save space' or remove ones you think are less "
            "relevant. Order roles newest-first, exactly as the profile lists them.\n\n"
            "PROJECTS: The `projects` field MUST include EVERY project from the candidate's "
            "profile `## Projects` section — same rules as EXPERIENCE: copy the name verbatim, "
            "preserve all bullets (rephrasing for relevance is fine, dropping is not). If the "
            "profile has no projects, return an empty list.\n\n"
            "EDUCATION: For each entry populate school, degree, dates, and split GPA + "
            "coursework into the dedicated fields. `gpa` is the bare value with its scale, e.g. "
            "'9.6/10' or '3.85/4.0' — DO NOT prefix it with 'GPA:' (the renderer adds that). "
            "`coursework` is a comma-separated list of relevant courses with NO leading "
            "'Course Work:' prefix (e.g. 'OS, ML, NLP, DBMS, Networking'). Use `details` only "
            "for honors/thesis text that doesn't fit GPA or coursework. If the profile lacks "
            "GPA or coursework, leave those fields empty rather than fabricating values."
        ),
    )
    profile_skill_line = (
        ", ".join(canonical_skills) if canonical_skills else "(profile has no skills)"
    )
    user = HumanMessage(
        content=(
            f"Target skills to align with: {', '.join(skills) if skills else '(none)'}\n\n"
            f"PROFILE SKILLS (ALL of these must appear in the output `skills` field, "
            f"reordered so the most job-relevant come first): {profile_skill_line}\n\n"
            f"ROLE: {job.title} at {job.company}\n\nJOB DESCRIPTION:\n{jd}\n\n"
            f"BASE PROFILE (source of truth):\n{profile_text[:20000]}"
        ),
    )
    result = invoke_structured(llm, TailoredResume, [sys, user])
    if canonical_skills:
        result.skills = merge_skills_preserving_order(result.skills, canonical_skills)
    return result
