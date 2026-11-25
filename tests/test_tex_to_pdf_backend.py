"""Tests for the LaTeX → PDF backend chain in ``jobapply.nodes.render``.

The chain order is:

1. Remote ``latex-on-http`` HTTP API (opt-in).
2. Local ``tectonic``.
3. Local ``pdflatex``.

The remote step only runs when ``JOBAPPLY_LATEX_API_DISABLE`` is explicitly
falsy ("0"); unset means disabled. These tests cover each branch by monkeypatching the inner
``_tex_to_pdf_*`` helpers; an additional test exercises the real HTTP code
path against a fake ``httpx.post`` so we know the request payload, response
parsing, and PDF write all work end-to-end without the network.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from jobapply.nodes import render


@pytest.fixture()
def tex_file(tmp_path: Path) -> Path:
    p = tmp_path / "resume.tex"
    p.write_text(r"\documentclass{article}\begin{document}hi\end{document}", encoding="utf-8")
    return p


def _enable_remote(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("JOBAPPLY_LATEX_API_DISABLE", "0")


def test_remote_api_is_off_when_env_unset(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, tex_file: Path
) -> None:
    """Fail closed: without explicit opt-in, resume content is never uploaded."""
    monkeypatch.delenv("JOBAPPLY_LATEX_API_DISABLE", raising=False)

    def boom(*_a: Any, **_k: Any) -> None:
        raise AssertionError("remote LaTeX API must not be called by default")

    monkeypatch.setattr(render, "_tex_to_pdf_remote", boom)
    monkeypatch.setattr(render, "_tex_to_pdf_tectonic", lambda *_a: None)
    monkeypatch.setattr(render, "_tex_to_pdf_pdflatex", lambda *_a: None)
    assert render.tex_to_pdf(tex_file, tmp_path) is None
    assert render.probe_tex_pdf_backend() != "latex-on-http"


def _disable_remote(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("JOBAPPLY_LATEX_API_DISABLE", "1")


def test_tex_to_pdf_uses_remote_first(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, tex_file: Path
) -> None:
    _enable_remote(monkeypatch)
    calls = {"remote": 0, "tectonic": 0, "pdflatex": 0}

    def fake_remote(_t: Path, out_dir: Path) -> Path | None:
        calls["remote"] += 1
        p = out_dir / "resume.pdf"
        p.write_bytes(b"%PDF-1.7 fake\n")
        return p

    monkeypatch.setattr(render, "_tex_to_pdf_remote", fake_remote)
    monkeypatch.setattr(
        render, "_tex_to_pdf_tectonic", lambda *_a, **_k: (_ for _ in ()).throw(AssertionError())
    )
    monkeypatch.setattr(
        render, "_tex_to_pdf_pdflatex", lambda *_a, **_k: (_ for _ in ()).throw(AssertionError())
    )

    out = render.tex_to_pdf(tex_file, tmp_path)

    assert out is not None and out.is_file()
    assert calls == {"remote": 1, "tectonic": 0, "pdflatex": 0}


def test_tex_to_pdf_falls_back_to_tectonic_when_remote_fails(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, tex_file: Path
) -> None:
    _enable_remote(monkeypatch)
    calls = {"remote": 0, "tectonic": 0, "pdflatex": 0}

    def fake_remote(*_a: Any, **_k: Any) -> Path | None:
        calls["remote"] += 1
        return None

    def fake_tectonic(_t: Path, out_dir: Path) -> Path | None:
        calls["tectonic"] += 1
        p = out_dir / "resume.pdf"
        p.write_bytes(b"%PDF-1.7 tectonic\n")
        return p

    monkeypatch.setattr(render, "_tex_to_pdf_remote", fake_remote)
    monkeypatch.setattr(render, "_tex_to_pdf_tectonic", fake_tectonic)
    monkeypatch.setattr(
        render, "_tex_to_pdf_pdflatex", lambda *_a, **_k: (_ for _ in ()).throw(AssertionError())
    )

    out = render.tex_to_pdf(tex_file, tmp_path)

    assert out is not None and out.is_file()
    assert calls == {"remote": 1, "tectonic": 1, "pdflatex": 0}


def test_tex_to_pdf_skips_remote_when_disabled(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, tex_file: Path
) -> None:
    _disable_remote(monkeypatch)
    calls = {"remote": 0, "pdflatex": 0}

    monkeypatch.setattr(
        render, "_tex_to_pdf_remote", lambda *_a, **_k: (_ for _ in ()).throw(AssertionError())
    )
    monkeypatch.setattr(render, "_tex_to_pdf_tectonic", lambda *_a, **_k: None)

    def fake_pdflatex(_t: Path, out_dir: Path) -> Path | None:
        calls["pdflatex"] += 1
        p = out_dir / "resume.pdf"
        p.write_bytes(b"%PDF-1.4 pdflatex\n")
        return p

    monkeypatch.setattr(render, "_tex_to_pdf_pdflatex", fake_pdflatex)

    out = render.tex_to_pdf(tex_file, tmp_path)

    assert out is not None and out.is_file()
    assert calls == {"remote": 0, "pdflatex": 1}


def test_tex_to_pdf_returns_none_when_everything_fails(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, tex_file: Path
) -> None:
    _enable_remote(monkeypatch)
    monkeypatch.setattr(render, "_tex_to_pdf_remote", lambda *_a, **_k: None)
    monkeypatch.setattr(render, "_tex_to_pdf_tectonic", lambda *_a, **_k: None)
    monkeypatch.setattr(render, "_tex_to_pdf_pdflatex", lambda *_a, **_k: None)

    assert render.tex_to_pdf(tex_file, tmp_path) is None


def test_probe_tex_pdf_backend_picks_remote_by_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _enable_remote(monkeypatch)
    assert render.probe_tex_pdf_backend() == "latex-on-http"


def test_probe_tex_pdf_backend_falls_through_when_disabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _disable_remote(monkeypatch)
    monkeypatch.setattr(render.shutil, "which", lambda name: None)
    assert render.probe_tex_pdf_backend() == ""


def test_probe_tex_pdf_backend_picks_tectonic_when_disabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _disable_remote(monkeypatch)
    monkeypatch.setattr(
        render.shutil, "which", lambda name: "/usr/bin/tectonic" if name == "tectonic" else None
    )
    assert render.probe_tex_pdf_backend() == "tectonic"


# --- end-to-end remote helper test, no network -------------------------------


class _FakeResponse:
    def __init__(self, status_code: int, content: bytes) -> None:
        self.status_code = status_code
        self.content = content


def test_remote_helper_posts_payload_and_writes_pdf(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, tex_file: Path
) -> None:
    """``_tex_to_pdf_remote`` must send the .tex content + selected compiler
    and write the body when the API returns 201 + a real %PDF- header.
    """
    monkeypatch.setenv("JOBAPPLY_LATEX_API_URL", "https://example.test/builds/sync")
    monkeypatch.setenv("JOBAPPLY_LATEX_API_COMPILER", "xelatex")
    monkeypatch.setenv("JOBAPPLY_LATEX_API_TIMEOUT", "30")

    captured: dict[str, Any] = {}

    def fake_post(url: str, *, json: dict[str, Any], timeout: float) -> _FakeResponse:
        captured["url"] = url
        captured["json"] = json
        captured["timeout"] = timeout
        return _FakeResponse(201, b"%PDF-1.7 fake-bytes\n")

    import httpx

    monkeypatch.setattr(httpx, "post", fake_post)

    out = render._tex_to_pdf_remote(tex_file, tmp_path)

    assert out is not None and out.is_file()
    assert out.read_bytes().startswith(b"%PDF-")
    assert captured["url"] == "https://example.test/builds/sync"
    assert captured["timeout"] == 30.0
    assert captured["json"]["compiler"] == "xelatex"
    assert captured["json"]["resources"][0]["main"] is True
    assert "documentclass" in captured["json"]["resources"][0]["content"]


def test_remote_helper_returns_none_on_non_201(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, tex_file: Path
) -> None:
    import httpx

    monkeypatch.setattr(
        httpx, "post", lambda *_a, **_k: _FakeResponse(400, b'{"error":"bad latex"}')
    )
    assert render._tex_to_pdf_remote(tex_file, tmp_path) is None


def test_remote_helper_returns_none_on_non_pdf_body(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, tex_file: Path
) -> None:
    """A 201 with a non-PDF body (e.g. an HTML proxy error page) must be
    rejected so we don't write garbage and treat it as success.
    """
    import httpx

    monkeypatch.setattr(httpx, "post", lambda *_a, **_k: _FakeResponse(201, b"<html>oops</html>"))
    assert render._tex_to_pdf_remote(tex_file, tmp_path) is None


def test_remote_helper_returns_none_on_network_error(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, tex_file: Path
) -> None:
    import httpx

    def boom(*_a: Any, **_k: Any) -> Any:
        raise httpx.ConnectError("no route")

    monkeypatch.setattr(httpx, "post", boom)
    assert render._tex_to_pdf_remote(tex_file, tmp_path) is None


def test_pdflatex_runs_hardened(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """User-editable .tex must not get shell escape or arbitrary file reads."""
    import subprocess

    tex = tmp_path / "resume.tex"
    tex.write_text("x", encoding="utf-8")
    seen: dict[str, Any] = {}

    def fake_run(cmd: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        seen["cmd"], seen["env"] = cmd, kwargs["env"]
        (tmp_path / "resume.pdf").write_bytes(b"%PDF")
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr(render.shutil, "which", lambda _n: "/usr/bin/pdflatex")
    monkeypatch.setattr(render.subprocess, "run", fake_run)
    assert render._tex_to_pdf_pdflatex(tex, tmp_path) == tmp_path / "resume.pdf"
    assert "-no-shell-escape" in seen["cmd"]
    assert seen["env"]["openin_any"] == "p"
    assert seen["env"]["openout_any"] == "p"
    assert seen["env"]["TEXMFOUTPUT"] == str(tmp_path.resolve())


@pytest.mark.skipif(__import__("shutil").which("pdflatex") is None, reason="pdflatex not installed")
def test_pdflatex_cannot_embed_files_outside_job_dir(tmp_path: Path) -> None:
    """Real-engine check: \\input of an absolute path outside the job dir fails."""
    secret = tmp_path / "secret.txt"
    secret.write_text("SECRETTOKEN123", encoding="utf-8")
    job = tmp_path / "job"
    job.mkdir()
    tex = job / "evil.tex"
    tex.write_text(
        "\\documentclass{article}\\begin{document}" f"\\input{{{secret}}}" "\\end{document}\n",
        encoding="utf-8",
    )
    assert render._tex_to_pdf_pdflatex(tex, job) is None

    ok = job / "ok.tex"
    ok.write_text(
        "\\documentclass{article}\\begin{document}hello\\end{document}\n", encoding="utf-8"
    )
    assert render._tex_to_pdf_pdflatex(ok, job) == job / "ok.pdf"
