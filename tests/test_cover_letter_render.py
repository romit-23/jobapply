"""Tests for the LaTeX cover-letter shell rendered by ``fill_cover_letter_tex``.

Covers header construction (left phone/email column, right links column),
multi-paragraph body splitting, multi-line closing, and graceful defaults
when the LLM omits the closing.
"""

from __future__ import annotations

from jobapply.models import ContactInfo, CoverLetter
from jobapply.nodes.render import fill_cover_letter_tex


def _full_contact() -> ContactInfo:
    return ContactInfo(
        email="arun@example.com",
        phone="+91 84850-19026",
        portfolio="https://arunaddagatla.vercel.app/",
        github="arun2728",
        linkedin="arun-addagatla",
        medium="@arunaddagatla",
        twitter="arun2728",
    )


def test_renders_name_and_role_into_header() -> None:
    out = fill_cover_letter_tex(
        CoverLetter(opening="Dear Hiring Manager,", body="I am writing to express interest."),
        contact=ContactInfo(),
        name="Arun Addagatla",
        role="Machine Learning Engineer",
    )
    assert "{\\Huge Arun Addagatla}" in out
    assert "Machine Learning Engineer" in out
    assert "%%JOBAPPLY_NAME%%" not in out
    assert "%%JOBAPPLY_ROLE%%" not in out


def test_left_block_emits_phone_and_email_with_hrefs() -> None:
    out = fill_cover_letter_tex(
        CoverLetter(),
        contact=_full_contact(),
        name="Arun",
        role="ML Engineer",
    )
    # tel: link with raw digits, label preserves the formatted phone string.
    assert r"\href{tel:+918485019026}" in out
    assert r"\faPhone" in out
    assert "+91 84850-19026" in out
    # mailto: hyperlink for email.
    assert r"\href{mailto:arun@example.com}" in out
    assert r"\faEnvelope" in out


def test_right_block_emits_link_lines_for_socials() -> None:
    out = fill_cover_letter_tex(
        CoverLetter(),
        contact=_full_contact(),
        name="Arun",
        role="ML Engineer",
    )
    assert r"LinkedIn: \href{https://linkedin.com/in/arun-addagatla}{arun-addagatla}" in out
    assert r"Portfolio: \href{https://arunaddagatla.vercel.app/}{arunaddagatla.vercel.app}" in out
    assert r"GitHub: \href{https://github.com/arun2728}{arun2728}" in out
    assert r"Medium: \href{https://medium.com/@arunaddagatla}{@arunaddagatla}" in out
    assert r"Twitter: \href{https://twitter.com/arun2728}{arun2728}" in out


def test_empty_contact_leaves_minipage_blocks_blank_but_template_intact() -> None:
    out = fill_cover_letter_tex(
        CoverLetter(opening="Dear Hiring Manager,", body="Hello."),
        contact=ContactInfo(),
        name="Jane Doe",
        role="",
    )
    assert "%%JOBAPPLY_LEFT%%" not in out
    assert "%%JOBAPPLY_RIGHT%%" not in out
    assert "%%JOBAPPLY_ROLE%%" not in out
    assert r"\begin{document}" in out
    assert r"\end{document}" in out


def test_body_splits_paragraphs_on_blank_lines() -> None:
    body = (
        "Para one talks about Python and ML.\n\n"
        "Para two talks about Kubernetes.\n\n"
        "Para three closes the pitch."
    )
    out = fill_cover_letter_tex(
        CoverLetter(opening="Dear Hiring Manager,", body=body, closing="Sincerely,\nArun"),
        contact=ContactInfo(),
        name="Arun",
        role="ML",
    )
    assert "Dear Hiring Manager," in out
    assert "Para one talks about Python and ML." in out
    assert "Para two talks about Kubernetes." in out
    assert "Para three closes the pitch." in out
    # Each paragraph should be separated by a blank line from the next.
    body_section = out.split("Date: \\today")[1]
    assert "Para one talks about Python and ML.\n\nPara two" in body_section


def test_body_collapses_intra_paragraph_newlines_to_spaces() -> None:
    body = "Line one of paragraph\nstill same paragraph.\n\nNew paragraph here."
    out = fill_cover_letter_tex(
        CoverLetter(opening="Hello,", body=body),
        contact=ContactInfo(),
        name="Arun",
        role="ML",
    )
    assert "Line one of paragraph still same paragraph." in out
    assert "New paragraph here." in out


def test_body_escapes_latex_specials() -> None:
    out = fill_cover_letter_tex(
        CoverLetter(
            opening="Dear Hiring Manager,",
            body="I scaled inference by 50% and shipped R&D for $1M ARR.",
        ),
        contact=ContactInfo(),
        name="Arun",
        role="",
    )
    assert r"50\%" in out
    assert r"R\&D" in out
    assert r"\$1M" in out


def test_multiline_closing_joined_with_double_backslash() -> None:
    out = fill_cover_letter_tex(
        CoverLetter(closing="Sincerely,\nArun Addagatla"),
        contact=ContactInfo(),
        name="Arun Addagatla",
        role="",
    )
    assert r"Sincerely, \\ Arun Addagatla" in out


def test_missing_closing_defaults_to_sincerely_plus_name() -> None:
    out = fill_cover_letter_tex(
        CoverLetter(opening="Hi,", body="Body."),
        contact=ContactInfo(),
        name="Jane Doe",
        role="",
    )
    assert r"Sincerely, \\ Jane Doe" in out


def test_role_and_name_are_latex_escaped() -> None:
    out = fill_cover_letter_tex(
        CoverLetter(),
        contact=ContactInfo(),
        name="A & B",
        role="R&D Lead",
    )
    assert r"{\Huge A \& B}" in out
    assert r"R\&D Lead" in out


def test_compiles_a_realistic_full_letter() -> None:
    cover = CoverLetter(
        header="ignored in latex",
        opening="Dear Hiring Manager,",
        body=(
            "I'm excited to apply for the Senior ML role at Acme.\n\n"
            "Most recently, I scaled an LLM inference engine to 1M+ requests/month.\n\n"
            "I'd love to discuss how my experience maps to your roadmap."
        ),
        closing="Sincerely,\nArun Addagatla",
    )
    out = fill_cover_letter_tex(
        cover,
        contact=_full_contact(),
        name="Arun Addagatla",
        role="Senior Machine Learning Engineer",
    )
    # No unsubstituted placeholders remain.
    assert "%%JOBAPPLY_" not in out
    # Document opens and closes correctly.
    assert out.lstrip().startswith("%%%%")
    assert out.rstrip().endswith(r"\end{document}")
    # Title block emitted.
    assert "{\\color{UI_blue} \\Large{COVER LETTER}}" in out
