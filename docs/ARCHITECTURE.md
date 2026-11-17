# Job Application Agent — Architecture & Operations Guide

> **Audience:** humans and AI agents who need to understand, run, debug or extend this system.
> **Scope:** every component, how data flows between them, where state lives, and how to operate it.
> **Last verified against the code:** 2026-11-12 (incl. the `ats_board` board-registry rework). If you change behaviour, update this file.
> For the original design intent and long-term roadmap, see [`PLAN.md`](../PLAN.md).
> This document describes what the code **actually does today**.

---

## Table of contents

1. [What this system is](#1-what-this-system-is)
2. [Quick start](#2-quick-start)
3. [Architecture at a glance](#3-architecture-at-a-glance)
4. [Runtime environment (Docker)](#4-runtime-environment-docker)
5. [Repository layout](#5-repository-layout)
6. [Configuration](#6-configuration)
7. [Data model](#7-data-model)
8. [Stage 1 — Discovery](#8-stage-1--discovery)
9. [Stage 2 — Normalise, ATS detection & dedup](#9-stage-2--normalise-ats-detection--dedup)
10. [Stage 3 — Enrichment](#10-stage-3--enrichment)
11. [Stage 4 — Hard filters](#11-stage-4--hard-filters)
12. [Stage 5 — Scoring](#12-stage-5--scoring)
13. [Stage 6 — Tailoring](#13-stage-6--tailoring)
14. [Stage 7 — Apply engine](#14-stage-7--apply-engine)
15. [Stage 8 — Tracking](#15-stage-8--tracking)
16. [The `run` orchestrator & scheduling](#16-the-run-orchestrator--scheduling)
17. [LLM layer](#17-llm-layer)
18. [CLI reference](#18-cli-reference)
19. [Scripts](#19-scripts)
20. [Operations runbook](#20-operations-runbook)
21. [Testing](#21-testing)
22. [Known issues & limitations](#22-known-issues--limitations)
23. [Design decisions](#23-design-decisions)
24. [Glossary](#24-glossary)

---

## 1. What this system is

An end-to-end job application agent for one candidate (Senior Data Engineer, based in India).
It:

1. **Discovers** data-engineering jobs from public ATS boards and job aggregators.
2. **Normalises and de-duplicates** them into one SQLite database.
3. **Enriches** each job with an LLM (seniority, visa sponsorship, relocation, required skills).
4. **Filters** out jobs that can never work (wrong country, wrong title, US-only, too old, …).
5. **Scores** remaining jobs 0–100 against the candidate profile, weighting visa/relocation highest.
6. **Tailors** a resume PDF, cover letter and form answers per job.
7. **Applies** through a real browser (Playwright), with a human doing the final Submit click.
8. **Tracks** responses by reading Gmail (read-only) and updates application status.

### Current operating mode (important)

| Setting | Value | Meaning |
|---|---|---|
| `active_ats` | `[greenhouse]` | Only Greenhouse jobs are discovered, tailored and applied to. |
| Apply mode used day-to-day | `--manual` | Agent fills the form, **you** click Submit (avoids reCAPTCHA / bot detection). |
| `shadow_mode` | `true` | Default `apply` (no flag) never submits. |
| US jobs | Excluded | H-1B sponsorship is impractical; Canada was added instead. |
| `min_score` | `65` | Calibrated to the visa-weighted rubric (see §12). |

### Candidate constants

| Field | Value |
|---|---|
| Location | India |
| Email | `<candidate>@example.com` (aliases `<candidate>+{tag}@example.com`) |
| Experience | Since 2017-07 (9+ years) |
| Work authorisation | Citizen of India; requires visa sponsorship everywhere else |
| Blacklisted employers | Cimpress, Vistaprint (`config/blacklist.yaml`) |

---

## 2. Quick start

All commands run from the project root on the host Mac:
`~/Vista/my_project/job_application_agent`

```bash
# Start the stack (app + ollama)
docker compose up -d

# Full pipeline: discover → score → tailor → apply → track → summary
docker compose exec app jobagent run --limit 20

# Review + submit tailored applications yourself
#   open http://localhost:6080/vnc.html first to watch/drive the browser
docker compose exec app jobagent apply --manual --limit 10

# Inspect what's in the database
docker compose exec app jobagent jobs --status tailored
docker compose exec app jobagent jobs --status submitted
docker compose exec app jobagent status
```

**No rebuild needed** for Python, YAML, prompt or script edits — `src/`, `config/`, `scripts/` and `data/`
are bind-mounted. Rebuild (`docker compose build`) only after changing `pyproject.toml` or the `Dockerfile`.

---

## 3. Architecture at a glance

```
                         ┌────────────────────── jobagent run ───────────────────────┐
                         │  preflight → discover → score → tailor → apply → track →  │
                         │  summary            (src/jobagent/scheduler.py)           │
                         └───────────────────────────────────────────────────────────┘

 ┌─────────────┐   RawJob   ┌──────────────┐  Job rows  ┌──────────────┐
 │ Collectors  │──────────▶│ normalise +  │──────────▶│   SQLite DB   │◀───────────────┐
 │ (discovery) │            │ dedup + ATS  │            │ data/jobagent │                │
 └─────┬───────┘            │  detection   │            │     .db       │                │
       │ slugs              └──────────────┘            └──────┬────────┘                │
 ┌─────▼────────────┐                                          │ status=discovered       │
 │ Board registry   │  (seeds + flywheel + backfill +          ▼                         │
 │ ats_board table  │   slug guesses → candidate →   ┌──────────────────┐                │
 └──────────────────┘   live / empty / dead)         │ Enrich (LLM)     │ → enriched     │
                                                     ├──────────────────┤                │
                                                     │ Hard filters     │ → ready_to_score / filtered_out
                                                     ├──────────────────┤                │
                                                     │ Score: embedding │ → queued / rejected_low_score
                                                     │ funnel + rubric  │                │
                                                     └────────┬─────────┘                │
                                                              ▼                          │
                                                     ┌──────────────────┐                │
                                                     │ Tailor (LLM +    │ Application    │
                                                     │ RenderCV)        │ status=tailored│
                                                     └────────┬─────────┘                │
                         data/applications/{job_id}/          ▼                          │
                         resume.pdf  cover_letter.txt  ┌──────────────────┐              │
                         answers.json plan.json  ────▶ │ Apply engine     │ submitted /  │
                                                       │ Playwright +     │ skipped /    │
                     noVNC http://localhost:6080 ◀──── │ ATS handlers     │ failed …     │
                                                       └────────┬─────────┘              │
                                                                ▼                        │
                                                       ┌──────────────────┐              │
                                         Gmail (RO) ─▶ │ Tracking         │ acknowledged/│
                                                       │ gmail_watcher    │ rejected/…   ┘
                                                       └──────────────────┘
```

**Key principle:** every stage reads rows in one status and writes them in the next. Stages are
idempotent and can be run independently (`jobagent discover`, `jobagent score`, …) or together
(`jobagent run`).

---

## 4. Runtime environment (Docker)

Defined in `docker-compose.yml`, `Dockerfile`, `docker/`.

### Services

| Service | Image | Ports | Purpose |
|---|---|---|---|
| `app` (container `jobagent`) | `jobagent:latest` (built locally) | `8501` Streamlit dashboard, `6080` noVNC | Runs the Python package, Playwright browsers, CLI |
| `ollama` (container `jobagent-ollama`) | `ollama/ollama:latest` | `11434` | Local embeddings (`nomic-embed-text`), free |
| `mailpit` | `axllent/mailpit:latest` | `8025` UI, `1025` SMTP | Fake inbox for tests. Only with the `dev` profile (`make up-dev`) |

### Inside the `app` container

`docker/app/entrypoint.sh` (run via `tini`) starts, in order:

1. **Xvfb** — a virtual X display (`DISPLAY=:99`). Playwright runs Chromium **headed** on it.
2. **x11vnc** — exposes the display as VNC on port 5900.
3. **websockify + noVNC** — bridges VNC to a web page on port 6080.
   → Open `http://localhost:6080/vnc.html` to watch or drive the live browser.
4. **CMD** — `streamlit run src/jobagent/dashboard/app.py` (dashboard on 8501).

CLI commands are executed with `docker compose exec app jobagent …`.

### Volumes (bind mounts)

| Host path | Container path | Notes |
|---|---|---|
| `./data` | `/app/data` | DB, packages, browser profiles, secrets, vault. **All persistent state.** |
| `./config` | `/app/config` | YAML config — edit on host, takes effect next run |
| `./src` | `/app/src` | Live code — no rebuild for `.py` edits |
| `./scripts` | `/app/scripts` | Helper scripts |
| `${RESUME_DIR:-~/Documents/resume}` | `/resume` (read-only) | `master_resume.txt` source of truth |
| `ollama_models` (named volume) | `/root/.ollama` | Model weights |

`tests/` is **not** mounted; copy it in before running tests (see §21).

### When to rebuild

| Change | Rebuild? |
|---|---|
| `.py`, `.yaml`, `.md` prompts, `scripts/` | No |
| New dependency in `pyproject.toml` | Yes — `docker compose build && docker compose up -d` |
| `Dockerfile` / `docker/app/entrypoint.sh` | Yes |

---

## 5. Repository layout

```
job_application_agent/
├── PLAN.md                     Original design document & roadmap
├── docs/ARCHITECTURE.md        ← this file
├── Dockerfile, docker-compose.yml, Makefile, pyproject.toml, uv.lock
├── config/                     Runtime configuration (see §6)
│   ├── search.yaml             titles, regions, country_allowlist, language rules
│   ├── guardrails.yaml         active_ats, caps, min_score, shadow mode, pacing
│   ├── models.yaml             LLM task → model mapping
│   ├── fetch.yaml              scraping backend routing (native vs paid fallbacks)
│   └── blacklist.yaml          employers never to apply to
├── prompts/                    LLM prompt templates
│   ├── enrich_jd.md            job-description extraction
│   ├── score_jd.md             scoring rubric (visa/relocation = 30 pts)
│   ├── tailor_plan.md          resume tailoring plan
│   └── cover_letter.md         cover letter
├── templates/resume/           RenderCV resume templates
├── scripts/                    Host/ops helpers incl. db_cleanup.py (see §19)
├── docker/                     entrypoint + aux service configs (novnc-chrome, langfuse, mailpit)
├── tests/                      pytest suite (see §21)
├── data/                       ALL runtime state (git-ignored)
│   ├── jobagent.db             SQLite database (WAL mode)
│   ├── profile/                master_profile.yaml, answers_seed.yaml (PII)
│   ├── applications/{job_id}/  tailored packages + screenshots
│   ├── browser_profiles/       persistent Playwright profiles per ATS family
│   ├── secrets/                client_secret.json, gmail_token.json
│   ├── vault/                  file-based credential store (fallback for keyring)
│   ├── registers/              visa-sponsor register CSVs (UK/NL)
│   └── collector_last_run.json quota-collector cooldown timestamps
└── src/jobagent/
    ├── cli.py                  Typer CLI — entry point `jobagent`
    ├── config.py               Settings (env vars) + YAML loaders
    ├── scheduler.py            `jobagent run` stage orchestrator
    ├── vault.py                per-tenant password storage
    ├── email_provider.py       Gmail API wrapper (narrow searches only)
    ├── db/                     engine.py, models.py (17 tables), repo.py
    ├── profile/                importer.py (resume → YAML), loader.py, schema.py
    ├── discovery/              base.py, runner.py, boards.py (probes + status rules),
    │                           company_universe.py (ats_board registry), collectors/*
    ├── pipeline/               normalise, ats_detect, dedup, enrich, filters, score
    ├── tailor/                 orchestrator, planner, render, cover_letter, answers
    ├── apply/                  orchestrator, browser, field_mapper, guardrails, manual,
    │                           verifier, account_manager, base, handlers/*
    ├── tracking/               gmail_watcher.py, summary.py
    ├── llm/                    client, structured, embeddings, cost
    ├── control/                flags.py (pause/stop), checkpoints.py
    ├── observability/          steps.py (app_step recorder)
    ├── dashboard/app.py        Streamlit dashboard (baseline)
    ├── email/, notify/         placeholder packages (empty)
```

---

## 6. Configuration

### 6.1 Environment variables (`.env`)

Read by `src/jobagent/config.py` (`Settings`, prefix `JOBAGENT_`). Access via `from jobagent.config import settings`.

| Variable | Default | Purpose |
|---|---|---|
| `JOBAGENT_PORTKEY_API_KEY` | — | **Required.** Cimpress AI Gateway key |
| `JOBAGENT_PORTKEY_BASE_URL` | `https://gateway.ai.cimpress.io/v1` | Gateway URL |
| `JOBAGENT_OLLAMA_BASE_URL` | `http://localhost:11434` (compose sets `http://ollama:11434`) | Embeddings |
| `JOBAGENT_OLLAMA_EMBED_MODEL` | `nomic-embed-text` | Embedding model |
| `JOBAGENT_DB_PATH` | `data/jobagent.db` | SQLite file (tests override this) |
| `JOBAGENT_DATA_DIR` / `PROFILES_DIR` / `SECRETS_DIR` | `data`, `data/profile`, `data/secrets` | Paths (auto-created) |
| `JOBAGENT_PROFILE_SOURCE` | `~/Documents/resume/master_resume.txt` | Resume source |
| `JOBAGENT_GMAIL_CLIENT_SECRET_PATH` | `data/secrets/client_secret.json` | Google OAuth client |
| `JOBAGENT_GMAIL_TOKEN_PATH` | `data/secrets/gmail_token.json` | OAuth token |
| `JOBAGENT_ADZUNA_APP_ID`, `_APP_KEY` | — | Adzuna API (quota) |
| `JOBAGENT_JOOBLE_API_KEY` | — | Jooble API (quota) |
| `JOBAGENT_REED_API_KEY` | — | Reed API (quota) |
| `JOBAGENT_APIFY_API_TOKEN`, `_FIRECRAWL_API_KEY` | — | Paid scraping fallbacks (unused today) |
| `JOBAGENT_TELEGRAM_BOT_TOKEN`, `_CHAT_ID` | — | Reserved; Telegram not implemented |
| `JOBAGENT_NOVNC_URL` | `http://localhost:6080/vnc.html` | Shown in manual-mode prompt |

### 6.2 `config/guardrails.yaml` — behaviour of apply & focus

| Key | Current | Effect |
|---|---|---|
| `active_ats` | `[greenhouse]` | Gates **discover** (which collectors run), **tailor** (which jobs get packages) and **apply** (which jobs are attempted). `[]` = all ATS types. |
| `daily_cap` | `40` | Max submissions per day |
| `per_channel_caps` | greenhouse 20, workday 10, … | Per-ATS daily caps |
| `per_company_cooldown_days` | `30` | No second submission to same company within N days |
| `min_score` | `65` | Score ≥ this → `queued` |
| `shadow_mode` | `true` | Default `apply` fills but never submits |
| `pacing.min_seconds`/`max_seconds` | `5` / `15` | Random delay between actions (**testing values**; production intent 45/180) |
| `pacing.active_hours` | 08–22 | No applying outside these hours |
| `per_application_budget` | 120 actions, 25 min, $1.00 | Budget for future LLM browser agent |

Loaded by `jobagent.apply.guardrails.load_guardrails()` → `ApplicationGuardrails` (Pydantic).

### 6.3 `config/search.yaml` — what jobs we want

| Key | Purpose |
|---|---|
| `titles` | Title allowlist (phrase match). Also used as keyword filter by the Greenhouse collector. |
| `exclude_titles` | intern, junior, graduate, data entry, sales/solutions engineer, ML engineer |
| `seniority` | `[senior, lead, staff]` (applied only when enrichment detected a seniority) |
| `max_age_days` | `21` |
| `regions` | india, remote, eu, uk_eea, me, apac, sea, **canada** — with `visa_policy` hints |
| `country_allowlist` | 44 ISO codes. **US is deliberately absent.** Known non-listed country → rejected. |
| `language_requirements.reject_if_required` | Languages that make a job unusable (German, Dutch, French, …) — enforced via scorer red flags |
| `must_have_any` | Skill hints (sql, python, dbt, snowflake, spark, airflow, databricks, bigquery) |

### 6.4 `config/models.yaml` — LLM routing

| Task | Model |
|---|---|
| `jd_extraction`, `scoring`, `email_classification`, `form_field_mapping`, `page_classify`, `otp_extraction`, `fact_check`, `pre_submit_verifier` | `@recommended/deepseek-ai/DeepSeek-V4.1-Flash-1017` (cheap) |
| `tailoring_plan`, `resume_rewrite`, `cover_letter`, `browser_agent` | `@Anthropic/eu.anthropic.claude-sonnet-5-5` (strong, EU-hosted) |
| `embedding` | Ollama `nomic-embed-text`, 768 dims |

Model IDs must be the gateway's **internal id** (with `@Provider/` prefix), not the display name.

### 6.5 Other config

- `config/blacklist.yaml` — Cimpress / Vistaprint by name, alias and domain. Plain "Vista" only matches on domain.
- `config/fetch.yaml` — routing intent for native vs Apify/Firecrawl fallbacks. All active collectors are `native`.
- `data/profile/answers_seed.yaml` (**PII, git-ignored**) — contact details, salary floors per country
  (incl. a `CA` row for Canada), EEO defaults, notice period, education, form policies.
- `data/profile/master_profile.yaml` — generated from the resume by `jobagent profile import`.

---

## 7. Data model

SQLite at `data/jobagent.db`, WAL mode, foreign keys on (`src/jobagent/db/engine.py`).
Models in `src/jobagent/db/models.py` (SQLModel). Tables are created with `create_tables()`;
**new columns on existing tables need a manual `ALTER TABLE`** (SQLModel does not migrate).

### 7.1 Tables

| Table | Purpose | Key columns |
|---|---|---|
| `company` | One row per employer | `name`, `name_norm`, `country`; legacy `ats_type` / `ats_slug` / `blacklisted` / `last_polled_at` are kept but **no longer drive polling** (see `ats_board`) |
| `ats_board` | **Board registry**: one row per ATS job board (Greenhouse / Lever / Ashby / Workday / …); collectors poll it, jobs go to `job` | `ats_type`, `slug`, `board_path` + `host` (Workday), `company_id`, `company_name`, `status` (candidate/live/empty/dead), `discovered_via`, `job_count`, `matched_count`, `consecutive_failures`, `last_error`, `last_checked_at`, `last_polled_at`, `last_job_change_at`. Unique on (`ats_type`, `slug`, `board_path`) |
| `job` | One row per unique job | see §7.2 |
| `job_source` | Every (job, source, url) a job was seen on | `job_id`, `source`, `url` |
| `score` | LLM scoring history per job | sub-scores, `pitch_angle`, model |
| `application` | One attempt/package per job | `job_id`, `status`, `resume_path`, `answers_json`, `submitted_at`, `created_at`, `updated_at` |
| `app_event` | Status transition log | `application_id`, `from_status`, `to_status`, `note`, `ts` |
| `app_step` | Per-action browser trace (observability) | |
| `app_checkpoint` | Resume points for interrupted applies | |
| `account` | ATS accounts created by the agent (Workday) | `ats`, `tenant`, `login_url`, `email_alias`, `keychain_ref`, `status` |
| `learned_trajectory` | Recorded action sequences for replay (future) | |
| `control_flag` | Pause/stop state | |
| `block` | Recorded blocks (CAPTCHA, login walls) | |
| `email` | Classified Gmail messages | `gmail_id` (unique), `application_id`, `classification`, `subject`, `snippet` |
| `question_seen` | Form questions encountered + answers | |
| `run` | One row per run / stage | items in/out, errors |
| `llm_call` | Every LLM call with tokens and cost | |

### 7.2 `job` columns worth knowing

| Column | Meaning |
|---|---|
| `source`, `source_job_id` | Where it was first stored (e.g. `adzuna`, `greenhouse`) |
| `url`, `apply_url` | Listing URL and the URL the apply engine opens |
| `ats_type` | `greenhouse`, `lever`, `ashby`, `workday`, … or `NULL` (aggregator, unresolved) |
| `country` | ISO code or `NULL` (unknown) |
| `content_hash` | `sha256(company_norm + title_norm + country)[:16]` — cross-source dedup key |
| `visa_sponsorship`, `relocation_support` | `"yes"` / `"no"` / `"unknown"` from enrichment |
| **`visa_ok`, `relocation_ok`** | Boolean flags, `True` only when confirmed `"yes"`. Added 2026-11-12. Filter with `--visa-confirmed` / `--relocation-confirmed`. |
| `seniority`, `years_required`, `required_skills_json` | From enrichment |
| `score` | Latest score 0–100 |
| `status` | Pipeline state (below) |

### 7.3 Job status lifecycle

```
discovered ──enrich──▶ enriched ──filters──▶ ready_to_score ──score──▶ queued ──(apply submitted)──▶ applied
     │                                  │                       │
     │ (LLM failed AND fails filters)   ▼                       ▼
     └──────────────────────────▶ filtered_out          rejected_low_score
```

- `queued` = scored ≥ `min_score`, eligible for tailoring.
- `applied` = a submitted application exists. **Note:** the apply engine does not yet set this
  automatically — see §22.

### 7.4 Application status lifecycle

| Status | Set by | Meaning |
|---|---|---|
| `queued` | legacy | Pre-tailor placeholder (older runs) |
| `tailored` | tailor | Package built, ready to apply |
| `shadow_stopped` | apply | Form filled, shadow mode stopped before Submit |
| `submitted` | apply | Submitted (auto-detected in live mode, or operator confirmed `y` in manual mode) |
| `skipped` | apply | No handler, ATS not in `active_ats`, cooldown, or operator chose `n` |
| `failed` | apply | Handler error / form not found / confirmation not detected |
| `blocked` | apply | Visible CAPTCHA, login wall |
| `superseded` | DB cleanup | An older/duplicate row replaced by a better one (kept for audit) |
| `acknowledged`, `rejected`, `interview_invite`, `assessment`, `offer` | tracking | From Gmail classification |
| `ghosted` | tracking | Submitted > 30 days, no reply |

### 7.5 Golden-standard invariants

These hold after cleanup and should be preserved by any new code:

1. **At most one non-`superseded` application per job.** Winner priority:
   `submitted > tailored > blocked > failed > shadow_stopped > skipped > queued`, ties → highest id.
2. **A job with a `submitted` application has `job.status = 'applied'`.**
3. Tailor never creates a package for a job that already has a `tailored` or `submitted` application.
4. `superseded` rows are never deleted (audit trail).

The cleanup snippet is in §20.5.

---

## 8. Stage 1 — Discovery

Entry: `jobagent discover` → `discovery/runner.py::discover_all(search_cfg, only_sources)`.

### 8.1 Flow

1. Load `config/search.yaml`.
2. `_load_collectors(only=…)` instantiates every collector in `_COLLECTOR_REGISTRY`, skipping:
   - collectors whose `name` is not in `only_sources` (from `--sources`, else `active_ats` in guardrails);
   - collectors still on **cooldown** (§8.3).
3. `run_discovery()` runs collectors concurrently (semaphore 5); a failing collector returns 0 jobs.
4. `CompanyUniverse.feed_jobs()` — the **flywheel**: any job URL (from a non-ATS collector) revealing an
   ATS board is added to `ats_board` as a `candidate` (§8.4).
5. `pipeline/dedup.py::bulk_upsert()` stores jobs (one SAVEPOINT per job).

`--sources all` disables the `active_ats` focus for one run.

### 8.2 Collectors (`src/jobagent/discovery/collectors/`)

All subclass `CollectorBase` (`discovery/base.py`) and return `list[RawJob]`.

| Collector | Source | Auth | Cooldown | Status |
|---|---|---|---|---|
| `greenhouse.py` | `boards-api.greenhouse.io/v1/boards/{slug}/jobs?content=true` | none | 0 | **Primary.** Boards from `ats_board`. Keyword-filters titles; real company name; `posted_at` = `first_published`; country from `offices` |
| `lever.py` | `api.lever.co/v0/postings/{slug}` | none | 0 | Boards from `ats_board`; most seed slugs are dead |
| `ashby.py` | `api.ashbyhq.com/posting-api/job-board/{slug}` | none | 0 | Boards from `ats_board`. Payload key is `jobs` (was wrongly read as `jobPostings` → 0 jobs before 2026-11-13) |
| `workday.py` | `{host}/wday/cxs/{tenant}/{board}/jobs` | CSRF only on 401/403 | 0 | Boards (host + board path) from `ats_board`. `limit` must be ≤ 20 (was 100 → HTTP 400). 422 = unknown tenant, 404 = unknown board. Most hand-written seeds were invalid; only DBS verified (2026-11-13) |
| `smartrecruiters.py`, `workable.py` | public APIs | none | 0 | No slugs configured |
| `arbeitnow.py`, `remotive.py`, `remoteok.py`, `himalayas.py`, `weworkremotely.py` | public feeds | none | 0 | Aggregators/remote boards |
| `mycareersfuture.py` | Singapore gov board | none | 0 | Works (collector `name` attribute missing) |
| `adzuna.py` | Adzuna API | app id/key | **168 h** | US removed from country map; CA included |
| `jooble.py` | Jooble API | key | **168 h** | |
| `reed.py` | Reed UK API | key | **168 h** | |
| `jobspy_collector.py`, `hn_hiring.py` | JobSpy / HN "Who's hiring" | — | 168 h / 0 | **Never loaded** — registry module-name mismatch (§22) |

### 8.3 Quota cooldowns

`runner.py::_COOLDOWN_HOURS` — free collectors `0`, quota collectors (Adzuna, Jooble, Reed, JobSpy) `168`.
After a successful quota-collector run, the timestamp is written to `data/collector_last_run.json`.
To force a quota collector early, delete its key from that file.

### 8.4 Board registry (`ats_board`, `discovery/company_universe.py`, `discovery/boards.py`)

```
seeds / flywheel / backfill / scripts ──▶ ats_board (candidate)
                                              │  boards validate  or  first collector poll
                                              ▼
                               live ─ empty ─ dead          (discovery/boards.py rules)
                                 │      │
                     collectors poll ◀──┘ (empty: every 3 days; dead: never)
                                 │
                                 ▼
                         job table (dedup.bulk_upsert)
```

- **Status rules** (`boards.apply_outcome`): 200 with jobs → `live`; 200 with 0 → `empty`;
  404 (Workday also 422) → `dead` immediately for a `candidate`, after **3 in a row** for a
  board that was live. Transient errors never kill a live board; a candidate that errors 3×
  becomes `dead`. Dead boards are re-probed by `boards validate` after 30 days.
- **`CompanyUniverse`** — `get_boards(ats)` (pollable `BoardRef`s), `get_slugs(ats)` (simple
  collectors), `add_board()` / `add_from_url()` / `feed_jobs()` (flywheel, skips jobs that came
  from an ATS collector), `record_outcomes()` (collectors + validator write back),
  `backfill()`, `validate()`, `stats()`. `ensure_initialized()` backfills automatically on first use.
- **Company linking:** once a probe/poll learns the real name (Greenhouse `name` /
  `company_name`), the board is linked to the existing `company` with the same `name_norm`, or
  its slug-named placeholder company is renamed.
- **Seeds:** `GREENHOUSE_SEEDS`, `LEVER_SEEDS`, `ASHBY_SEEDS`, `WORKDAY_SEEDS` enter as
  `candidate` (`discovered_via=seed`) — guesses are fine, the validator sorts them out.

**Adding a board:** `jobagent boards add https://job-boards.greenhouse.io/acme` (or
`jobagent boards add acme --ats lever`). Workday needs a full board URL. Bulk-discover
Greenhouse slugs with `scripts/discover_greenhouse_slugs.py` (§19).

### 8.5 Greenhouse-specific details

- Titles are filtered with `_matches(title, search.titles)` **before** storing.
- `_country_from_offices(offices)`:
  all offices US → `"US"` (rejected later); exactly one non-US country → that country;
  US + one non-US → the non-US one; several non-US → `None`; no offices → `None`.
- Both `job-boards.greenhouse.io` and the EU domain `job-boards.eu.greenhouse.io` are supported.

---

## 9. Stage 2 — Normalise, ATS detection & dedup

### 9.1 Normalise (`pipeline/normalise.py`)

`normalise(raw) → Job`: HTML→Markdown description, `title_norm`, `company_norm`
(legal suffixes stripped), `detect_country(location_raw, hint)`, `detect_remote_type`,
`content_hash`, and `ats_type` via `detect_ats(apply_url or url)`.

### 9.2 ATS detection (`pipeline/ats_detect.py`)

Ordered regex table `ATS_PATTERNS`:

| Pattern | ATS |
|---|---|
| `(boards\|job-boards(.eu)?).greenhouse.io/{slug}` | greenhouse (slug captured) |
| `?gh_jid=` on any domain | greenhouse (no slug) |
| `jobs.lever.co/{slug}` | lever |
| `jobs.ashbyhq.com/{slug}` | ashby |
| `{tenant}.wd{n}.myworkdayjobs.com`, `{tenant}.myworkday.com` | workday |
| smartrecruiters, bamboohr, linkedin, `mailto:` | respective types |

`extract_ats_slug(url) → (ats, slug) | None`.

### 9.3 Dedup (`pipeline/dedup.py`)

`upsert_job(raw)`:

1. **Layer 1** — same `(source, source_job_id)` → update `last_seen_at`.
2. **Layer 2** — same `content_hash` from another source → update `last_seen_at`, add `job_source`, and
   **ATS upgrade**: if the existing row has `ats_type` NULL/unknown and the new one is known
   (e.g. an Adzuna job later found on a Greenhouse board), set `ats_type` + `apply_url`;
   if it had been `filtered_out`/`rejected_low_score` and now passes filters, reset to `discovered`.
3. **New** — find/create `company` (falls back to URL host if the source gives no company name) and insert.

`bulk_upsert` returns `(new, updated)` — what `discover` prints.

---

## 10. Stage 3 — Enrichment

`pipeline/enrich.py::run_enrichment()` — up to 1000 `discovered` jobs per run, batches of 50,
10 concurrent LLM calls, prompt `prompts/enrich_jd.md`, task `jd_extraction`.

`apply_enrichment()` writes `visa_sponsorship`, `relocation_support`, `seniority`, `years_required`,
`required_skills_json`, sets `visa_ok`/`relocation_ok` when `"yes"`, and status → `enriched`.

On LLM failure the job is checked against the hard filters; if it fails it becomes `filtered_out`
(so it isn't retried every run). Otherwise it stays `discovered` and is retried next run.

**Reality check:** most job descriptions never mention sponsorship, so `visa_sponsorship` is
`unknown` for ~all queued jobs. `visa_ok=True` is rare and meaningful.

---

## 11. Stage 4 — Hard filters

`pipeline/filters.py::apply_filters(job, search_cfg, answers) → FilterResult(passed, reason)`.
`run_filters()` applies it to `enriched` jobs → `ready_to_score` or `filtered_out`.
Rules run in order; the first failure wins:

| # | Rule | Rejects when |
|---|---|---|
| a0 | Country allowlist | `country` known and not in `country_allowlist` (e.g. `US`) |
| a0 | US company slug | `country` NULL and company slug in `_US_ONLY_SLUGS` (~50 US-HQ companies) |
| a0 | US location signals | `country` NULL and title/city contains "- USA", "united states", "san francisco", … |
| a0 | US-only phrases | `country` NULL and description contains e.g. "based in the united states", "unable to sponsor", "must be authorized to work in the us" |
| a | Title allowlist | title matches no phrase in `titles` |
| b | Excluded keywords | title contains an `exclude_titles` word |
| c | Age | `posted_at` older than `max_age_days` |
| d | Blacklist | company matches `blacklist.yaml` |
| e | Seniority | detected seniority not in `[senior, lead, staff]` |
| g | Visa | non-IN job with `visa_sponsorship == "no"` |
| h | Recent submission | a **submitted** application to the same company in the last 30 days (skipped/failed don't count) |

---

## 12. Stage 5 — Scoring

`pipeline/score.py`. In `jobagent run` the scheduler awaits `_run_scoring_async(limit=200, embed_threshold=0.35)`
directly (the sync `run_scoring()` wraps `asyncio.run()` and must not be used inside an event loop).

1. **Embedding funnel (free):** cosine similarity between the job (title + skills + description excerpt) and
   the profile embedding (Ollama). Below `0.35` → `rejected_low_score` without an LLM call.
2. **LLM rubric:** prompt `prompts/score_jd.md`, validated into `ScoringResult`:

| Component | Points | Notes |
|---|---|---|
| `skills_match` | 30 | |
| `experience_match` | 20 | |
| `domain_match` | 10 | |
| **`location_visa_fit`** | **30** | 30 sponsorship + relocation confirmed · 25 sponsorship confirmed · 20 sponsorship yes/relocation no · **15 unknown** · 10 remote-worldwide · 5 US/high-risk · 0 explicitly denied |
| `seniority_fit` | 10 | |

Plus `missing_must_haves`, `red_flags`, `pitch_angle` (reused by tailoring).

3. `score ≥ min_score (65)` → `queued`, else `rejected_low_score`.

Why 65: with visa unknown (15/30) a strong job tops out around 75. Every real submission so far scored 65–81.

---

## 13. Stage 6 — Tailoring

Entry: `jobagent tailor --limit N` → `tailor/orchestrator.py::run_tailoring()`.

**Job selection** (`_get_queued_jobs`): `job.status == 'queued'`, no existing `tailored`/`submitted`
application, ATS in `active_ats`, ordered by known-ATS first then score.

**Per job:**

| Step | Module | Output |
|---|---|---|
| Plan (archetype, bullet selection, summary) | `planner.py` + `prompts/tailor_plan.md` (Sonnet) | `plan.json` |
| Render resume | `render.py` (RenderCV + `templates/resume/`) + ATS parse check | `resume.pdf` |
| Cover letter | `cover_letter.py` + `prompts/cover_letter.md` | `cover_letter.txt` |
| Form answers | `answers.py` — country-aware salary, notice period, authorisation, EEO, ~109 keys | `answers.json` |
| Persist | orchestrator | `application` row, `status='tailored'`, `resume_path` |

All files land in `data/applications/{job_id}/`. Fabrication guard: bullets come only from the master profile;
invalid bullet ids are dropped (logged as `plan.invalid_bullets_dropped`).

---

## 14. Stage 7 — Apply engine

Entry: `jobagent apply [--manual|--live] [--visa-confirmed] [--limit N]` → `apply/orchestrator.py::run_apply()`.

### 14.1 Modes

| Mode | Behaviour |
|---|---|
| default (shadow) | Fill form, screenshot, stop. Status `shadow_stopped`. |
| `--manual` (**recommended**) | Fill form, scroll to Submit, print banner with noVNC link, wait for operator: `y` → `submitted`, `n` → `skipped`, `q` → stop run. No TTY → `skipped` (never assumes a submission). |
| `--live` | Fill and click Submit; confirmation detected from page text / iframe form reset. Fragile with reCAPTCHA. |

### 14.2 Orchestrator loop

1. Guards: active hours, `data/STOP` sentinel, daily cap.
2. `_get_tailored_applications()` — `tailored` apps with a package; known-ATS first, then `visa_ok` first,
   then score. `--visa-confirmed` restricts to `visa_ok=True`.
3. Per application:
   - **ATS focus gate:** ATS not in `active_ats` → `skipped`.
   - Company cooldown → `skipped`.
   - Unknown ATS → `_resolve_ats_via_redirect()` (open page, follow Apply/popups). Rarely succeeds for Adzuna/Jooble.
   - No handler → `skipped` (not counted as failed).
   - `handler.apply(...)` → `ApplyResult` → `_update_app_status()` + `app_event`.
4. `close_all()` browser contexts.

### 14.3 Handlers (`apply/handlers/`)

Interface: `ApplyHandlerBase.apply(job, app, package_dir, shadow_mode, manual_mode=False) → ApplyResult`.
Registered in `orchestrator._load_handlers()`: greenhouse, lever, ashby, email_apply, workday.

#### Greenhouse (`greenhouse.py`) — production-quality

1. Navigate (`?gh_jid=` appended), human pause, **dismiss cookie consent** first (backdrops intercept clicks).
2. If no Greenhouse iframe, click an Apply link (force-click fallback). Detect `iframe[src*=greenhouse.io]`.
3. **Form guard:** `_looks_like_application_form()` requires `input[type=file]` or `#application_form` —
   prevents filling job-alert/newsletter widgets that also have name/email fields.
4. Visible CAPTCHA → `blocked` (invisible reCAPTCHA is ignored).
5. **Early pass** (before name/city fill, which re-renders the form): relocation (`"willing to relocate"`),
   sponsorship, right-to-work comboboxes — clicks the `.select__control` wrapper and picks from `.select__menu`.
6. **Scope** with `_form_scope()`: `form#application_form` → `form#application-form` → `form:has(#first_name)`
   (stable after upload; `form:has(input[type=file])` is not, because the file input is replaced by a chip).
7. `fill_all_common_fields()` (name, email, phone, LinkedIn, city, "how did you hear") + resume upload.
8. Cover letter (textarea or file).
9. EEO: native `<select>` and React-select comboboxes (gender/race/veteran/disability → prefer-not / not a veteran).
10. Custom questions: radios, Google-Places location autocomplete, free-text "countries with right to work",
    combobox patterns (sponsorship Yes, relocation, sanctions No, acknowledge).
11. Native selects, consent checkboxes (by `required` or label text).
12. Screenshot `step_03_filled.png` → shadow / manual / live branch.

Screenshots: `data/applications/{job_id}/screenshots/step_01_loaded … step_04_*`.

#### Workday (`workday.py`) — built, untested live

Account login/creation via `AccountManager`, then a page loop (≤10 pages) using `data-automation-id`
selectors: My Information → My Experience (resume upload + autofill audit) → Application Questions →
Voluntary Disclosures → Review → Confirmation. Supports shadow/manual/live.

#### Lever, Ashby, email_apply

Earlier-phase handlers; accept `manual_mode` but have no manual branch yet.

### 14.4 Supporting modules

| Module | Role |
|---|---|
| `browser.py` | Playwright persistent contexts per ATS family (`data/browser_profiles/{family}`), headed on Xvfb, `playwright-stealth` applied per context. Persistence is why Greenhouse stopped asking for OTP after the first submission. |
| `field_mapper.py` | `build_field_values(profile, job, answers)`; label/placeholder/role-based filling; resume upload fallback |
| `guardrails.py` | Loads guardrails, caps, cooldowns, `human_like_pause`, active hours, STOP sentinel |
| `manual.py` | `await_manual_submit()` prompt, `focus_form()` |
| `verifier.py` | Pre-submit verification helpers |
| `account_manager.py` | Create / log in / reuse ATS accounts; alias `<candidate>+{ats}-{tenant}@example.com`; email verification via `EmailProvider` |
| `../vault.py` | 24-char generated passwords; macOS keyring, else `data/vault/.keyring.json` (0600) |
| `../email_provider.py` | Gmail API; **only narrow searches** (`deliveredto:` alias or `from:` domain + `after:`); extracts verify links / OTPs |

---

## 15. Stage 8 — Tracking

`tracking/gmail_watcher.py::run_gmail_watcher()` — run by `jobagent track` and the `track` stage.

1. Load `submitted`/`acknowledged` applications from the last 90 days.
2. **Ghosting** (works without Gmail): submitted > 30 days ago → `ghosted`.
3. If Gmail connected: narrow search per application (Workday alias, else company domain + since submit date).
4. Classify: keyword heuristics first (free), then LLM fallback
   → `acknowledged | rejected | interview_invite | assessment | offer | other`.
5. Store in `email` (dedup by `gmail_id`), update application status, write `app_event`.

`tracking/summary.py` — `build_run_summary(since_hours=24)` + `print_summary()` (submitted/failed/skipped
counts, email responses, submissions table, all-time counts). Note the "submitted" count is a 24 h window.

---

## 16. The `run` orchestrator & scheduling

`scheduler.py::run()` executes stages in order, each recorded as a `run` row; a failing stage is logged
and the next stage still runs.

| Stage | Does |
|---|---|
| `preflight` | DB, Portkey key, Ollama, Gmail token, master profile. Hard-fails only on DB/profile. |
| `discover` | `discover_all(only_sources=active_ats)` |
| `score` | enrichment → filters → `_run_scoring_async(200, 0.35)` |
| `tailor` | `run_tailoring(limit)` |
| `apply` | `run_apply(limit, force_shadow)` — non-interactive (no `--manual` in `run`) |
| `track` | `run_gmail_watcher()` |
| `summary` | cost stats + `print_summary()` |

`jobagent run --stage <name>` runs a single stage.

### Daily schedule (macOS launchd)

- `scripts/install_schedule.py` installs `~/Library/LaunchAgents/io.local.jobagent.daily.plist` (08:00 daily).
- The plist runs `scripts/daily_run.sh`, which: checks Docker → `docker compose up -d app` → waits for the
  container → refreshes the Gmail token on the host (if refreshable) → `jobagent run --limit 20`.
- Logs: `~/Library/Logs/jobagent/daily.log` and `daily.err`.
- `--status` / `--remove` flags on the installer. Runs only while you are logged in.

---

## 17. LLM layer

| File | Role |
|---|---|
| `llm/client.py` | `get_client()` → cached `openai.OpenAI` pointed at the gateway. `api_key="dummy"`; real auth via `x-portkey-api-key` header (+ Valar forwarding headers). |
| `llm/structured.py` | `structured_complete(task, messages, model=PydanticModel)` — resolves model from `models.yaml`, parses JSON, repair-retries |
| `llm/embeddings.py` | Ollama embeddings + `cosine_similarity` |
| `llm/cost.py` | Records `llm_call` rows; `today_cost()`, `session_stats()` |

The client is synchronous; async code wraps calls in `asyncio.to_thread`.
A full run currently costs roughly $0.2–0.4; tailoring is the main cost.

---

## 18. CLI reference

`jobagent --help` for the live list. All via `docker compose exec app jobagent …`.

| Command | Key options | Notes |
|---|---|---|
| `run` | `--limit`, `--regions`, `--shadow`, `--stage` | Full pipeline |
| `discover` | `--sources a,b` (or `all`), `--regions`, `--limit`, `--dry-run` | Default sources = `active_ats` |
| `score` | `--limit` | Enrich + filter + score; prints top-20 by score (includes non-appliable aggregator jobs — informational) |
| `tailor` | `--limit` | Builds packages |
| `apply` | `--manual`, `--live`, `--visa-confirmed`, `--limit` | Use `-it` / a real terminal for `--manual` |
| `track` | — | Gmail check + summary |
| `jobs` | `--status` (queued, tailored, submitted, failed, skipped, all, …), `--country CA,GB`, `--ats greenhouse`, `--min-score`, `--visa-confirmed`, `--relocation-confirmed`, `--limit` | Columns: ID, score, country, **V** (visa_ok), **R** (relocation_ok), ATS, source, status, title, URL/submitted date. Application statuses query the `application` table; `queued` queries `job`. |
| `status` | — | Today's counts, LLM cost, jobs by source |
| `profile import` / `profile show` | | Resume → `master_profile.yaml` |
| `llm-ping` | | Gateway smoke test |
| `gmail-connect` | | **Broken inside Docker** — use `scripts/gmail_connect.py` on host (§22) |
| `db-init` | | Create tables (+ one-off `ats_board` backfill) |
| `boards stats` | | Board counts per ATS × status |
| `boards list` | `--ats`, `--status`, `--limit` | Boards sorted by matched/total jobs |
| `boards validate` | `--ats`, `--limit`, `--all`, `--concurrency` | Probe candidates + dead boards due for recheck (`--all` = everything) |
| `boards add` | `URL` or `SLUG --ats X`, `--no-validate` | Add (and probe) a board |
| `boards backfill` | | Import legacy `company.ats_slug`, ATS URLs in `job`, seeds (idempotent) |
| `schedule` | `--remove`, `--status` | Wraps `install_schedule.py` |
| `ctl pause` / `ctl resume` / `ctl stop` | | Control flags |

Makefile mirrors common commands: `make up`, `make run`, `make discover`, `make shell`, `make logs`,
`make novnc`, `make dashboard`, `make gmail-connect`, `make pull-models`, `make rebuild`.

---

## 19. Scripts

| Script | Where to run | Purpose |
|---|---|---|
| `scripts/daily_run.sh` | host (launchd) | Daily wrapper (§16) |
| `scripts/install_schedule.py` | host | Install/remove/status the launchd job |
| `scripts/gmail_connect.py` | **host** | OAuth sign-in via local browser; writes `data/secrets/gmail_token.json`. Needs `pip install google-auth-oauthlib` in the host venv. |
| `scripts/discover_greenhouse_slugs.py` | container | Mines new Greenhouse slugs: takes company names from jobs with score ≥ `--min-score` (default 70) and unknown ATS, generates slug variants, probes the public Boards API (free) and saves every existing board to `ats_board` as `candidate` (`discovered_via=guess`; `--no-save` to only report). Skips slugs already known in any status. `--batch` caps candidates (default 500). |
| `scripts/db_cleanup.py` | container | Idempotent data cleanup in 4 phases: (1) delete test/orphan rows (`test-co` accounts, `testco` boards, test-URL jobs, orphan `job_source`/`score`), (2) standardise nulls (`country='?'`→NULL, missing `apply_url`←`url`, `ats_type` recovered from URL), (3) enrich `company` (real names from `ats_board`, country from its jobs), (4) backfill `job.country` from company and mark US jobs `filtered_out`. **Dry run by default**; prints before/after counts. Run: `docker compose exec app python scripts/db_cleanup.py` then add `--apply`. |

---

## 20. Operations runbook

### 20.1 Daily

```bash
docker compose exec app jobagent run --limit 20            # or let launchd do it at 08:00
docker compose exec app jobagent jobs --status tailored --ats greenhouse
docker compose exec app jobagent apply --manual --limit 10 # watch http://localhost:6080/vnc.html
```

### 20.2 Gmail token

Testing-mode OAuth tokens expire after ~7 days. When preflight shows `Gmail token ✗`:

```bash
cd ~/Vista/my_project/job_application_agent
python scripts/gmail_connect.py
```

One-time setup (already done): Google Cloud project → Gmail API enabled → OAuth **Desktop app** client →
JSON saved as `data/secrets/client_secret.json` → `<candidate>@example.com` added under **Audience → Test users**
(without this Google returns `403 access_denied`).

### 20.3 Growing Greenhouse coverage

```bash
docker compose exec app python scripts/discover_greenhouse_slugs.py --min-score 70 --batch 1000
docker compose exec app jobagent boards validate --ats greenhouse   # candidates → live/empty/dead
docker compose exec app jobagent boards stats
docker compose exec app jobagent discover
```

Check a slug by hand: `https://boards-api.greenhouse.io/v1/boards/<slug>/jobs` (200 = valid board).

### 20.4 Inspecting the DB

```bash
docker compose exec app jobagent jobs --status all --min-score 80 --limit 100
sqlite3 data/jobagent.db      # from the host; data/ is mounted
```

```sql
.headers on
.mode column
SELECT id, title, ats_type, country, score, status, visa_ok, relocation_ok
  FROM job WHERE status='queued' ORDER BY score DESC LIMIT 20;
SELECT a.id, j.title, a.status, a.submitted_at
  FROM application a JOIN job j ON j.id=a.job_id WHERE a.status='submitted';
SELECT status, COUNT(*) FROM application GROUP BY status;
```

GUI: `open -a TablePlus data/jobagent.db`.

### 20.5 Restoring the golden standard

Two complementary tools:

1. **Data hygiene** — `scripts/db_cleanup.py` (test/orphan rows, nulls, company names/countries, US jobs).
   Always dry-run first:

   ```bash
   docker compose exec app python scripts/db_cleanup.py          # report only
   docker compose exec app python scripts/db_cleanup.py --apply  # write
   ```

2. **Application invariants** (§7.5) — one active application per job, submitted ⇒ `applied`.
   Run after manual DB edits or if duplicates appear:

```bash
docker compose exec -T app python3 - <<'EOF'
from collections import defaultdict
from datetime import datetime, timezone
from sqlmodel import select
from jobagent.db.engine import get_session
from jobagent.db.models import Application, Job
P = {'submitted':10,'tailored':9,'blocked':8,'failed':7,'shadow_stopped':6,'skipped':5,'queued':4}
with get_session() as s:
    by_job = defaultdict(list)
    for a in s.exec(select(Application)).all():
        if a.status != 'superseded':
            by_job[a.job_id].append(a)
    n = 0
    for apps in by_job.values():
        apps.sort(key=lambda a: (P.get(a.status, 0), a.id), reverse=True)
        for loser in apps[1:]:
            loser.status, loser.updated_at = 'superseded', datetime.now(timezone.utc); n += 1
        if apps[0].status == 'submitted':
            j = s.get(Job, apps[0].job_id)
            if j and j.status != 'applied':
                j.status = 'applied'
    s.commit(); print('superseded', n)
EOF
```

### 20.6 Re-queueing / re-scoring jobs

- Re-score with new rubric: delete the job's `score` rows, set `job.score=None`, `status='enriched'`, then `jobagent score`.
- Re-enrich: set `status='discovered'`.
- Force a borderline job into the queue: set `status='queued'` (it must still pass the tailor ATS gate).

### 20.7 Emergency stop

`touch data/STOP` (checked between applications) or `jobagent ctl stop`. Remove the file to resume.

### 20.8 Adding a database column

1. Add the field to the model in `db/models.py` with a default.
2. `ALTER TABLE <table> ADD COLUMN <col> <TYPE> NOT NULL DEFAULT <v>` via `get_engine()` (SQLModel won't do it).
3. Backfill if derivable; update writers (e.g. `apply_enrichment`) and readers (CLI).

---

## 21. Testing

```bash
docker compose exec -T app rm -rf /app/tests        # IMPORTANT: else docker cp nests a stale tests/tests copy
docker cp tests jobagent:/app/tests
docker compose exec -w /app -T app sh -c \
  'uv run --with pytest --with pytest-asyncio pytest tests -q -p no:cacheprovider'
```

Current baseline: **183 passed, 11 skipped** (verified 2026-11-12). Earlier "202 passed / 22 skipped"
figures were inflated by a nested `tests/tests` copy being collected twice.

| File | Covers |
|---|---|
| `test_config.py` | Settings & YAML loading |
| `test_discovery.py` | ATS detection, normalisation, seed lists |
| `test_importer.py` | Resume parser |
| `test_phase5c.py` | Vault, EmailProvider, AccountManager, Workday collector (mocked HTTP) & handler |
| `test_phase6.py` | Cooldowns, Gmail watcher, ghosting, summary, schedule scripts |
| `test_pipeline_integration.py` | dedup, filters, handler routing, shadow apply, CLI smoke tests |
| `test_ats_board.py` | Board registry: `apply_outcome` status rules, poll/validation due-dates, `CompanyUniverse` add/backfill/record_outcomes/company linking, probes and collectors writing outcomes back |

**DB isolation:** `tests/conftest.py` redirects `JOBAGENT_DB_PATH` to a session temp DB, and per-test
`tmp_db` fixtures reset both `db.engine._engine` and `config.settings`. Any new test touching the DB must
use `tmp_db` — an earlier version leaked fake `testco` jobs into production.

---

## 22. Known issues & limitations

| # | Issue | Impact | Suggested fix |
|---|---|---|---|
| 1 | Apply does not set `job.status='applied'` on submission | Job status drifts; fixed by §20.5 | Set it in `orchestrator._update_app_status` when status is `submitted` |
| 2 | Collector registry points to `collectors.jobspy` / `collectors.hnhiring`; files are `jobspy_collector.py` / `hn_hiring.py` | JobSpy and HN never run | Fix module paths in `runner._COLLECTOR_REGISTRY` |
| 3 | `MyCareersFutureCollector` has no `name` | Shows as `''` in stats; can't be targeted with `--sources` | Add `name = "mycareersfuture"` |
| 4 | ~~Workday collector gets 400/422 from every tenant~~ Fixed 2026-11-13: 400 was `limit>20`; 422 = seed tenants that don't exist (now marked dead). Only 1 verified Workday board | Few Workday jobs; handler untested live | Add real Workday board URLs with `jobagent boards add <url>` |
| 5 | `jobagent gmail-connect` inside Docker fails (`Missing redirect_uri`) | Must use host script | Point CLI help to `scripts/gmail_connect.py`, or implement loopback flow with a published port |
| 6 | Adzuna/Jooble apply URLs hide the real ATS behind JS click tracking | Most aggregator jobs are not appliable | Rely on Greenhouse boards + dedup ATS upgrade |
| 7 | `visa_sponsorship` almost always `unknown` | `--visa-confirmed` returns few jobs | Company-level sponsor signals (UK/NL sponsor registers in `data/registers/`) |
| 8 | ~~`company.last_polled_at` never updated~~ Resolved by `ats_board` (`last_polled_at`, `last_checked_at`, `last_job_change_at` written by collectors/validator). `company.last_polled_at` is legacy | — | — |
| 9 | `GREENHOUSE_SEEDS` still contains US companies and duplicates. Dead seeds are no longer polled (registry marks them `dead`), but **live US boards are still polled** and their jobs rejected by filters | Some wasted API calls | Remove US-HQ slugs from seeds, or mark their boards `dead` |
| 10 | Live-mode confirmation detection is fragile (iframes, reCAPTCHA Enterprise) | Use `--manual` | — |
| 11 | Some React-select EEO fields (gender/ethnicity) not filled | Voluntary fields; user can fill | Click `.select__control` wrapper as in relocation fix |
| 12 | `tailor` CLI "Recent tailored packages" table shows stale rows | Cosmetic | Filter to current run |
| 13 | Pacing at testing values (5–15 s) | Higher bot-detection risk | Restore 45/180 for unattended live runs |
| 14 | Telegram alerts / Auth Assist, full dashboard not implemented; `email/`, `notify/` empty | Manual monitoring only | See PLAN.md Phase 5a / 6 |
| 15 | Lever: 2 live / 48 dead boards; Ashby: 11 live / 24 dead (2026-11-12 registry stats) | Low yield | Slug discovery for Lever/Ashby (`boards add`, a guess script like the Greenhouse one) |
| 16 | 120 `company` rows still have a slug as their name | Cosmetic in CLI / cover letters | `python scripts/db_cleanup.py --apply` (phase 3) |

---

## 23. Design decisions

| Decision | Why |
|---|---|
| **Greenhouse-only focus** (`active_ats`) | Most reliable handler; public API; avoid half-working ATSs polluting the queue |
| **Manual submit** as default workflow | reCAPTCHA Enterprise and bot detection make fully automatic submission unreliable; a human click is genuine and gives certain confirmation |
| **US excluded, Canada added** | H-1B lottery makes US impractical; Canada's Global Talent Stream is accessible |
| **Visa/relocation = 30% of score** | It is the main feasibility constraint for an India-based candidate |
| **`min_score` 65** | Calibrated to the rubric where "unknown sponsorship" caps the visa component at 15 |
| **Country from Greenhouse `offices`** | Structured and reliable vs free-text `location`; lets US roles be rejected before paying for enrichment |
| **Content-hash dedup + ATS upgrade** | One row per real job across sources; aggregator duplicates become appliable when found on a board |
| **Weekly cooldown for quota APIs** | Stay inside Adzuna/Jooble/Reed free tiers |
| **SQLite + bind mounts** | Single user, zero ops, inspectable from the host |
| **Narrow Gmail searches, read-only scope** | Personal inbox; only fetch mail tied to an application |
| **`superseded` instead of delete** | Keep an audit trail while enforcing one active application per job |

---

## 24. Glossary

| Term | Meaning |
|---|---|
| **ATS** | Applicant Tracking System (Greenhouse, Lever, Ashby, Workday, …) |
| **Slug** | Company identifier on an ATS board, e.g. `adyen` in `job-boards.greenhouse.io/adyen` |
| **Board registry** | The `ats_board` table: every known ATS job board and its status; what collectors poll. Accessed through `CompanyUniverse` (name kept for compatibility) |
| **Board status** | `candidate` (never checked) → `live` (has jobs) / `empty` (0 jobs, polled every 3 days) / `dead` (404/unknown tenant, re-probed after 30 days) |
| **Flywheel** | Auto-adding a `candidate` board whenever an ATS board URL appears in a job found by a non-ATS collector |
| **Validator** | `jobagent boards validate` — probes candidate and stale-dead boards and updates their status |
| **Aggregator** | Board that re-lists jobs from many companies (Adzuna, Jooble, Reed) |
| **Package** | `data/applications/{job_id}/` — resume, cover letter, answers, plan, screenshots |
| **Shadow mode** | Fill the form but never submit |
| **Manual mode** | Fill the form, operator submits |
| **noVNC** | Browser-based view of the container's virtual display (`:6080`) |
| **Golden standard** | DB invariants in §7.5 |
| **Superseded** | Application row replaced by a better one for the same job |
| **Ghosted** | Submitted > 30 days with no reply |
| **visa_ok / relocation_ok** | Boolean flags: sponsorship / relocation explicitly confirmed in the JD |
