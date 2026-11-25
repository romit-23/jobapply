"""Render `jobapply.toml` from an :class:`AppConfig` with helpful comments.

We hand-roll a tiny TOML emitter so the file stays human-friendly (sections,
comments, default placeholders) instead of using a one-shot dumper that drops
context.  The output is a strict subset of TOML that ``tomllib`` can re-read.
"""

from __future__ import annotations

from typing import Any

from jobapply.config import (
    DEFAULT_BASE_URLS,
    DEFAULT_LATEX_API_TIMEOUT,
    DEFAULT_LATEX_API_URL,
    DEFAULT_MODELS,
    PROVIDER_NAMES,
    AppConfig,
    LatexApiConfig,
    ProviderConfig,
)


def _toml_str(s: str) -> str:
    return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _toml_bool(b: bool) -> str:
    return "true" if b else "false"


def _toml_value(v: Any) -> str:
    if isinstance(v, bool):
        return _toml_bool(v)
    if isinstance(v, (int, float)):
        return str(v)
    if isinstance(v, list):
        return "[" + ", ".join(_toml_value(x) for x in v) + "]"
    return _toml_str(str(v))


def _kv(key: str, value: Any) -> str:
    return f"{key} = {_toml_value(value)}\n"


def _provider_block(name: str, pc: ProviderConfig) -> str:
    lines: list[str] = [f"\n[providers.{name}]\n"]
    default_model = DEFAULT_MODELS.get(name, "")
    default_base = DEFAULT_BASE_URLS.get(name)

    if name == "ollama":
        lines.append("# Ollama is local; api_key usually empty. Adjust base_url if remote.\n")
    elif name == "openrouter":
        lines.append(
            "# OpenRouter is an OpenAI-compatible router that exposes hundreds\n"
            "# of models across vendors via `vendor/model` ids (e.g.\n"
            "# `openai/gpt-4o-mini`, `anthropic/claude-3-5-sonnet`,\n"
            "# `meta-llama/llama-3.3-70b-instruct`). Create an API key at\n"
            "# https://openrouter.ai/keys. Browse models / pricing at\n"
            "# https://openrouter.ai/models. The `api_key` field accepts\n"
            '# the "env:VAR_NAME" indirection.\n',
        )
    elif name == "cloudflare":
        lines.append(
            "# Workers AI uses an OpenAI-compatible /v1 endpoint per account.\n"
            "# Create an API token with the 'Workers AI' permission at\n"
            "# https://dash.cloudflare.com/profile/api-tokens. Find your\n"
            "# account_id on the Workers & Pages overview page. All fields\n"
            '# accept the "env:VAR_NAME" indirection.\n'
            "#\n"
            "# Set `gateway_id` to route requests through Cloudflare AI\n"
            "# Gateway's Unified API instead of hitting Workers AI directly.\n"
            "# That's required for BYOK third-party models such as\n"
            "# `openai/gpt-5` or `anthropic/claude-...`. Leave it unset to\n"
            "# call native `@cf/...` models on Workers AI directly.\n",
        )
    else:
        lines.append(
            '# Tip: use api_key = "env:VAR_NAME" to pull the value from an env var\n'
            "# at runtime instead of storing the secret in this file.\n",
        )

    if pc.api_key is not None:
        lines.append(_kv("api_key", pc.api_key))
    else:
        placeholder = "" if name == "ollama" else "REPLACE_ME"
        lines.append(f'# api_key = "{placeholder}"\n')

    if name == "cloudflare":
        if pc.account_id is not None:
            lines.append(_kv("account_id", pc.account_id))
        else:
            lines.append('# account_id = "REPLACE_ME"\n')
        if pc.gateway_id is not None:
            lines.append(_kv("gateway_id", pc.gateway_id))
        else:
            lines.append('# gateway_id = "my-gateway"  # optional, enables BYOK models\n')

    if pc.base_url is not None:
        lines.append(_kv("base_url", pc.base_url))
    elif default_base:
        lines.append(f'# base_url = "{default_base}"\n')

    model = pc.model or default_model
    if model:
        lines.append(_kv("model", model))

    if pc.max_tokens is not None:
        lines.append(_kv("max_tokens", pc.max_tokens))
    elif name == "cloudflare":
        # Highlight the knob for cloudflare specifically because Workers
        # AI defaults to a tiny 256-token cap; the bundled fallback
        # (4096) keeps the structured-output agents working out of the
        # box but users hitting truncation can bump it from here.
        lines.append("# max_tokens = 4096  # bump if structured outputs get truncated\n")
    return "".join(lines)


def render_config_toml(cfg: AppConfig) -> str:
    """Serialize an :class:`AppConfig` back to a commented TOML document."""
    out: list[str] = [
        "# jobapply configuration. Provider keys may live here or in env vars.\n",
        "# Add `jobapply.toml` to .gitignore if you store secrets in this file.\n\n",
        _kv("provider", cfg.provider),
    ]
    if cfg.model:
        out.append(_kv("model", cfg.model))
    out.append(_kv("min_fit", cfg.min_fit))
    out.append(_kv("results_wanted", cfg.results_wanted))
    out.append(_kv("hours_old", cfg.hours_old))
    out.append(_kv("concurrency", cfg.concurrency))
    out.append(_kv("sites", cfg.sites))
    out.append(_kv("profile_path", cfg.profile_path))
    out.append(_kv("output_dir", cfg.output_dir))
    if cfg.ledger_path:
        out.append(_kv("ledger_path", cfg.ledger_path))

    for name in PROVIDER_NAMES:
        pc = cfg.providers.get(name, ProviderConfig())
        out.append(_provider_block(name, pc))
    out.append(_latex_api_block(cfg.latex_api))
    return "".join(out)


def _latex_api_block(la: LatexApiConfig) -> str:
    """Render the [latex_api] section.

    Only emit the block if it diverges from defaults; otherwise leave it
    commented so the file stays minimal for users who don't care.
    """
    is_default = (
        not la.enabled
        and la.url == DEFAULT_LATEX_API_URL
        and la.compiler == "pdflatex"
        and la.timeout == DEFAULT_LATEX_API_TIMEOUT
    )
    header = (
        "\n# LaTeX → PDF compile service (opt-in). When enabled, jobapply POSTs\n"
        "# your tailored .tex — i.e. your resume content — to a latex-on-http\n"
        "# instance. Off by default: local `tectonic`/`pdflatex` are used, and\n"
        "# Markdown PDFs are always produced. Self-host to keep data local:\n"
        "#   docker run -d -p 8080:8080 yotools/latex-on-http\n"
    )
    if is_default:
        return (
            header
            + "# [latex_api]\n"
            + "# enabled = false\n"
            + f'# url = "{DEFAULT_LATEX_API_URL}"\n'
            + '# compiler = "pdflatex"\n'
            + f"# timeout = {DEFAULT_LATEX_API_TIMEOUT}\n"
        )
    lines = [header, "[latex_api]\n"]
    lines.append(_kv("enabled", la.enabled))
    lines.append(_kv("url", la.url))
    lines.append(_kv("compiler", la.compiler))
    lines.append(_kv("timeout", la.timeout))
    return "".join(lines)
