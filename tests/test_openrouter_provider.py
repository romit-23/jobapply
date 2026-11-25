"""Tests for the OpenRouter provider integration.

OpenRouter is an OpenAI-compatible router (one API key, many vendors), so
the goal here is to verify the wiring around it: it shows up as a known
provider, the env-var fallbacks resolve in the right order, the base URL
defaults correctly, and ``create_chat_model`` builds a ``ChatOpenAI``
pointed at ``openrouter.ai/api/v1`` with the right credentials.
"""

from __future__ import annotations

import pytest

from jobapply.config import (
    DEFAULT_BASE_URLS,
    DEFAULT_MODELS,
    PROVIDER_NAMES,
    AppConfig,
    ProviderConfig,
    get_api_key,
    get_base_url,
)
from jobapply.config_writer import render_config_toml
from jobapply.llm import create_chat_model

# ---------------------------------------------------------------------------
# Config layer
# ---------------------------------------------------------------------------


def test_openrouter_is_a_known_provider_with_default_model() -> None:
    assert "openrouter" in PROVIDER_NAMES
    # Ids on OpenRouter are always `vendor/model`.
    assert "/" in DEFAULT_MODELS["openrouter"]


def test_openrouter_default_base_url_points_at_router() -> None:
    assert DEFAULT_BASE_URLS["openrouter"] == "https://openrouter.ai/api/v1"


def test_get_api_key_reads_openrouter_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    monkeypatch.setenv("OPENROUTER_API_KEY", "key-from-env")
    assert get_api_key(AppConfig(), "openrouter") == "key-from-env"


def test_get_api_key_prefers_openrouter_config_over_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OPENROUTER_API_KEY", "from-env")
    cfg = AppConfig(providers={"openrouter": ProviderConfig(api_key="from-toml")})
    assert get_api_key(cfg, "openrouter") == "from-toml"


def test_get_api_key_supports_env_indirection_for_openrouter(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("MY_OR_KEY", "indirect-value")
    cfg = AppConfig(providers={"openrouter": ProviderConfig(api_key="env:MY_OR_KEY")})
    assert get_api_key(cfg, "openrouter") == "indirect-value"


def test_get_base_url_returns_openrouter_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("OPENROUTER_BASE_URL", raising=False)
    assert get_base_url(AppConfig(), "openrouter") == DEFAULT_BASE_URLS["openrouter"]


def test_get_base_url_honors_openrouter_env_override(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENROUTER_BASE_URL", "https://my-proxy.example.com/v1")
    assert get_base_url(AppConfig(), "openrouter") == "https://my-proxy.example.com/v1"


def test_get_base_url_honors_openrouter_config_override() -> None:
    cfg = AppConfig(providers={"openrouter": ProviderConfig(base_url="https://custom.test/v1")})
    assert get_base_url(cfg, "openrouter") == "https://custom.test/v1"


# ---------------------------------------------------------------------------
# config_writer
# ---------------------------------------------------------------------------


def test_render_config_toml_emits_openrouter_block() -> None:
    cfg = AppConfig(
        providers={
            "openrouter": ProviderConfig(
                api_key="sk-or-test",
                model="openai/gpt-4o-mini",
            )
        }
    )
    text = render_config_toml(cfg)
    assert "[providers.openrouter]" in text
    assert 'api_key = "sk-or-test"' in text
    assert 'model = "openai/gpt-4o-mini"' in text


def test_render_config_toml_openrouter_placeholder_when_unset() -> None:
    text = render_config_toml(AppConfig())
    assert "[providers.openrouter]" in text
    assert '# api_key = "REPLACE_ME"' in text


# ---------------------------------------------------------------------------
# llm.create_chat_model
# ---------------------------------------------------------------------------


def test_create_chat_model_openrouter_builds_chatopenai(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """OpenRouter is OpenAI-compatible, so we re-use ChatOpenAI but point
    its base_url at the router."""
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)

    cfg = AppConfig(
        providers={"openrouter": ProviderConfig(api_key="sk-or-test")},
    )
    llm = create_chat_model("openrouter", "openai/gpt-4o-mini", cfg)

    base_url = getattr(llm, "openai_api_base", None) or getattr(llm, "base_url", None)
    assert base_url == "https://openrouter.ai/api/v1"
    assert getattr(llm, "model_name", None) == "openai/gpt-4o-mini"


def test_create_chat_model_openrouter_requires_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    with pytest.raises(RuntimeError, match="OpenRouter API key"):
        create_chat_model("openrouter", "openai/gpt-4o-mini", AppConfig())


def test_create_chat_model_openrouter_passes_user_max_tokens(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Users can cap completion tokens through `[providers.openrouter].max_tokens`."""
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    cfg = AppConfig(
        providers={
            "openrouter": ProviderConfig(api_key="sk-or-test", max_tokens=2048),
        }
    )
    llm = create_chat_model("openrouter", "openai/gpt-4o-mini", cfg)
    assert getattr(llm, "max_tokens", None) == 2048
