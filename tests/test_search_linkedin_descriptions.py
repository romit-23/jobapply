"""Verify the ``linkedin_fetch_description`` plumbing.

Without this flag JobSpy's LinkedIn scraper returns metadata only —
empty ``description`` for every hit — which silently breaks
``jobapply tailor`` later. We default the flag to True at the model
layer, plumb it through ``_scrape_once``, and let CLI users override
with ``--no-linkedin-descriptions``. These tests pin all three layers.

We don't import pandas / JobSpy directly here on purpose — the
project's macOS numpy install segfaults on init in CI, and avoiding
the import keeps the suite green. ``_scrape_once`` already accepts a
plain ``list[dict]`` from ``scrape_jobs`` (it only calls ``to_dict``
when the result is a DataFrame), so a list-returning fake is enough
to drive the code path.
"""

from __future__ import annotations

import sys
import types
from typing import Any

import pytest

from jobapply.models import JobSearchInput


def test_jobsearch_input_default_fetches_linkedin_descriptions() -> None:
    inp = JobSearchInput(titles=["Backend"])
    assert inp.linkedin_fetch_description is True


def test_jobsearch_input_can_disable_linkedin_descriptions() -> None:
    inp = JobSearchInput(titles=["Backend"], linkedin_fetch_description=False)
    assert inp.linkedin_fetch_description is False


def _install_fake_jobspy(monkeypatch: pytest.MonkeyPatch, captured: list[dict[str, Any]]) -> None:
    """Replace the top-level ``jobspy`` module with a fake whose
    ``scrape_jobs`` records its kwargs and returns an empty result.
    Done at module level so ``from jobspy import scrape_jobs`` inside
    ``_scrape_once`` resolves to our fake on its next call.
    """
    fake = types.ModuleType("jobspy")

    def _fake_scrape_jobs(**kwargs: Any) -> list[dict[str, Any]]:
        captured.append(kwargs)
        return []

    fake.scrape_jobs = _fake_scrape_jobs  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "jobspy", fake)


def test_iter_search_jobs_forwards_linkedin_fetch_description(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: list[dict[str, Any]] = []
    _install_fake_jobspy(monkeypatch, captured)
    # Import after the fake is installed so iter_search_jobs's own
    # imports don't trigger the real JobSpy package side-effects.
    from jobapply.agents.search import iter_search_jobs

    list(iter_search_jobs(JobSearchInput(titles=["Backend"])))

    assert captured, "scrape_jobs was never called"
    assert captured[0]["linkedin_fetch_description"] is True


def test_iter_search_jobs_honors_disabled_flag(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: list[dict[str, Any]] = []
    _install_fake_jobspy(monkeypatch, captured)
    from jobapply.agents.search import iter_search_jobs

    list(iter_search_jobs(JobSearchInput(titles=["Backend"], linkedin_fetch_description=False)))

    assert captured[0]["linkedin_fetch_description"] is False


def test_iter_search_jobs_populates_application_hints(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Recruiter-supplied apply-by-email info should be parsed off the
    JD body and attached to every yielded :class:`RawJob` so downstream
    `--with-email` tailoring can pre-fill the recipient + subject."""
    fake = types.ModuleType("jobspy")

    def _fake_scrape_jobs(**_kwargs: Any) -> list[dict[str, Any]]:
        return [
            {
                "title": "ETL + SQL",
                "company": "PwC",
                "location": "Mumbai",
                "site": "linkedin",
                "job_url": "https://www.linkedin.com/jobs/view/4408659493",
                "description": (
                    "Hi everyone! Greetings from PwC.\n"
                    "Please send your resume to kirthana.xx.tpr@pwc.com\n"
                    "Please mention subject line as Job Application- Skillset\n"
                ),
            }
        ]

    fake.scrape_jobs = _fake_scrape_jobs  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "jobspy", fake)
    from jobapply.agents.search import iter_search_jobs

    jobs = list(iter_search_jobs(JobSearchInput(titles=["ETL"])))
    assert len(jobs) == 1
    assert jobs[0].application is not None
    assert jobs[0].application.primary_email == "kirthana.xx.tpr@pwc.com"
    assert jobs[0].application.subject_line == "Job Application- Skillset"


def test_iter_search_jobs_omits_application_when_no_hints(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """JDs with no apply-by-email info should leave ``application``
    as ``None`` rather than carry an empty block — that keeps the
    CSV/JSON tidy and the ``has_any`` check meaningful."""
    fake = types.ModuleType("jobspy")
    fake.scrape_jobs = lambda **_kwargs: [  # type: ignore[attr-defined]
        {"title": "Eng", "company": "X", "site": "indeed", "description": "Apply via easy-apply."}
    ]
    monkeypatch.setitem(sys.modules, "jobspy", fake)
    from jobapply.agents.search import iter_search_jobs

    jobs = list(iter_search_jobs(JobSearchInput(titles=["Eng"])))
    assert jobs[0].application is None
