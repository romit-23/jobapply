# JobApply: System Design

This document explains how JobApply works, based on the code in this repository.
Every claim points to the file or function that implements it. Anything that could
not be confirmed from the code is marked **Not confirmed**.

---

## 1. What the system does

JobApply does two jobs:

1. **Find jobs.** It scrapes public job boards (Indeed, LinkedIn, Google Jobs, and others) through the open-source **JobSpy** library.
2. **Tailor applications.** For each job, it uses an LLM (Gemini, Claude, GPT, and others) to rewrite your resume bullets, write a cover letter, and optionally draft an email. It then renders everything as Markdown, LaTeX, and PDF.

It runs **entirely on your machine**, as a Python app with two front doors:

| Front door | Entry point | Who uses it |
|---|---|---|
| CLI | `jobapply` command → `jobapply/cli.py` (Typer app, `main()`) | Terminal users |
| Web UI | `jobapply ui` → `jobapply/server.py` (FastAPI) + `web/` (React) | Browser users |

Both front doors share the same core modules (agents, rendering, storage).

---

## 2. High-level architecture

```
┌──────────────────────────────────────────────────────────────────────────┐
│                              USER'S MACHINE                              │
│                                                                          │
│  ┌────────────┐      ┌─────────────────────┐                             │
│  │  Terminal  │────► │  cli.py (Typer)     │──┐                          │
│  └────────────┘      └─────────────────────┘  │                          │
│                                               │                          │
│  ┌────────────┐ HTTP ┌─────────────────────┐  │   ┌──────────────────┐   │
│  │  Browser   │────► │  server.py (FastAPI)│──┼──►│  CORE            │   │
│  │  React SPA │◄──── │  + tasks.py threads │  │   │  agents/*        │   │
│  │  (web/)    │ JSON └─────────────────────┘  │   │  graph.py (CLI)  │   │
│  └────────────┘                               │   │  tailor_one.py   │   │
│                                               │   │  nodes/render.py │   │
│                                               └──►│  llm.py          │   │
│                                                   └────────┬─────────┘   │
│                                                            │             │
│  ┌──────────── LOCAL STORAGE ──────────────────────────────▼──────────┐  │
│  │ jobapply.toml, .env, profile.json                                  │  │
│  │ .jobapply/ledger.db       (CLI dedupe ledger)                      │  │
│  │ output/web/workspace.db   (UI job catalog)                         │  │
│  │ output/run-*/checkpoint.sqlite, meta.json, jobs.json, jobs.csv     │  │
│  │ output/**/jobs/<slug>/    (resume/cover .md .tex .pdf)             │  │
│  └────────────────────────────────────────────────────────────────────┘  │
└───────────────┬─────────────────────────┬─────────────────────┬──────────┘
                │                         │                     │
                ▼                         ▼                     ▼
     ┌────────────────────┐   ┌──────────────────────┐  ┌──────────────────┐
     │ Job boards         │   │ LLM provider API     │  │ latex-on-http    │
     │ (via JobSpy)       │   │ Gemini / Anthropic / │  │ (remote LaTeX →  │
     │ Indeed, LinkedIn,  │   │ OpenAI / Ollama /    │  │  PDF compiler)   │
     │ Google, ...        │   │ Cloudflare / OpenRtr │  │                  │
     └────────────────────┘   └──────────────────────┘  └──────────────────┘
```

---

## 3. Major components

| Component | File(s) | Responsibility |
|---|---|---|
| CLI | `jobapply/cli.py` | Commands: `init`, `config`, `ui`, `workspace`, `list`, `run`, `search`, `resume`, `tailor` |
| Web API | `jobapply/server.py` | FastAPI app factory `create_app()`, REST endpoints, serves the built React bundle |
| Background tasks | `jobapply/tasks.py` | `TaskManager`: runs long jobs (search, tailor) on daemon threads, tracks progress in memory |
| Frontend | `web/src/` | React + TanStack Query SPA (Dashboard, JobDetail, Freeform, Searches, ArtifactEditor) |
| Config | `jobapply/config.py`, `config_writer.py` | Loads `jobapply.toml` and `.env`, resolves API keys and model names |
| LLM factory | `jobapply/llm.py` | `create_chat_model(provider, model, cfg)` returns a LangChain chat model |
| Structured LLM calls | `jobapply/agents/_structured.py` | `invoke_structured()`: forces JSON-schema output, with repair and retry |
| Job search | `jobapply/agents/search.py` | `iter_search_jobs()` / `search_jobs()`: JobSpy wrapper |
| JD hint extraction | `jobapply/jd_extract.py` | Regex extraction of recruiter email, subject line, and instructions (no LLM) |
| JD backfill | `jobapply/jd_backfill.py` | `fetch_description_from_url()`: scrapes a LinkedIn job page when a description is missing |
| AI agents | `jobapply/agents/*.py` | `fit_scorer`, `resume_tailor`, `cover_letter`, `email_drafter`, `networking`, `jd_parser` |
| CLI pipeline | `jobapply/graph.py`, `graph_nodes.py`, `graph_state.py` | LangGraph state machine: search → dedupe → process_one (loop) |
| Single-job pipeline | `jobapply/tailor_one.py` | `tailor_for_job_description()`, used by `jobapply tailor` and the UI |
| Rendering | `jobapply/nodes/render.py`, `templates/` | Markdown, LaTeX, and PDF generation |
| Persistence | `jobapply/ledger.py`, `workspace.py`, `nodes/persist.py`, `run_meta.py` | SQLite databases, JSON and CSV outputs |
| Profile | `jobapply/profile.py`, `profile_import.py`, `profile_validation.py` | `profile.json` schema, resume import, validation |

---

## 4. Sources of truth

This is the most important section. Each kind of data has exactly one owner.

| Data | Source of truth | Derived copies (not authoritative) |
|---|---|---|
| **Job listings** | Live job boards, fetched by JobSpy at search time | `RawJob` objects, `job.json`, `jobs.json`, `jobs.csv`, DB rows |
| **Candidate facts** (name, roles, dates, schools, skills) | `profile.json` | `profile_text` (Markdown made by `profile_to_text()`), tailored resumes |
| **Settings & provider choice** | `jobapply.toml` | Resolved `AppConfig` in memory |
| **Secrets (API keys)** | `jobapply.toml` `api_key` value, or env vars / `.env` | None |
| **"Already processed?" (CLI)** | `.jobapply/ledger.db` → table `jobs_ledger` | `cached` entries in `jobs.json` |
| **Job catalog (UI and `--workspace`)** | `output/web/workspace.db` → tables `workspace_jobs`, `workspace_searches` | `output/web/jobs.json`, `jobs.csv` (rebuilt by `Workspace.flush_files()`) |
| **CLI run progress** | `output/run-*/checkpoint.sqlite` (LangGraph checkpointer) | Console progress |
| **CLI run inputs (for resume)** | `output/run-*/meta.json` | — |
| **Background task status (UI)** | In-memory `TaskManager` in `tasks.py` | Polled by the browser. **Lost when the server restarts.** |

### 4.1 Source of truth for finding jobs

**Where jobs come from:** the only job source in the code is the **JobSpy** library (`python-jobspy` in `pyproject.toml`). It is called in one place: `_scrape_once()` in `jobapply/agents/search.py`, which calls `jobspy.scrape_jobs(...)`.

- **No official job-board APIs are used.** JobSpy scrapes public job-board pages. The app does not use any API key for job boards.
- **Which sites:** set by `sites` in `jobapply.toml`. The default in `AppConfig` (`config.py`) is `["indeed", "linkedin", "google"]`. The README says JobSpy also supports ZipRecruiter and Glassdoor; you enable them through the same `sites` list. **Not confirmed in code:** the exact site names JobSpy accepts. That list lives in the JobSpy library, not in this repo.
- **Job descriptions:** JobSpy returns a `description` field. For LinkedIn, the full description needs an extra page request, so the app passes `linkedin_fetch_description=True` by default (`JobSearchInput.linkedin_fetch_description`). This makes LinkedIn searches slower, but without it LinkedIn descriptions come back empty.
- **Missing descriptions later:** `jobapply tailor --job <job.json>` can recover a missing description by scraping the job URL (`jd_backfill.fetch_description_from_url()`, called from `tailor_one._load_job_from_json()`).
- **Manual source:** the user can also paste a job description (UI "Paste JD" → `POST /api/freeform`, or `jobapply tailor --jd file`). In that case the pasted text is the source of truth, and the LLM agent `parse_job_description()` pulls out title, company, and location.

**Known behavior seen in practice:** JobSpy sometimes logs `JobSpy:Google - Google returned no job data`. The search continues with the other sites.

---

## 5. Job search flow (fetch → process → filter → store → show)

### 5.1 Fetch

`iter_search_jobs(inp: JobSearchInput)` in `agents/search.py`:

```
For each title in inp.titles:
    search_term = "<title> <skill1> <skill2> ..."      ← skills are appended to the query
    per_title   = max(5, min(results_wanted, 200 // number_of_titles))
    rows = _scrape_once(site_name, search_term, location,
                        results_wanted=per_title, hours_old, is_remote,
                        linkedin_fetch_description)
         └─ wrapped in tenacity @retry: 3 attempts, exponential wait 2–30s
```

Search inputs (`JobSearchInput` in `models.py`):

| Field | Meaning | Default |
|---|---|---|
| `titles` | Job titles to search (at least 1) | required |
| `skills` | Added to the search query; also used by the fit scorer | `[]` |
| `location` | City or country | none |
| `remote` | Remote-only filter passed to JobSpy | `false` |
| `results_wanted` | Maximum unique jobs to return (1–500) | 30 |
| `hours_old` | Maximum posting age in hours | 720 (30 days) |
| `site_names` | Which boards to scrape | from config |

### 5.2 Process (normalize each row)

For each row JobSpy returns:

1. **Map fields:** `title`, `company`, `location`, `site`, `job_url`, and `apply_url` (`job_url_apply`, falling back to `job_url`).
2. **Build a stable ID:** `utils.stable_job_id()` = first 32 hex characters of `SHA-256(site | company | title | location | apply_url | job_url)`, after whitespace normalization. The same posting gets the same ID across runs, and this ID is the key for every deduplication step.
3. **Dedupe within the search:** a `seen` set drops repeat IDs (for example, the same job found under two titles).
4. **Extract application hints (no LLM):** `jd_extract.extract_application_hints(description)` uses regex to find recruiter emails, a requested email subject line, and application instructions. The result is stored as `RawJob.application` and shown on job cards (the yellow email address in the UI).
5. **Build a `RawJob`** (`models.py`), which also keeps the original JobSpy row in `raw`.
6. **Stop** once `results_wanted` unique jobs have been yielded.

### 5.3 Filter

Filtering happens in layers:

| Filter | Where | Rule |
|---|---|---|
| Age | JobSpy (`hours_old`) | Drop postings older than N hours |
| Remote | JobSpy (`is_remote`) | Remote-only when set |
| Same-search duplicates | `iter_search_jobs` `seen` set | Same `job_id` is skipped |
| Already in UI workspace | `Workspace.is_seen(job_id)` | Counted as "duplicate" (UI search, CLI `--workspace`) |
| Already processed (CLI) | `ledger.should_skip()` | Skip if the ledger row has the same `job_id` and the same **profile hash**, with status `done` or `skipped` |
| Low fit (CLI `run`) | `graph_nodes.process_one_node` | If `fit.score < min_fit` (default 0.35), the job is marked `skipped` and not tailored |
| Low fit (UI) | — | **The UI does not apply `min_fit`.** It only shows a score when "score" is ticked on the search form. The user picks which jobs to tailor. |

`--force` (CLI) or `force: true` (UI) bypasses the dedupe filters.

**Profile hash:** `utils.profile_hash(profile_text)` = first 32 hex characters of the SHA-256 of the profile text. If you edit `profile.json`, the hash changes, so the CLI ledger treats every job as new for the updated profile.

### 5.4 Store

There are two storage paths, depending on the front door.

**CLI `jobapply run` (default mode):**

```
output/run-YYYYMMDD-HHMMSS/
├── meta.json            ← search input + all fetched jobs (run_meta.write_meta)
├── checkpoint.sqlite    ← LangGraph state after every node
├── jobs.json            ← JobsIndex: one JobRecord per job (persist.upsert_job_record)
├── jobs.csv             ← spreadsheet view (persist.write_jobs_csv)
└── jobs/<slug>/         ← per-job folder (slug = utils.slugify(title, company, job_id))
    ├── job.json
    ├── resume.md / resume.tex / resume.pdf
    └── cover_letter.md / cover_letter.tex / cover_letter.pdf
.jobapply/ledger.db      ← jobs_ledger table, shared by all CLI runs in this folder
```

**UI (and CLI with `--workspace <dir>`):**

```
output/web/                   ← DEFAULT_WEB_WORKSPACE in server.py
├── workspace.db              ← SQLite: workspace_jobs + workspace_searches
├── jobs.json, jobs.csv       ← rebuilt from the DB by Workspace.flush_files()
├── meta.json
└── jobs/<slug>/              ← same per-job artifact folders
```

`workspace_jobs` stores the **full `JobRecord` as JSON** in `record_json`, plus a few plain columns (`title`, `company`, `status`, `fit_score`, ...) for easy SQL filtering. `workspace_searches` stores one row per search or run, with counts of fetched, new, and duplicate jobs.

### 5.5 Show

- **UI:** `GET /api/jobs` reads `workspace.db` and returns all `JobRecord`s. `web/src/pages/Dashboard.tsx` filters (All / Pending / Tailored / Low fit / Failed), searches text, and sorts (highest fit, title, workspace order) **in the browser**.
- **CLI:** prints a summary table (`_print_run_summary`, `_print_top_matches`) and writes `jobs.csv` for spreadsheets.

---

## 6. Job processing flows

There are **three processing paths**. They share the same agents but differ in orchestration.

### 6.1 CLI `jobapply run`: LangGraph pipeline

Built in `graph.build_workflow()`, compiled with a SQLite checkpointer in `graph.compile_app()`.

```
             ┌───────┐
             │ START │
             └───┬───┘
                 │ route_start(): skip_search set? (true for `jobapply resume`)
          ┌──────┴──────┐
         no            yes
          ▼             │
   ┌──────────────┐     │
   │ search_node  │     │   search_jobs() → jobs_raw; writes meta.json
   └──────┬───────┘     │
          ▼             ▼
   ┌───────────────────────┐
   │ dedupe_node           │  workspace.is_seen / ledger.should_skip
   │                       │  → cached records written to jobs.json
   │                       │  → new jobs: ledger.upsert_pending + queue
   └──────────┬────────────┘
              │ route_after_dedupe(): queue empty?
        ┌─────┴─────┐
       yes         no
        ▼           ▼
     ┌─────┐  ┌───────────────────┐
     │ END │◄─┤ process_one_node  │◄──┐  takes queue[0], returns queue[1:]
     └─────┘  └─────────┬─────────┘   │
                        └─────────────┘  route_after_process(): loop while queue not empty
```

`process_one_node` (in `graph_nodes.py`) does this for **one** job:

```
1. llm = create_chat_model(provider, model)
2. fit = score_fit(llm, profile_text, job, skills)              ← LLM call #1
   └─ if fit.score < min_fit → status=skipped, return
3. resume = tailor_resume(llm, profile, job, ...)               ← LLM call #2
   ledger status → tailored
4. cover  = write_cover_letter(llm, profile_text, job, resume)  ← LLM call #3
5. (optional --with-networking) write_networking(...)           ← LLM call #4
6. Write resume.md, cover_letter.md, resume.tex, cover_letter.tex
7. Unless --no-pdf: md_to_pdf(...) and tex_to_pdf(...)
8. JobRecord(status=done) → jobs.json, ledger (done + file paths), workspace (if set)
On any exception: JobRecord(status=failed, error=traceback); continue with next job
```

Important properties:

- **Sequential.** Jobs are processed one at a time. `concurrency` exists in `AppConfig` and is written to `jobapply.toml`, but **no code reads it to run jobs in parallel**. It is unused.
- **Crash-safe.** State is checkpointed after every node. `jobapply resume <run-dir>` rebuilds state from `meta.json` (`graph_nodes.bootstrap_resume_state`) with `skip_search=True`. By default it deletes `checkpoint.sqlite` and relies on the ledger to skip finished jobs.
- **One failure doesn't stop the run.** Errors are caught per job.

### 6.2 UI search and tailoring: background tasks

The UI does **not** use the LangGraph pipeline. It calls the same building blocks directly inside background tasks (`server.py` + `tasks.TaskManager`).

**Search** (`POST /api/search`):

```
create search row (ws.start_search)
for raw in iter_search_jobs(input):         ← streams results one by one
    if workspace already has job_id and not force → duplicate
    else:
        if "score" ticked → score_fit(...)   ← optional LLM call
        ws.upsert_job(record)               ← writes to workspace.db
        write jobs/<slug>/job.json
    update task label/percent
finally: ws.finish_search(counts); ws.flush_files() → jobs.json + jobs.csv
```

**Tailor one job** (`POST /api/jobs/{id}/tailor`) and **tailor selected** (`POST /api/run`):

```
_tailor_record(...) → tailor_one.tailor_for_job_description(...)
    1. tailor_resume(...)        ← LLM
    2. write_cover_letter(...)   ← LLM
    3. write .md and .tex files
    4. render PDFs (unless no_pdf)
    5. optional email draft
→ ws.upsert_job(status=done)  or  status=failed with error
```

There is **no fit-scoring step** in UI tailoring. `/api/run` skips jobs already `done` unless `force` is set.

### 6.3 Single job description: `jobapply tailor` and UI "Paste JD"

`tailor_one.tailor_for_job_description()`:

```
Input: a JD file (.md/.txt/.docx/.pdf), a job.json from a previous run, or pasted text
  ├─ job.json with empty description → jd_backfill scrapes the job URL
  ├─ title/company unknown → parse_job_description(llm, text)   ← LLM
  ▼
tailor_resume → write_cover_letter → render files/PDFs → (optional) draft_application_email
```

---

## 7. AI / LLM processing

### 7.1 Provider selection

`llm.create_chat_model(provider, model, cfg)` maps the provider name to a LangChain class:

| Provider | LangChain class | Key source (in order) |
|---|---|---|
| `gemini` | `ChatGoogleGenerativeAI` | toml `api_key` → `GOOGLE_API_KEY` → `GEMINI_API_KEY` |
| `anthropic` | `ChatAnthropic` | toml → `ANTHROPIC_API_KEY` |
| `openai` | `ChatOpenAI` | toml → `OPENAI_API_KEY` (custom `base_url` supported) |
| `ollama` | `ChatOllama` | none (local server, default `http://127.0.0.1:11434`) |
| `openrouter` | `ChatOpenAI` + OpenRouter URL | toml → `OPENROUTER_API_KEY` |
| `cloudflare` | `ChatOpenAI` + Workers AI URL | toml → `CLOUDFLARE_API_TOKEN`, plus `account_id` |

Key resolution is in `config.get_api_key()`. A toml value of `env:VAR_NAME` is read from the environment. `.env` is loaded by `config.load_dotenv_if_present()` and never overrides variables that are already set. Only **one** provider is needed. The others are alternatives.

### 7.2 Structured output

Every agent calls `invoke_structured(llm, Schema, messages)` (`agents/_structured.py`):

1. Calls `llm.with_structured_output(Schema)`, so the model must return JSON that matches a Pydantic schema.
2. If validation fails, it tries to repair common shape mistakes locally (`_try_recover`).
3. Otherwise it re-asks the model with a corrective message, up to 2 extra attempts.

This retry is for **bad JSON only**. Network and server errors (such as 503) are retried by the provider SDK. For Gemini, `langchain_google_genai` defaults to `max_retries=6` with exponential backoff (confirmed in the installed package, not in this repo).

### 7.3 Agents and what each one sends to the LLM

| Agent | Function | Output schema | Input sent to the LLM |
|---|---|---|---|
| Fit scorer | `fit_scorer.score_fit` | `FitScore` (score 0–1, rationale, missing keywords) | JD (≤12k chars) + profile (≤16k) + skills |
| Resume tailor | `resume_tailor.tailor_resume` | `_TailoredBullets` (summary, reordered skills, bullets) | JD (≤12k) + profile (≤20k) + role/project index |
| Cover letter | `cover_letter.write_cover_letter` | `CoverLetter` (header, opening, body, closing) | JD (≤10k) + profile + tailored resume |
| Email drafter | `email_drafter.draft_application_email` | `EmailDraft` (to, subject, body) | JD + profile + optional resume/cover + recipient |
| Networking | `networking.write_networking` | `OutreachMessages` | JD + profile |
| JD parser | `jd_parser.parse_job_description` | `JobDescriptionMeta` (title, company, location) | Pasted JD (≤12k) |
| Profile import | `profile_import.llm_extract_profile` | `Profile` | Raw resume text |

### 7.4 Truthfulness guard in resume tailoring

The resume tailor is designed so the **LLM cannot change facts**. In `_tailor_with_profile()`:

- **From `profile.json`, copied as-is (no LLM):** name, contact info, company names, roles, dates, project names, education.
- **From the LLM:** only the summary, the skill **order**, and **rephrased** bullets.
- **Safety nets:** `merge_skills_preserving_order` adds back any skill the LLM dropped. `_pair_experience` / `_pair_projects` fall back to the original bullets if the LLM returns the wrong number of lists.
- **The prompt also says:** "Do not invent companies, roles, dates, schools, degrees, GPAs, locations, technologies, or metrics."

---

## 8. Rendering (documents and PDFs)

`nodes/render.py`:

| Output | Function | How |
|---|---|---|
| `resume.md` | `render_resume_markdown()` | Jinja template `templates/resume.md.j2` |
| `cover_letter.md` | `render_cover_markdown()` | `templates/cover_letter.md.j2` |
| `resume.tex` | `fill_resume_tex()` | Fills `templates/resume.tex` (LaTeX-escaped) |
| `cover_letter.tex` | `fill_cover_letter_tex()` | Fills `templates/cover_letter.tex` |
| Markdown → PDF | `md_to_pdf()` | Fallback chain: **pandoc → weasyprint → fpdf2** (fpdf2 always works) |
| LaTeX → PDF | `tex_to_pdf()` | Fallback chain: **remote latex-on-http API → tectonic → pdflatex** |

The remote LaTeX API (`config.DEFAULT_LATEX_API_URL` = `https://latex.ytotech.com/builds/sync`) receives the **full `.tex` source**, which contains your name, contact details, and work history. You can turn it off (`[latex_api] enabled = false` or `JOBAPPLY_LATEX_API_DISABLE=1`) or point it at a self-hosted instance.

---

## 9. Web API reference

All endpoints are defined in `create_app()` in `jobapply/server.py`.

| Method | Path | Purpose | Long task? |
|---|---|---|---|
| GET | `/api/status` | Workspace size, profile loaded, provider/model, sites | no |
| GET | `/api/profile` | Returns `profile.json` content | no |
| GET | `/api/providers` | Provider list + configured defaults (for the model picker) | no |
| GET | `/api/jobs` | All jobs in the workspace | no |
| GET | `/api/jobs/{id}` | One job | no |
| DELETE | `/api/jobs/{id}` | Remove a job | no |
| GET | `/api/jobs/{id}/artifacts/{name}` | Download/view an artifact (allow-listed names) | no |
| PUT | `/api/jobs/{id}/artifacts/{name}` | Save edited artifact text (only `EDITABLE_ARTIFACTS`) | no |
| POST | `/api/jobs/{id}/artifacts/{name}/compile` | Recompile an edited `.tex` to PDF | no |
| GET | `/api/searches` | Search/run history (`workspace_searches`) | no |
| GET | `/api/tasks` | Recent background tasks | no |
| GET | `/api/tasks/{id}` | One task's status, progress, and log | no |
| POST | `/api/tasks/{id}/cancel` | Cancel a running task | no |
| POST | `/api/search` | Start a job search | **yes** |
| POST | `/api/run` | Tailor a list of selected jobs | **yes** |
| POST | `/api/jobs/{id}/tailor` | Tailor one job | **yes** |
| POST | `/api/freeform` | Tailor a pasted JD | **yes** |
| POST | `/api/jobs/{id}/email` | Draft an application email | **yes** |
| GET | `/api/jobs/{id}/email-hint` | Pre-filled recipient/subject from `jd_extract` | no |
| GET | `/` and `/{path}` | Serves the React bundle from `jobapply/web_dist/` | no |

Long-task endpoints return a `TaskRecord` right away. The work runs on a daemon thread (`TaskManager.submit`), and the browser polls for the result.

---

## 10. Frontend-to-backend flow

```
Browser (React, web/src)                        Server (FastAPI)
─────────────────────────                       ─────────────────
SearchForm  ── POST /api/search ──────────────►  TaskManager.submit("search")
     │       ◄── { task_id, status: pending } ─      └─ thread: iter_search_jobs → workspace.db
     ▼
TaskProgress / useTaskPoll(task_id)
     │  every 1.2s: GET /api/tasks/{id} ──────►  returns status, %, label, log
     │  stops when status is succeeded / failed / cancelled
     ▼
on done → invalidate React Query cache ["jobs"]
     │
Dashboard ── GET /api/jobs ───────────────────►  workspace.db → JobRecord[]
     │
JobCard → "Details" → JobDetail ── POST /api/jobs/{id}/tailor ──► task ──► tailor_one
     │
ArtifactPreview / ArtifactEditor ── GET/PUT artifacts, POST compile
```

Polling (`web/src/lib/hooks.ts`):

| Hook | What it polls | Interval |
|---|---|---|
| `useTaskPoll(taskId)` | `GET /api/tasks/{id}` for one task | 1.2s, stops when the task finishes |
| `useActiveTasks()` (mounted in `Layout.tsx`) | `GET /api/tasks?limit=50` | 1.5s while any task is active, otherwise 5s (always on) |

When a tracked task finishes, the Layout-level `useActiveTasks({ invalidateOnFinish: true })` refreshes the `jobs`, `status`, and `searches` queries once.

---

## 11. Database schemas

### `ledger.db`: table `jobs_ledger` (`ledger.py`, CLI only)

| Column | Notes |
|---|---|
| `id` (PK) | `job_id` |
| `profile_hash` | Profile version that processed it |
| `status` | `pending` / `tailored` / `done` / `skipped` / `failed` |
| `site`, `company`, `title`, `location`, `apply_url`, `job_url` | Copies for inspection |
| `run_id` | Which run last touched it |
| `paths_json` | JSON map of artifact file paths |
| `created_at`, `updated_at` | Timestamps |

### `workspace.db`: table `workspace_jobs` (`workspace.py`)

| Column | Notes |
|---|---|
| `job_id` (PK) | Stable job ID |
| `title`, `company`, `location`, `site`, `job_url`, `apply_url` | For SQL filtering |
| `status`, `fit_score` | Indexed / quick view |
| `record_json` | **Full `JobRecord`**: the real data |
| `first_search_id`, `last_search_id` | Links to `workspace_searches` |
| `first_seen_at`, `last_seen_at`, `processed_at` | Timestamps |

### `workspace.db`: table `workspace_searches`

`id`, `command` ("search" or "run"), `search_input_json`, `provider`, `model`, `started_at`, `finished_at`, `fetched`, `new_jobs`, `duplicate_jobs`.

### Job status values (`models.LedgerStatus`)

`pending`, `tailored`, `rendered`, `done`, `skipped`, `failed`, `cached`. **Not confirmed:** `rendered` is defined but no code sets it.

---

## 12. External services

| Service | Used for | Data sent | Configured by |
|---|---|---|---|
| Job boards (via JobSpy scraping) | Finding jobs | Search terms, location | `sites`, search inputs |
| LinkedIn job pages (direct HTTP) | Description backfill | Job URL | automatic in `tailor` |
| LLM provider (one of 6) | Fit, tailoring, cover letter, email, profile import | **Resume/profile text + JD** | `jobapply.toml`, env |
| latex-on-http (`latex.ytotech.com` by default) | LaTeX → PDF | **Full resume/cover `.tex`** | `[latex_api]`, env |

---

## 13. One-time setup flow (`jobapply init`)

```
jobapply init --resume resume.pdf
  1. Interactive wizard → providers, keys (or env:VAR), models → writes jobapply.toml
  2. profile_import.extract_text_from_resume()
       .pdf → pypdf   .docx → python-docx   .md/.txt → read   .doc → error
  3. llm_extract_profile(llm, text) → Profile                    ← LLM call
  4. Save profile.json; profile_validation prints required/recommended gaps
```

`jobapply ui` refuses to start until both `jobapply.toml` and `profile.json` exist (`cli.ui_cmd`).

---

## 14. Error handling summary

| Failure | Where handled | Result |
|---|---|---|
| JobSpy network error | `@retry` on `_scrape_once` (3 tries) | Search fails if all 3 tries fail |
| Missing API key | `llm.create_chat_model` raises `RuntimeError` | Clear message naming the env var |
| LLM returns bad JSON | `invoke_structured` repair + 2 retries | Raises `ValidationError` after that |
| LLM server busy (503/429) | Provider SDK retries | Job marked `failed` with traceback |
| Any per-job error (CLI) | `process_one_node` try/except | Job `failed`; run continues |
| Any per-job error (UI batch) | `/api/run` loop try/except | Job `failed`; batch continues |
| PDF tool missing | Fallback chains in `render.py` | Next tool tried; fpdf2 is the last resort |
| CLI crash mid-run | LangGraph checkpoint + ledger | `jobapply resume <run-dir>` continues |
| UI server restart | — | **In-progress tasks and the task list are lost** (in memory only) |

---

## 15. Known limitations (from the code)

1. **`concurrency` setting is unused.** Processing is always sequential.
2. **UI tasks are in memory.** Restarting `jobapply ui` loses running tasks and history in `/api/tasks`. Job data is safe in `workspace.db`.
3. **The UI has no `min_fit` filter.** Only the CLI `run` auto-skips low-fit jobs.
4. **No fallback model.** If the configured model is overloaded (503), the task fails after the SDK's retries. It does not switch models.
5. **Default model names can go stale.** `DEFAULT_MODELS` in `config.py` (for example `gemini-2.0-flash`) can be retired by the provider.
6. **Privacy defaults.** The README says your resume stays local "unless you tell it to", but by default profile text goes to the chosen LLM provider and `.tex` files go to the public latex-on-http service.
7. **Scraping is fragile.** JobSpy depends on job-board HTML and anti-bot behavior, so results per site can be empty or change without notice.
