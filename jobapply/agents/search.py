"""JobSpy search with retries."""

from __future__ import annotations

import math
from collections.abc import Iterator
from typing import Any, cast

from tenacity import retry, stop_after_attempt, wait_exponential

from jobapply.jd_extract import extract_application_hints
from jobapply.models import JobSearchInput, RawJob
from jobapply.utils import stable_job_id


def _row_to_dict(row: Any) -> dict[str, Any]:
    if hasattr(row, "to_dict"):
        return dict(row.to_dict())
    if isinstance(row, dict):
        return dict(row)
    return dict(row._asdict()) if hasattr(row, "_asdict") else dict(row)


@retry(wait=wait_exponential(multiplier=1, min=2, max=30), stop=stop_after_attempt(3))
def _scrape_once(
    *,
    site_name: list[str],
    search_term: str,
    location: str | None,
    results_wanted: int,
    hours_old: int,
    is_remote: bool,
    linkedin_fetch_description: bool = True,
) -> list[dict[str, Any]]:
    from jobspy import scrape_jobs

    # ``linkedin_fetch_description`` makes JobSpy issue an extra GET per
    # LinkedIn hit to scrape the full JD off the public job-view page.
    # JobSpy defaults this to False (fast, but `description` is blank
    # for every LinkedIn row), which silently breaks `jobapply tailor`
    # later — so we default to True and let the caller opt out.
    raw = scrape_jobs(
        site_name=site_name,
        search_term=search_term,
        location=location or "",
        results_wanted=results_wanted,
        hours_old=hours_old,
        is_remote=is_remote,
        linkedin_fetch_description=linkedin_fetch_description,
    )
    if hasattr(raw, "to_dict"):
        records = raw.to_dict("records")
        return cast(list[dict[str, Any]], records)
    return cast(list[dict[str, Any]], list(raw))


_MISSING = {"", "nan", "none", "<na>", "nat"}


def _text(value: Any) -> str:
    """JobSpy returns a pandas frame, so missing cells arrive as float NaN
    (or pd.NA / NaT). ``str(value or "")`` turned those into the literal
    company / URL "nan"; map every missing marker to ""."""
    if value is None:
        return ""
    if isinstance(value, float) and math.isnan(value):
        return ""
    text = str(value).strip()
    return "" if text.lower() in _MISSING else text


def _looks_remote(d: dict[str, Any]) -> bool:
    """Whether a JobSpy row is a remote role.

    Indeed ignores ``is_remote`` whenever ``hours_old`` is set and LinkedIn
    ignores it entirely, so the query flag alone returns on-site jobs. Trust
    JobSpy's per-row ``is_remote`` and fall back to the location text.
    """
    flag = d.get("is_remote")
    if flag is True or _text(flag).lower() == "true":
        return True
    return "remote" in _text(d.get("location")).lower()


_REMOTE_ALIASES = {"remote", "anywhere", "worldwide", "remote only", "fully remote"}


def _normalize_location(location: str | None, remote: bool) -> tuple[str | None, bool]:
    """Treat a location of just "Remote"/"Anywhere" as the remote filter.

    Job boards geocode the location string, so "Remote" is either ignored
    (LinkedIn returned Stockholm/London/NYC on-site roles in testing) or
    misread as a place. JobSpy's ``is_remote`` flag is the real filter.
    """
    loc = (location or "").strip()
    if loc.lower() in _REMOTE_ALIASES:
        return None, True
    return (loc or None), remote


def iter_search_jobs(inp: JobSearchInput) -> Iterator[RawJob]:
    """Stream JobSpy hits as they arrive, deduped by stable job_id.

    Yields one :class:`RawJob` at a time in title-major order so callers
    can persist results incrementally (e.g. ``jobapply search`` flushes
    ``jobs.json``/``jobs.csv`` after each yielded job for live feedback
    instead of waiting for the whole batch to finish). The generator
    stops as soon as ``inp.results_wanted`` unique jobs have been
    yielded.
    """
    site_name = inp.site_names or ["indeed", "linkedin", "google"]
    skills_q = " ".join(inp.skills) if inp.skills else ""
    location, is_remote = _normalize_location(inp.location, inp.remote)
    seen: set[str] = set()
    yielded = 0
    per_title = max(5, min(inp.results_wanted, 200 // max(1, len(inp.titles))))

    for title in inp.titles:
        term = f"{title.strip()} {skills_q}".strip()
        rows = _scrape_once(
            site_name=site_name,
            search_term=term,
            location=location,
            results_wanted=per_title,
            hours_old=inp.hours_old,
            is_remote=is_remote,
            linkedin_fetch_description=inp.linkedin_fetch_description,
        )
        for row in rows:
            d = _row_to_dict(row)
            if is_remote and not _looks_remote(d):
                continue
            title_s = _text(d.get("title"))
            company = _text(d.get("company"))
            location_s = _text(d.get("location"))
            site = _text(d.get("site"))
            job_url = _text(d.get("job_url")) or _text(d.get("url"))
            apply_url = _text(d.get("job_url_apply")) or _text(d.get("apply_url")) or job_url
            jid = stable_job_id(
                site=site,
                company=company,
                title=title_s,
                location=location_s,
                apply_url=apply_url or None,
                job_url=job_url or None,
            )
            if jid in seen:
                continue
            seen.add(jid)
            description = _text(d.get("description"))
            # Pull recipient email + subject-line / instruction hints
            # out of the JD body so downstream `--with-email` can
            # pre-fill `--email-to` / `--email-context` instead of
            # making the user copy-paste from the description.
            hints = extract_application_hints(description)
            yield RawJob(
                job_id=jid,
                title=title_s,
                company=company,
                location=location_s,
                description=description,
                job_url=job_url or None,
                apply_url=apply_url or None,
                site=site,
                date_posted=_text(d.get("date_posted")) or _text(d.get("date")) or None,
                application=hints if hints.has_any else None,
                raw=d,
            )
            yielded += 1
            if yielded >= inp.results_wanted:
                return


def search_jobs(inp: JobSearchInput) -> list[RawJob]:
    """Run JobSpy for each title; merge and dedupe by stable job_id.

    Thin wrapper around :func:`iter_search_jobs` that materializes the
    full list upfront. Use the generator directly when you want to
    react to results as they arrive.
    """
    return list(iter_search_jobs(inp))
