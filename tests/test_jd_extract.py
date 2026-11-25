"""Heuristic extractor tests for ``jobapply.jd_extract``.

These pin the regex behaviour against a representative slice of the
real JDs we've seen in the wild — especially the PwC pattern
("Please send your resume to <email> / Please mention subject line
as ...") that prompted the feature in the first place.
"""

from __future__ import annotations

from jobapply.jd_extract import (
    extract_application_hints,
    extract_emails,
    extract_instructions,
    extract_subject_line,
    hints_to_email_context,
)

PWC_JD = """
Hi everyone!!!
Greeting from
#PwC
PwC India is actively looking for talented candidates those who will be at Mumbai location and also willing to relocate candidates!!
Education: B. Tech, M. Tech, B.E, MBA
Interested candidates please fill below form:
https://lnkd.in/dCMskEKC
Please Send your resume to
kirthana.xx.tpr@pwc.com
Please mention subject line as Job Application- Skillset
ETL + SQL (4 to 8 years of experience) Location: Mumbai
"""  # noqa: E501


def test_extract_emails_dedupes_and_lowercases() -> None:
    text = "Apply: Recruiter@Acme.com or recruiter@acme.com. Backup: hr@acme.com."
    assert extract_emails(text) == ["recruiter@acme.com", "hr@acme.com"]


def test_extract_emails_strips_trailing_punctuation() -> None:
    text = "Reach out to (jane@example.org), or backup>foo@bar.io<"
    out = extract_emails(text)
    assert "jane@example.org" in out
    assert "foo@bar.io" in out
    # No trailing punctuation snuck in.
    for addr in out:
        assert addr[-1].isalnum()


def test_extract_emails_handles_empty_or_none() -> None:
    assert extract_emails("") == []
    assert extract_emails("no email here") == []


def test_extract_subject_line_pwc_pattern() -> None:
    """The original motivating example: 'mention subject line as ...'."""
    assert (
        extract_subject_line("Please mention subject line as Job Application- Skillset")
        == "Job Application- Skillset"
    )


def test_extract_subject_line_with_quotes() -> None:
    assert (
        extract_subject_line('Please use subject line as "Backend Engineer Application".')
        == "Backend Engineer Application"
    )


def test_extract_subject_line_smart_quotes() -> None:
    assert (
        extract_subject_line("Please mention subject line as \u201cJob Application Skillset\u201d")
        == "Job Application Skillset"
    )


def test_extract_subject_line_subject_colon_form() -> None:
    assert extract_subject_line("Subject: ML Engineer - 2026") == "ML Engineer - 2026"


def test_extract_subject_line_with_subject_form() -> None:
    assert (
        extract_subject_line("Email me with subject 'Open Source Contribution'")
        == "Open Source Contribution"
    )


def test_extract_subject_line_returns_none_when_absent() -> None:
    assert extract_subject_line("Just apply via the LinkedIn easy-apply button.") is None


def test_extract_instructions_picks_apply_sentences() -> None:
    out = extract_instructions(
        "We're hiring a Backend Engineer. "
        "Please send your resume to recruiter@acme.com. "
        "Mention subject line as Backend Application. "
        "Salary will be competitive."
    )
    assert any("send your resume" in s.lower() for s in out)
    assert any("subject line" in s.lower() for s in out)
    assert not any("salary" in s.lower() for s in out)


def test_extract_instructions_caps_at_max() -> None:
    """Long enumerations of 'please send'/'please share' shouldn't blow up the context."""
    chunk = "Please send your resume to a@b.com. " * 20
    assert len(extract_instructions(chunk)) <= 6


def test_extract_application_hints_full_pwc_example() -> None:
    """End-to-end against the real PwC JD body that triggered this feature."""
    hints = extract_application_hints(PWC_JD)
    assert hints.has_any
    assert "kirthana.xx.tpr@pwc.com" in hints.emails
    assert hints.primary_email == "kirthana.xx.tpr@pwc.com"
    assert hints.subject_line == "Job Application- Skillset"
    # Both motivating instructions should surface:
    joined = " | ".join(hints.instructions).lower()
    assert "send your resume" in joined
    assert "subject line" in joined


def test_extract_application_hints_empty_when_no_signals() -> None:
    hints = extract_application_hints(
        "Senior Backend Engineer wanted. We use Python and Postgres. "
        "Apply via LinkedIn easy-apply."
    )
    # No email, no subject, no apply-by-email instructions → empty.
    assert not hints.has_any
    assert hints.primary_email is None


def test_primary_email_picks_address_after_trigger_phrase() -> None:
    """When two emails appear in the JD, the one closest to a 'send your
    resume to' trigger should win — not just the first one mentioned."""
    text = (
        "For technical questions ping engineering@example.com.\n"
        "Please send your resume to recruiter@example.com\n"
    )
    hints = extract_application_hints(text)
    assert hints.primary_email == "recruiter@example.com"
    # The other address is still surfaced for completeness.
    assert "engineering@example.com" in hints.emails


def test_primary_email_falls_back_to_first_when_no_trigger() -> None:
    text = "Questions: hello@example.com or careers@example.com."
    hints = extract_application_hints(text)
    assert hints.primary_email == "hello@example.com"


def test_hints_to_email_context_combines_subject_and_instructions() -> None:
    hints = extract_application_hints(PWC_JD)
    ctx = hints_to_email_context(hints)
    assert "Job Application- Skillset" in ctx
    assert "send your resume" in ctx.lower() or "send your" in ctx.lower()


def test_hints_to_email_context_empty_when_no_hints() -> None:
    assert hints_to_email_context(extract_application_hints("")) == ""
