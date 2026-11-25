"""Tests for ``LatexApiConfig`` and the env-var bridge consumed by render.py."""

from __future__ import annotations

import pytest

from jobapply.config import (
    DEFAULT_LATEX_API_TIMEOUT,
    DEFAULT_LATEX_API_URL,
    AppConfig,
    LatexApiConfig,
    apply_latex_api_env,
)


def test_latex_api_defaults() -> None:
    cfg = AppConfig()
    # Opt-in: the remote API receives resume content.
    assert cfg.latex_api.enabled is False
    assert cfg.latex_api.url == DEFAULT_LATEX_API_URL
    assert cfg.latex_api.compiler == "pdflatex"
    assert cfg.latex_api.timeout == DEFAULT_LATEX_API_TIMEOUT


def test_apply_latex_api_env_writes_env_vars(monkeypatch: pytest.MonkeyPatch) -> None:
    for k in (
        "JOBAPPLY_LATEX_API_URL",
        "JOBAPPLY_LATEX_API_DISABLE",
        "JOBAPPLY_LATEX_API_COMPILER",
        "JOBAPPLY_LATEX_API_TIMEOUT",
    ):
        monkeypatch.delenv(k, raising=False)

    cfg = AppConfig(
        latex_api=LatexApiConfig(
            enabled=False,
            url="http://localhost:8080/builds/sync",
            compiler="xelatex",
            timeout=45.0,
        )
    )
    apply_latex_api_env(cfg)

    import os

    assert os.environ["JOBAPPLY_LATEX_API_URL"] == "http://localhost:8080/builds/sync"
    assert os.environ["JOBAPPLY_LATEX_API_DISABLE"] == "1"
    assert os.environ["JOBAPPLY_LATEX_API_COMPILER"] == "xelatex"
    assert os.environ["JOBAPPLY_LATEX_API_TIMEOUT"] == "45.0"


def test_apply_latex_api_env_does_not_clobber_existing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Pre-existing env vars take precedence over TOML so users can override
    from the shell without editing the file.
    """
    monkeypatch.setenv("JOBAPPLY_LATEX_API_URL", "https://override.test/build")
    monkeypatch.delenv("JOBAPPLY_LATEX_API_DISABLE", raising=False)

    cfg = AppConfig(latex_api=LatexApiConfig(enabled=True, url="http://from-toml/build"))
    apply_latex_api_env(cfg)

    import os

    assert os.environ["JOBAPPLY_LATEX_API_URL"] == "https://override.test/build"
    assert os.environ["JOBAPPLY_LATEX_API_DISABLE"] == "0"
