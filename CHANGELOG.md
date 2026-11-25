# Changelog

All notable changes to JobApply are documented in this file.
The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Security
- **Remote LaTeX compilation is now opt-in.** `[latex_api] enabled` defaults
  to `false`, and `render.tex_to_pdf` treats an unset
  `JOBAPPLY_LATEX_API_DISABLE` as disabled, so tailored resumes are no longer
  uploaded to `latex.ytotech.com` unless you choose to. `jobapply init` asks.
  **Upgrading:** if you relied on the remote service and your
  `jobapply.toml` has no explicit `[latex_api] enabled = true`, add it (or
  install `tectonic`).
- **Web UI API hardened against malicious websites.** `jobapply ui` now
  rejects requests whose `Host` header isn't `127.0.0.1`/`localhost`
  (blocks DNS rebinding), and POST/PUT/DELETE under `/api/` require an
  `X-JobApply-Client` header plus a same-origin (or Vite dev) `Origin`
  (blocks CSRF, e.g. a page silently starting a paid `/api/run`). Binding
  `--host 0.0.0.0` disables the Host check and prints a warning.
- **Local `pdflatex` compiles run sandboxed.** `-no-shell-escape` plus
  kpathsea paranoid mode (`openin_any=p`, `openout_any=p`, `TEXMFOUTPUT` =
  job dir), so a `.tex` edited in the browser can no longer `\input` files
  from elsewhere on disk into the PDF.

### Added
- Jobs dashboard sorting by fit or title, visible-result batch selection,
  status counts, and controls to clear filters and selection.
- Retry feedback for failed job loading and inline batch tailoring errors.
- Product Hunt launch kit, marketing assets, and a VHS tape script for the
  terminal demo (`assets/`).
- `CODE_OF_CONDUCT.md`, `SECURITY.md`, GitHub issue templates, and a pull
  request template.
- README hero image, gallery screenshots, and badges.

### Changed
- Project badges, package metadata, and issue template links now point to
  `romit-23/jobapply`.
- Tightened `.gitignore` so personal artifacts (`Profile_Updated.md`,
  `LinkedInProfile*`, `*-test.pdf`, etc.) cannot be accidentally committed.

## [0.1.0] - Unreleased

- Initial public release: JobSpy search, LangGraph pipeline, ledger dedupe,
  SQLite checkpoints, structured `profile.json`, Gemini / Anthropic / OpenAI
  / Ollama / Cloudflare Workers AI providers, Markdown + LaTeX PDF export
  (with remote `latex-on-http` default), CSV export sorted by fit score,
  and `jobapply resume` to continue from a previous run's `meta.json`.
