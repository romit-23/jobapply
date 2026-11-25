"""Typer CLI: init, config, run, search, tailor, resume, list."""

from __future__ import annotations

import contextlib
import json
import sys
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import questionary
import typer
from rich.console import Console
from rich.progress import (
    BarColumn,
    Progress,
    SpinnerColumn,
    TextColumn,
    TimeElapsedColumn,
)
from rich.table import Table

from jobapply import __version__
from jobapply.agents.fit_scorer import score_fit
from jobapply.agents.search import iter_search_jobs
from jobapply.config import (
    DEFAULT_BASE_URLS,
    DEFAULT_MODELS,
    PROVIDER_NAMES,
    AppConfig,
    ProviderConfig,
    apply_latex_api_env,
    find_config_path,
    get_account_id,
    get_api_key,
    get_base_url,
    load_config,
    load_dotenv_if_present,
)
from jobapply.config_writer import render_config_toml
from jobapply.graph_nodes import bootstrap_resume_state
from jobapply.jd_extract import hints_to_email_context
from jobapply.llm import create_chat_model
from jobapply.models import (
    FitScore,
    JobRecord,
    JobSearchInput,
    JobsIndex,
    LedgerStatus,
    RawJob,
)
from jobapply.nodes.persist import write_job_json, write_jobs_csv, write_jobs_csv_from_path
from jobapply.nodes.render import (
    probe_md_pdf_backend,
    probe_tex_pdf_backend,
    slug_from_paths,
)
from jobapply.profile import (
    Profile,
    ProfileLoadError,
    load_profile,
    profile_skill_list,
    profile_to_text,
    save_profile,
)
from jobapply.profile_import import (
    SUPPORTED_SUFFIXES,
    ResumeImportError,
    extract_profile_from_resume,
    extract_profile_from_text,
)
from jobapply.profile_validation import (
    ProfileIssue,
    validate_profile,
    validate_profile_path,
)
from jobapply.runner import run_pipeline
from jobapply.tailor_one import (
    JD_SUPPORTED_SUFFIXES,
    TAILOR_INPUT_SUFFIXES,
    JobDescriptionReadError,
    TailorEmailRequest,
    peek_application_hints,
    tailor_for_job_description,
)
from jobapply.utils import atomic_write_json, slugify
from jobapply.utils import profile_hash as profile_hash_fn
from jobapply.workspace import Workspace

app = typer.Typer(no_args_is_help=True, add_completion=False)
console = Console()

DEFAULT_PROFILE_FILENAME = "profile.json"


def _version_callback(value: bool) -> None:
    if value:
        typer.echo(f"jobapply {__version__}")
        raise typer.Exit()


@app.callback()
def _root(
    version: bool = typer.Option(
        False,
        "--version",
        "-V",
        callback=_version_callback,
        is_eager=True,
        help="Show the jobapply version and exit.",
    ),
) -> None:
    """JobApply — search every job board and tailor your resume with AI."""


def _ledger_db_path(cfg: AppConfig, cwd: Path | None = None) -> Path:
    """Resolve the ledger path: explicit config value wins; otherwise default.

    Relative ``cfg.ledger_path`` values are resolved against ``cwd`` so users
    can write ``ledger_path = "ledger.db"`` and have it live next to
    ``jobapply.toml``.
    """
    base = cwd or Path.cwd()
    if cfg.ledger_path:
        path = Path(cfg.ledger_path).expanduser()
        if not path.is_absolute():
            path = base / path
        path.parent.mkdir(parents=True, exist_ok=True)
        return path
    return base / ".jobapply" / "ledger.db"


def _print_run_summary(run_dir: Path, n_searched: int) -> None:
    """Tally jobs.json by status and print a friendly summary.

    Prints a clear hint when every job was deduped from the ledger so the user
    knows why the run dir looks empty and how to re-run with ``--force``.
    """
    jobs_path = run_dir / "jobs.json"
    if not jobs_path.is_file():
        if n_searched == 0:
            console.print("[yellow]No jobs returned by search.[/yellow]")
        else:
            console.print(
                "[yellow]Search returned "
                f"{n_searched} jobs, but none were processed.[/yellow] "
                "[dim]Pass --force to ignore the ledger and re-process.[/dim]",
            )
        return
    try:
        data = json.loads(jobs_path.read_text(encoding="utf-8"))
        records = data.get("jobs", [])
    except (OSError, json.JSONDecodeError):
        return
    counts = Counter(str(r.get("status", "")) for r in records)
    if not records:
        return

    table = Table(title=f"Run summary ({len(records)} jobs)", show_header=True)
    table.add_column("Status", style="bold")
    table.add_column("Count", justify="right")
    order = [
        LedgerStatus.done.value,
        LedgerStatus.cached.value,
        LedgerStatus.skipped.value,
        LedgerStatus.failed.value,
        LedgerStatus.pending.value,
    ]
    for status in order:
        if counts.get(status):
            table.add_row(status, str(counts[status]))
    for status, n in counts.items():
        if status not in order:
            table.add_row(status, str(n))
    console.print(table)

    cached = counts.get(LedgerStatus.cached.value, 0)
    if cached and counts.get(LedgerStatus.done.value, 0) == 0:
        console.print(
            f"[yellow]All {cached} jobs were already processed in earlier "
            "runs.[/yellow] [dim]Re-run with `--force` to ignore the ledger, "
            "or inspect `.jobapply/ledger.db` for prior artifact paths.[/dim]",
        )

    csv_path = write_jobs_csv_from_path(jobs_path, run_dir=run_dir)
    if csv_path is not None:
        console.print(
            f"[green]Wrote[/green] {csv_path} "
            "[dim](import into Google Sheets via File → Import)[/dim]"
        )


def _report_profile_issues(
    issues: list[ProfileIssue],
    *,
    profile_path: Path,
    context: str,
) -> bool:
    """Print profile issues; return True when there are required gaps.

    ``context`` is shown before the bullet list (e.g. "after import" or
    "before this run"). The caller decides what to do with the truthy
    return value (during ``init`` we just keep going; ``run``/``resume``
    keep going too but the user has been clearly warned).
    """
    if not issues:
        return False
    required = [i for i in issues if i.is_required]
    recommended = [i for i in issues if not i.is_required]
    header_color = "red" if required else "yellow"
    header_label = f"{len(required)} required" if required else f"{len(recommended)} recommended"
    console.print(
        f"\n[{header_color}]profile.json needs attention[/{header_color}] "
        f"({header_label} — {context}, {profile_path})"
    )
    for issue in required:
        console.print(f"  [red]•[/red] {issue.message}")
    for issue in recommended:
        console.print(f"  [yellow]•[/yellow] {issue.message}")
    console.print(
        "[dim]Edit the file above and re-run when ready. "
        "Required gaps will produce empty resume sections.[/dim]"
    )
    return bool(required)


def _validate_resume_path(raw: str) -> Path | None:
    """Parse user input into a usable resume path or report why it can't be used."""
    text = raw.strip().strip('"').strip("'")
    if not text:
        return None
    expanded = Path(text).expanduser().resolve()
    if not expanded.is_file():
        console.print(f"[red]Not a file:[/red] {expanded}")
        return None
    if expanded.suffix.lower() not in SUPPORTED_SUFFIXES:
        console.print(
            f"[red]Unsupported format[/red] '{expanded.suffix}'. "
            f"Use one of: {', '.join(SUPPORTED_SUFFIXES)}.",
        )
        return None
    return expanded


def _read_pasted_resume() -> str:
    """Read pasted resume text from stdin. Empty string if user bailed.

    Uses :func:`questionary.text` with ``multiline=True`` when stdin is a
    TTY so users get the standard editor affordance; otherwise we drain
    stdin (``cat resume.txt | jobapply init --paste``).
    """
    if not sys.stdin.isatty():
        return sys.stdin.read()
    answer = questionary.text(
        (
            "Paste your resume below. "
            "Press Esc then Enter (or Meta+Enter) when finished, "
            "or leave blank to abort."
        ),
        multiline=True,
        default="",
    ).ask()
    if answer is None:
        raise typer.Exit(1)
    return str(answer)


def _save_imported_profile(profile: Profile, target: Path) -> None:
    """Write ``profile`` to ``target`` and report any validation issues."""
    save_profile(profile, target)
    console.print(f"[green]Wrote[/green] {target}")
    issues = validate_profile(profile)
    _report_profile_issues(issues, profile_path=target, context="after import")


def _import_profile_from_path(
    resume_path: Path,
    cfg: AppConfig,
    target: Path,
    *,
    force: bool,
) -> bool:
    """Convert ``resume_path`` to ``target`` profile.json. Returns True on success."""
    if (
        target.is_file()
        and not force
        and not questionary.confirm(
            f"{target.name} already exists. Overwrite with imported resume?",
            default=False,
        ).ask()
    ):
        console.print(f"[yellow]Skip[/yellow] {target} (kept existing)")
        return False
    console.print(f"[dim]Importing {resume_path.name}…[/dim]")
    try:
        profile = extract_profile_from_resume(resume_path, cfg)
    except ResumeImportError as exc:
        console.print(f"[red]Resume import failed:[/red] {exc}")
        return False
    _save_imported_profile(profile, target)
    return True


def _import_profile_from_paste(
    resume_text: str,
    cfg: AppConfig,
    target: Path,
    *,
    force: bool,
) -> bool:
    """Convert pasted ``resume_text`` to ``target`` profile.json."""
    if (
        target.is_file()
        and not force
        and not questionary.confirm(
            f"{target.name} already exists. Overwrite with imported resume?",
            default=False,
        ).ask()
    ):
        console.print(f"[yellow]Skip[/yellow] {target} (kept existing)")
        return False
    console.print("[dim]Importing pasted resume text…[/dim]")
    try:
        profile = extract_profile_from_text(resume_text, cfg)
    except ResumeImportError as exc:
        console.print(f"[red]Resume import failed:[/red] {exc}")
        return False
    _save_imported_profile(profile, target)
    return True


def _ask_provider_settings(provider: str, current: ProviderConfig) -> ProviderConfig:
    """Prompt for api_key / base_url / model. Empty input keeps the current value."""
    default_model = current.model or DEFAULT_MODELS.get(provider, "")
    default_base = current.base_url or DEFAULT_BASE_URLS.get(provider, "")

    api_key: str | None = current.api_key
    if provider != "ollama":
        # Cloudflare's "API key" field is actually their API token. Hint it
        # in the prompt so users know which credential to paste.
        if provider == "cloudflare":
            prompt = "Cloudflare API token (Workers AI scope; or env:VAR_NAME, blank to skip)"
        elif provider == "openrouter":
            prompt = (
                "OpenRouter API key (https://openrouter.ai/keys; " "or env:VAR_NAME, blank to skip)"
            )
        else:
            prompt = f"{provider} API key (or env:VAR_NAME, blank to skip)"
        entered = questionary.password(prompt, default=current.api_key or "").ask()
        if entered is None:
            raise typer.Exit(1)
        api_key = entered.strip() or None

    account_id: str | None = current.account_id
    gateway_id: str | None = current.gateway_id
    if provider == "cloudflare":
        entered_account = questionary.text(
            "Cloudflare account id (find it on the Workers & Pages overview page)",
            default=current.account_id or "",
        ).ask()
        if entered_account is None:
            raise typer.Exit(1)
        account_id = entered_account.strip() or None

        # Optional. Setting this routes through AI Gateway's compat endpoint,
        # which is required for BYOK models like `openai/gpt-5`.
        entered_gateway = questionary.text(
            (
                "Cloudflare AI Gateway slug (optional — leave blank for direct "
                "Workers AI; set this to use BYOK models like openai/gpt-5)"
            ),
            default=current.gateway_id or "",
        ).ask()
        if entered_gateway is None:
            raise typer.Exit(1)
        gateway_id = entered_gateway.strip() or None

    base_url: str | None = current.base_url
    # Cloudflare's base URL is derived from account_id (+ optional gateway_id),
    # so don't prompt for it.
    if provider in {"ollama", "openai", "openrouter"} or (
        current.base_url is not None and provider != "cloudflare"
    ):
        entered_url = questionary.text(
            f"{provider} base URL (blank for default)",
            default=default_base,
        ).ask()
        if entered_url is None:
            raise typer.Exit(1)
        base_url = entered_url.strip() or None

    model_prompt = f"{provider} model"
    if provider == "cloudflare" and gateway_id:
        # Hint the right naming scheme for the gateway path.
        model_prompt += " (use provider/model, e.g. openai/gpt-5)"
    elif provider == "openrouter":
        # OpenRouter routes by `vendor/model` ids; surface that so users
        # don't paste a bare OpenAI model id by mistake.
        model_prompt += " (use vendor/model, e.g. openai/gpt-4o-mini)"
    model = questionary.text(model_prompt, default=default_model).ask()
    if model is None:
        raise typer.Exit(1)
    return ProviderConfig(
        api_key=api_key,
        base_url=base_url,
        model=model.strip() or None,
        account_id=account_id,
        gateway_id=gateway_id,
    )


def _interactive_config(existing: AppConfig | None) -> AppConfig:
    """Walk the user through provider + connection settings.

    Lets the user configure one or more providers in the same flow so they
    can flip between them at runtime via ``--provider``. The active
    provider written to ``provider = "..."`` is the one used when no
    ``--provider`` flag is passed.
    """
    base = existing or AppConfig()

    # Default-check providers that already have any settings persisted plus
    # the currently-active one so re-running `jobapply config` mirrors the
    # state on disk.
    pre_checked = {name for name in PROVIDER_NAMES if base.providers.get(name)}
    pre_checked.add(base.provider)
    choices = [
        questionary.Choice(name, value=name, checked=name in pre_checked) for name in PROVIDER_NAMES
    ]
    selected = questionary.checkbox(
        "Which providers do you want to configure? " "(space to toggle, enter to confirm)",
        choices=choices,
        validate=lambda picked: bool(picked) or "Pick at least one provider.",
    ).ask()
    if not selected:
        raise typer.Exit(1)

    providers: dict[str, ProviderConfig] = dict(base.providers)
    for provider in selected:
        console.print(f"\n[bold]Configuring[/bold] {provider}")
        current = base.provider_config(provider)
        providers[provider] = _ask_provider_settings(provider, current)

    # Default provider must be one of the picked ones; preserve the
    # previous default when it's still in the selection.
    default_provider = base.provider if base.provider in selected else selected[0]
    if len(selected) > 1:
        chosen_default = questionary.select(
            "Default provider (used when --provider isn't passed at runtime)",
            choices=list(selected),
            default=default_provider,
        ).ask()
        if not chosen_default:
            raise typer.Exit(1)
        default_provider = chosen_default

    output_dir = (
        questionary.text(
            "Output directory",
            default=base.output_dir,
        ).ask()
        or base.output_dir
    )

    # Migrate legacy `profile.md` configs to the new JSON filename so the
    # next `init` writes profile.json AND the toml points at it. Custom
    # paths that aren't the literal default are preserved — power users who
    # set `profile_path = "candidates/jane.json"` keep their override.
    profile_path = base.profile_path
    if not profile_path or profile_path.strip().lower() == "profile.md":
        profile_path = DEFAULT_PROFILE_FILENAME

    return AppConfig(
        provider=default_provider,
        model=base.model,
        min_fit=base.min_fit,
        results_wanted=base.results_wanted,
        hours_old=base.hours_old,
        concurrency=base.concurrency,
        sites=base.sites,
        profile_path=profile_path,
        output_dir=output_dir,
        ledger_path=base.ledger_path,
        providers=providers,
        latex_api=base.latex_api,
    )


def _persist_config(cfg: AppConfig, path: Path) -> None:
    path.write_text(render_config_toml(cfg), encoding="utf-8")
    console.print(f"[green]Wrote[/green] {path}")


def _ordered_provider_choices(cfg: AppConfig) -> list[str]:
    """Providers offered at the runtime picker.

    Surfaces every provider the user actually configured first (preserving
    the order from ``jobapply.toml``) and falls back to the active
    provider when nothing else has been configured. We deliberately don't
    include unknown providers from :data:`PROVIDER_NAMES` — that would
    push the user toward a provider they have no credentials for.
    """
    configured = list(cfg.providers.keys())
    if cfg.provider not in configured:
        configured.append(cfg.provider)
    seen: set[str] = set()
    ordered: list[str] = []
    for name in configured:
        if name and name not in seen:
            ordered.append(name)
            seen.add(name)
    return ordered


def _resolve_provider_and_model(
    cfg: AppConfig,
    *,
    provider: str | None,
    model: str | None,
    yes: bool,
) -> tuple[str, str]:
    """Pick the provider + model for this run.

    ``--provider`` / ``--model`` always win. When neither is passed and
    the user is in interactive mode, prompt with the providers they
    configured in ``jobapply.toml`` (default = the active one) and let
    them override the resolved model.
    """
    if provider:
        prov = provider.lower().strip()
    elif yes:
        prov = (cfg.provider or "").lower().strip()
    else:
        choices = _ordered_provider_choices(cfg)
        default_provider = cfg.provider if cfg.provider in choices else choices[0]
        if len(choices) == 1:
            prov = choices[0]
        else:
            answer = questionary.select(
                "Provider for this run",
                choices=choices,
                default=default_provider,
            ).ask()
            if not answer:
                raise typer.Exit(1)
            prov = answer.lower().strip()

    if model:
        mdl = model
    else:
        default_model = cfg.resolved_model(prov)
        if yes or not default_model:
            mdl = default_model
        else:
            answer = questionary.text(
                f"Model for {prov} (Enter to keep default)",
                default=default_model,
            ).ask()
            if answer is None:
                raise typer.Exit(1)
            mdl = answer.strip() or default_model

    if not mdl:
        console.print(f"[red]No model configured for provider {prov}.[/red]")
        raise typer.Exit(1)
    return prov, mdl


def _run_resume_import(
    cfg: AppConfig,
    profile_target: Path,
    *,
    resume_path: str | None,
    paste_flag: bool,
    force: bool,
    non_interactive: bool,
) -> bool:
    """Drive the resume → profile.json import.

    Honors three input shapes, in priority order:

    1. ``--resume`` (file path).
    2. ``--paste`` (drain stdin OR open the multiline editor).
    3. Interactive prompts: ask for a path; if blank, fall back to a paste
       prompt. One of the two MUST resolve to non-empty input.

    Returns True iff a profile.json was successfully written. Raises
    :class:`typer.Exit` if the user cancels in interactive mode without
    providing a usable resume — ``init`` is not allowed to finish without
    one.
    """
    if resume_path:
        chosen = _validate_resume_path(resume_path)
        if chosen is None:
            console.print(
                "[red]--resume points at an unusable path.[/red] "
                "Re-run `jobapply init` with a valid file.",
            )
            raise typer.Exit(1)
        return _import_profile_from_path(chosen, cfg, profile_target, force=force)

    if paste_flag:
        text = _read_pasted_resume()
        if not text.strip():
            console.print(
                "[red]No resume text received via --paste.[/red] "
                "Pipe text on stdin or rerun without --paste.",
            )
            raise typer.Exit(1)
        return _import_profile_from_paste(text, cfg, profile_target, force=force)

    if non_interactive:
        console.print(
            "[red]A resume is required.[/red] Pass --resume PATH or --paste "
            "(or rerun without --non-interactive for the prompts).",
        )
        raise typer.Exit(1)

    # Interactive: ask for a file first, fall back to paste if blank.
    while True:
        answer = questionary.text(
            "Path to your resume "
            f"({'/'.join(SUPPORTED_SUFFIXES)}; press Enter to paste text instead)",
            default="",
        ).ask()
        if answer is None:
            raise typer.Exit(1)
        if answer.strip():
            chosen = _validate_resume_path(answer)
            if chosen is None:
                # The validator already explained why; re-prompt.
                continue
            return _import_profile_from_path(chosen, cfg, profile_target, force=force)

        text = _read_pasted_resume()
        if text.strip():
            return _import_profile_from_paste(text, cfg, profile_target, force=force)

        console.print(
            "[red]A resume is required.[/red] Provide a file path or paste "
            "your resume text. (Ctrl+C to abort.)",
        )


@app.command()
def init(
    force: bool = typer.Option(
        False,
        "--force",
        help="Overwrite existing profile.json/jobapply.toml without confirmation.",
    ),
    non_interactive: bool = typer.Option(
        False,
        "--non-interactive",
        help=(
            "Skip interactive provider prompts and write a default jobapply.toml. "
            "Still requires --resume or --paste so a profile.json is produced."
        ),
    ),
    resume_path: str | None = typer.Option(
        None,
        "--resume",
        "-r",
        help=(
            "Path to your resume "
            f"({', '.join(SUPPORTED_SUFFIXES)}). "
            "The configured LLM extracts profile.json from it."
        ),
    ),
    paste: bool = typer.Option(
        False,
        "--paste",
        help=(
            "Read resume text from stdin (or open a multiline prompt) instead "
            "of reading a file. Useful when you don't have a file handy."
        ),
    ),
) -> None:
    """Configure your provider and import your resume into ``profile.json``.

    A resume is mandatory: pass ``--resume PATH``, ``--paste`` (with text on
    stdin or via the interactive editor), or answer the interactive prompts.
    The configured LLM is invoked once to populate the JSON schema, so make
    sure your provider credentials work before you run ``init``.
    """
    load_dotenv_if_present()
    root = Path.cwd()
    cfg_path = find_config_path(root)

    cfg: AppConfig
    if non_interactive:
        if cfg_path.is_file() and not force:
            console.print(f"[yellow]Skip[/yellow] existing {cfg_path}")
            cfg = load_config(root)
        else:
            cfg = AppConfig()
            _persist_config(cfg, cfg_path)
    else:
        console.print("[bold]Welcome to jobapply.[/bold] Let's configure your provider.\n")
        existing = load_config(root) if cfg_path.is_file() else None
        if existing and not force:
            if questionary.confirm(
                f"{cfg_path.name} already exists. Update it now?",
                default=True,
            ).ask():
                cfg = _interactive_config(existing)
                _persist_config(cfg, cfg_path)
            else:
                console.print("[dim]Keeping existing config.[/dim]")
                cfg = existing
        else:
            cfg = _interactive_config(existing)
            _persist_config(cfg, cfg_path)

    profile_target = Path(cfg.profile_path or DEFAULT_PROFILE_FILENAME)
    if not profile_target.is_absolute():
        profile_target = root / profile_target

    imported = _run_resume_import(
        cfg,
        profile_target,
        resume_path=resume_path,
        paste_flag=paste,
        force=force,
        non_interactive=non_interactive,
    )

    if not imported:
        console.print(
            "[red]profile.json was not written.[/red] Re-run `jobapply init` "
            "with a valid resume so future `jobapply run` commands have data "
            "to work with.",
        )
        raise typer.Exit(1)

    if non_interactive:
        console.print(
            "[dim]Edit jobapply.toml to add your provider keys, or rerun "
            "`jobapply init` for the interactive flow.[/dim]",
        )
    else:
        console.print(
            "\n[bold green]Setup complete.[/bold green] "
            "Run `jobapply run --titles ...` to start.\n"
            "[dim]Tip: add `jobapply.toml` to .gitignore if you stored secrets in it.[/dim]",
        )


@app.command("config")
def config_cmd(
    show: bool = typer.Option(False, "--show", help="Print the resolved config and exit."),
) -> None:
    """Update or inspect provider settings stored in jobapply.toml."""
    load_dotenv_if_present()
    root = Path.cwd()
    cfg_path = find_config_path(root)

    if show:
        cfg = load_config(root) if cfg_path.is_file() else AppConfig()
        # Print raw to avoid Rich treating `[providers.x]` as markup tags.
        typer.echo(render_config_toml(cfg))
        return

    existing = load_config(root) if cfg_path.is_file() else None
    cfg = _interactive_config(existing)
    _persist_config(cfg, cfg_path)


@app.command("ui")
def ui_cmd(
    host: str = typer.Option(
        "127.0.0.1",
        "--host",
        help="Host interface to bind. Use 0.0.0.0 to expose on the LAN.",
    ),
    port: int = typer.Option(8000, "--port", "-p", help="Port to listen on."),
    workspace: str | None = typer.Option(
        None,
        "--workspace",
        "-w",
        help=(
            "Workspace directory the UI reads/writes from. Defaults to "
            "`output/web` under the current directory."
        ),
    ),
    reload: bool = typer.Option(
        False,
        "--reload",
        help="Enable uvicorn auto-reload (developer use).",
    ),
    open_browser: bool = typer.Option(
        True,
        "--open/--no-open",
        help="Open the UI in your default browser once the server is ready.",
    ),
) -> None:
    """Start the JobApply web UI (FastAPI + React) on this machine.

    The bundled React frontend is served from
    ``jobapply/web_dist/`` if it has been built. Run ``cd web && npm
    run build`` once to produce the bundle (you only need a node
    toolchain to build, not to use, the UI).

    For frontend development, run the Vite dev server separately:
    ``cd web && npm run dev`` — it proxies ``/api`` calls to the
    FastAPI server you've started here.
    """
    import os
    import threading
    import webbrowser

    import uvicorn

    from jobapply.server import WEB_DIST_DIRNAME, create_app

    cwd = Path.cwd()

    # The UI exposes search/run/tailor — all of which need a configured
    # provider + an imported profile.json. Bail early with a friendly
    # pointer to `jobapply init` so users don't get confusing errors
    # from inside the SPA later. We accept either `jobapply.toml` or
    # `.jobapply.toml` to mirror `find_config_path`'s lookup order.
    toml_path = cwd / "jobapply.toml"
    dot_toml_path = cwd / ".jobapply.toml"
    if not toml_path.is_file() and not dot_toml_path.is_file():
        console.print(
            "[red]No jobapply.toml found in[/red] "
            f"[bold]{cwd}[/bold]\n"
            "[dim]The web UI needs a configured provider + profile before "
            "it can search or tailor jobs.[/dim]\n\n"
            "[bold]Run this first:[/bold]\n"
            "  [cyan]jobapply init --resume <path/to/your/resume>[/cyan]\n\n"
            "[dim]The init command imports your resume into "
            "profile.json, configures an LLM provider, and writes "
            "jobapply.toml. Once it finishes, re-run "
            "`jobapply ui`.[/dim]"
        )
        raise typer.Exit(1)

    # profile.json is created by `jobapply init` alongside the toml,
    # so a missing profile typically means the user hand-wrote a
    # jobapply.toml without going through init. Surface the same hint
    # rather than letting every API request 400 with the same error.
    profile_rel = "profile.json"
    try:
        cfg_peek = load_config(cwd)
        profile_rel = cfg_peek.profile_path or profile_rel
    except Exception:  # noqa: BLE001 - defer detailed errors to create_app
        pass
    profile_full = (cwd / profile_rel).expanduser()
    if not profile_full.is_absolute():
        profile_full = cwd / profile_rel
    if not profile_full.is_file():
        console.print(
            "[red]profile.json was not found at[/red] "
            f"[bold]{profile_full}[/bold]\n"
            "[dim]Your jobapply.toml exists but the resume hasn't been "
            "imported yet.[/dim]\n\n"
            "[bold]Run this first:[/bold]\n"
            "  [cyan]jobapply init --resume <path/to/your/resume>[/cyan]\n\n"
            "[dim](Or `jobapply init --paste` to paste the text directly.)[/dim]"
        )
        raise typer.Exit(1)

    ws_path: Path | None = None
    if workspace:
        ws_path = Path(workspace).expanduser()
        if not ws_path.is_absolute():
            ws_path = cwd / ws_path

    app_obj = create_app(cwd=cwd, workspace_path=ws_path)
    dist = Path(__file__).parent / WEB_DIST_DIRNAME
    bundle_present = (dist / "index.html").is_file()
    if not bundle_present:
        console.print(
            "[yellow]No frontend bundle found at "
            f"{dist}.[/yellow]\n"
            "[dim]Run [bold]cd web && npm install && npm run build[/bold] to "
            "build it, or run [bold]npm run dev[/bold] for live-reload "
            "development on http://localhost:5173.[/dim]"
        )

    url = f"http://{host}:{port}"
    console.print(
        f"[bold green]JobApply UI[/bold green] → {url}\n" f"[dim]API docs: {url}/docs[/dim]"
    )

    if open_browser and bundle_present and not os.environ.get("JOBAPPLY_NO_OPEN"):
        # Defer the browser launch until the server is actually
        # listening; otherwise the browser hits a connection refused.
        def _open_later() -> None:
            import time

            time.sleep(1.2)
            with contextlib.suppress(Exception):
                webbrowser.open(url)

        threading.Thread(target=_open_later, daemon=True).start()

    uvicorn.run(
        app_obj,
        host=host,
        port=port,
        reload=reload,
        log_level="info",
    )


@app.command("workspace")
def workspace_cmd(
    workspace_path: str = typer.Argument(
        ...,
        help="Path to a workspace directory (created by `jobapply search/run --workspace`).",
    ),
    limit: int = typer.Option(
        20,
        "--limit",
        "-n",
        help="How many recent search/run invocations to show.",
    ),
) -> None:
    """Inspect a centralized workspace: catalog size + recent searches.

    Prints the SQLite-backed workspace catalog summary plus the latest
    search / run history rows recorded in ``workspace_searches`` so you
    can see at a glance how big the workspace has grown and which
    invocations contributed to it.
    """
    target = Path(workspace_path).expanduser()
    if not target.is_absolute():
        target = Path.cwd() / target
    target = target.resolve()
    if not target.exists() or not (target / "workspace.db").is_file():
        console.print(
            f"[red]Not a workspace:[/red] {target}\n"
            "[dim]Run `jobapply search --workspace <path>` to create one.[/dim]"
        )
        raise typer.Exit(1)
    ws = Workspace.open(target)
    total = ws.count()
    console.print(
        f"[bold]Workspace[/bold] {ws.path}\n"
        f"[dim]{total} job{'s' if total != 1 else ''} cataloged "
        f"in {ws.db_path.name}[/dim]"
    )
    searches = ws.list_searches(limit=limit)
    if not searches:
        console.print("[dim]No search/run history yet.[/dim]")
        return
    table = Table(title=f"Last {len(searches)} invocations", show_header=True)
    table.add_column("#", justify="right")
    table.add_column("Command")
    table.add_column("Started")
    table.add_column("Provider/Model")
    table.add_column("Fetched", justify="right")
    table.add_column("New", justify="right", style="green")
    table.add_column("Dup", justify="right", style="yellow")
    for entry in searches:
        started = entry.started_at.isoformat(timespec="seconds") if entry.started_at else ""
        pm = " / ".join(filter(None, [entry.provider, entry.model])) or "—"
        table.add_row(
            str(entry.id or ""),
            entry.command or "",
            started,
            pm,
            str(entry.fetched),
            str(entry.new_jobs),
            str(entry.duplicate_jobs),
        )
    console.print(table)


@app.command("list")
def list_runs(
    output_dir: str | None = typer.Option(None, "--output-dir", "-o"),
) -> None:
    """List recent output runs (directories under output/)."""
    cfg = load_config(Path.cwd())
    root = Path.cwd() / (output_dir or cfg.output_dir)
    if not root.is_dir():
        console.print("[yellow]No output directory yet.[/yellow]")
        raise typer.Exit(0)
    rows = sorted(root.glob("run-*"), key=lambda p: p.stat().st_mtime, reverse=True)[:30]
    table = Table(title="Runs")
    table.add_column("Run")
    table.add_column("Has meta")
    table.add_column("Has jobs.json")
    for p in rows:
        table.add_row(
            p.name,
            "yes" if (p / "meta.json").is_file() else "no",
            "yes" if (p / "jobs.json").is_file() else "no",
        )
    console.print(table)


@app.command()
def run(
    titles: str | None = typer.Option(None, "--titles", "-t", help="Comma-separated job titles"),
    skills: str | None = typer.Option(None, "--skills", "-s", help="Comma-separated skills"),
    location: str | None = typer.Option(None, "--location", "-l"),
    remote: bool = typer.Option(False, "--remote"),
    results: int | None = typer.Option(
        None,
        "--results",
        "-n",
        help="Override jobapply.toml `results_wanted`.",
    ),
    provider: str | None = typer.Option(
        None,
        "--provider",
        help=(
            "Pick one of the providers configured in jobapply.toml "
            "(gemini | anthropic | openai | ollama | cloudflare | openrouter). "
            "Defaults to the active `provider` field."
        ),
    ),
    model: str | None = typer.Option(
        None,
        "--model",
        help=(
            "Override the model id for this run. Defaults to the chosen "
            "provider's `[providers.<name>].model` value."
        ),
    ),
    min_fit: float | None = typer.Option(
        None,
        "--min-fit",
        help="Override jobapply.toml `min_fit`.",
    ),
    profile_path: str | None = typer.Option(
        None,
        "--profile",
        help="Override jobapply.toml `profile_path` (must be a profile.json file).",
    ),
    output_dir: str | None = typer.Option(
        None,
        "--output-dir",
        "-o",
        help="Override jobapply.toml `output_dir`.",
    ),
    workspace: str | None = typer.Option(
        None,
        "--workspace",
        "-w",
        help=(
            "Path to a centralized workspace directory. When set, this run "
            "appends into the workspace's SQLite catalog (workspace.db) and "
            "shares per-job artifacts with previous searches, instead of "
            "creating a new run-<ts> folder. Jobs already cataloged are "
            "deduped automatically (use --force to re-process them)."
        ),
    ),
    with_networking: bool = typer.Option(False, "--with-networking"),
    no_pdf: bool = typer.Option(False, "--no-pdf"),
    linkedin_descriptions: bool = typer.Option(
        True,
        "--linkedin-descriptions/--no-linkedin-descriptions",
        help=(
            "Fetch the full LinkedIn JD per hit (slower, but required "
            "for accurate scoring/tailoring). LinkedIn's search endpoint "
            "only returns metadata, so disabling this leaves descriptions "
            "blank. Default: on."
        ),
    ),
    force: bool = typer.Option(
        False,
        "--force",
        help="Ignore ledger dedupe (still writes new run)",
    ),
    yes: bool = typer.Option(False, "--yes", "-y", help="Non-interactive defaults"),
) -> None:
    """Search jobs and tailor resume + cover letter for each result.

    CLI flags override values from ``jobapply.toml``; absent flags fall back to
    the config so settings like ``results_wanted`` actually take effect.
    """
    load_dotenv_if_present()
    root = Path.cwd()
    cfg = load_config(root)
    apply_latex_api_env(cfg)
    if not yes:
        titles = titles or questionary.text("Job titles (comma-separated)").ask()
        skills = skills or questionary.text("Primary skills (comma-separated)", default="").ask()
        if location is None:
            location = questionary.text("Location (or blank)", default="").ask()
    if not titles:
        console.print("[red]titles required[/red]")
        raise typer.Exit(1)
    title_list = [x.strip() for x in titles.split(",") if x.strip()]
    skill_list = [x.strip() for x in (skills or "").split(",") if x.strip()]
    prov, mdl = _resolve_provider_and_model(cfg, provider=provider, model=model, yes=yes)

    effective_results = results if results is not None else cfg.results_wanted
    effective_min_fit = min_fit if min_fit is not None else cfg.min_fit
    effective_profile = profile_path or cfg.profile_path
    effective_output = output_dir or cfg.output_dir

    prof = Path(effective_profile)
    try:
        profile = load_profile(prof)
    except ProfileLoadError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(1) from exc
    profile_text = profile_to_text(profile)
    canonical_skills = profile_skill_list(profile)
    _report_profile_issues(
        validate_profile(profile),
        profile_path=prof,
        context="before this run",
    )
    ph = profile_hash_fn(profile_text)
    run_id = f"run-{datetime.now(UTC).strftime('%Y%m%d-%H%M%S')}"
    workspace_dir = _resolve_workspace_dir(workspace, root)
    workspace_obj: Workspace | None = None
    if workspace_dir is not None:
        workspace_obj = Workspace.open(workspace_dir)
        run_dir = workspace_obj.path
    else:
        run_dir = root / effective_output / run_id
        run_dir.mkdir(parents=True, exist_ok=True)
    ledger_path = _ledger_db_path(cfg)
    search_input = JobSearchInput(
        titles=title_list,
        skills=skill_list,
        location=location or None,
        remote=remote,
        results_wanted=effective_results,
        hours_old=cfg.hours_old,
        site_names=cfg.sites,
        linkedin_fetch_description=linkedin_descriptions,
    ).model_dump(mode="json")
    workspace_search_id: int | None = None
    if workspace_obj is not None:
        workspace_search_id = workspace_obj.start_search(
            command="run",
            search_input=search_input,
            provider=prov,
            model=mdl,
        )
    initial = {
        "run_id": run_id,
        "run_dir": str(run_dir.resolve()),
        "profile_path": str(prof.resolve()),
        "profile_text": profile_text,
        "profile_hash": ph,
        "profile_skills": canonical_skills,
        "provider": prov,
        "model": mdl,
        "min_fit": effective_min_fit,
        "with_networking": with_networking,
        "no_pdf": no_pdf,
        "force": force,
        "ledger_db_path": str(ledger_path.resolve()),
        "search_input": search_input,
        "jobs_raw": [],
        "queue": [],
    }
    if workspace_obj is not None:
        initial["workspace_path"] = str(workspace_obj.path)
        if workspace_search_id is not None:
            initial["workspace_search_id"] = workspace_search_id
    _validate_keys(cfg, prov)
    backend = probe_md_pdf_backend() or "none"
    backend_note = {
        "pandoc": "[green]pandoc[/green] (high quality)",
        "weasyprint": "[green]weasyprint[/green] (good)",
        "fpdf2": (
            "[yellow]fpdf2[/yellow] (basic — install [bold]pandoc[/bold] "
            "or [bold]brew install pango[/bold] for nicer output)"
        ),
        "none": "[red]none available[/red]",
    }[backend]
    tex_backend = probe_tex_pdf_backend() or "none"
    tex_backend_note = {
        "latex-on-http": f"[green]latex-on-http[/green] ([dim]{cfg.latex_api.url}[/dim])",
        "tectonic": "[green]tectonic[/green] (local)",
        "pdflatex": "[green]pdflatex[/green] (local)",
        "none": (
            "[red]none available[/red] — enable [bold]latex_api[/bold] "
            "in jobapply.toml or install tectonic/pdflatex"
        ),
    }[tex_backend]
    workspace_summary = ""
    if workspace_obj is not None:
        existing = workspace_obj.count()
        workspace_summary = (
            f"\n[dim]Workspace[/dim] {workspace_obj.path} "
            f"[dim]({existing} job{'s' if existing != 1 else ''} cataloged)[/dim]"
        )
    console.print(
        f"[bold]Run[/bold] {run_id} → {run_dir}\n"
        f"[dim]results_wanted={effective_results}  min_fit={effective_min_fit}  "
        f"sites={','.join(cfg.sites)}[/dim]\n"
        f"[dim]Markdown PDF backend:[/dim] {backend_note}\n"
        f"[dim]LaTeX PDF backend:[/dim] {tex_backend_note}{workspace_summary}",
    )
    final_state = run_pipeline(
        initial, run_dir=run_dir, run_id=run_id, show_progress=True, console=console
    )
    n_searched = len(final_state.get("jobs_raw") or [])
    if workspace_obj is not None:
        # Re-render jobs.json + jobs.csv from the DB so the run summary
        # reflects the entire workspace catalog (the graph already
        # upserted into workspace.db as it processed each job).
        workspace_obj.flush_files(
            run_id=run_id,
            search_input=search_input,
            profile_path=str(prof.resolve()),
            provider=prov,
            model=mdl,
        )
        run_results = final_state.get("results") or []
        new_jobs = sum(1 for r in run_results if r.get("status") not in {"cached"})
        duplicate_jobs = sum(1 for r in run_results if r.get("status") == "cached")
        if workspace_search_id is not None:
            workspace_obj.finish_search(
                workspace_search_id,
                fetched=n_searched,
                new_jobs=new_jobs,
                duplicate_jobs=duplicate_jobs,
            )
    _print_run_summary(run_dir, n_searched)
    if workspace_obj is not None:
        total = workspace_obj.count()
        console.print(
            f"[dim]Workspace now contains {total} job{'s' if total != 1 else ''} "
            f"at {workspace_obj.path}.[/dim]"
        )
    console.print("[green]Finished.[/green]")


def _validate_keys(cfg: AppConfig, provider: str) -> None:
    p = provider.lower().strip()
    if p == "ollama":
        return
    if not get_api_key(cfg, p):
        console.print(
            f"[yellow]Warning:[/yellow] no API key found for provider={p}. "
            "Set it via `jobapply config` or an env var.",
        )
    if p == "cloudflare" and not get_account_id(cfg, p):
        console.print(
            "[yellow]Warning:[/yellow] no Cloudflare account id found. "
            "Set [providers.cloudflare].account_id in jobapply.toml or "
            "CLOUDFLARE_ACCOUNT_ID in env.",
        )
    if p in {"openai", "ollama", "openrouter"}:
        get_base_url(cfg, p)


def _record_from_raw_job(
    job: RawJob,
    *,
    fit: FitScore | None = None,
    error: str | None = None,
) -> JobRecord:
    """Wrap a :class:`RawJob` from the search agent into a :class:`JobRecord`.

    The search-only flow doesn't run the tailor / cover-letter / render
    nodes, so the resulting record is pure metadata + an optional
    :class:`FitScore`. We mark it ``pending`` because that's what the
    rest of the pipeline expects for "not yet processed by the tailor"
    — and it keeps ``write_jobs_csv``'s sort logic predictable
    (`pending` rows still sort by `-fit.score`, so scored jobs surface
    highest-fit-first).
    """
    return JobRecord(
        job_id=job.job_id,
        title=job.title,
        company=job.company,
        location=job.location,
        # Truncate to keep jobs.json + CSV manageable; the CSV truncates
        # again to 1000 chars before writing the row anyway.
        description=(job.description or "")[:5000],
        job_url=job.job_url,
        apply_url=job.apply_url,
        site=job.site,
        status=LedgerStatus.pending,
        fit=fit,
        application=job.application,
        error=error,
        processed_at=datetime.now(UTC),
    )


def _flush_search_state(run_dir: Path, idx: JobsIndex) -> None:
    """Atomically rewrite ``jobs.json`` + ``jobs.csv`` for the current index.

    Called after every fetched/scored job so users get incremental
    visibility — open the CSV mid-run and you'll see the latest hits
    sorted by descending fit score (when scoring is enabled). Writes
    are tiny (a few hundred KB at the worst) so doing this per-job is
    cheap relative to a single LLM call.
    """
    atomic_write_json(run_dir / "jobs.json", idx.model_dump(mode="json"))
    write_jobs_csv(run_dir, idx)


def _persist_job_record(run_dir: Path, record: JobRecord) -> Path:
    """Write a per-job JSON to ``<run_dir>/jobs/<slug>/job.json``.

    The file mirrors the ``JobRecord`` schema (so we get title /
    company / location / description / URLs / fit score in one place)
    and is the contract ``jobapply tailor --job <path>`` consumes —
    users can pick a job out of a search batch and tailor against it
    without re-pasting the JD anywhere.
    """
    slug = slugify(record.title, record.company, record.job_id)
    job_dir = slug_from_paths(slug, run_dir)
    write_job_json(job_dir, record.model_dump(mode="json"))
    return job_dir / "job.json"


def _resolve_workspace_dir(workspace: str | None, root: Path) -> Path | None:
    """Expand ``--workspace`` into an absolute path (relative to ``root``).

    Returns ``None`` when the flag is unset so callers can fall through
    to the timestamped-folder behaviour.
    """
    if not workspace:
        return None
    p = Path(workspace).expanduser()
    if not p.is_absolute():
        p = root / p
    return p.resolve()


def _print_top_matches(records: list[JobRecord], *, limit: int = 5) -> None:
    """Render a small Rich table of the highest-scoring jobs.

    Silently no-ops when nothing has a fit score — keeps the search
    summary clean for the unscored flow.
    """
    scored = [r for r in records if r.fit is not None]
    if not scored:
        return
    top = sorted(scored, key=lambda r: -(r.fit.score if r.fit else 0.0))[:limit]
    table = Table(title=f"Top {len(top)} matches", show_header=True)
    table.add_column("Score", justify="right", style="bold")
    table.add_column("Title")
    table.add_column("Company")
    table.add_column("Location")
    table.add_column("Link", overflow="fold")
    for r in top:
        score_text = f"{r.fit.score:.2f}" if r.fit else ""
        link = r.apply_url or r.job_url or ""
        table.add_row(score_text, r.title or "", r.company or "", r.location or "", link)
    console.print(table)


@app.command("search")
def search_cmd(
    titles: str | None = typer.Option(None, "--titles", "-t", help="Comma-separated job titles."),
    skills: str | None = typer.Option(
        None,
        "--skills",
        "-s",
        help=(
            "Comma-separated skills. Boost the search query and (when --score "
            "is set) bias the fit-scorer toward what you care about."
        ),
    ),
    location: str | None = typer.Option(None, "--location", "-l"),
    remote: bool = typer.Option(False, "--remote"),
    results: int | None = typer.Option(
        None,
        "--results",
        "-n",
        help="Override jobapply.toml `results_wanted`.",
    ),
    sites: str | None = typer.Option(
        None,
        "--sites",
        help=(
            "Comma-separated JobSpy sites (indeed, linkedin, google, "
            "ziprecruiter, glassdoor). Defaults to jobapply.toml `sites`."
        ),
    ),
    score: bool = typer.Option(
        False,
        "--score",
        help=(
            "Score each fetched job against profile.json. Requires the "
            "active provider's credentials and a populated profile."
        ),
    ),
    provider: str | None = typer.Option(
        None,
        "--provider",
        help=(
            "Pick one of the providers configured in jobapply.toml "
            "(gemini | anthropic | openai | ollama | cloudflare | openrouter). "
            "Only used with --score."
        ),
    ),
    model: str | None = typer.Option(
        None,
        "--model",
        help=(
            "Override the model id for scoring. Defaults to the chosen "
            "provider's `[providers.<name>].model` value. Only used with --score."
        ),
    ),
    profile_path: str | None = typer.Option(
        None,
        "--profile",
        help=(
            "Override jobapply.toml `profile_path` (must be a profile.json "
            "file). Only used with --score."
        ),
    ),
    output_dir: str | None = typer.Option(
        None,
        "--output-dir",
        "-o",
        help="Override jobapply.toml `output_dir`. Artifacts land in <output>/search-<ts>/.",
    ),
    workspace: str | None = typer.Option(
        None,
        "--workspace",
        "-w",
        help=(
            "Path to a centralized workspace directory. When set, this run "
            "appends into the workspace's SQLite catalog (workspace.db) "
            "instead of creating a new search-<ts> folder; jobs already in "
            "the workspace are reported as duplicates and skipped (use "
            "--force to re-fetch)."
        ),
    ),
    force: bool = typer.Option(
        False,
        "--force",
        help=(
            "In --workspace mode, re-fetch and overwrite jobs that are "
            "already in workspace.db. Ignored without --workspace."
        ),
    ),
    linkedin_descriptions: bool = typer.Option(
        True,
        "--linkedin-descriptions/--no-linkedin-descriptions",
        help=(
            "Fetch the full LinkedIn JD per hit (slower, but required "
            "for accurate scoring/tailoring). LinkedIn's search endpoint "
            "only returns metadata, so disabling this leaves descriptions "
            "blank. Default: on."
        ),
    ),
    yes: bool = typer.Option(False, "--yes", "-y", help="Non-interactive defaults (skip prompts)."),
) -> None:
    """Fetch jobs (and optionally score them) into a CSV — no resume tailoring.

    The lightweight cousin of ``jobapply run``: fans out to every
    configured site via JobSpy, dedupes, and writes
    ``output/search-<ts>/jobs.{json,csv}``. Pass ``--score`` to also run
    the LLM fit scorer over each job — that's the only branch that
    needs your provider credentials and ``profile.json``. Use
    ``--provider``/``--model`` to override the active LLM for this
    invocation.

    Pass ``--workspace <path>`` to append into a centralized,
    SQLite-backed workspace folder instead of creating a fresh
    ``search-<ts>`` directory each time. Duplicate jobs (matched by
    stable ``job_id``) are detected automatically and skipped.
    """
    load_dotenv_if_present()
    root = Path.cwd()
    cfg = load_config(root)

    if not yes:
        titles = titles or questionary.text("Job titles (comma-separated)").ask()
        skills = skills or questionary.text("Primary skills (comma-separated)", default="").ask()
        if location is None:
            location = questionary.text("Location (or blank)", default="").ask()
    if not titles:
        console.print("[red]titles required[/red]")
        raise typer.Exit(1)

    title_list = [x.strip() for x in titles.split(",") if x.strip()]
    skill_list = [x.strip() for x in (skills or "").split(",") if x.strip()]
    site_list = [x.strip() for x in sites.split(",") if x.strip()] if sites else cfg.sites
    effective_results = results if results is not None else cfg.results_wanted
    effective_output = output_dir or cfg.output_dir

    profile_text = ""
    profile_path_str = ""
    prov = ""
    mdl = ""
    if score:
        prov, mdl = _resolve_provider_and_model(cfg, provider=provider, model=model, yes=yes)
        _validate_keys(cfg, prov)
        effective_profile = profile_path or cfg.profile_path
        prof = Path(effective_profile)
        if not prof.is_absolute():
            prof = root / prof
        try:
            profile = load_profile(prof)
        except ProfileLoadError as exc:
            console.print(f"[red]{exc}[/red]")
            raise typer.Exit(1) from exc
        profile_text = profile_to_text(profile)
        profile_path_str = str(prof.resolve())
        _report_profile_issues(
            validate_profile(profile),
            profile_path=prof,
            context="before this search",
        )
    elif provider or model or profile_path:
        # Be loud about the no-op so users don't think their flag took
        # effect when it actually didn't.
        console.print(
            "[yellow]--provider / --model / --profile are ignored without " "--score.[/yellow]"
        )

    workspace_dir = _resolve_workspace_dir(workspace, root)
    workspace_obj: Workspace | None = None
    run_id = f"search-{datetime.now(UTC).strftime('%Y%m%d-%H%M%S')}"
    if workspace_dir is not None:
        workspace_obj = Workspace.open(workspace_dir)
        run_dir = workspace_obj.path
    else:
        run_dir = root / effective_output / run_id
        run_dir.mkdir(parents=True, exist_ok=True)

    search_input = JobSearchInput(
        titles=title_list,
        skills=skill_list,
        location=location or None,
        remote=remote,
        results_wanted=effective_results,
        hours_old=cfg.hours_old,
        site_names=site_list,
        linkedin_fetch_description=linkedin_descriptions,
    )

    score_summary = f"  provider={prov}  model={mdl}" if score else "  [dim](no scoring)[/dim]"
    workspace_summary = ""
    if workspace_obj is not None:
        existing = workspace_obj.count()
        workspace_summary = (
            f"\n[dim]Workspace[/dim] {workspace_obj.path} "
            f"[dim]({existing} job{'s' if existing != 1 else ''} already cataloged)[/dim]"
        )
    console.print(
        f"[bold]Search[/bold] {run_id} → {run_dir}\n"
        f"[dim]results_wanted={effective_results}  sites={','.join(site_list)}"
        f"  score={score}[/dim]{score_summary}{workspace_summary}"
    )

    # Build an empty index up-front and seed the on-disk artifacts so
    # users can `tail`/refresh the CSV the moment the first job lands.
    idx = JobsIndex(
        run_id=run_id,
        search=search_input,
        profile_path=profile_path_str,
        provider=prov,
        model=mdl,
        jobs=workspace_obj.all_records() if workspace_obj is not None else [],
    )
    jobs_path = run_dir / "jobs.json"
    csv_path = run_dir / "jobs.csv"
    meta_path = run_dir / "meta.json"

    search_id: int | None = None
    if workspace_obj is not None:
        search_id = workspace_obj.start_search(
            command="search",
            search_input=search_input,
            provider=prov,
            model=mdl,
        )

    def _write_meta(*, fetched: int, new_jobs: int = 0, duplicates: int = 0) -> None:
        # meta.json is informational; rewriting it on each flush gives
        # users a quick glance at progress (`fetched` ticks up live).
        payload: dict[str, Any] = {
            "run_id": run_id,
            "search_input": search_input.model_dump(mode="json"),
            "scored": score,
            "provider": prov,
            "model": mdl,
            "profile_path": profile_path_str,
            "fetched": fetched,
        }
        if workspace_obj is not None:
            payload["workspace"] = str(workspace_obj.path)
            payload["workspace_total_jobs"] = workspace_obj.count()
            payload["new_jobs"] = new_jobs
            payload["duplicate_jobs"] = duplicates
            payload["search_id"] = search_id
        atomic_write_json(meta_path, payload)

    if workspace_obj is not None:
        # Workspace mode: re-render jobs.json/csv from the DB so the
        # initial flush reflects the existing catalog instead of an
        # empty index.
        workspace_obj.flush_files(
            run_id=run_id,
            search_input=search_input,
            profile_path=profile_path_str,
            provider=prov,
            model=mdl,
        )
    else:
        _flush_search_state(run_dir, idx)
    _write_meta(fetched=0)
    jobs_subdir = run_dir / "jobs"
    console.print(
        f"[dim]Streaming results to[/dim] {jobs_path}, {csv_path}, "
        f"[dim]and per-job JSONs under[/dim] {jobs_subdir}"
    )

    records: list[JobRecord] = []  # records produced by THIS invocation
    duplicate_count = 0
    new_count = 0
    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        TextColumn("[bold]{task.completed}[/bold] jobs"),
        TimeElapsedColumn(),
        console=console,
        transient=False,
    ) as progress:
        task = progress.add_task("Searching job boards", total=None)
        for j in iter_search_jobs(search_input):
            rec = _record_from_raw_job(j)
            if workspace_obj is not None:
                already_seen = workspace_obj.is_seen(rec.job_id)
                if already_seen and not force:
                    duplicate_count += 1
                    progress.update(
                        task,
                        advance=1,
                        description=(
                            f"Searching job boards "
                            f"[dim](dup: {duplicate_count}, new: {new_count})[/dim]"
                        ),
                    )
                    continue
                inserted = workspace_obj.upsert_job(rec, search_id=search_id)
                if inserted:
                    new_count += 1
                else:
                    # ``--force`` overwrote a row we already had.
                    duplicate_count += 1
                _persist_job_record(run_dir, rec)
                workspace_obj.flush_files(
                    run_id=run_id,
                    search_input=search_input,
                    profile_path=profile_path_str,
                    provider=prov,
                    model=mdl,
                )
                _write_meta(
                    fetched=new_count + duplicate_count,
                    new_jobs=new_count,
                    duplicates=duplicate_count,
                )
                records.append(rec)
                progress.update(
                    task,
                    advance=1,
                    description=(
                        f"Searching job boards "
                        f"[dim](dup: {duplicate_count}, new: {new_count})[/dim]"
                    ),
                )
            else:
                records.append(rec)
                idx.jobs.append(rec)
                _persist_job_record(run_dir, rec)
                _flush_search_state(run_dir, idx)
                _write_meta(fetched=len(records))
                progress.update(task, advance=1)
    if workspace_obj is not None:
        console.print(
            f"[green]Fetched[/green] {len(records)} jobs "
            f"[dim]({new_count} new, {duplicate_count} duplicate)[/dim]"
        )
    else:
        console.print(f"[green]Fetched[/green] {len(records)} jobs")

    if score and records:
        try:
            llm = create_chat_model(prov, mdl, cfg=cfg)
        except RuntimeError as exc:
            console.print(f"[red]{exc}[/red]")
            raise typer.Exit(1) from exc
        with Progress(
            SpinnerColumn(),
            TextColumn("[progress.description]{task.description}"),
            BarColumn(),
            TextColumn("{task.completed}/{task.total}"),
            TimeElapsedColumn(),
            console=console,
            transient=False,
        ) as progress:
            task = progress.add_task("Scoring jobs", total=len(records))
            for i, raw_record in enumerate(list(records)):
                # Re-build the RawJob handle from the persisted record so
                # the scorer sees the same fields callers do. We pull the
                # full description from the source list (already truncated
                # to 5000 chars by `_record_from_raw_job`).
                job = RawJob(
                    job_id=raw_record.job_id,
                    title=raw_record.title,
                    company=raw_record.company,
                    location=raw_record.location,
                    description=raw_record.description,
                    job_url=raw_record.job_url,
                    apply_url=raw_record.apply_url,
                    site=raw_record.site,
                    application=raw_record.application,
                )
                fit: FitScore | None = None
                err: str | None = None
                try:
                    fit = score_fit(
                        llm,
                        profile_text=profile_text,
                        job=job,
                        skills=skill_list,
                    )
                except Exception as exc:  # noqa: BLE001
                    # One bad job shouldn't kill the whole batch; record
                    # the failure on this row and keep going.
                    err = f"score failed: {exc}"
                new_rec = _record_from_raw_job(job, fit=fit, error=err)
                records[i] = new_rec
                _persist_job_record(run_dir, new_rec)
                if workspace_obj is not None:
                    workspace_obj.upsert_job(new_rec, search_id=search_id)
                    workspace_obj.flush_files(
                        run_id=run_id,
                        search_input=search_input,
                        profile_path=profile_path_str,
                        provider=prov,
                        model=mdl,
                    )
                else:
                    idx.jobs[i] = new_rec
                    _flush_search_state(run_dir, idx)
                desc_label = (job.title or job.company or job.job_id)[:48]
                progress.update(task, advance=1, description=f"Scoring {desc_label}")

    if workspace_obj is not None and search_id is not None:
        workspace_obj.finish_search(
            search_id,
            fetched=len(records) + duplicate_count,
            new_jobs=new_count,
            duplicate_jobs=duplicate_count,
        )

    console.print(
        f"[green]Wrote[/green] {jobs_path}\n"
        f"[green]Wrote[/green] {csv_path} "
        "[dim](import into Google Sheets via File → Import)[/dim]"
    )

    if score:
        scored_ok = sum(1 for r in records if r.fit is not None)
        scored_failed = sum(1 for r in records if r.error)
        console.print(
            f"[dim]Scored {scored_ok}/{len(records)} jobs"
            + (f" ({scored_failed} failed)" if scored_failed else "")
            + ".[/dim]"
        )
        _print_top_matches(records)
    if workspace_obj is not None:
        total = workspace_obj.count()
        console.print(
            f"[dim]Workspace now contains {total} job{'s' if total != 1 else ''} "
            f"({new_count} added this search, {duplicate_count} duplicates skipped).[/dim]"
        )
    console.print("[green]Finished search.[/green]")


@app.command()
def resume(
    run_name: str = typer.Argument(..., help="Run folder name, e.g. run-20260101-120000"),
    output_dir: str | None = typer.Option(None, "--output-dir", "-o"),
    reset_checkpoint: bool = typer.Option(
        True,
        "--reset-checkpoint/--keep-checkpoint",
        help="Rebuild queue from meta.json (recommended after failures).",
    ),
    no_pdf: bool | None = typer.Option(None, "--no-pdf"),
    with_networking: bool | None = typer.Option(None, "--with-networking"),
) -> None:
    """Resume a run using meta.json (skips search). Rebuilds queue via ledger dedupe."""
    load_dotenv_if_present()
    root = Path.cwd()
    cfg = load_config(root)
    apply_latex_api_env(cfg)
    run_dir = root / (output_dir or cfg.output_dir) / run_name
    if not run_dir.is_dir():
        console.print(f"[red]Unknown run:[/red] {run_dir}")
        raise typer.Exit(1)
    ck = run_dir / "checkpoint.sqlite"
    if reset_checkpoint and ck.is_file():
        ck.unlink()
        console.print("[dim]Removed old checkpoint.sqlite[/dim]")
    ledger_path = _ledger_db_path(cfg)
    initial = bootstrap_resume_state(run_dir, ledger_path)
    if no_pdf is not None:
        initial["no_pdf"] = no_pdf
    if with_networking is not None:
        initial["with_networking"] = with_networking
    run_id = initial["run_id"]
    _validate_keys(cfg, str(initial["provider"]))
    profile_str = str(initial.get("profile_path") or "")
    if profile_str:
        _report_profile_issues(
            validate_profile_path(Path(profile_str)),
            profile_path=Path(profile_str),
            context="before this resume",
        )
    console.print(f"[bold]Resume[/bold] {run_id} → {run_dir}")
    final_state = run_pipeline(
        initial, run_dir=run_dir, run_id=run_id, show_progress=True, console=console
    )
    n_searched = len(final_state.get("jobs_raw") or [])
    _print_run_summary(run_dir, n_searched)
    console.print("[green]Finished resume.[/green]")


def _validate_jd_path(raw: str) -> Path | None:
    """Parse a JD path argument the way ``_validate_resume_path`` does.

    Strips quotes, expands ``~``, resolves the absolute path, and
    rejects unreadable / wrong-extension files with a friendly message
    so the CLI doesn't drop a stack trace on the user.
    """
    text = raw.strip().strip('"').strip("'")
    if not text:
        return None
    expanded = Path(text).expanduser().resolve()
    if not expanded.is_file():
        console.print(f"[red]Not a file:[/red] {expanded}")
        return None
    if expanded.suffix.lower() not in TAILOR_INPUT_SUFFIXES:
        console.print(
            f"[red]Unsupported job description format[/red] '{expanded.suffix}'. "
            f"Use one of: {', '.join(TAILOR_INPUT_SUFFIXES)}.",
        )
        return None
    return expanded


def _print_tailor_summary(
    *,
    job_dir: Path,
    artifacts: dict[str, Path | None],
    email_to: str | None,
) -> None:
    """Pretty-print the artifacts produced by ``jobapply tailor``."""
    table = Table(title=f"Tailored artifacts → {job_dir}", show_header=True)
    table.add_column("Artifact", style="bold")
    table.add_column("Path")
    rows: list[tuple[str, Path | None]] = [
        ("resume.md", artifacts.get("resume_md")),
        ("resume.pdf", artifacts.get("resume_pdf")),
        ("resume.tex", artifacts.get("resume_tex")),
        ("resume (LaTeX PDF)", artifacts.get("resume_latex_pdf")),
        ("cover_letter.md", artifacts.get("cover_md")),
        ("cover_letter.pdf", artifacts.get("cover_pdf")),
        ("cover_letter.tex", artifacts.get("cover_tex")),
        ("cover_letter (LaTeX PDF)", artifacts.get("cover_latex_pdf")),
        ("email.txt", artifacts.get("email_path")),
    ]
    for label, path in rows:
        if path is not None:
            table.add_row(label, str(path))
    console.print(table)
    if email_to:
        console.print(
            f"[dim]Email drafted for {email_to} — copy "
            f"{artifacts.get('email_path')} into your mail client.[/dim]"
        )


@app.command()
def tailor(
    job_description: str | None = typer.Option(
        None,
        "--job",
        "-j",
        help=(
            "Path to a job description "
            f"({', '.join(JD_SUPPORTED_SUFFIXES)}) OR a saved per-job "
            "JSON written by `jobapply search` / `jobapply run` "
            "(e.g. output/search-<ts>/jobs/<slug>/job.json). "
            "The configured LLM tailors your resume + cover letter "
            "to it; passing a saved JSON skips the LLM JD-parser call."
        ),
    ),
    profile_path: str | None = typer.Option(
        None,
        "--profile",
        help="Override jobapply.toml `profile_path` (must point at profile.json).",
    ),
    output_dir: str | None = typer.Option(
        None,
        "--output-dir",
        "-o",
        help="Override jobapply.toml `output_dir`. Artifacts land in <output>/tailor-<ts>/.",
    ),
    title_override: str | None = typer.Option(
        None,
        "--title",
        help="Skip JD title detection and force this title (slug + agents use it).",
    ),
    company_override: str | None = typer.Option(
        None,
        "--company",
        help="Skip JD company detection and force this company.",
    ),
    location_override: str | None = typer.Option(
        None,
        "--location",
        "-l",
        help="Optional location override (purely informational).",
    ),
    skills: str | None = typer.Option(
        None,
        "--skills",
        "-s",
        help="Comma-separated skills to bias the tailor towards (in addition to the JD).",
    ),
    provider: str | None = typer.Option(
        None,
        "--provider",
        help=(
            "Pick one of the providers configured in jobapply.toml "
            "(gemini | anthropic | openai | ollama | cloudflare | openrouter). "
            "Defaults to the active `provider` field."
        ),
    ),
    model: str | None = typer.Option(
        None,
        "--model",
        help=(
            "Override the model id for this tailor run. Defaults to the "
            "chosen provider's `[providers.<name>].model` value."
        ),
    ),
    no_pdf: bool = typer.Option(False, "--no-pdf", help="Skip PDF rendering."),
    with_email: bool = typer.Option(
        False,
        "--with-email",
        help="Also draft a ready-to-paste application email (requires --email-to or prompt).",
    ),
    email_to: str | None = typer.Option(
        None,
        "--email-to",
        help="Recipient email address for the drafted application email.",
    ),
    email_context: str | None = typer.Option(
        None,
        "--email-context",
        help=(
            "Optional extra context for the drafted email "
            "(e.g. referrals, availability, prior contact)."
        ),
    ),
    yes: bool = typer.Option(False, "--yes", "-y", help="Non-interactive defaults."),
) -> None:
    """Tailor your resume + cover letter for a single job description.

    Pass a JD file with ``--job <path>`` and the configured LLM produces a
    tailored ``resume.md`` / ``resume.pdf`` (plus a styled LaTeX PDF) and
    matching ``cover_letter.*`` files under ``<output_dir>/tailor-<ts>/``.

    ``--job`` also accepts a saved per-job JSON written by
    ``jobapply search`` / ``jobapply run`` (the
    ``output/search-<ts>/jobs/<slug>/job.json`` files): pick a row from
    the search CSV, point ``tailor`` at its JSON, and the title /
    company / location come straight from the search result with no
    LLM JD-parser call.

    Add ``--with-email`` to also produce a ready-to-paste application
    email — the recipient address is required (``--email-to`` or
    interactive prompt). ``--email-context`` lets you weave in
    referrals, availability, or any prior contact you've had with the
    recruiter so the draft sounds personal rather than generic.
    """
    load_dotenv_if_present()
    root = Path.cwd()
    cfg = load_config(root)
    apply_latex_api_env(cfg)

    if not job_description and not yes:
        job_description = questionary.text(
            "Path to the job description "
            f"({'/'.join(JD_SUPPORTED_SUFFIXES)}) or a saved jobs/<slug>/job.json",
            default="",
        ).ask()
    if not job_description:
        console.print(
            "[red]A job description path is required.[/red] "
            "Pass --job <path> or rerun without --yes for the prompt.",
        )
        raise typer.Exit(1)
    jd_path = _validate_jd_path(job_description)
    if jd_path is None:
        raise typer.Exit(1)

    # Peek at the JD body BEFORE the email prompts so we can pre-fill
    # `--email-to` / `--email-context` with the recipient + subject
    # the recruiter embedded in the description (e.g. PwC's "Send
    # your resume to kirthana.xx.tpr@pwc.com / Subject: Job
    # Application — Skillset"). Failure is silent; we fall back to
    # the original prompt behaviour.
    detected_hints = peek_application_hints(jd_path) if with_email else None

    if with_email and detected_hints and detected_hints.has_any:
        bits: list[str] = []
        if detected_hints.primary_email:
            bits.append(f"recipient [bold]{detected_hints.primary_email}[/bold]")
        if detected_hints.subject_line:
            bits.append(f"subject '[bold]{detected_hints.subject_line}[/bold]'")
        if bits:
            console.print(
                "[dim]Detected from JD:[/dim] "
                + ", ".join(bits)
                + " [dim](override with --email-to / --email-context)[/dim]"
            )

    if with_email and not email_to:
        if detected_hints and detected_hints.primary_email:
            if yes:
                email_to = detected_hints.primary_email
            else:
                email_to = questionary.text(
                    "Recipient email address for the application email",
                    default=detected_hints.primary_email,
                ).ask()
        elif not yes:
            email_to = questionary.text(
                "Recipient email address for the application email",
                default="",
            ).ask()
    if with_email and not (email_to or "").strip():
        console.print(
            "[red]--with-email requires --email-to[/red] "
            "(or rerun without --yes so we can prompt for it).",
        )
        raise typer.Exit(1)
    if with_email and email_context is None:
        suggested = hints_to_email_context(detected_hints) if detected_hints else ""
        if yes:
            email_context = suggested
        else:
            ctx_answer = questionary.text(
                "Any extra context for the email? (referrals, availability, etc.; blank to skip)",
                default=suggested,
            ).ask()
            email_context = ctx_answer if ctx_answer is not None else ""

    prov, mdl = _resolve_provider_and_model(cfg, provider=provider, model=model, yes=yes)
    _validate_keys(cfg, prov)

    effective_profile = profile_path or cfg.profile_path
    effective_output = output_dir or cfg.output_dir
    prof = Path(effective_profile)
    if not prof.is_absolute():
        prof = root / prof
    try:
        profile = load_profile(prof)
    except ProfileLoadError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(1) from exc
    profile_text = profile_to_text(profile)
    canonical_skills = profile_skill_list(profile)
    _report_profile_issues(
        validate_profile(profile),
        profile_path=prof,
        context="before this tailor run",
    )

    skill_list = [x.strip() for x in (skills or "").split(",") if x.strip()]
    output_root = root / effective_output / f"tailor-{datetime.now(UTC).strftime('%Y%m%d-%H%M%S')}"
    output_root.mkdir(parents=True, exist_ok=True)

    backend = probe_md_pdf_backend() or "none"
    tex_backend = probe_tex_pdf_backend() or "none"
    console.print(
        f"[bold]Tailor[/bold] {jd_path.name} → {output_root}\n"
        f"[dim]profile={prof}  provider={prov}  model={mdl}[/dim]\n"
        f"[dim]Markdown PDF backend:[/dim] {backend}  "
        f"[dim]LaTeX PDF backend:[/dim] {tex_backend}",
    )

    try:
        llm = create_chat_model(prov, mdl, cfg=cfg)
    except RuntimeError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(1) from exc

    email_request: TailorEmailRequest | None = None
    if with_email:
        email_request = TailorEmailRequest(
            recipient=(email_to or "").strip(),
            additional_info=(email_context or "").strip(),
        )

    try:
        outputs = tailor_for_job_description(
            llm,
            jd_path=jd_path,
            profile_text=profile_text,
            profile_skills=canonical_skills,
            output_root=output_root,
            target_skills=skill_list,
            title_override=title_override,
            company_override=company_override,
            location_override=location_override,
            no_pdf=no_pdf,
            email=email_request,
            profile=profile,
        )
    except JobDescriptionReadError as exc:
        console.print(f"[red]Job description error:[/red] {exc}")
        raise typer.Exit(1) from exc

    artifacts: dict[str, Path | None] = {
        "resume_md": outputs.resume_md,
        "resume_pdf": outputs.resume_pdf,
        "resume_tex": outputs.resume_tex,
        "resume_latex_pdf": outputs.resume_latex_pdf,
        "cover_md": outputs.cover_md,
        "cover_pdf": outputs.cover_pdf,
        "cover_tex": outputs.cover_tex,
        "cover_latex_pdf": outputs.cover_latex_pdf,
        "email_path": outputs.email_path,
    }
    _print_tailor_summary(
        job_dir=outputs.job_dir,
        artifacts=artifacts,
        email_to=outputs.email.to if outputs.email else None,
    )
    console.print(f"[green]Tailored[/green] '{outputs.job.title}' at {outputs.job.company}.")


def main() -> None:
    app()


if __name__ == "__main__":
    main()
