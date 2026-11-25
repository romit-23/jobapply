"""Markdown / LaTeX rendering and optional PDF via pandoc / tectonic."""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path
from urllib.parse import urlparse

from jinja2 import Environment, FileSystemLoader

from jobapply.models import (
    ContactInfo,
    CoverLetter,
    EducationItem,
    ExperienceRole,
    ProjectItem,
    TailoredResume,
)


def _templates_dir() -> Path:
    return Path(__file__).resolve().parent.parent / "templates"


def latex_escape(s: str) -> str:
    rep = {
        "\\": r"\textbackslash{}",
        "&": r"\&",
        "%": r"\%",
        "$": r"\$",
        "#": r"\#",
        "_": r"\_",
        "{": r"\{",
        "}": r"\}",
        "~": r"\textasciitilde{}",
        "^": r"\textasciicircum{}",
    }
    out = []
    for ch in s or "":
        out.append(rep.get(ch, ch))
    return "".join(out)


def _url_escape(url: str) -> str:
    """Escape characters that break ``\\href{...}`` (% and #)."""
    return url.replace("%", r"\%").replace("#", r"\#")


# ----------------------------- contact info -------------------------------- #

# Domain patterns used to extract the username when a full URL is supplied.
_GITHUB_RE = re.compile(r"(?:https?://)?(?:www\.)?github\.com/([^/?#]+)", re.IGNORECASE)
_LINKEDIN_RE = re.compile(
    r"(?:https?://)?(?:www\.)?linkedin\.com/(?:in/)?([^/?#]+)",
    re.IGNORECASE,
)
_MEDIUM_RE = re.compile(
    r"(?:https?://)?(?:www\.)?medium\.com/(?:@)?([^/?#]+)",
    re.IGNORECASE,
)
_TWITTER_RE = re.compile(
    r"(?:https?://)?(?:www\.)?(?:twitter\.com|x\.com)/([^/?#]+)",
    re.IGNORECASE,
)


def _strip_handle(value: str) -> str:
    return value.strip().lstrip("@/").rstrip("/")


def _normalize_phone(value: str) -> tuple[str, str]:
    label = value.strip()
    digits = re.sub(r"[^\d+]", "", label)
    href = f"tel:{digits or label}"
    return href, label


def _normalize_email(value: str) -> tuple[str, str]:
    label = value.strip()
    return f"mailto:{label}", label


def _normalize_via_regex(
    value: str,
    pattern: re.Pattern[str],
    base_url: str,
    label_prefix: str,
    *,
    keep_at_in_label: bool = False,
) -> tuple[str, str]:
    """Match ``pattern`` against ``value`` to extract a username, or treat
    ``value`` as a bare handle. Return ``(href, label)``.
    """
    match = pattern.search(value)
    user = match.group(1) if match else _strip_handle(value)
    user = user.strip("/")
    href = f"{base_url}{user}"
    label = f"{label_prefix}/@{user}" if keep_at_in_label else f"{label_prefix}/{user}"
    return href, label


def _normalize_portfolio(value: str) -> tuple[str, str]:
    raw = value.strip()
    href = raw if raw.startswith(("http://", "https://")) else f"https://{raw}"
    parsed = urlparse(href)
    host = parsed.netloc.removeprefix("www.")
    label = host or raw or href
    return href, label


# fontawesome5 commands. Falls back gracefully if a viewer lacks the glyph.
_ICON: dict[str, str] = {
    "phone": r"\faPhone",
    "email": r"\faEnvelope",
    "portfolio": r"\faGlobe",
    "github": r"\faGithub",
    "linkedin": r"\faLinkedin",
    "medium": r"\faMedium",
    "twitter": r"\faTwitter",
    "location": r"\faMapMarker",
}


def _icon_block(href: str, icon: str, label: str, raise_height: str = "-0.15") -> str:
    """Render a single ``\\href{...}{\\raisebox{...} <icon>\\ <label>}`` entry."""
    safe_url = _url_escape(href)
    safe_label = latex_escape(label)
    return f"\\href{{{safe_url}}}{{\\raisebox{{{raise_height}\\height}} {icon}\\ {safe_label}}}"


def _location_block(value: str) -> str:
    """Plain-text location with an icon, no hyperlink."""
    return f"\\raisebox{{-0.15\\height}} {_ICON['location']}\\ {latex_escape(value.strip())}"


def build_contact_subtitle(c: ContactInfo) -> str:
    """Build the multi-icon subtitle row used inside ``\\documentTitle{}{...}``."""
    parts: list[str] = []
    if c.phone:
        href, label = _normalize_phone(c.phone)
        parts.append(_icon_block(href, _ICON["phone"], label, raise_height="-0.05"))
    if c.email:
        href, label = _normalize_email(c.email)
        parts.append(_icon_block(href, _ICON["email"], label))
    if c.portfolio:
        href, label = _normalize_portfolio(c.portfolio)
        parts.append(_icon_block(href, _ICON["portfolio"], label))
    if c.github:
        href, label = _normalize_via_regex(
            c.github,
            _GITHUB_RE,
            base_url="https://github.com/",
            label_prefix="github",
        )
        parts.append(_icon_block(href, _ICON["github"], label))
    if c.linkedin:
        href, label = _normalize_via_regex(
            c.linkedin,
            _LINKEDIN_RE,
            base_url="https://linkedin.com/in/",
            label_prefix="linkedin",
        )
        parts.append(_icon_block(href, _ICON["linkedin"], label))
    if c.medium:
        href, label = _normalize_via_regex(
            c.medium,
            _MEDIUM_RE,
            base_url="https://medium.com/@",
            label_prefix="medium",
            keep_at_in_label=True,
        )
        parts.append(_icon_block(href, _ICON["medium"], label))
    if c.twitter:
        href, label = _normalize_via_regex(
            c.twitter,
            _TWITTER_RE,
            base_url="https://twitter.com/",
            label_prefix="twitter",
        )
        parts.append(_icon_block(href, _ICON["twitter"], label))
    if c.location:
        parts.append(_location_block(c.location))
    return " ~ | ~ ".join(parts)


def build_contact_markdown(c: ContactInfo) -> str:
    """Compact markdown contact line: pipe-separated hyperlinks."""
    parts: list[str] = []
    if c.phone:
        href, label = _normalize_phone(c.phone)
        parts.append(f"[{label}]({href})")
    if c.email:
        href, label = _normalize_email(c.email)
        parts.append(f"[{label}]({href})")
    if c.portfolio:
        href, label = _normalize_portfolio(c.portfolio)
        parts.append(f"[{label}]({href})")
    if c.github:
        href, label = _normalize_via_regex(c.github, _GITHUB_RE, "https://github.com/", "github")
        parts.append(f"[{label}]({href})")
    if c.linkedin:
        href, label = _normalize_via_regex(
            c.linkedin, _LINKEDIN_RE, "https://linkedin.com/in/", "linkedin"
        )
        parts.append(f"[{label}]({href})")
    if c.medium:
        href, label = _normalize_via_regex(
            c.medium,
            _MEDIUM_RE,
            "https://medium.com/@",
            "medium",
            keep_at_in_label=True,
        )
        parts.append(f"[{label}]({href})")
    if c.twitter:
        href, label = _normalize_via_regex(
            c.twitter, _TWITTER_RE, "https://twitter.com/", "twitter"
        )
        parts.append(f"[{label}]({href})")
    if c.location:
        parts.append(c.location.strip())
    return " | ".join(parts)


def _summary_latex(summary: str) -> str:
    if not summary.strip():
        return ""
    return (
        f"\\section{{Summary}}\n\\hspace{{10pt}}{latex_escape(summary.strip())}\n\\vspace{{2pt}}\n"
    )


def _skills_latex(skills: list[str]) -> str:
    """Two-column bulleted skills, matching the MTeck multicols layout."""
    if not skills:
        return ""
    items = "\n".join(rf"  \item {latex_escape(s)}" for s in skills)
    return (
        "\\section{Skills}\n"
        "\\begin{multicols}{2}\n"
        "\\begin{itemize}[itemsep=-2px, parsep=1pt, leftmargin=20pt]\n"
        f"{items}\n"
        "\\end{itemize}\n"
        "\\end{multicols}\n"
    )


def _resume_list(bullets: list[str]) -> str:
    if not bullets:
        return ""
    body = "\n".join(rf"  \item {latex_escape(b)}" for b in bullets)
    return f"\\begin{{resume_list}}\n{body}\n\\end{{resume_list}}\n"


def _experience_latex(exp: list[ExperienceRole]) -> str:
    if not exp:
        return ""
    parts: list[str] = ["\\section{Experience}"]
    for role in exp:
        parts.append(rf"\headingBf{{{latex_escape(role.company)}}}{{{latex_escape(role.dates)}}}")
        parts.append(rf"\headingIt{{{latex_escape(role.role)}}}{{}}")
        parts.append(_resume_list(role.bullets))
    return "\n".join(parts) + "\n"


def _projects_latex(projects: list[ProjectItem]) -> str:
    if not projects:
        return ""
    parts: list[str] = ["\\section{Projects}"]
    for p in projects:
        parts.append(rf"\headingBf{{{latex_escape(p.name)}}}{{}}")
        parts.append(_resume_list(p.bullets))
    return "\n".join(parts) + "\n"


def _education_latex(education: list[EducationItem]) -> str:
    """Render education entries in MTeck style.

    Layout per entry::

        \\headingBf{<school>}{<dates>}
        \\headingIt{<degree>}{GPA: <gpa>}        % right cell shows GPA when present
        \\headingIt{Course Work: <coursework>}{} % full-width line, only when set
        \\headingIt{<details>}{}                  % only if no gpa/coursework filled it

    Empty cells are still emitted (``\\headingIt{...}{}``) so the table-like
    alignment matches the Experience section.
    """
    if not education:
        return ""
    parts: list[str] = ["\\section{Education}"]
    for e in education:
        # Location piggy-backs on the school cell so we don't need a
        # second header row per entry. Falls back gracefully when
        # ``location`` is absent (older profiles, legacy callers).
        school_label = e.school
        if e.location.strip():
            school_label = (
                f"{e.school}, {e.location.strip()}" if e.school.strip() else e.location.strip()
            )
        parts.append(rf"\headingBf{{{latex_escape(school_label)}}}{{{latex_escape(e.dates)}}}")
        right = ""
        if e.gpa.strip():
            right = rf"\textbf{{GPA:}} \textbf{{{latex_escape(e.gpa.strip())}}}"
        elif e.details.strip() and not e.coursework.strip():
            right = latex_escape(e.details.strip())
        parts.append(rf"\headingIt{{{latex_escape(e.degree)}}}{{{right}}}")
        if e.coursework.strip():
            parts.append(rf"\headingIt{{Course Work: {latex_escape(e.coursework.strip())}}}{{}}")
        if e.details.strip() and e.gpa.strip() and not e.coursework.strip():
            # GPA already took the right cell; surface details on its own line.
            parts.append(rf"\headingIt{{{latex_escape(e.details.strip())}}}{{}}")
    return "\n".join(parts) + "\n"


def render_resume_markdown(tr: TailoredResume) -> str:
    env = Environment(
        loader=FileSystemLoader(str(_templates_dir())),
        autoescape=False,
    )
    tpl = env.get_template("resume.md.j2")
    contact_md = build_contact_markdown(tr.contact) if tr.contact.has_any() else ""
    return str(tpl.render(tr=tr, contact_md=contact_md))


def render_cover_markdown(cl: CoverLetter) -> str:
    env = Environment(
        loader=FileSystemLoader(str(_templates_dir())),
        autoescape=False,
    )
    tpl = env.get_template("cover_letter.md.j2")
    return str(tpl.render(cl=cl))


# ----------------------------- cover letter LaTeX -------------------------------- #

# Maximum height (in \height units) used by \raisebox before each glyph in the
# cover-letter contact column. Phones look right slightly higher than envelopes.
_COVER_LIFT = "-0.15"
_COVER_LIFT_PHONE = "-0.05"


def _cover_left_block(c: ContactInfo) -> str:
    """Two-line phone + email column, top-left of the cover-letter header."""
    lines: list[str] = []
    if c.phone:
        href, label = _normalize_phone(c.phone)
        lines.append(
            f"\\href{{{_url_escape(href)}}}{{"
            f"\\raisebox{{{_COVER_LIFT_PHONE}\\height}} {_ICON['phone']}\\ "
            f"{latex_escape(label)}}}"
        )
    if c.email:
        href, label = _normalize_email(c.email)
        lines.append(
            f"\\href{{{_url_escape(href)}}}{{"
            f"\\raisebox{{{_COVER_LIFT}\\height}} {_ICON['email']}\\ "
            f"{latex_escape(label)}}}"
        )
    return " \\\\\n".join(lines)


def _cover_right_block(c: ContactInfo) -> str:
    """Right-aligned LinkedIn / Portfolio / GitHub / Medium / Twitter links."""
    lines: list[str] = []
    if c.linkedin:
        href, _ = _normalize_via_regex(
            c.linkedin, _LINKEDIN_RE, "https://linkedin.com/in/", "linkedin"
        )
        lines.append(f"LinkedIn: \\href{{{_url_escape(href)}}}{{link}}")
    if c.portfolio:
        href, _ = _normalize_portfolio(c.portfolio)
        lines.append(f"Portfolio: \\href{{{_url_escape(href)}}}{{link}}")
    if c.github:
        href, _ = _normalize_via_regex(c.github, _GITHUB_RE, "https://github.com/", "github")
        lines.append(f"GitHub: \\href{{{_url_escape(href)}}}{{link}}")
    if c.medium:
        href, _ = _normalize_via_regex(
            c.medium, _MEDIUM_RE, "https://medium.com/@", "medium", keep_at_in_label=True
        )
        lines.append(f"Medium: \\href{{{_url_escape(href)}}}{{link}}")
    if c.twitter:
        href, _ = _normalize_via_regex(c.twitter, _TWITTER_RE, "https://twitter.com/", "twitter")
        lines.append(f"Twitter: \\href{{{_url_escape(href)}}}{{link}}")
    return " \\\\\n".join(lines)


def _cover_body_paragraphs(opening: str, body: str) -> str:
    """Escape and stitch the salutation + body paragraphs into LaTeX.

    Paragraphs are split on blank lines and joined with blank lines, so the
    template's ``\\setlength{\\parskip}{12pt}`` produces clean breaks.
    """
    chunks: list[str] = []
    if opening and opening.strip():
        chunks.append(latex_escape(opening.strip()))
    if body and body.strip():
        for para in re.split(r"\n\s*\n", body.strip()):
            para = para.strip()
            if not para:
                continue
            collapsed = re.sub(r"\s*\n\s*", " ", para)
            chunks.append(latex_escape(collapsed))
    return "\n\n".join(chunks)


def _cover_closing_block(closing: str, name: str) -> str:
    """Render the bottom signoff. Multi-line closings become ``\\\\``-joined."""
    text = (closing or "").strip()
    if not text:
        text = f"Sincerely,\n{name.strip()}" if name.strip() else "Sincerely,"
    lines = [latex_escape(line.strip()) for line in text.splitlines() if line.strip()]
    if not lines:
        return latex_escape("Sincerely,")
    return " \\\\ ".join(lines)


def fill_cover_letter_tex(
    cover: CoverLetter,
    *,
    contact: ContactInfo,
    name: str,
    role: str,
) -> str:
    """Render :class:`CoverLetter` into the entry-level cover-letter LaTeX shell.

    Uses ``contact`` from the tailored resume so phone / email / links match the
    resume header. ``name`` is shown large in the center; ``role`` is the small
    accent line beneath (typically the job title being applied to).
    """
    tex_path = _templates_dir() / "cover_letter.tex"
    template = tex_path.read_text(encoding="utf-8")

    safe_name = latex_escape(name.strip() or "Candidate")
    safe_role = latex_escape(role.strip())
    left = _cover_left_block(contact)
    right = _cover_right_block(contact)
    body = _cover_body_paragraphs(cover.opening, cover.body)
    closing = _cover_closing_block(cover.closing, name)

    out = template.replace("%%JOBAPPLY_NAME%%", safe_name)
    out = out.replace("%%JOBAPPLY_ROLE%%", safe_role)
    out = out.replace("%%JOBAPPLY_LEFT%%", left)
    out = out.replace("%%JOBAPPLY_RIGHT%%", right)
    out = out.replace("%%JOBAPPLY_BODY%%", body)
    out = out.replace("%%JOBAPPLY_CLOSING%%", closing)
    return out


def fill_resume_tex(tr: TailoredResume) -> str:
    """Render :class:`TailoredResume` into the MTeck-styled LaTeX shell.

    The Summary section is intentionally not rendered: it tends to read like
    boilerplate marketing and is already covered by the cover letter. The
    ``TailoredResume.summary`` field is still populated by the LLM and
    consumed by the cover-letter agent for context.
    """
    tex_path = _templates_dir() / "resume.tex"
    template = tex_path.read_text(encoding="utf-8")
    body = "\n".join(
        section
        for section in (
            _skills_latex(tr.skills),
            _experience_latex(tr.experience),
            _projects_latex(tr.projects),
            _education_latex(tr.education),
        )
        if section
    )
    title = latex_escape(tr.document_title or "Resume")
    if tr.contact.has_any():
        subtitle = build_contact_subtitle(tr.contact)
    else:
        subtitle = latex_escape(tr.contact_line or "")
    out = template.replace("%%JOBAPPLY_TITLE%%", title)
    out = out.replace("%%JOBAPPLY_SUBTITLE%%", subtitle)
    out = out.replace("%%JOBAPPLY_BODY%%", body)
    return out


# Minimal print stylesheet for the WeasyPrint markdown fallback. Mirrors a
# typical resume / cover-letter layout: 1in margins, serif body, tight headings.
_MD_PRINT_CSS = """
@page { size: Letter; margin: 1in; }
body {
  font-family: "Charter", "Source Serif Pro", "Georgia", serif;
  font-size: 10.5pt;
  line-height: 1.35;
  color: #111;
}
h1 { font-size: 22pt; margin: 0 0 4pt 0; color: #0e6e55; }
h2 { font-size: 13pt; margin: 12pt 0 4pt 0; color: #0e6e55;
     border-bottom: 1px solid #a16f0b; padding-bottom: 2pt; }
h3 { font-size: 11pt; margin: 8pt 0 2pt 0; }
p  { margin: 4pt 0; }
ul { margin: 2pt 0 6pt 0; padding-left: 1.1em; }
li { margin: 1pt 0; }
a  { color: inherit; text-decoration: none; }
hr { border: none; border-top: 1px solid #a16f0b; margin: 6pt 0; }
strong { color: #111; }
"""


_weasyprint_probe_cache: bool | None = None


def _weasyprint_available() -> bool:
    """Cache + silence WeasyPrint's import probe.

    WeasyPrint's native loader prints a multi-line banner to stderr when
    Pango/Cairo can't be loaded. Python re-runs failed imports on every
    attempt, so without caching the banner shows up once per markdown-to-PDF
    call (4× per job). We probe once with stdout/stderr suppressed and
    remember the result.
    """
    global _weasyprint_probe_cache
    if _weasyprint_probe_cache is not None:
        return _weasyprint_probe_cache

    import contextlib
    import io
    import logging

    # WeasyPrint emits its banner via the ``weasyprint`` logger AND via stderr.
    # Suppress both during the probe — failures still propagate as exceptions.
    weasy_logger = logging.getLogger("weasyprint")
    prior_level = weasy_logger.level
    prior_propagate = weasy_logger.propagate
    weasy_logger.setLevel(logging.CRITICAL + 1)
    weasy_logger.propagate = False
    try:
        with (
            contextlib.redirect_stderr(io.StringIO()),
            contextlib.redirect_stdout(io.StringIO()),
        ):
            from weasyprint import HTML

            HTML(string="<p>probe</p>")  # forces native lib resolution
        _weasyprint_probe_cache = True
    except Exception:
        _weasyprint_probe_cache = False
    finally:
        weasy_logger.setLevel(prior_level)
        weasy_logger.propagate = prior_propagate
    return _weasyprint_probe_cache


def _md_to_pdf_pandoc(md_path: Path, pdf_path: Path) -> bool:
    """Try pandoc first — best layout when LaTeX is also installed."""
    try:
        import pypandoc

        pypandoc.convert_file(
            str(md_path),
            "pdf",
            outputfile=str(pdf_path),
            extra_args=["--standalone", "-V", "geometry:margin=1in"],
        )
        return pdf_path.is_file()
    except Exception:
        return False


def _md_to_pdf_weasyprint(md_path: Path, pdf_path: Path) -> bool:
    """Pure-Python fallback: markdown -> HTML -> PDF via WeasyPrint.

    Avoids the pandoc / LaTeX dependency. Imports lazily and short-circuits
    once we know the underlying Pango/Cairo libraries can't be loaded — this
    keeps WeasyPrint's huge "could not import some external libraries"
    banner from spamming the run output every time the tool falls back to
    fpdf2.
    """
    if not _weasyprint_available():
        return False

    try:
        import markdown as md_lib
        from weasyprint import CSS, HTML
    except Exception:
        return False

    try:
        text = md_path.read_text(encoding="utf-8")
        html_body = md_lib.markdown(
            text,
            extensions=["extra", "sane_lists", "smarty"],
            output_format="html5",
        )
        html_doc = (
            "<!doctype html><html><head><meta charset='utf-8'>"
            f"<title>{md_path.stem}</title></head>"
            f"<body>{html_body}</body></html>"
        )
        HTML(string=html_doc, base_url=str(md_path.parent)).write_pdf(
            str(pdf_path),
            stylesheets=[CSS(string=_MD_PRINT_CSS)],
        )
        return pdf_path.is_file()
    except Exception:
        return False


def _md_to_pdf_fpdf2(md_path: Path, pdf_path: Path) -> bool:
    """Last-resort fallback: markdown -> HTML -> PDF via ``fpdf2``.

    ``fpdf2`` is pure Python with no native deps, so this path always works
    when WeasyPrint can't load Pango/Cairo and pandoc isn't installed. The
    output is plainer than WeasyPrint's but always renders.
    """
    try:
        import markdown as md_lib
        from fpdf import FPDF
    except Exception:
        return False

    try:
        text = md_path.read_text(encoding="utf-8")
        html = md_lib.markdown(
            text,
            extensions=["extra", "sane_lists"],
            output_format="html5",
        )
        pdf = FPDF(format="Letter", unit="pt")
        pdf.set_margins(left=54, top=54, right=54)
        pdf.set_auto_page_break(auto=True, margin=54)
        pdf.add_page()
        pdf.set_font("Helvetica", size=11)
        pdf.write_html(html)
        pdf.output(str(pdf_path))
        return pdf_path.is_file()
    except Exception:
        return False


def md_to_pdf(md_path: Path, pdf_path: Path) -> bool:
    """Convert a markdown file to PDF, trying multiple backends in order.

    1. ``pypandoc`` — best layout when pandoc + a LaTeX engine are installed.
    2. WeasyPrint — high-quality HTML rendering; needs Pango/Cairo (system).
    3. ``fpdf2`` — pure-Python last resort; always works.

    Returns ``True`` on the first success, ``False`` if every backend fails.
    """
    if _md_to_pdf_pandoc(md_path, pdf_path):
        return True
    if _md_to_pdf_weasyprint(md_path, pdf_path):
        return True
    return _md_to_pdf_fpdf2(md_path, pdf_path)


def probe_md_pdf_backend() -> str:
    """Return the name of the first available markdown-to-PDF backend.

    Used by the CLI to print a one-line hint at run start so users know
    whether output will be high-quality (pandoc / weasyprint) or basic
    (fpdf2). Returns ``""`` if nothing is available (extremely unlikely
    since fpdf2 has no native deps).
    """
    try:
        import pypandoc

        pypandoc.get_pandoc_version()
        return "pandoc"
    except Exception:
        pass
    if _weasyprint_available():
        return "weasyprint"
    try:
        from fpdf import FPDF

        FPDF()
        return "fpdf2"
    except Exception:
        return ""


# --- LaTeX → PDF ----------------------------------------------------------- #
#
# Strategy (in order):
#   1. Remote latex-on-http API — OPT-IN (it uploads resume content).
#      Configurable via env vars so deployments and CI pick up overrides
#      without touching the rendering layer:
#        JOBAPPLY_LATEX_API_URL       (defaults to the public ytotech.com instance)
#        JOBAPPLY_LATEX_API_DISABLE   (remote step runs only when this is
#                                      explicitly "0"/"false"/"no"/"off";
#                                      unset means disabled)
#        JOBAPPLY_LATEX_API_COMPILER  (pdflatex | xelatex | lualatex | latexmk)
#        JOBAPPLY_LATEX_API_TIMEOUT   (seconds)
#   2. Local `tectonic`  — auto-fetches missing packages, single binary.
#   3. Local `pdflatex`  — last-resort fallback for full TeX Live installs.
#
# `jobapply.config.apply_latex_api_env` bridges TOML config → env so the CLI
# stays the single source of truth without coupling render.py to config.py.

DEFAULT_LATEX_API_URL = "https://latex.ytotech.com/builds/sync"
DEFAULT_LATEX_API_TIMEOUT = 120.0
_LATEX_ENABLED_VALUES = {"0", "false", "no", "off"}


def _latex_api_disabled() -> bool:
    # Fail closed: the remote API receives resume content, so it only runs
    # when explicitly enabled (``apply_latex_api_env`` writes "0" when the
    # user opted in via ``[latex_api] enabled = true``).
    raw = os.environ.get("JOBAPPLY_LATEX_API_DISABLE", "").strip().lower()
    return raw not in _LATEX_ENABLED_VALUES


def _latex_api_url() -> str:
    return os.environ.get("JOBAPPLY_LATEX_API_URL", "").strip() or DEFAULT_LATEX_API_URL


def _latex_api_compiler() -> str:
    value = os.environ.get("JOBAPPLY_LATEX_API_COMPILER", "").strip()
    return value or "pdflatex"


def _latex_api_timeout() -> float:
    raw = os.environ.get("JOBAPPLY_LATEX_API_TIMEOUT", "").strip()
    if not raw:
        return DEFAULT_LATEX_API_TIMEOUT
    try:
        return float(raw)
    except ValueError:
        return DEFAULT_LATEX_API_TIMEOUT


def _tex_to_pdf_remote(tex_path: Path, out_dir: Path) -> Path | None:
    """POST the .tex content to a latex-on-http endpoint and write the PDF.

    Returns the output path on success, ``None`` on any failure (network
    error, non-201 response, write failure). Failures are silent so the
    caller can transparently fall back to local engines.
    """
    try:
        import httpx  # lazy; httpx is already a project dependency
    except Exception:
        return None

    try:
        latex = tex_path.read_text(encoding="utf-8")
    except OSError:
        return None

    payload = {
        "compiler": _latex_api_compiler(),
        "resources": [{"main": True, "content": latex}],
    }
    try:
        resp = httpx.post(_latex_api_url(), json=payload, timeout=_latex_api_timeout())
    except Exception:
        return None

    # latex-on-http returns 201 + application/pdf on success; failures come back
    # as 4xx/5xx with a JSON build log. We only treat a real PDF as success.
    if resp.status_code != 201:
        return None
    body = resp.content
    if not body.startswith(b"%PDF-"):
        return None

    out_dir.mkdir(parents=True, exist_ok=True)
    pdf_path = out_dir / (tex_path.stem + ".pdf")
    try:
        pdf_path.write_bytes(body)
    except OSError:
        return None
    return pdf_path


def _tex_to_pdf_tectonic(tex_path: Path, out_dir: Path) -> Path | None:
    tectonic = shutil.which("tectonic")
    if not tectonic:
        return None
    try:
        proc = subprocess.run(
            [tectonic, "--outdir", str(out_dir), str(tex_path)],
            capture_output=True,
            text=True,
            timeout=120,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if proc.returncode != 0:
        return None
    return out_dir / (tex_path.stem + ".pdf")


def _tex_to_pdf_pdflatex(tex_path: Path, out_dir: Path) -> Path | None:
    pdflatex = shutil.which("pdflatex")
    if not pdflatex:
        return None
    # The .tex may have been edited in the web UI, so treat it as untrusted:
    # no shell escape, and kpathsea "paranoid" mode so \input{/abs/path}
    # can't embed arbitrary local files (e.g. ~/.ssh keys) into the PDF.
    # TEXMFOUTPUT whitelists the job dir, which holds the .tex itself.
    env = {
        **os.environ,
        "openin_any": "p",
        "openout_any": "p",
        "TEXMFOUTPUT": str(out_dir.resolve()),
    }
    try:
        proc = subprocess.run(
            [
                pdflatex,
                "-no-shell-escape",
                "-interaction=nonstopmode",
                f"-output-directory={out_dir}",
                str(tex_path),
            ],
            capture_output=True,
            text=True,
            timeout=120,
            env=env,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if proc.returncode != 0:
        return None
    return out_dir / (tex_path.stem + ".pdf")


def tex_to_pdf(tex_path: Path, out_dir: Path) -> Path | None:
    """Compile a ``.tex`` file to PDF via remote API, then local engines.

    Order: remote latex-on-http → ``tectonic`` → ``pdflatex``. Returns the
    path to the produced PDF on first success, or ``None`` if every backend
    failed. See module docstring for env-var configuration.
    """
    if not _latex_api_disabled():
        out = _tex_to_pdf_remote(tex_path, out_dir)
        if out and out.is_file():
            return out
    out = _tex_to_pdf_tectonic(tex_path, out_dir)
    if out and out.is_file():
        return out
    out = _tex_to_pdf_pdflatex(tex_path, out_dir)
    if out and out.is_file():
        return out
    return None


def probe_tex_pdf_backend() -> str:
    """Return the name of the first backend ``tex_to_pdf`` will attempt.

    Used by the CLI for a one-line startup banner. Mirrors the shape of
    :func:`probe_md_pdf_backend`; values: ``"latex-on-http"``,
    ``"tectonic"``, ``"pdflatex"``, or ``""`` when nothing is configured.
    """
    if not _latex_api_disabled():
        return "latex-on-http"
    if shutil.which("tectonic"):
        return "tectonic"
    if shutil.which("pdflatex"):
        return "pdflatex"
    return ""


def slug_from_paths(job_slug: str, run_dir: Path) -> Path:
    p = run_dir / "jobs" / job_slug
    p.mkdir(parents=True, exist_ok=True)
    return p
