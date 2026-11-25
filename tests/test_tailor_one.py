"""End-to-end tailor_one tests with a fully mocked LLM.

These tests exercise the orchestration layer in
``jobapply.tailor_one`` without hitting the network: the fake LLM
returns canned structured outputs for every schema the pipeline
requests, so we can assert on the artifact tree on disk.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from jobapply.models import (
    CoverLetter,
    EmailDraft,
    JobDescriptionMeta,
    TailoredResume,
)
from jobapply.tailor_one import (
    TAILOR_INPUT_SUFFIXES,
    JobDescriptionReadError,
    TailorEmailRequest,
    _load_job_from_json,
    peek_application_hints,
    read_job_description_text,
    tailor_for_job_description,
)

PROFILE_TEXT = (
    "# Profile\n"
    "## Header\n"
    "- **Name:** Jane Doe\n"
    "- **Email:** jane@example.com\n\n"
    "## Skills\n- Python\n- Kubernetes\n"
)


class _FakeStructured:
    def __init__(self, out: Any) -> None:
        self._out = out

    def invoke(self, _msgs: Any) -> Any:
        return self._out


class FakeChatModel:
    """Returns a sensible canned object for every schema the pipeline
    asks for. Matches the schemas used by the tailor flow only — fit
    scoring isn't part of single-JD tailoring."""

    def with_structured_output(self, schema: type[Any]) -> _FakeStructured:
        if schema.__name__ == "JobDescriptionMeta":
            return _FakeStructured(
                JobDescriptionMeta(
                    title="Backend Engineer",
                    company="Acme",
                    location="Remote",
                )
            )
        if schema.__name__ == "TailoredResume":
            return _FakeStructured(
                TailoredResume(
                    document_title="Jane Doe",
                    contact_line="jane@example.com",
                    summary="Experienced backend engineer.",
                    skills=["Python", "Kubernetes"],
                    experience=[],
                    projects=[],
                )
            )
        if schema.__name__ == "CoverLetter":
            return _FakeStructured(
                CoverLetter(
                    header="Jane Doe",
                    opening="Hi team,",
                    body="I would love to join.",
                    closing="Best,\nJane",
                )
            )
        if schema.__name__ == "EmailDraft":
            return _FakeStructured(
                EmailDraft(
                    to="",
                    subject="Backend Engineer Application — Jane Doe",
                    body="Hi team,\n\nReady-to-paste body.\n\nBest,\nJane",
                )
            )
        raise AssertionError(f"unexpected schema {schema}")


@pytest.fixture()
def jd_path(tmp_path: Path) -> Path:
    p = tmp_path / "jd.txt"
    p.write_text(
        "We are hiring a Backend Engineer at Acme. Remote. "
        "Python, Postgres, Kubernetes required.\n",
        encoding="utf-8",
    )
    return p


def test_tailor_writes_resume_and_cover_letter(tmp_path: Path, jd_path: Path) -> None:
    output_root = tmp_path / "tailor-test"
    out = tailor_for_job_description(
        FakeChatModel(),
        jd_path=jd_path,
        profile_text=PROFILE_TEXT,
        profile_skills=["Python", "Kubernetes"],
        output_root=output_root,
        target_skills=["Python"],
        no_pdf=True,
    )

    assert out.job.title == "Backend Engineer"
    assert out.job.company == "Acme"
    assert out.job.location == "Remote"

    assert out.resume_md.is_file()
    assert out.resume_tex.is_file()
    assert out.cover_md.is_file()
    assert out.cover_tex.is_file()

    assert out.resume_pdf is None  # no_pdf=True
    assert out.cover_pdf is None
    assert out.email is None
    assert out.email_path is None

    assert "Jane Doe" in out.resume_md.read_text(encoding="utf-8")
    assert "I would love to join" in out.cover_md.read_text(encoding="utf-8")

    meta_file = out.job_dir / "tailor_meta.json"
    assert meta_file.is_file()
    assert "Backend Engineer" in meta_file.read_text(encoding="utf-8")


def test_tailor_with_email_writes_email_artifact(tmp_path: Path, jd_path: Path) -> None:
    output_root = tmp_path / "tailor-test"
    out = tailor_for_job_description(
        FakeChatModel(),
        jd_path=jd_path,
        profile_text=PROFILE_TEXT,
        profile_skills=["Python", "Kubernetes"],
        output_root=output_root,
        no_pdf=True,
        email=TailorEmailRequest(
            recipient="recruiter@acme.com",
            additional_info="Referred by Bob.",
        ),
    )

    assert out.email is not None
    assert out.email.to == "recruiter@acme.com"
    assert out.email_path is not None
    assert out.email_path.is_file()

    text = out.email_path.read_text(encoding="utf-8")
    assert text.startswith("To: recruiter@acme.com")
    assert "Subject: Backend Engineer Application" in text
    assert "Ready-to-paste body" in text


def test_tailor_skips_llm_meta_call_when_overrides_complete(tmp_path: Path, jd_path: Path) -> None:
    """When --title/--company/--location are all supplied, the JD parser
    is skipped (saves a call) and the overrides are honoured verbatim."""
    output_root = tmp_path / "tailor-test"

    class _NoMetaLLM(FakeChatModel):
        def with_structured_output(self, schema: type[Any]) -> _FakeStructured:
            assert (
                schema.__name__ != "JobDescriptionMeta"
            ), "JD parser should be skipped when overrides are complete"
            return super().with_structured_output(schema)

    out = tailor_for_job_description(
        _NoMetaLLM(),
        jd_path=jd_path,
        profile_text=PROFILE_TEXT,
        profile_skills=["Python", "Kubernetes"],
        output_root=output_root,
        title_override="Senior Platform Engineer",
        company_override="Globex",
        location_override="NYC",
        no_pdf=True,
    )

    assert out.job.title == "Senior Platform Engineer"
    assert out.job.company == "Globex"
    assert out.job.location == "NYC"


def test_read_job_description_rejects_unsupported_extension(tmp_path: Path) -> None:
    bad = tmp_path / "jd.html"
    bad.write_text("<p>jd</p>", encoding="utf-8")
    with pytest.raises(JobDescriptionReadError, match="Unsupported"):
        read_job_description_text(bad)


def test_read_job_description_rejects_missing_file(tmp_path: Path) -> None:
    with pytest.raises(JobDescriptionReadError, match="No such file"):
        read_job_description_text(tmp_path / "does_not_exist.txt")


def test_read_job_description_rejects_empty_file(tmp_path: Path) -> None:
    p = tmp_path / "empty.txt"
    p.write_text("   \n   ", encoding="utf-8")
    with pytest.raises(JobDescriptionReadError, match="extract any text"):
        read_job_description_text(p)


def test_tailor_falls_back_to_synthetic_meta_when_overrides_missing(
    tmp_path: Path, jd_path: Path
) -> None:
    """Even when the LLM (somehow) returns blank metadata and the user
    didn't override, the pipeline still writes artifacts under a sane
    fallback slug rather than crashing."""

    class _BlankMetaLLM(FakeChatModel):
        def with_structured_output(self, schema: type[Any]) -> _FakeStructured:
            if schema.__name__ == "JobDescriptionMeta":
                return _FakeStructured(JobDescriptionMeta())
            return super().with_structured_output(schema)

    output_root = tmp_path / "tailor-test"
    out = tailor_for_job_description(
        _BlankMetaLLM(),
        jd_path=jd_path,
        profile_text=PROFILE_TEXT,
        profile_skills=["Python", "Kubernetes"],
        output_root=output_root,
        no_pdf=True,
    )

    assert out.job.title == "Tailored Application"
    assert out.job.company == "Unknown Company"
    assert out.resume_md.is_file()


# ---------------------------------------------------------------------------
# Saved-job JSON input path
# ---------------------------------------------------------------------------


def test_tailor_input_suffixes_includes_json() -> None:
    """The CLI uses TAILOR_INPUT_SUFFIXES to validate ``--job`` paths;
    saved per-job JSONs must be in that allowlist."""
    assert ".json" in TAILOR_INPUT_SUFFIXES
    # Original JD formats stay supported.
    for s in (".md", ".txt", ".pdf", ".docx"):
        assert s in TAILOR_INPUT_SUFFIXES


def test_load_job_from_json_accepts_raw_job_shape(tmp_path: Path) -> None:
    """The bare RawJob dump (what ``graph_nodes.dedupe_node`` writes) is
    the most common shape — load it cleanly with no loss."""
    p = tmp_path / "job.json"
    p.write_text(
        json.dumps(
            {
                "job_id": "abc123",
                "title": "Backend Engineer",
                "company": "Acme",
                "location": "Remote",
                "description": "We are hiring a Backend Engineer at Acme.",
                "job_url": "https://example.com/jobs/abc123",
                "apply_url": "https://example.com/apply/abc123",
                "site": "indeed",
            }
        ),
        encoding="utf-8",
    )
    job = _load_job_from_json(p)
    assert job.title == "Backend Engineer"
    assert job.company == "Acme"
    assert job.location == "Remote"
    assert job.apply_url == "https://example.com/apply/abc123"
    assert job.description.startswith("We are hiring")


def test_load_job_from_json_accepts_job_record_shape(tmp_path: Path) -> None:
    """The richer JobRecord dump (from ``cli._persist_job_record``) has
    extra fields like ``status`` and ``fit`` — those should be ignored,
    not crash the loader."""
    p = tmp_path / "job.json"
    p.write_text(
        json.dumps(
            {
                "job_id": "abc123",
                "title": "Backend Engineer",
                "company": "Acme",
                "location": "Remote",
                "description": "We are hiring a Backend Engineer at Acme.",
                "job_url": "https://example.com/jobs/abc123",
                "apply_url": "https://example.com/apply/abc123",
                "site": "indeed",
                "status": "pending",
                "fit": {"score": 0.84, "rationale": "ok", "missing_keywords": []},
                "tailored_resume": None,
            }
        ),
        encoding="utf-8",
    )
    job = _load_job_from_json(p)
    assert job.title == "Backend Engineer"
    # `fit` is silently dropped — RawJob has no such field.
    assert not hasattr(job, "fit") or job.model_dump().get("fit") is None
    assert job.apply_url == "https://example.com/apply/abc123"


def test_load_job_from_json_rejects_empty_description(tmp_path: Path) -> None:
    """A truncated / placeholder file should fail loudly when there is
    no URL to backfill from — and the message must point the user at
    the workaround (``jobapply search`` defaults / paste-as-file)."""
    p = tmp_path / "job.json"
    p.write_text(
        json.dumps({"title": "X", "company": "Y", "description": "   "}),
        encoding="utf-8",
    )
    with pytest.raises(JobDescriptionReadError, match="no `description`"):
        # Inject a fetcher that always returns None so we don't hit the network.
        _load_job_from_json(p, fetcher=lambda _url: None)


def test_load_job_from_json_backfills_from_apply_url(tmp_path: Path) -> None:
    """If the saved JSON has an empty description but a usable URL, we
    should fetch the JD on the fly and persist it back to disk so the
    next tailor run is instant."""
    p = tmp_path / "job.json"
    payload = {
        "job_id": "abc123",
        "title": "ETL + SQL",
        "company": "PwC",
        "location": "Mumbai",
        "description": "",
        "apply_url": "https://www.linkedin.com/jobs/view/4408659493",
        "job_url": "https://www.linkedin.com/jobs/view/4408659493",
        "site": "linkedin",
    }
    p.write_text(json.dumps(payload), encoding="utf-8")

    calls: list[str] = []

    def _fake_fetch(url: str) -> str:
        calls.append(url)
        return "About the role:\nDesign and operate ETL pipelines on AWS S3 ..."

    job = _load_job_from_json(p, fetcher=_fake_fetch)

    assert job.description.startswith("About the role:")
    # The first URL we try is apply_url — we should never fall through
    # to job_url when apply_url succeeds.
    assert calls == ["https://www.linkedin.com/jobs/view/4408659493"]
    # File must be rewritten so subsequent runs skip the backfill.
    rewritten = json.loads(p.read_text(encoding="utf-8"))
    assert rewritten["description"].startswith("About the role:")
    # Other fields must be preserved verbatim.
    assert rewritten["job_id"] == "abc123"
    assert rewritten["company"] == "PwC"


def test_load_job_from_json_falls_through_to_job_url(tmp_path: Path) -> None:
    """When ``apply_url`` is missing or fails, we should retry with
    ``job_url`` before giving up."""
    p = tmp_path / "job.json"
    payload = {
        "title": "X",
        "company": "Y",
        "description": "",
        "apply_url": "",  # empty → skip
        "job_url": "https://www.linkedin.com/jobs/view/123",
    }
    p.write_text(json.dumps(payload), encoding="utf-8")

    def _fake_fetch(url: str) -> str:
        assert url == "https://www.linkedin.com/jobs/view/123"
        return "Long enough description to count as recovered text."

    job = _load_job_from_json(p, fetcher=_fake_fetch)
    assert "Long enough description" in job.description


def test_load_job_from_json_errors_when_backfill_fails(tmp_path: Path) -> None:
    """If neither URL yields anything, we surface the friendly error
    that points users at the manual workarounds."""
    p = tmp_path / "job.json"
    p.write_text(
        json.dumps(
            {
                "title": "X",
                "company": "Y",
                "description": "",
                "apply_url": "https://example.com/jobs/1",
                "job_url": "https://example.com/jobs/1",
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(JobDescriptionReadError, match="backfill couldn't recover"):
        _load_job_from_json(p, fetcher=lambda _url: None)


def test_load_job_from_json_rejects_malformed_file(tmp_path: Path) -> None:
    p = tmp_path / "job.json"
    p.write_text("not actually json {", encoding="utf-8")
    with pytest.raises(JobDescriptionReadError, match="Could not read"):
        _load_job_from_json(p)


def test_tailor_with_json_input_skips_jd_parser(tmp_path: Path) -> None:
    """When the user passes a saved job.json, the LLM JD parser must NOT
    be called — title / company / location come straight from the file.
    """
    job_json = tmp_path / "job.json"
    job_json.write_text(
        json.dumps(
            {
                "job_id": "abc123",
                "title": "Senior Backend Engineer",
                "company": "Globex",
                "location": "NYC",
                "description": "Hiring a Senior Backend Engineer at Globex in NYC.",
                "site": "indeed",
            }
        ),
        encoding="utf-8",
    )

    class _NoJDParserLLM(FakeChatModel):
        def with_structured_output(self, schema: type[Any]) -> _FakeStructured:
            assert (
                schema.__name__ != "JobDescriptionMeta"
            ), "JD parser should be skipped for saved-job JSON input"
            return super().with_structured_output(schema)

    out = tailor_for_job_description(
        _NoJDParserLLM(),
        jd_path=job_json,
        profile_text=PROFILE_TEXT,
        profile_skills=["Python", "Kubernetes"],
        output_root=tmp_path / "tailor-test",
        no_pdf=True,
    )

    assert out.job.title == "Senior Backend Engineer"
    assert out.job.company == "Globex"
    assert out.job.location == "NYC"
    assert out.resume_md.is_file()
    assert out.cover_md.is_file()


def test_tailor_with_json_input_honors_overrides(tmp_path: Path) -> None:
    """``--title`` / ``--company`` / ``--location`` still win over the
    JSON values, in case the user wants to tweak them at the CLI."""
    job_json = tmp_path / "job.json"
    job_json.write_text(
        json.dumps(
            {
                "job_id": "abc123",
                "title": "Backend Engineer",
                "company": "Acme",
                "location": "Remote",
                "description": "Hiring a Backend Engineer at Acme.",
                "site": "indeed",
            }
        ),
        encoding="utf-8",
    )

    out = tailor_for_job_description(
        FakeChatModel(),
        jd_path=job_json,
        profile_text=PROFILE_TEXT,
        profile_skills=["Python", "Kubernetes"],
        output_root=tmp_path / "tailor-test",
        title_override="Staff Engineer",
        company_override="Initech",
        no_pdf=True,
    )

    assert out.job.title == "Staff Engineer"
    assert out.job.company == "Initech"
    # Untouched override falls back to the JSON value.
    assert out.job.location == "Remote"


# ---------------------------------------------------------------- #
# Application-hint loading                                          #
# ---------------------------------------------------------------- #


_PWC_DESCRIPTION = (
    "Hi everyone! Greetings from PwC.\n"
    "Please send your resume to kirthana.xx.tpr@pwc.com\n"
    "Please mention subject line as Job Application- Skillset\n"
    "ETL + SQL position based out of Mumbai."
)


def test_load_job_from_json_extracts_hints_from_description(tmp_path: Path) -> None:
    """Legacy ``job.json`` files (no ``application`` field) should get
    hints extracted from their description on load — and the file
    should be rewritten with the new block so future runs are
    instant."""
    p = tmp_path / "job.json"
    p.write_text(
        json.dumps(
            {
                "job_id": "abc",
                "title": "ETL + SQL",
                "company": "PwC",
                "description": _PWC_DESCRIPTION,
                "site": "linkedin",
            }
        ),
        encoding="utf-8",
    )

    job = _load_job_from_json(p, fetcher=lambda _u: None)

    assert job.application is not None
    assert job.application.primary_email == "kirthana.xx.tpr@pwc.com"
    assert job.application.subject_line == "Job Application- Skillset"
    # File was rewritten — primary_email persisted.
    rewritten = json.loads(p.read_text(encoding="utf-8"))
    assert rewritten["application"]["primary_email"] == "kirthana.xx.tpr@pwc.com"


def test_load_job_from_json_prefers_persisted_application_block(tmp_path: Path) -> None:
    """When ``application`` is already on disk we should use it as-is
    (don't re-extract — the search-side extractor already ran)."""
    p = tmp_path / "job.json"
    p.write_text(
        json.dumps(
            {
                "job_id": "abc",
                "title": "ETL + SQL",
                "company": "PwC",
                "description": _PWC_DESCRIPTION,
                "site": "linkedin",
                "application": {
                    "emails": ["override@example.com"],
                    "primary_email": "override@example.com",
                    "subject_line": "Custom subject",
                    "instructions": [],
                },
            }
        ),
        encoding="utf-8",
    )
    job = _load_job_from_json(p, fetcher=lambda _u: None)
    assert job.application is not None
    assert job.application.primary_email == "override@example.com"
    assert job.application.subject_line == "Custom subject"


def test_load_job_from_json_extracts_hints_after_url_backfill(tmp_path: Path) -> None:
    """When we recover the description from URL backfill, the hints
    in the persisted JSON (if any) shouldn't be trusted — re-extract
    against the freshly-recovered text."""
    p = tmp_path / "job.json"
    p.write_text(
        json.dumps(
            {
                "job_id": "abc",
                "title": "ETL + SQL",
                "company": "PwC",
                "description": "",
                "apply_url": "https://www.linkedin.com/jobs/view/123",
                "site": "linkedin",
                # Stale (or wrong) application block from a previous run.
                "application": {
                    "primary_email": "stale@example.com",
                    "emails": ["stale@example.com"],
                    "subject_line": "stale subject",
                    "instructions": [],
                },
            }
        ),
        encoding="utf-8",
    )

    job = _load_job_from_json(p, fetcher=lambda _u: _PWC_DESCRIPTION)

    assert job.application is not None
    assert job.application.primary_email == "kirthana.xx.tpr@pwc.com"
    assert job.application.subject_line == "Job Application- Skillset"


def test_peek_application_hints_reads_json_application_block(tmp_path: Path) -> None:
    p = tmp_path / "job.json"
    p.write_text(
        json.dumps(
            {
                "title": "X",
                "description": _PWC_DESCRIPTION,
                "application": {
                    "emails": ["already@parsed.com"],
                    "primary_email": "already@parsed.com",
                    "subject_line": "Already parsed",
                    "instructions": ["already parsed"],
                },
            }
        ),
        encoding="utf-8",
    )
    hints = peek_application_hints(p)
    assert hints.primary_email == "already@parsed.com"
    assert hints.subject_line == "Already parsed"


def test_peek_application_hints_falls_back_to_description(tmp_path: Path) -> None:
    p = tmp_path / "job.json"
    p.write_text(
        json.dumps({"title": "X", "description": _PWC_DESCRIPTION}),
        encoding="utf-8",
    )
    hints = peek_application_hints(p)
    assert hints.primary_email == "kirthana.xx.tpr@pwc.com"
    assert hints.subject_line == "Job Application- Skillset"


def test_peek_application_hints_handles_raw_text_files(tmp_path: Path) -> None:
    p = tmp_path / "jd.txt"
    p.write_text(_PWC_DESCRIPTION, encoding="utf-8")
    hints = peek_application_hints(p)
    assert hints.primary_email == "kirthana.xx.tpr@pwc.com"


def test_peek_application_hints_returns_empty_on_missing_file(tmp_path: Path) -> None:
    """Never raises — the CLI relies on the empty fallback to
    seamlessly degrade to its original prompt behaviour."""
    hints = peek_application_hints(tmp_path / "nope.txt")
    assert not hints.has_any
