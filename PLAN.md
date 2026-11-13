# Job Application Agent — Implementation Plan

> An end-to-end, self-hosted system that **discovers** jobs, **scores** them against your profile, **tailors** a resume and cover letter for each one, **applies** automatically, and **tracks** what happens next.

| Item | Decision |
|---|---|
| Target roles | Data Engineer, Analytics Engineer, Senior/Lead DE, Data Platform Engineer |
| Geographies | India (onsite/hybrid), Remote (global), and **with visa sponsorship + relocation**: **UK and all of Europe** (EU/EEA + CH), Middle East, **APAC** (Singapore, Australia, New Zealand, Japan, Hong Kong, Malaysia, Thailand, Vietnam) |
| Seniority | 9–10 years' experience → **Senior / Lead / Staff** Data & Analytics Engineer. Salary bands per country are in §4.4 |
| Autonomy | **Zero human intervention by default.** You only **observe** (live view, traces, recordings) and **control** (pause / resume / stop at any step). One optional exception, **Auth Assist** (§10A.3a): when a site needs a CAPTCHA, an SMS OTP or a sign-in only you can do, the agent pings you and waits up to 10 minutes while other jobs carry on. If you don't respond, it skips. |
| Run mode | **One click**: a "Run" button in the dashboard (or `jobagent run`) runs discover → score → tailor → apply → track to completion. Scheduling comes later. |
| Hardest part | **ATSs that require an account** (Workday, Taleo, iCIMS, SuccessFactors, Oracle, …). The agent creates the account, verifies the email, fills every page and submits (see §10A) |
| LLM | Personal **Portkey** gateway with paid models; local Ollama only for embeddings (free) |
| Cost | All infra/tools are free and open source; the only spend is LLM tokens (~$1–4/day, see §12) |
| Host | **Docker** (`docker compose up`) – runs on Mac, Linux or any VM; no local Python install needed |
| Candidate defaults | See §19 (resolved). Values that are PII live in `data/profile/answers_seed.yaml` (git-ignored) |
| Starting point | No structured resume, so the system first builds a **Master Career Profile** |

---

## 1. Goals, non-goals, reality check

### Goals
1. Pull jobs from as many sources as practical, every few hours.
2. Normalise and de-duplicate them, then add visa, remote and seniority details.
3. Score fit against a structured profile and reject poor matches cheaply.
4. Build a **truthful**, ATS-friendly tailored resume (PDF), cover letter and screening answers.
5. Submit automatically through the right channel (ATS form, Easy Apply or email), **including creating an ATS account, verifying the email, logging in and filling multi-page wizards** with no human input.
6. Give you **full observability** (live browser, step timeline, recordings, traces) and **control** (pause, resume, stop or abort at any step).
7. Track every application and read the inbox to update its status.
8. Send a daily digest and show everything in a dashboard.

### Non-goals
- Inventing experience. The tailoring step may **select and rephrase** facts from the profile, never add new ones.
- Solving CAPTCHAs automatically or getting around bot protection. **No manual queue:** a blocked application either goes to optional Auth Assist (a time-boxed ping to you) or is marked `blocked`, logged with evidence, retried once later, and then skipped.
- Human approval steps. Every decision point has an automated policy instead (see §9 and §10A).

### Reality check (read this)
- **"All jobs on the internet" can't be reached literally.** About 85–90% coverage of relevant postings is realistic by combining aggregators (LinkedIn, Indeed, Naukri, Bayt and others), **direct ATS APIs** (Greenhouse, Lever, Ashby, and so on) and remote/visa job boards.
- **Auto-applying on LinkedIn breaks its User Agreement** and can get your account restricted. The plan uses LinkedIn **mainly for discovery**. Wherever an external apply link exists, it applies on the company's ATS instead. Easy Apply sits behind a feature flag with strict daily caps.
- **Volume is not the goal; precision is.** At most ~25–40 high-fit applications a day beats 300 spray-and-pray ones. Recruiters notice repeat low-fit applications, and several ATSs share candidate records across a company's roles.

---

## 2. High-level architecture

```
                ┌──────── Run trigger: dashboard "Run" button / `jobagent run` (scheduler later) ───┐
                │                                                                                  │
 ┌──────────────▼──────────────┐   ┌────────────────┐   ┌──────────────┐   ┌─────────────────────┐ │
 │ 1. DISCOVERY (collectors)   │──▶│ 2. NORMALISE   │──▶│ 3. ENRICH    │──▶│ 4. MATCH & SCORE    │ │
 │ JobSpy, ATS APIs, job-board │   │ canonical      │   │ ATS detect,  │   │ hard filters →      │ │
 │ APIs/RSS, HN, sponsor lists │   │ schema, dedup  │   │ visa, remote,│   │ embeddings →        │ │
 └─────────────────────────────┘   └────────────────┘   │ seniority    │   │ LLM rubric score    │ │
                                                        └──────────────┘   └─────────┬───────────┘ │
                                                                                     │ score ≥ T   │
 ┌──────────────────────┐   ┌──────────────────────┐   ┌──────────────────────┐      ▼             │
 │ 8. TRACK & LEARN     │◀──│ 7. APPLY ENGINE      │◀──│ 6. QA / GUARDRAILS   │◀── 5. TAILOR      │
 │ Gmail parse, status, │   │ ATS handlers,        │   │ fact-check, ATS parse│    resume, CL,    │
 │ outcomes → re-weight │   │ Easy Apply, email,   │   │ test, caps, dedup by │    Q&A answers    │
 └──────────┬───────────┘   │ generic LLM browser  │   │ company, kill switch │                   │
            │               └──────────────────────┘   └──────────────────────┘                   │
            ▼                                                                                      │
 ┌──────────────────────────────────┐        ┌─────────────────────────────────────┐              │
 │ 9. Dashboard (Streamlit)         │        │ Shared: SQLite DB, file store,      │◀─────────────┘
 │ 10. Telegram daily digest/alerts │        │ Portkey LLM client, Keychain secrets│
 └──────────────────────────────────┘        └─────────────────────────────────────┘
```

**Design principle: every stage is a DB-driven state machine.** Each stage picks up rows in status `X` and moves them to `Y`. All stages are idempotent and can be retried, so a crash never loses work or sends a duplicate application.

```
job:  discovered → normalised → enriched → filtered_out | scored → rejected_low_score | queued
app:  queued → tailoring → tailored → qa_failed | ready → applying → submitted | failed | blocked | skipped_unanswerable
      applying sub-states: account_check → account_create → email_verify → login → form_page[n] → review → submit
      control states (any time): paused | stopping | stopped | awaiting_auth   (resumable from last checkpoint)
      submitted → acknowledged → rejected | interview | offer | ghosted (no reply after 30 days)
```

---

## 3. Component 0: Master Career Profile (build this first)

There is no structured resume yet, and everything downstream depends on good source data. So the first step builds a **career knowledge base** that is richer than any single resume.

### 3.1 Inputs
**Primary source (decided): your master resume, `~/Documents/resume/master_resume.txt`.** It's already a bullet bank with IDs, tags, variants and guardrails, so the LinkedIn export and LLM interview are **no longer needed**. They become optional extras for filling gaps.

**Importer (`jobagent profile import`): a deterministic parser** (no LLM) for the `== SECTION ==` / `[ID]` / `TAGS:` / `RESUME:` format. It produces `master_profile.yaml`, and the original file stays the single source of truth. Re-running the import picks up your edits.

| Resume element | How the system uses it |
|---|---|
| `[VISTA-01]` … `[BARC-04]` bullet IDs + `TAGS` | The bullet bank. The tailoring planner (§8.1) may only pick these IDs. Tags (CORE, AE, PLAT, REL, PERF, ML, LEAD, FIN, SWE, STAKE) feed ranking. |
| `RESUME` / `SHORT` / `ALT` | Allowed wordings. `RESUME` is the default, `SHORT` is used for 1-page or Workday "description" fields, and `ALT` only when its archetype matches. |
| `DETAIL (STAR)`, `INTERVIEW_PREP` | **Never put on the resume.** Used only as context for cover letters and free-text answers ("Describe a challenging project"). |
| `TITLE VARIANTS`, `SUMMARY VARIANTS` | Chosen per job archetype; the summary may be lightly trimmed, never extended. |
| `TAILORING RECIPES` | **Seeds for the planner.** A cheap LLM call classifies each JD into an archetype (AE/SaaS, FinTech/Risk, Platform/ML-infra, BigCo/Lead). The recipe gives the default bullet order, and the planner then adjusts it to the JD. |
| `SKILLS (FULL INVENTORY)` | Skills section (5–7 rows, reordered by the JD). Also the allow-list for the fact-checker. |
| **`NOT_YET`** | **A hard deny-list.** The fact-checker rejects any resume, cover letter or answer that claims Kafka, Redshift, Scala, AML/KYC and so on. Screening questions like "Years of Kafka?" are answered **0 / No**, honestly. |
| `[VERIFY]` / `VERIFY:` markers and `OPEN ITEMS` | **Gate:** a bullet with an unresolved VERIFY marker is used in its **number-free form** (the metric is dropped) until you resolve it. The importer prints the open-item list on every run. |
| `...` placeholders (e.g. VISTA-04 ALT) | Incomplete text, so never used. |
| `ALT` text with conditions (e.g. "use 'Evaluated and drove' only if you made the decision") | Treated as **disabled** until you remove the condition or confirm it in `answers_seed.yaml`. |
| `WORK AUTHORIZATION` per country | Answers and cover-letter relocation line. Countries not listed use the default (Indian citizen, needs sponsorship). The `§81a` note stays conversation-only, as you wrote. |
| `RELOCATION_LINE_TEMPLATE` | Used verbatim in cover letters, with `[CITY]` filled in. |
| `LANGUAGES` | Hindi native and English fluent go on every resume. Spanish only for ES/LatAm roles (your rule). German is never claimed. |
| `EDUCATION` + anabin note | Shown on the resume. For DE roles, the answer to "Is your degree recognised?" depends on the anabin check (open item). |
| `FILE NAMING` | Output PDF is `Bipin_Singh_Data_Engineer_{Company}.pdf`. |
| `FORM_ONLY` (postal code, DOB) | Only used to fill forms. **Never printed** on the resume. DOB is filled only when the field is *required*. |
| `SKILL YEARS` | "Years with X" answers: whole years since the first role that shows the skill (9 / 5 / 4 as of Nov 2026). `NOT_YET` → 0. |
| Note keys (`NOTE`, `BASIS`, `VERIFIED`, `PLACEMENT`, `OPTIONAL`, `INTERVIEW_PREP`) | Treated as **internal notes**: they guide the planner (for example BARC-02's "don't claim you produced the submissions") but are **never printed** or sent to a form. Any key the parser doesn't know is treated the same way. |
| Deny-list precision | `NOT_YET` matches whole terms. "Glue Data Catalog" is denied, while "AWS Glue" (in the skills list) stays allowed. "Data lineage/metadata management" (claimed) is separate from "data catalogue tools" (denied). |
| `USES_METRICS_FROM` on summaries | A summary inherits its bullets' VERIFY status. For example, SUMMARY-AE contains VISTA-04's 85%, so its number-free form is used until that's resolved. |

Optional later: a LinkedIn export to cross-check dates, and certifications or projects if you add them.

### 3.2 Format: `data/profile/master_profile.yaml`
```yaml
basics:
  name: Bipin Singh
  email: bipin2295@gmail.com
  phone: { country_code: "+91", number: ... }
  location: { city: Pune, country: IN }
  links: { linkedin: linkedin.com/in/bipin-singh-98aa7a129 }
  experience_start: 2017-07    # "9+ years" is computed, never hard-coded
work_authorization:          # used to answer screening questions
  IN: citizen
  EU: requires_sponsorship
  AE: requires_sponsorship
  SA: requires_sponsorship
  QA: requires_sponsorship
  APAC (SG, AU, NZ, JP, HK, MY, TH, VN): requires_sponsorship
  US: requires_sponsorship
languages:
  - { name: English, level: fluent }
  - { name: Hindi, level: native }
  - { name: Spanish, level: elementary, show_on_resume_for: [ES, LATAM] }   # answer honestly if asked elsewhere
preferences:                 # real values: data/profile/answers_seed.yaml
  notice_period: { text: "4 weeks", days: 30 }
  willing_to_relocate: true
  compensation: see §4.4. Per-country {currency, period, expected, floor}, loaded from answers_seed.yaml
experience:
  - id: vista
    company: Vista (Cimpress)
    title: Senior Data Engineer
    start: 2022-07
    end: present
    location: Remote
    bullets:                  # generated by the importer from master_resume.txt
      - id: VISTA-04
        tags: [CORE, AE, PERF, PLAT]
        resume: "Re-platformed a clickstream conversion-funnel pipeline (100M+ daily events) ..."
        alt: { text: "Evaluated and drove ...", enabled: false, reason: "incomplete (...)" }
        metrics: { cost_reduction_pct: 85 }       # numbers extracted for the fact-checker's exact-match check
        verify_open: true                         # → number-free form used until resolved
        skills: [spark, databricks, dbt, snowflake]   # LLM-extracted once at import, cached
skills:
  languages: [Python, SQL]
  data: [dbt, Snowflake, Databricks, Spark, Airflow, BigQuery]
  cloud: [AWS, GCP]
projects: [...]
education: [...]
certifications: [...]
answer_bank:                  # reusable screening answers (grows over time, see §8.4)
  years_experience_total: computed from experience_start (9 as of 2026-11)
  years_experience: { python: ..., sql: ..., dbt: ..., snowflake: ..., airflow: ..., spark: ... }   # you confirm once
  not_yet_skills: → always "0" / "No"   # from the resume's NOT_YET list
  require_sponsorship: { IN: "No", default: "Yes" }
  willing_to_relocate: "Yes"
  notice_period: "4 weeks"       # numeric "days" fields → 30
  earliest_start: "30 days from offer acceptance (relocation roles: 30 days or on visa approval, whichever is later)"
  assessments_ok: "Yes"
  gender / diversity: "Prefer not to say"
  salary_answer_policy:       # how salary fields are filled, by job country
    rule: answer the "expected" value for the JOB's country from §4.4, in its local currency and period
    IN: { current: "<current LPA>", expected: "<expected LPA>" }   # values in answers_seed.yaml
    AE: { expected: "<amount> AED per month" }   # free-text range field → "<floor>–<expected> AED per month"
    remote_pinned_to_country: use that country's band
    remote_global: use the REMOTE (USD) band
    current_ctc_outside_india: only if asked → "<current LPA> INR (~USD equivalent)"; skipped where asking is prohibited (some US states)
    numeric_only_field: the expected number in the currency the form asks for
    unsupported_currency: convert from USD at a fixed rate table in config
```

**Acceptance:** the import parses all 15 bullets, 5 titles, 5 summaries and 4 recipes with no errors. The open-item list is printed. A default resume and one resume per archetype render and pass the ATS parse test.

---

## 4. Component 1: Job Discovery (collectors)

Every collector implements one interface: `fetch(search_config) -> list[RawJob]`. Each one is isolated, rate-limited and has its own error handling, so one broken source never stops the pipeline.

### 4.1 Source matrix

| Tier | Source | Method | Region strength | Notes |
|---|---|---|---|---|
| A | **JobSpy** (`python-jobspy`) | Scraper library | Global | One call covers LinkedIn, Indeed, Glassdoor, Google Jobs, ZipRecruiter, **Naukri**, **Bayt**. Use proxies-off and low volume. |
| A | **Greenhouse** | Public JSON `boards-api.greenhouse.io/v1/boards/{token}/jobs?content=true` | Global, EU heavy | No auth. Very reliable. |
| A | **Lever** | `api.lever.co/v0/postings/{company}?mode=json` | Global | No auth. |
| A | **Ashby** | `api.ashbyhq.com/posting-api/job-board/{name}` | Startups, EU | No auth. |
| A | **SmartRecruiters** | `api.smartrecruiters.com/v1/companies/{id}/postings` | EU, ME | No auth. |
| A | **Workable / Recruitee / Personio / Teamtailor** | Public widget/XML/JSON feeds | **EU** (Personio = DE) | Per-company feeds. |
| B | **Arbeitnow API** | `arbeitnow.com/api/job-board-api` | **EU/DE**, has a `visa_sponsorship` flag | Free. |
| B | **Adzuna API** | Free key | IN, DE, NL, UK, FR, PL… | Good aggregator. |
| B | **Jooble API** | Free key | Global | Aggregator. |
| B | **Remotive / RemoteOK / Himalayas / WeWorkRemotely RSS** | Free APIs/RSS | Remote | |
| B | **HN "Who is hiring"** | Algolia HN API | Remote/US/EU | Monthly thread, parsed by an LLM. |
| B | **Relocate.me, Landing.jobs, EuroTechJobs** | Scrape (light) | **EU relocation** | Built around visa and relocation. |
| C | **GulfTalent, NaukriGulf** | Scrape (light) | **Middle East** | Bayt is already in JobSpy. |
| B | **Reed API** | Free key | **UK** | The biggest UK board with an official API. Adzuna and JobSpy (LinkedIn, Indeed UK) also cover the UK. |
| B | **Welcome to the Jungle (Otta), CWJobs** | Scrape (light) | **UK / EU tech** | Otta has a "visa sponsorship" filter. |
| B | **EURES** (EU job-mobility portal) | Public search API | **All EU/EEA** | Covers smaller markets (CZ, RO, HU, the Baltics and others) that big aggregators miss. |
| A | **MyCareersFuture** (SG government) | Public JSON API `api.mycareersfuture.gov.sg/v2/jobs` | **Singapore** | Free, no key; the **salary range is shown on every job**, which is great for calibration. |
| B | **SEEK** | Scrape (light) | **Australia, New Zealand** | The biggest AU/NZ board; has a "visa sponsorship" keyword search. |
| B | **JobStreet / JobsDB** (SEEK Asia) | Scrape (light) | **MY, SG, TH, HK** | The main boards in South-East Asia. |
| B | **TokyoDev, Japan Dev** | Scrape (light) | **Japan** | Curated **English-speaking** companies with visa-sponsorship and "no Japanese required" filters. Best Japan source by far. |
| B | **ITviec** | Scrape (light) | **Vietnam** | The main tech board in Vietnam. |
| — | *JobSpy / Adzuna country params* | — | AU, NZ, SG, JP, MY, HK, VN, TH | Indeed, LinkedIn and Glassdoor via JobSpy, and Adzuna (AU, NZ, SG), already cover APAC with a country setting. |
| C | **Instahyre, Cutshort, Wellfound, foundit** | Scrape | India | Phase 3, often login-gated. |
| C | **Workday tenants** (`*.myworkdayjobs.com`) | Hidden JSON `/wday/cxs/{tenant}/{site}/jobs` | Large enterprises | Works without auth for search. |

### 4.2 "Company universe" flywheel (the key coverage trick)
ATS APIs need a company slug. Build the list automatically:
1. Whenever any collector returns an `apply_url`, regex out the ATS and slug, for example `boards.greenhouse.io/(\w+)`, `jobs.lever.co/([\w-]+)`, `jobs.ashbyhq.com/([\w-]+)`, `([\w-]+).myworkdayjobs.com`.
2. Seed the list from **visa sponsor registers**:
   - UK Home Office *Register of licensed sponsors* (CSV, public)
   - Netherlands IND *Public register of recognised sponsors*
   - **New Zealand** Immigration NZ *Accredited Employers* list (public)
   - Optionally, Germany/Ireland lists of known tech employers
   - Australia, Singapore and Japan don't publish sponsor registers, so they rely on JD signals and curated boards (TokyoDev, Japan Dev)
3. Store the result in `companies(ats, slug, name, country, sponsor_registers[])`, then poll every known company's board directly.

After a few weeks this becomes thousands of companies polled directly. That is better coverage than any aggregator, with full job descriptions.

### 4.3 Search configuration: `config/search.yaml`
```yaml
titles: ["senior data engineer", "lead data engineer", "staff data engineer", "data engineer",
         "senior analytics engineer", "lead analytics engineer", "analytics engineer",
         "data platform engineer", "senior dbt developer"]
must_have_any: [sql, python, dbt, snowflake, spark, airflow, databricks, bigquery]
exclude_titles: [intern, junior, graduate, "principal scientist", "data entry", "sales"]   # tunable
seniority: [senior, lead, staff]       # 9–10 yrs: mid-level roles are excluded as under-levelled
max_age_days: 14
regions:
  india:   { countries: [IN], cities: [Bengaluru, Hyderabad, Pune, Gurugram, Remote], modes: [onsite, hybrid, remote] }
  remote:  { modes: [remote], allowed_timezones: ["UTC-1..UTC+12"] }   # EU through APAC working hours
  eu:      { countries: [DE, NL, IE, LU, SE, DK, FI, AT, BE, FR, ES, PT, PL, IT, CZ, EE, LT, SI, CY, MT,
                         HU, RO, BG, HR, SK, LV, GR],                  visa_policy: strict,  require_relocation: preferred }
  uk_eea:  { countries: [UK, CH, NO, IS],                               visa_policy: strict,  require_relocation: preferred }
  # Not included: non-EU Balkans, UA, TR, RS. Pay is low and sponsorship for Indian nationals is rare.
  me:      { countries: [AE, SA, QA],                                   visa_policy: lenient, require_relocation: preferred }
  apac:    { countries: [SG, AU, NZ, JP, HK],                           visa_policy: strict,  require_relocation: preferred }
  sea:     { countries: [MY, TH, VN],                                   visa_policy: lenient, require_relocation: preferred }
  apac_optional: { countries: [KR, TW], enabled: false }   # mostly need Korean/Mandarin; enable to test
  # Not included: ID, PH. Senior DE pay there is usually below your current CTC and sponsorship is rare.
language_requirements:        # reject if the JD *requires* any of these (nice-to-have is fine)
  reject_if_required: [German, Dutch, French, Arabic, Swedish, Danish, Finnish, Norwegian, Icelandic, Polish, Italian,
                       Spanish, Portuguese, Czech, Slovak, Hungarian, Romanian, Bulgarian, Croatian, Slovenian,
                       Estonian, Latvian, Lithuanian, Greek,
                       Japanese, Korean, Mandarin, Cantonese, Thai, Vietnamese, Malay]
salary_filter:                # applied only when the posting states salary; unknown salary → keep
  source: §4.4 floors          # compares the posting's MAX salary against the country floor
run_mode: manual             # one-click; switch to `scheduled` later with discovery_cron
# schedule: { discovery_cron: "0 */4 * * *" }
```

**Visa and relocation policy (used in §6 enrichment and §7 scoring):**
- `strict` (UK, EU/EEA, CH, SG, AU, NZ, JP, HK): sponsorship **explicitly ruled out** ("no sponsorship", "must have the right to work", "citizens/PR only") → reject. `unknown` → keep, −10 score unless the company is on a sponsor register or a curated visa board.
- `lenient` (ME, MY, TH, VN): employers hiring foreigners normally handle the work permit, so only an explicit "locals only / must already hold a visa" → reject.
- **Relocation:** "no relocation assistance" stated → reject. Relocation package mentioned → +5 score. Not mentioned → neutral. (Most ads don't say, so making it a hard requirement would remove too many good roles.)
- Screening answers: "Require sponsorship?" → Yes; "Willing to relocate?" → Yes; "Need relocation support?" → Yes.

### 4.3a Scraping backends: native first, Firecrawl and Apify as metered fallbacks

**Short answer: yes, both can be used, but as optional fallbacks, not as the main engine.** The reasons are cost and fit:

| | Firecrawl | Apify |
|---|---|---|
| Free tier (checked Nov 2026) | 1,000 credits/month (≈1,000 pages, or **≈200 pages with JSON extraction**), 10 scrapes/min | $5/month usage credit; **blocked** until next month once used up |
| Strength | Any web page → clean Markdown or structured JSON; renders JavaScript | **Ready-made, maintained scrapers ("actors")** for LinkedIn Jobs, Indeed, SEEK, Naukri, Glassdoor and others; handles anti-bot and proxies |
| Our volume | ~2,000+ JD pages per run → **free tier is gone in one run** | LinkedIn/SEEK actors are paid per result → a few runs per month on $5 |
| Applying / forms | Not suitable: no persistent logged-in sessions, Keychain secrets or live view | Not suitable for the same reasons |

**Why not MCP inside the pipeline:** an MCP server is a tool for an *LLM* to call. Routing thousands of page fetches through an LLM would cost tokens on every call for no benefit. The pipeline calls the **Python SDKs** (`firecrawl-py`, `apify-client`) directly. **Where MCP *is* useful: during development.** I (the coding agent) can use the Firecrawl or Apify MCP to look at job-site page structures quickly while writing collectors and playbooks.

**Design: a pluggable `FetchBackend` per source, with a monthly budget:**
```yaml
# config/fetch.yaml
default_backend: native            # httpx / JobSpy / Playwright, free and unlimited
sources:
  greenhouse|lever|ashby|smartrecruiters|workday|mycareersfuture: native   # public JSON APIs, so no need
  linkedin:   { primary: native_jobspy, fallback: apify, apify_actor: <linkedin-jobs-actor> }
  seek:       { primary: native,        fallback: apify }
  naukri:     { primary: native_jobspy, fallback: apify }
  unknown_career_page: { primary: native_playwright, fallback: firecrawl }   # odd company career sites
budgets:
  firecrawl: { monthly_credits: 1000, reserve_for: [unknown_career_page, jd_cleanup] }
  apify:     { monthly_usd: 5.0 }
fallback_when: [blocked_403, captcha, parse_failed, zero_results_but_expected]
```
- **Native first**: about 90% of jobs come from public ATS JSON APIs, where Firecrawl and Apify add nothing.
- **Fallback triggers**: only when the native scraper fails (blocked, broken HTML) for a high-value source such as LinkedIn, SEEK or Naukri, or for an unknown career page that needs JavaScript rendering.
- **Budget guard**: every credit used is tracked in `runs`. When the monthly budget is used up, the source is skipped and a dashboard warning shows. There's never a surprise bill.
- **Upgrade path**: if fallback use turns out to be valuable, Apify Starter ($19/month) or Firecrawl Hobby ($19/month) is a config change. **Free by default.**

### 4.4 Compensation bands by country

For a **Senior/Lead Data or Analytics Engineer with 9–10 years' experience** in dbt, Snowflake, Databricks, Airflow, Python and SQL. Figures are gross annual **base** pay in local currency.
- **Expected** = what the agent writes in salary fields. It's set around the market median-to-p75 for this profile, so it isn't priced out.
- **Floor** = the hard filter. A posting whose maximum is below this is skipped.

> **The actual expected/floor values are personal and live only in the git-ignored
> `data/profile/answers_seed.yaml` (`compensation:` block, one row per country).** This table lists
> which countries have a band, the currency/period used, and the typical visa route.

| Region | Country | Currency / period | Typical visa route |
|---|---|---|---|
| India | IN | INR (LPA) | — |
| Remote | Global/USD | USD / year | contractor/EOR |
| EU | DE | EUR / year | EU Blue Card |
| EU | NL | EUR / year | Highly Skilled Migrant (+30% ruling) |
| EU | IE | EUR / year | Critical Skills Employment Permit |
| EU | LU | EUR / year | EU Blue Card |
| EU | SE | SEK / year | Work permit |
| EU | DK | DKK / year | Pay Limit / Positive List |
| EU | FI | EUR / year | Specialist permit |
| EU | AT | EUR / year (incl. 13th/14th) | Red-White-Red / Blue Card |
| EU | BE | EUR / year | Single permit / Blue Card |
| EU | FR | EUR / year | Talent Passport |
| EU | ES | EUR / year | Highly Qualified Professional |
| EU | PT | EUR / year | Tech visa / Blue Card |
| EU | PL | PLN / year (employment contract) | Work permit / Blue Card |
| EU | IT | EUR / year | Blue Card |
| EU | CZ | CZK / year | Employee Card / Blue Card |
| EU | EE | EUR / year | D-visa / Blue Card |
| EU | LT | EUR / year | Blue Card |
| EU | SI | EUR / year | Single permit / Blue Card |
| EU | CY / MT | EUR / year | Single permit / Key Employee |
| EU (low priority) | HU | HUF / year | Blue Card |
| EU (low priority) | RO | RON / year | Blue Card |
| EU (low priority) | BG / HR / SK / LV | EUR / year | Blue Card |
| EU (low priority) | GR | EUR / year | Blue Card |
| UK | London | GBP / year | Skilled Worker (employer on the sponsor register) |
| UK | Rest of UK | GBP / year | Skilled Worker |
| EEA | CH | CHF / year | Non-EU permit (quota; hard) |
| EEA | NO | NOK / year | Skilled worker permit |
| EEA (low priority) | IS | ISK / year | Work permit |
| ME | AE | AED / month | Employer-sponsored |
| ME | SA | SAR / month | Employer-sponsored |
| ME | QA | QAR / month | Employer-sponsored |
| APAC | SG | SGD / year | Employment Pass (COMPASS) |
| APAC | AU | AUD / year (+ super) | Skills in Demand (482) |
| APAC | NZ | NZD / year | Accredited Employer Work Visa |
| APAC | JP | JPY / year | Highly Skilled Professional / Engineer |
| APAC | HK | HKD / year | General Employment Policy |
| SEA | MY | MYR / year | Employment Pass Cat. I |
| SEA | TH | THB / year | BOI work permit / LTR visa |
| SEA | VN | USD / year | Work permit |
| North America | CA | CAD / year | Global Talent Stream / Express Entry |

**Notes:**
- These are **starting estimates** from public salary surveys and typical market ranges. They are not live data. The system recalibrates them (below).
- **Low-priority markets** (HU, RO, BG, HR, SK, LV, GR, IS, MY, VN): pay is at or below your India target in USD (or the market is tiny). They're kept but ranked lower when scores tie.
- **Switzerland:** non-EU permits are quota-limited, so expect few sponsored roles. Kept because the pay is high.
- **UK:** the Home Office sponsor register (already in §4.2) marks every company that *can* sponsor, so UK visa detection is the most reliable of all regions.
- **MY, VN and lower-end TH pay at or below your India target in USD**, but cost of living is much lower and tax is light. They're kept because you asked for them, but they get lower priority when scores tie. Set `enabled: false` on that row to drop a country.
- **Japan:** most data roles require Japanese (JLPT N2+), so the language filter will remove the majority. The English-speaking ones come mainly from TokyoDev, Japan Dev and international companies.
- **Australia:** `super` (11.5–12% retirement contribution) is paid on top, so forms asking for "base" get the base figure and forms asking for "package" get `package_incl_super` from the seed.
- **Netherlands / Sweden / Denmark / Austria:** several forms ask for monthly salary. The agent divides by 12, or by 14 for Austria.

**Automatic calibration** (added in Phase 3, no manual work): after each run, a job computes the **p50/p75 of posted salaries** for jobs that scored at least 75, per country. Good sources are MyCareersFuture, Adzuna's salary histogram API (AU, NZ, SG, DE, NL, FR, PL, AT, ES…) and posted ranges in EU and AU ads. The dashboard shows *configured vs observed*. If they differ by more than 15% across at least 20 samples, it **suggests** a new value; it doesn't change anything until you accept it, so no low-ball answers go out silently.

---

## 5. Component 2: Normalisation and de-duplication

**Canonical `Job` model** (Pydantic): `id, source, source_job_id, url, apply_url, ats_type, company, company_normalised, title, title_normalised, location_raw, country, city, remote_type, description_md, posted_at, salary_min/max/currency, employment_type, first_seen_at, last_seen_at, content_hash`.

**Dedup is layered:**
1. Exact: `(source, source_job_id)`.
2. Cross-source: `sha1(company_normalised | title_normalised | country)`.
3. Fuzzy: `rapidfuzz` title ratio > 90 at the same company, **or** description embedding cosine > 0.95.
4. When two records match, keep the one with the best `apply_url` (direct ATS beats aggregator) and merge sources.

Descriptions are converted to clean Markdown with `markdownify` and `selectolax`.

---

## 6. Component 3: Enrichment

| Field | How |
|---|---|
| `ats_type` | URL pattern match (Greenhouse, Lever, Ashby, Workable, SmartRecruiters, Workday, iCIMS, Taleo, SuccessFactors, Personio, Teamtailor, BambooHR, LinkedIn Easy Apply, email, unknown). Follow redirects with `httpx` when the link is an aggregator link. |
| `visa_sponsorship` | 1) The company appears on a sponsor register. 2) Regex for phrases like "visa sponsorship", "relocation package", "Blue Card", "we sponsor". 3) Negative regex for "must have the right to work", "no sponsorship", "EU citizens only". 4) The LLM extraction pass (below) gives the final value: `yes / no / unknown` plus evidence. |
| `remote_type`, `seniority`, `years_required`, `required_skills`, `nice_to_have_skills`, `salary` | **One structured LLM extraction call** with a cheap model and JSON schema output. The result is cached by `content_hash`. |
| Company info | Optional: size and industry from the description. Blacklist/whitelist checks. |

**Visa rule:** apply the `strict` / `lenient` policy from §4.3 by country. Also extract `relocation_support: yes / no / unknown` in the same LLM call.

---

## 7. Component 4: Matching and scoring (a cost-aware funnel)

```
~2,000 new jobs/day ─▶ [Hard filters, free] ─▶ ~400 ─▶ [Embedding sim, free/local] ─▶ ~120 ─▶ [LLM rubric, cheap model] ─▶ ~30–50 queued
```

1. **Hard filters (rules):** title include/exclude, age, country/mode, visa rule, years-required ≤ yours + 2, blacklisted companies, already applied to the same company in the last N days.
2. **Embedding similarity:** local Ollama `nomic-embed-text` or `bge-m3`, stored with `sqlite-vec`. Compare profile vs JD and keep the top-K above a threshold. This costs nothing.
3. **LLM rubric score** (JSON output, cheap or medium model):
   ```json
   { "score": 0-100,
     "skills_match": 0-40, "experience_match": 0-25, "domain_match": 0-10,
     "location_visa_fit": 0-15, "seniority_fit": 0-10,
     "missing_must_haves": ["kafka"], "red_flags": ["requires German C1"],
     "pitch_angle": "Emphasise dbt migration + Snowflake cost optimisation" }
   ```
4. **Decision:** `score ≥ 75` → auto-apply queue. `60–74` → "maybe" list in the dashboard, not auto-applied. Below 60 → reject. Thresholds live in config.
5. **Priority order:** score × freshness (applying within 48h matters a lot) × channel reliability.

---

## 8. Component 5: Tailoring (resume, cover letter, answers)

### 8.1 Resume pipeline
1. **Plan (LLM, strong model):** input is the JD, the structured requirements and the full master profile. It returns JSON with selected bullet IDs per role (ranked), suggested **rephrasings** of those bullets (mirroring JD wording only where it's true), skills ordering, summary text, and section ordering.
2. **Constraint:** the output refers to `bullet_id`s, so every line traces back to a real fact.
3. **Fact-check pass (separate LLM call plus rules):** each rewritten bullet is compared with its source bullet. It must not add tools, numbers, titles or dates that aren't in the source. Numbers are checked with regex for an exact match. If a bullet fails, it falls back to the original wording.
4. **Render:** use **RenderCV** (YAML → Typst → PDF, free, ATS-friendly single column) or a custom **Typst** template driven by Jinja2. Aim for 1 page (≤ 5 years experience) or 2 pages maximum.
5. **ATS parse test:** extract text from the PDF with `pypdf`, then confirm the name, email, section headers and ≥ 80% of the JD's required keywords that you *actually have* are present and in reading order.
6. **Output:** `data/applications/{job_id}/resume.pdf`, `resume.yaml`, `plan.json`.

### 8.2 Cover letter
Only generated when the form has a field for it or asks for it. About 180–250 words, built from `pitch_angle` plus two of the strongest relevant bullets plus a company-specific sentence taken from the JD. It goes through the same fact-check.

### 8.3 Screening questions
- **Deterministic first:** map common fields straight from `answer_bank` (work authorisation by country, sponsorship, notice period, CTC, relocation, years with X, LinkedIn URL, gender/EEO → "prefer not to say").
- **LLM fallback:** for free-text questions ("Why do you want to work here?"), answer from the profile plus the JD.
- **Unknown factual question** (for example "Do you have a German driving licence?"): no human is asked. A fixed policy decides, in this order:
  1. **Inferable from the profile?** For example, "Do you have 3+ years of Spark?" is answered from `years_experience`. The LLM must cite the profile field it used.
  2. **Has a safe truthful default?** Optional yes/no → "No". Optional free text → skip. "How did you hear about us?" → "Job board".
  3. **Required, factual, and can't be answered truthfully** → the application becomes `skipped_unanswerable`, the question is logged, and the agent moves to the next job. **It never guesses on legal or eligibility questions** (work authorisation, criminal record, licences, security clearance, salary history in jurisdictions where asking is prohibited).

### 8.4 Learning loop (offline, optional)
Every question is saved to `questions_seen` with its frequency. The dashboard ranks the questions that caused the most skips ("this question blocked 14 jobs"). You can add answers to `answer_bank` whenever you like, and that fixes future runs. The agent never waits for you.

---

## 9. Component 6: QA and guardrails (required for fully-auto mode)

| Guardrail | Default |
|---|---|
| Global daily cap | 40 applications/day |
| Per-channel caps | LinkedIn Easy Apply ≤ 15/day (feature-flagged, **off** at first); Workday ≤ 10/day |
| Per-company cap | 1 active application per company per 30 days (configurable for large companies) |
| Never re-apply | Unique on `(company_normalised, title_normalised)` forever |
| Freshness | Skip jobs older than 21 days |
| Min score | 75 |
| Fact-check must pass | Yes |
| ATS parse test must pass | Yes |
| **Shadow mode** (first ~1 week, automatic) | The agent runs everything, including account creation, but stops at the final Submit. An **automated verifier** (§10A.6) grades each filled form. Live submission switches on automatically when the per-ATS fill success rate is ≥ 90% over 20 runs. No human sign-off. |
| **Pre-submit verifier** | Before every real Submit, an independent LLM call reviews the review page or final form screenshot against the application package. If it fails, the agent fixes the fields (up to 2 attempts) or aborts. |
| **Stop / pause** | Graceful and hard stop, globally, per worker, or per application. See §10B |
| Human-like pacing | Random 45–180s between submits on the *same* ATS family; different families can run in parallel (max 3 browsers) |
| Language filter | Reject when German, Dutch, French, Arabic or other non-English languages are **required** (§4.3) |
| Salary filter | Reject when the posted salary is below the regional floor (§4.3); unknown salary → keep |
| Blacklist | **Cimpress, Vistaprint** (`config/blacklist.yaml`). Matched by company name **and** careers domain, so it doesn't hit unrelated "Vista …" companies |

---

## 10. Component 7: Apply engine

A **strategy pattern**: `ApplyHandler.can_handle(job) -> bool`, then `apply(job, package) -> Result`. Handlers are tried from most to least reliable.

| Priority | Handler | Tech | Notes |
|---|---|---|---|
| 1 | **Greenhouse** | Playwright | Standard form layout. Upload the resume and map the custom questions. |
| 1 | **Lever** | Playwright | Simple single page. |
| 1 | **Ashby** | Playwright | React form. Stable field labels. |
| 1 | **Workable / SmartRecruiters / Personio / Teamtailor / Recruitee** | Playwright | EU-heavy, so these are worth it. |
| 1 | **Workday** | Playwright playbook + agent fallback | Needs an account per tenant. Fully automated by the account agent (§10A). Uses stable `data-automation-id` attributes. **Top priority**, because it's your biggest pain point. |
| 2 | **Taleo / iCIMS / SuccessFactors / Oracle Recruiting Cloud / Phenom / Jobvite / Avature** | Agent with per-family hints | Account-based. Handled by the §10A account agent. |
| 2 | **LinkedIn Easy Apply** | Playwright with a persistent logged-in profile | ToS risk. Feature flag, low caps, randomised pacing. |
| 2 | **Email apply** | Gmail API `gmail.send` (off in the initial phase) | When the JD says "send CV to ...". Short tailored email plus PDF. |
| 3 | **Generic LLM browser agent** | **`browser-use`** (open source) + Portkey model | For unknown ATSs. Gets the DOM/accessibility tree, maps fields to the profile and fills them. Slower and costs more tokens, so it gets a lower daily cap. |
| — | **No handler succeeded** | — | `failed` or `blocked` with full trace and recording. Auto-retried once after 24h with the generic agent, then dropped. No human queue. |

**Common handler behaviour:**
- Use a persistent Playwright browser context (cookies kept), headed or headless as configured.
- Map fields by **label text, not CSS selectors**: `get_by_label`, `get_by_role`. An LLM mapper is the fallback for unmapped required fields.
- Take screenshots before and after submit and save the HTML of the confirmation page as proof.
- Detect success through a confirmation URL/text or a confirmation email.
- When a CAPTCHA, SMS/phone OTP or SSO wall appears → Auth Assist if it's on (§10A.3a), else `blocked` with evidence. **Email** OTPs and verification links are handled automatically (§10A.3).
- Retries: at most 2, with backoff. A selector failure becomes a bug report in the dashboard with the screenshot.

---

## 10A. Account-creation browser agent (your biggest pain point)

This covers ATSs where you must **create an account, verify your email, log in, and then fill 4–8 pages** of details that are already on your resume. The agent does all of it without any human step.

### 10A.1 Target ATS families (by priority)

| ATS | How common for DE roles in IN/EU/ME | Account? | Notes |
|---|---|---|---|
| **Workday** (`*.myworkdayjobs.com`) | Very high (enterprises, banks, MNCs in IN/EU/ME) | Yes, **one per company tenant** | Pages: My Information → My Experience → Application Questions → Voluntary Disclosures → Self Identify → Review. Stable `data-automation-id` attributes. |
| **SAP SuccessFactors** | High in EU and ME | Yes | Long single form plus a profile. |
| **Oracle Recruiting Cloud / Taleo** | High in ME, banks, telecom | Yes (email + OTP or password) | Taleo is older and has many pages. |
| **iCIMS** | Medium | Yes | Often uses an iframe. |
| **Phenom, Avature, Jobvite, Eightfold, SmartRecruiters (some)** | Medium | Sometimes | Handled by the agent with family hints. |

### 10A.2 Identity and credential vault
- **Your main Gmail, `bipin2295@gmail.com`** (decided). Since it's your personal inbox, access is kept tight:
  - **Per-tenant aliases** with plus-addressing: `bipin2295+acme-workday@gmail.com`. Mail still arrives in your inbox, but every message can be matched to the exact account. If a site rejects `+`, the agent falls back to `bipin2295@gmail.com` and matches mail by sender domain plus time window instead.
  - **No Gmail filter or label needed.** Every read is a **narrow, server-side search**: either `deliveredto:bipin2295+{tenant}@gmail.com newer_than:2d`, or `from:({ats/company domains}) after:{application time}`. The agent only fetches messages that match an application it made. **Other mail is never fetched or sent to an LLM.**
  - **Access method, initial phase (decided): manual sign-in in the browser.** No app passwords. Run `jobagent gmail connect` (or click **Connect Gmail** in the dashboard). Your normal browser opens Google's sign-in and consent page, you log in yourself (including your own 2FA), and you approve **read-only** access (`gmail.readonly`). The agent never sees or stores your Google password. Only the access token is kept, in Keychain.
    - It uses your own free Google Cloud project with the OAuth app in "Testing" status, and you as the only test user (one-time, about 10 minutes, steps in §20).
    - **Expected manual step:** Google expires "Testing" tokens after **7 days**. The preflight check (§10C) catches that and shows **Reconnect Gmail**. One click, sign in again, done. That fits your "keep it manual" choice.
    - If Gmail isn't connected during a run, email verification falls back to **Auth Assist**: *"Acme Workday sent a verification email to bipin2295+acme-workday@gmail.com, please click it"*. The agent continues automatically once the page moves on.
    - Later (optional): IMAP + App Password for fully hands-free access.
  - **Sending** (email-apply channel): **off in the initial phase**, since read-only access can't send. Later it can be turned on with the `gmail.send` scope, capped at 5/day and only to addresses written in the JD.
  - **Never** sign into "Sign in with Google" on ATS sites automatically. If a site only offers Google SSO, it goes to Auth Assist (§10A.3a).
- **Generated passwords:** 20 characters that satisfy every complexity rule (upper, lower, digit, symbol from a safe set). Each one is stored in **macOS Keychain** (`keyring`) under `jobagent/{ats}/{tenant}`. They are never logged, and the LLM never sees them: the agent types them through a `fill_secret(field)` tool that injects the value directly.
- **`accounts` table:** `ats, tenant, login_url, email_alias, keychain_ref, status (creating|unverified|active|locked), created_at, last_login_at, data_submitted_json`. Accounts are reused: the second job at the same company skips sign-up, and Workday pre-fills "My Experience".
- **Self-healing login:** if login fails, the agent runs "Forgot password" → reads the email → sets a new password → updates Keychain.

### 10A.3 Automated email verification and OTP
`EmailProvider` interface: Gmail API with read-only OAuth in production (scoped as in §10A.2), Auth Assist fallback when not connected, **Mailpit** (local, free) in tests.
1. After sign-up, the agent calls `wait_for_email(to=alias, from_domain≈ats/company, since=t0, timeout=10m)`.
2. It extracts the **verification link** (regex for `verify|activate|confirm` URLs, with an LLM fallback) or the **OTP code** (`\b\d{4,8}\b` next to "code", with an LLM fallback).
3. It opens the link in the **same browser context**, or types the code.
4. Timeout → click "Resend" once → still nothing → `blocked: email_verification_timeout`.
5. **SMS/phone OTP** (sent to the candidate's mobile number) can't be read automatically → Auth Assist (below), else `blocked`.

### 10A.3a Auth Assist (optional, time-boxed, off the critical path)
For the few things only you can do: **CAPTCHA, SMS OTP, Google/LinkedIn SSO, a "verify it's you" prompt**.
1. The agent **parks** that application. Its browser context is saved and the application is marked `awaiting_auth`. Other applications keep running in parallel.
2. You get a Telegram message: *"Workday @ Acme needs a CAPTCHA → [open live view]"*. The link opens the noVNC live browser at that page.
3. You solve it in the live view (or reply `/otp 123456` for SMS codes). The agent detects the page has moved on and **continues on its own**.
4. **Timeout of 10 minutes** (configurable) → the application is marked `blocked`, and it gets retried in the next run.
5. `auth_assist: on | off` in `guardrails.yaml`. When it's off, the system behaves exactly as zero-touch.

Since you're starting runs manually for now, you'll usually be around, so most of these should get through.

### 10A.4 Agent architecture: "Playbook first, Agent fallback, Learn from the Agent"

```
            ┌──────────────── Application Orchestrator (per job) ────────────────┐
            │  goal + application package + account creds ref + guardrails       │
            └───────────────┬────────────────────────────────────────────────────┘
                            ▼
   ┌─────────────── Page loop (until confirmation or abort) ───────────────┐
   │ 1. OBSERVE   accessibility tree + numbered-element screenshot          │
   │ 2. CLASSIFY  page_type ∈ {login, signup, verify_email, upload_resume,  │
   │              personal_info, experience, education, questions,          │
   │              disclosures, self_identify, review, confirmation,         │
   │              captcha, error, unknown}                                  │
   │ 3. PLAN      Layer 1: deterministic PLAYBOOK for (ats, page_type)?     │
   │              Layer 2: LEARNED trajectory for (tenant, page signature)? │
   │              Layer 3: LLM AGENT (browser-use / custom loop)            │
   │ 4. ACT       restricted tools: click, type, select, combobox_pick,     │
   │              upload, check, scroll, wait, fill_secret, read_email      │
   │ 5. VERIFY    read field values back from the DOM; required fields set? │
   │ 6. CHECKPOINT persist page state → check STOP/PAUSE flags              │
   └────────────────────────────────────────────────────────────────────────┘
```

- **Layer 1, playbooks** (`playbooks/workday.yaml` and others): step definitions keyed on stable attributes. They are cheap, fast and testable.
- **Layer 2, learned trajectories:** when the LLM agent completes a page, the action sequence is saved against a **page signature** (a hash of form labels and structure). Next time the same tenant/page appears, it is replayed without the LLM and verified afterwards. **Cost per application drops over time.**
- **Layer 3, LLM agent:** handles any unknown page or step. When a playbook step fails mid-page, the agent takes over that page and gives control back on the next recognised page.
- **Field value resolver** (shared by all layers), in order: `master_profile` → `answer_bank` → tailored package (resume, cover letter) → LLM inference **with citation** → default policy (§8.3) → else `skipped_unanswerable`.

### 10A.5 Hard form widgets (solved once, reused)
- **Resume-upload autofill** (Workday, SuccessFactors): upload the PDF, let the ATS parse it, then **audit and correct** every parsed field against the profile. ATS parsers often mangle dates and titles.
- **Repeating sections:** "Add another work experience/education" N times, with deletion of wrong auto-parsed entries.
- **Typeahead/comboboxes** (Workday country, phone code, school, "How did you hear"): type → wait for options → fuzzy-pick the best match (`rapidfuzz`) → verify.
- **Date pickers**, split month/year selects, "currently work here" checkboxes.
- **Phone with country code**, structured addresses, state/province lists.
- **iframes** (iCIMS) and shadow DOM. Playwright frame locators pierce them.
- **EEO / self-identify / disability:** answer from `answer_bank`, default "Prefer not to say / Decline".
- **Consent checkboxes:** privacy/terms required for applying → accept. **Marketing opt-ins and "share my profile with other companies" → always unchecked.**

### 10A.6 Automated verification (replaces a human reviewer)
1. **Per-page read-back:** after filling, read every field's value from the DOM and diff it against the intended values. Mismatches are fixed with up to 2 retries.
2. **Pre-submit verifier** (a separate LLM call on a different model from the agent): input is the review page text and screenshot plus the application package. Output is `{pass, issues[]}`. It checks name, email, phone, work authorisation, sponsorship answer, salary answer, the correct resume file and the correct job title. On fail, the agent fixes and re-verifies. A second fail means abort (`failed: verifier`).
3. **Post-submit:** detect the confirmation page or text, save HTML and a screenshot, then confirm with the "Thank you for applying" email when it arrives.

### 10A.7 Safety boundaries (so autonomy can't go wrong)
- **Domain allowlist per application:** the job's ATS domain, its SSO/auth domains and the company careers domain. Navigating anywhere else is blocked.
- **Anti-phishing:** `fill_secret` only works when the current domain matches the account's registered domain.
- **Prompt-injection defence:** page and JD text are treated as untrusted data. The agent's goal and tool set are fixed in the system prompt. Instructions found on a page ("ignore previous instructions…") are ignored and logged.
- **Forbidden actions:** payments, deleting accounts, withdrawing other applications, uploading any file other than this application's resume or cover letter, and messaging recruiters.
- **Budgets per application:** maximum 120 actions, maximum 25 minutes, maximum $1.00 in LLM spend. Exceeding any of them means abort with the trace saved.
- **One live session per tenant at a time.** No parallel logins to the same Workday tenant.

### 10A.8 Browser hygiene (legitimate, not evasion)
- Playwright with **real Chrome** (`channel="chrome"`) and a persistent profile per ATS family, a normal user agent, your real timezone/locale, and human-like typing and pacing delays.
- Headed browser on a virtual display (Docker + Xvfb) so you can watch it live (§10B). Headless is optional.
- **No CAPTCHA solving, fingerprint spoofing or proxies.** If a site blocks automation, it's skipped.

### 10A.9 Account lifecycle and privacy
- Every account records what personal data was submitted.
- `jobagent accounts list` and `jobagent accounts purge --older-than 180d` help with GDPR deletion requests (the email is generated by the agent and sent automatically).

---

## 10B. Observability and control (your only touchpoints)

### 10B.1 What you can watch
| Layer | Tool (free) | What you get |
|---|---|---|
| **Live browser** | Headed Chrome in Docker + **Xvfb + noVNC**, embedded in the dashboard | Watch any running application in real time, from your laptop or phone |
| **Step timeline** | Dashboard, from `app_steps` | Every action with time, page type, element, value (PII-masked), agent's short reasoning, screenshot thumbnail, tokens/cost |
| **Session replay** | Playwright **video** (`record_video_dir`) + **trace.zip** (Playwright Trace Viewer: DOM snapshots, network, console) | Full post-mortem of any application |
| **LLM tracing** | **Langfuse** (self-hosted, Docker, free) + Portkey logs, one trace ID per application | Every prompt and response, latency and cost, grouped per job |
| **Metrics** | Dashboard (optionally Prometheus + Grafana) | Funnel, success rate per ATS/tenant, average time and cost per application, top block/skip reasons, Layer-1/2/3 usage ratio |
| **Logs** | `structlog` JSON with `run_id`, `application_id`, `step` | Searchable and correlated |
| **Notifications** | Telegram (no reply needed) | Each submission (with confirmation screenshot), each failure or block (with reason), interview emails, daily digest |

### 10B.2 How you control it
| Command | Scope | Behaviour |
|---|---|---|
| **Pause** | global / stage / ATS / application | Finishes the current atomic action, checkpoints, then waits. **Resume** continues from the checkpoint. |
| **Stop (graceful)** | same | Finishes the current page, checkpoints, closes the browser. The application becomes `stopped` and can be resumed later. |
| **Abort (hard)** | same | Cancels the task immediately and closes the browser context. The application is `stopped` and the state is re-checked on resume (was it actually submitted?). |
| **Disable** | ATS / tenant / company | For example "stop applying via Taleo", without stopping everything else. |
| **Caps live-edit** | global | Change daily caps or the score threshold on the fly. |

**Channels:** dashboard buttons, Telegram (`/pause`, `/resume`, `/stop`, `/abort 123`, `/status`, `/live 123` returns the noVNC link), CLI (`jobagent ctl pause --ats workday`), and the file sentinel `data/STOP`.

**How it's built:**
- A `control_flags` table holds the control state. Each worker keeps an in-memory `asyncio.Event` that is refreshed every second.
- **Cooperative cancellation checks run before every agent action** (not just between jobs), so a stop takes effect within about 1–2 seconds.
- **The Submit click is the only critical section.** A stop that arrives during it waits until the click and its confirmation finish, so you never end up with an unknown half-submitted state.
- **Checkpoints** (`app_checkpoints`) save the account state, the last completed page and the filled values. Resume logs back in and continues; it never repeats sign-up.

---

## 10C. One-click run (how you'll use it day to day)

**Start:** press **▶ Run** in the dashboard, or run `jobagent run` in a terminal. Optional flags: `--limit 20`, `--regions eu,me`, `--shadow`.

What a run does, end to end, with no further input:
1. **Preflight (about 10 seconds):** check Portkey, Ollama, the Gmail token (expired → "Reconnect Gmail" prompt, or continue with Auth Assist for verification emails), Docker (noVNC/Langfuse), disk space and today's caps. If anything fails, it stops with a clear message instead of failing halfway through.
2. **Discover** from all collectors in parallel, then normalise and dedup.
3. **Enrich and score** new jobs only (cached jobs are free).
4. **Tailor** the top N jobs by priority.
5. **Apply** with up to 3 parallel browsers (one per ATS family), with live view available.
6. **Track:** run the narrow Gmail searches for each open application and update statuses.
7. **Summary:** a run report in the dashboard and on Telegram covering found, scored, applied, blocked and skipped (with reasons), cost, and time taken.

The run is a single resumable `runs` row. If you close the laptop or stop it, the next ▶ Run continues where it left off. **Scheduling** later is just the same `jobagent run` called from `launchd`.

---

## 11. Components 8–10: Tracking, inbox and UX

- **Gmail watcher** (Gmail API, read-only, limited to per-application alias and domain searches, §10A.2): on each run, and every 30 minutes while a run is active, it fetches new mail, matches it to an application by company/domain/subject, and an LLM classifies it as `acknowledged | rejected | interview_invite | assessment | offer | other`. Status updates automatically, and interviews trigger a Telegram alert.
- **Ghosting:** after 30 days with no reply, mark it `ghosted`.
- **Dashboard (Streamlit):** funnel metrics, the job list with scores, the application detail view (JD, tailored PDF, answers, screenshots), the **live agent view and controls (§10B)**, the top skip/block reasons, config toggles and stop/pause.
- **Telegram bot** (free): a daily digest (found / applied / responses), instant alerts for interviews and failures, and the `/stop`, `/start` and `/status` commands.
- **Analytics feedback:** response rate by source, ATS, region, score band and resume variant. This is used to adjust thresholds every few weeks and later, optionally, to calibrate scoring.

---

## 12. LLM layer (Cimpress AI Gateway / Portkey) ✅ Connected

**Gateway:** `https://gateway.ai.cimpress.io/v1` — the same gateway OpenCode uses, so the same API key works.

**How it works:** the gateway is a Portkey instance. Auth flows via the `x-portkey-api-key` header (the OpenAI SDK's `api_key` is set to `"dummy"`). The model field uses the **internal ID** from the gateway's `.well-known` document, not the display name. The gateway then routes to the correct underlying provider (EU-hosted Anthropic, Valar, OpenAI Bedrock, Vertex AI EU, etc.).

**Current model mapping** (`config/models.yaml`):

  | Task | Model (internal id) | Why |
  |---|---|---|
  | JD extraction, scoring, field mapping, classify, OTP | `@recommended/deepseek-ai/DeepSeek-V4.1-Flash-1017` | Fast, vision-capable, cheap (~$0.26/$1.02 per 1M) |
  | Resume plan, rewrite, cover letter, browser agent | `@Anthropic/eu.anthropic.claude-sonnet-5-5` | Strong, EU-hosted (GDPR), same quality as OpenCode session |
  | Fact-check, pre-submit verifier | `@recommended/deepseek-ai/DeepSeek-V4.1-Flash-1017` | **Different model from the writer** for independent judgment |
  | Embeddings | Ollama `nomic-embed-text` (local) | Free, no tokens |

- **Structured output** with Pydantic validation and auto repair-retry (up to 2 times).
- **Local embeddings** via Ollama: similarity search costs zero tokens.
- **Prompts** versioned in `prompts/*.md`, version hash stored on every output.

**Rough cost on the Cimpress gateway:**
- Extraction + scoring: ~150 jobs × ~4k tokens on DeepSeek Flash ≈ **~$0.07/day**
- Tailoring + cover letter: ~40/day × ~15k tokens on Claude Sonnet ≈ **~$0.50–1.50/day**
- Account-based browser agent: ~$0.10–0.50 per application initially
- **Total ≈ $1–3/day** (cheaper than public Portkey because the gateway has negotiated rates)

> Note: this is your **personal/work API key** routed through Vista's gateway. This is for personal job search only — keep it on your personal machine, not a shared server.

---

## 13. Data model (SQLite, via SQLModel/SQLAlchemy)

```
companies(id, name, name_norm, ats_type, ats_slug, country, on_sponsor_register, blacklisted, last_polled_at)
jobs(id, company_id, source, source_job_id, url, apply_url, ats_type, title, title_norm, country, city,
     remote_type, description_md, posted_at, salary_min, salary_max, currency, content_hash,
     visa_sponsorship, seniority, years_required, required_skills_json, status, first_seen_at, last_seen_at)
job_sources(job_id, source, source_url)                    -- one job seen on many boards
scores(job_id, model, prompt_version, score, breakdown_json, pitch_angle, created_at)
applications(id, job_id, status, channel, resume_path, cover_letter_path, answers_json, plan_json,
             fact_check_json, dry_run, attempts, error, submitted_at, confirmation_path, created_at)
app_events(application_id, ts, from_status, to_status, note)
emails(id, gmail_id, application_id, received_at, from_addr, subject, classification, snippet)
questions_seen(id, question_text_norm, ats_type, example_job_id, answer, answered_by, created_at)
runs(id, stage, started_at, ended_at, items_in, items_out, errors)   -- observability
llm_calls(id, task, model, prompt_version, tokens_in, tokens_out, cost_usd, latency_ms, cached, trace_id)

-- account agent + observability/control (§10A, §10B)
accounts(id, ats, tenant, login_url, email_alias, keychain_ref, status, created_at, last_login_at, data_submitted_json)
app_steps(id, application_id, ts, page_type, layer, action, target, value_masked, reasoning, screenshot_path,
          tokens, cost_usd, ok, error)
app_checkpoints(application_id, page_index, page_signature, account_id, filled_values_json, updated_at)
learned_trajectories(id, ats, tenant, page_signature, actions_json, success_count, fail_count, last_used_at)
control_flags(scope, scope_id, state, updated_at, updated_by)   -- scope: global|stage|ats|tenant|application
blocks(id, application_id, reason, evidence_path, created_at)  -- captcha, sms_otp, verifier_fail, budget, ...
```
Embeddings go in `sqlite-vec` virtual tables: `job_embeddings`, `bullet_embeddings`.

---

## 14. Tech stack (all free)

| Concern | Choice |
|---|---|
| Language | Python 3.12, `uv` for env/deps |
| Models / validation | Pydantic v2, SQLModel |
| DB | SQLite (WAL mode) + `sqlite-vec`; Postgres optional later |
| HTTP / scraping | `httpx`, `selectolax`, `python-jobspy`, `feedparser` |
| Browser automation | Playwright (real Chrome channel), `browser-use` for the LLM agent layer |
| Live view | Docker + Xvfb + noVNC, embedded in the dashboard |
| Replay / tracing | Playwright video + Trace Viewer; **Langfuse** (self-hosted) for LLM traces |
| Email (tests) | Mailpit (local fake inbox) behind an `EmailProvider` interface |
| LLM | `openai` SDK → Portkey; Ollama for embeddings |
| Text matching | `rapidfuzz` |
| Resume rendering | RenderCV / Typst, Jinja2 |
| PDF parse | `pypdf` |
| **Deployment** | **Docker + Docker Compose** (see §14A). No local Python install needed. |
| Scheduler | APScheduler (in-process); Docker container stays up 24/7 or is started manually |
| Dashboard | Streamlit |
| Notifications | `python-telegram-bot` |
| Email | Gmail API, read-only, OAuth sign-in in your browser (`google-auth-oauthlib`, `google-api-python-client`); IMAP optional later |
| Scraping fallbacks | `apify-client`, `firecrawl-py`, both optional and budget-capped (§4.3a) |
| Secrets | macOS Keychain via `keyring`, `.env` only for non-secrets |
| CLI | Typer |
| Quality | ruff, mypy, pytest, pytest-recording (VCR) for HTTP fixtures |
| Logs | `structlog` → JSON files |

---

## 14A. Docker deployment

### Why Docker
Everything runs inside one container. You need Docker Desktop on Mac or Docker Engine on Linux — no Python, no uv, no Playwright, no Chromium install on the host. The same image runs on your Mac today and on a free Oracle Cloud VM tomorrow.

### Services (`docker-compose.yml`)

| Service | Image | Ports | Purpose |
|---|---|---|---|
| **app** | `jobagent:latest` (built locally) | 8501 (dashboard), 6080 (noVNC) | The agent: Python + Playwright + Xvfb + noVNC + Streamlit |
| **ollama** | `ollama/ollama:latest` | 11434 | Local embeddings – free, runs on CPU |
| **mailpit** | `axllent/mailpit:latest` | 8025 (UI), 1025 (SMTP) | Fake inbox for email-verification tests (dev profile only) |

### First-time setup (< 10 minutes)
```bash
cp .env.example .env              # fill in JOBAGENT_PORTKEY_API_KEY and others
make build                        # ~5 min – downloads Python, Playwright, Chromium
make up                           # starts app + ollama
make pull-models                  # downloads nomic-embed-text into ollama (~300 MB)
make import                       # parses master_resume.txt → data/profile/master_profile.yaml
# → Dashboard at http://localhost:8501
# → Live browser view at http://localhost:6080/vnc.html
```

### Daily use
```bash
make run          # full discover → score → tailor → apply → track
make status       # today's summary
make novnc        # open live browser view to watch the agent
make logs         # follow container logs
make shell        # bash inside the container for debugging
```

### Data persistence
All runtime data lives in `./data/` on the host (bind-mounted into `/app/data`). The container is stateless — rebuild it any time without losing jobs, applications or the DB.

| Host path | Container path | Contents |
|---|---|---|
| `./data/` | `/app/data/` | DB, profile, applications, screenshots, secrets |
| `./config/` | `/app/config/` | Config YAMLs (edit on host, container picks them up immediately) |
| `${RESUME_DIR}/` | `/resume/` (read-only) | Your master_resume.txt |

`RESUME_DIR` defaults to `~/Documents/resume`. Override it in `.env`.

### Ports
| Port | Service | URL |
|---|---|---|
| 8501 | Streamlit dashboard | http://localhost:8501 |
| 6080 | noVNC (live browser view) | http://localhost:6080/vnc.html |
| 11434 | Ollama API | http://localhost:11434 |
| 8025 | Mailpit web (dev only) | http://localhost:8025 |

### Image layers (build cache)
```
Layer 1: system packages (xvfb, x11vnc, novnc, fonts) – cached for weeks
Layer 2: uv binary
Layer 3: Python deps from uv.lock                       – cached until uv.lock changes
Layer 4: Playwright Chromium                            – cached until playwright version changes
Layer 5: app source (src/, config/, etc.)               – fastest to rebuild
```
A code-only change rebuilds only Layer 5 in ~5 seconds.

### Running on a free VM (optional, for 24/7 operation)
Oracle Cloud Always Free gives you an ARM64 VM with 4 cores and 24 GB RAM. The same `docker compose up` command works because the image is multi-arch (python:3.12-slim-bookworm supports `linux/arm64`).

---

## 15. Repository layout

```
job_application_agent/
├── PLAN.md
├── pyproject.toml
├── config/
│   ├── search.yaml           # roles, regions, filters, thresholds
│   ├── guardrails.yaml       # caps, shadow mode, budgets, pacing, domain rules
│   ├── models.yaml           # task → Portkey model/config mapping
│   └── blacklist.yaml
├── data/                     # git-ignored
│   ├── profile/master_profile.yaml
│   ├── registers/            # UK/NL sponsor CSVs
│   ├── applications/{job_id}/ (resume.pdf, cover.pdf, answers.json, screenshots/)
│   ├── browser_profiles/
│   └── jobagent.db
├── prompts/                  # versioned prompt templates
├── templates/resume/         # Typst/RenderCV themes
├── src/jobagent/
│   ├── cli.py                # typer: profile | discover | score | tailor | apply | run | dashboard
│   ├── config.py
│   ├── db/ (models.py, repo.py, migrations/)
│   ├── llm/ (client.py, structured.py, embeddings.py, cost.py)
│   ├── profile/ (importer_linkedin.py, interview.py, schema.py)
│   ├── discovery/
│   │   ├── base.py
│   │   ├── jobspy_collector.py
│   │   ├── ats/ (greenhouse.py, lever.py, ashby.py, smartrecruiters.py, workable.py, workday.py, personio.py)
│   │   ├── boards/ (arbeitnow.py, adzuna.py, jooble.py, remotive.py, remoteok.py, himalayas.py, hn.py, relocate_me.py)
│   │   └── company_universe.py
│   ├── pipeline/ (normalise.py, dedup.py, enrich.py, visa.py, filters.py, score.py, prioritise.py)
│   ├── tailor/ (planner.py, rewriter.py, factcheck.py, render.py, ats_parse_test.py, cover_letter.py, answers.py)
│   ├── apply/
│   │   ├── base.py, browser.py, field_resolver.py, guardrails.py, orchestrator.py
│   │   ├── handlers/ (greenhouse.py, lever.py, ashby.py, workable.py, smartrecruiters.py,
│   │   │              linkedin_easy_apply.py, email_apply.py)
│   │   ├── agent/ (page_loop.py, observe.py, classify_page.py, tools.py, llm_agent.py,
│   │   │           trajectories.py, verifier.py, widgets/ (combobox.py, datepicker.py, repeaters.py, upload.py))
│   │   ├── playbooks/ (workday.yaml, successfactors.yaml, taleo.yaml, icims.yaml, oracle.yaml)
│   │   └── accounts/ (vault.py, signup.py, login.py, password_reset.py, email_verify.py)
│   ├── email/ (provider.py, gmail.py, mailpit.py)
│   ├── control/ (flags.py, checkpoints.py, commands.py)
│   ├── observability/ (steps.py, recorder.py, langfuse.py, metrics.py)
│   ├── tracking/ (gmail_watcher.py, classifier.py, status.py)
│   ├── notify/telegram_bot.py
│   ├── dashboard/app.py
│   └── scheduler.py
├── Dockerfile                # single-stage build: Python 3.12 + Playwright + Xvfb + noVNC
├── docker-compose.yml        # services: app + ollama (+ mailpit dev profile)
├── Makefile                  # make build / up / import / run / status / shell / novnc / ...
├── .dockerignore
├── docker/
│   └── app/entrypoint.sh     # starts Xvfb + VNC, inits DB, then execs the command
└── tests/ (unit/, fixtures/ recorded HTTP + saved ATS HTML pages,
            mock_ats/  ← local multi-step "Workday-like" app: signup → email verify → 6 pages → review,
            e2e/ against mock forms + Mailpit)
```

---

## 16. Phased roadmap

Each phase ends with something usable. Estimates assume part-time work (evenings and weekends).

### Phase 0: Foundation ✅ COMPLETE
- Repo, `uv`, ruff/mypy/pytest, config loading, all 16 SQLite models (WAL mode), Typer CLI, structlog.
- Portkey LLM client, structured output with repair-retry, Ollama embeddings, cost tracker.
- **Docker packaging**: single-image stack (`app` + `ollama` + mailpit dev sidecar); Xvfb + noVNC for live browser view; Makefile wraps every command; 16/16 tests pass inside the container.
- **Done.** `make build && make up && make import` gives you a running dashboard and a parsed profile.
- Portkey client with structured output, cost logging and caching; Ollama embeddings.
- `jobagent run` orchestrator skeleton with preflight checks and a resumable `runs` row (stages are stubs at first and get filled in by later phases).
- **Done when:** `jobagent llm ping` returns structured JSON and logs the cost, and `jobagent run` executes the stub stages end to end.

### Phase 1: Master profile (3–4 days)
- `master_resume.txt` parser → `master_profile.yaml` (deterministic), one-time LLM skill tagging per bullet, VERIFY/NOT_YET gates.
- Profile schema validation and default resume rendering (RenderCV/Typst).
- **Done when:** a polished general resume PDF exists and passes the ATS parse test. *(Already useful on its own.)*

### Phase 2: Discovery and normalisation (4–6 days)
- JobSpy collector, Greenhouse/Lever/Ashby/SmartRecruiters collectors, Arbeitnow/Adzuna/Remotive/RemoteOK.
- Company-universe flywheel and sponsor-register import (UK + NL).
- Normalise, dedup and ATS detection.
- **Done when:** `jobagent discover` stores 1k+ unique, deduped jobs across IN/EU/ME/remote.

### Phase 3: Enrich and score (3–4 days)
- LLM extraction, visa detection, hard filters, embedding funnel, rubric scoring, prioritisation.
- Streamlit v1: browse scored jobs and spot-check scoring quality.
- **Done when:** the top 30 jobs/day look right to you ≥ 80% of the time. Tune prompts and thresholds until they do.

### Phase 4: Tailoring and QA (4–5 days)
- Planner, rewriter, fact-checker, renderer, cover letter, answer engine, ATS parse test.
- **Done when:** a tailored package for 20 sample jobs passes fact-check with no fabrications on manual review.

### Phase 5a: Observability and control foundation (3–4 days), built *before* any live applying
- `app_steps` recorder, Playwright video/trace, Docker noVNC live view, Langfuse, `control_flags` + checkpoints + cooperative cancellation, Telegram `/pause /resume /stop /abort /status /live /otp`.
- Dashboard **▶ Run** button and **Auth Assist** (park → Telegram ping with live-view link → auto-continue or time out).
- **Done when:** you can watch a scripted browser run live and stop it mid-page within 2 seconds, then resume from the checkpoint.

### Phase 5b: Simple ATS handlers (4–6 days)
- Field resolver, widgets, pre-submit verifier, guardrails.
- Handlers: **Greenhouse → Lever → Ashby → Workable → SmartRecruiters → email**.
- **Shadow mode** on real jobs, which auto-promotes to live at ≥ 90% verified fill rate.
- **Done when:** these ATSs submit live, unattended, with confirmations captured.

### Phase 5c: Account agent and Workday (8–12 days), your core pain point
- `bipin2295+tenant` alias scheme, browser-consent Gmail OAuth (read-only), Keychain vault, `EmailProvider` (Gmail API + Auth Assist fallback + Mailpit), sign-up/login/reset flows.
- Page loop (observe → classify → plan → act → verify → checkpoint), LLM agent layer with restricted tools, domain allowlist, budgets.
- **Workday playbook** (all 6 pages, resume-autofill audit, repeaters, comboboxes).
- Learned trajectories (record and replay per tenant/page signature).
- Build and test against `tests/mock_ats` first, then real Workday tenants in shadow mode.
- **Done when:** ≥ 80% of Workday applications complete end to end (new account → verified → submitted) with no human input, and the second application to the same tenant reuses the account.

### Phase 6: Go live plus tracking (3–4 days)
- Ramp caps 10 → 40/day automatically while the success rate stays high.
- Gmail watcher and classifier, run summary report.
- **Done when:** 7 consecutive one-click runs finish unattended (apart from optional Auth Assist pings) and you only *watched*.
- *Later:* `launchd` schedule calling the same `jobagent run`.

### Phase 7: Coverage expansion (ongoing)
- SuccessFactors → Oracle/Taleo → iCIMS → Phenom/others (each one starts as LLM-agent-only, then gets a playbook once volume justifies it).
- LinkedIn Easy Apply (flagged), ME/India scrapers (GulfTalent, NaukriGulf, Instahyre), Personio/Teamtailor/Recruitee.
- Analytics-driven tuning, and an optional free 24/7 VM (Oracle Cloud Always Free ARM, which runs the same Docker compose).

**Total to an autonomous MVP including Workday: about 6–8 weeks part-time.**

---

## 17. Testing strategy
- **Unit tests:** normalisers, dedup, ATS URL detection, visa regex, guardrails, answer mapping.
- **Recorded HTTP** (VCR cassettes) for every collector, so the suite runs offline and catches schema drift.
- **Saved HTML snapshots** of real ATS forms plus a **local mock application server** for handler tests.
- **LLM evals:** a small golden set (about 30 JDs with your hand-labelled fit scores) is re-run whenever a prompt or model changes. Track agreement and fabrication rate (target: 0).
- **Mock ATS (`tests/mock_ats`):** a small FastAPI app that copies the hard parts: sign-up with password rules, email verification through Mailpit, a resume-parse autofill with deliberate errors, repeaters, comboboxes, iframes, a review page and a confirmation page. Variants inject a CAPTCHA, a prompt-injection string, an off-domain redirect and an unanswerable question, to prove the agent blocks, ignores or skips correctly.
- **Control tests:** stop/pause/abort at random steps, then resume. The run must finish with exactly one submission and no duplicate account.
- **Canary:** each day, one real job per ATS family runs in shadow mode. A drop in fill rate auto-disables that ATS and sends an alert.

---

## 18. Risks and mitigations

| Risk | Impact | Mitigation |
|---|---|---|
| LinkedIn/Indeed restrict your account | High | LinkedIn for discovery only by default, apply through company ATS; Easy Apply flagged, low caps, human pacing |
| Fabricated or exaggerated resume content | High (reputation, offers rescinded) | Bullet-ID constraint, independent fact-check model, number regex check, 0-fabrication eval gate |
| Low-quality mass applications harm your reputation with companies | Medium | Score ≥ 75, per-company caps, never re-apply |
| Site/ATS changes break scrapers or handlers | Medium | Label-based selectors, LLM fallback mapper, daily canaries, isolated collectors |
| CAPTCHAs / SMS OTP / bot detection | Medium (likely 10–20% of account-based ATS attempts) | Don't bypass. Auth Assist (time-boxed ping), else `blocked`, retry next run, then skip. Tracked as a metric. |
| Agent access to your **personal** Gmail | High | Read-only OAuth scope; queries limited to per-application alias and domain searches; other mail never fetched or sent to an LLM; token in Keychain, revocable any time at myaccount.google.com → Security → Third-party access; no sending in the initial phase |
| Your phone number and CTC shared with many companies | Low/Med | Expected for job applications; the `accounts.data_submitted_json` log records exactly who received what |
| Wrong screening answer (e.g. sponsorship, CTC) | High | Deterministic answer bank first; inference must cite the profile; legal/eligibility questions are never guessed → `skipped_unanswerable` |
| Agent misfills an account-based form | High | Per-page DOM read-back, independent pre-submit verifier, shadow mode until ≥ 90%, auto-disable an ATS on a quality drop |
| Prompt injection from job pages | Medium | Untrusted-content isolation, fixed tool set, domain allowlist, `fill_secret` domain binding |
| Account sprawl (dozens of ATS accounts holding your PII) | Medium | Per-tenant `+` aliases, `accounts` registry with data-submitted log, purge/GDPR-deletion command |
| Agent loops and burns tokens | Medium | Per-application caps on actions, time and $; learned trajectories reduce LLM use over time |
| PII leakage (resume, CTC, credentials) | High | Local-only data dir, Keychain for secrets, `.gitignore`, no PII in logs, Portkey log redaction if available |
| LLM cost runaway | Low/Med | Funnel filtering, caching, Portkey budget limit, `llm_calls` cost tracking |
| Duplicate submissions after a crash | Medium | Idempotent state machine, `applying` lock with timeout, confirmation check before retry |
| Legal / ToS for scraping | Medium | Prefer official/public APIs, respect robots.txt and rate limits, personal and non-commercial use only |

---

## 19. Resolved decisions

| # | Topic | Decision | Where it's used |
|---|---|---|---|
| 1 | Salary bands | **Per country** (India, each EU country, UAE, APAC/SEA, Canada) at median-to-p75 for 9–10 yrs; values in `answers_seed.yaml`, auto-calibrated | §4.3, §4.4 |
| 2 | Notice / start date | **30 days**; relocation roles: 30 days or on visa approval, whichever is later | answer bank |
| 3 | Blacklist | **Cimpress, Vistaprint** | §9, `config/blacklist.yaml` |
| 3c | UK + all of Europe | UK (London/rest), CH, NO, IS and all EU-27 except the micro-states; bands in §4.4; CEE/South-East Europe marked low priority | §4.3, §4.4 |
| 3d | Form-only PII | Postal code (always); DOB (only when required, never on the CV) — values in `answers_seed.yaml` | §3.1, seed |
| 3e | Skill years | Since the first role showing the skill (not blanket total experience; see note) | resume `SKILL YEARS` |
| 3a | Regions added | **SG, AU, NZ, JP, HK** (strict visa check), **MY, TH, VN** (lenient); KR/TW off by default; ID/PH excluded | §4.3 |
| 3b | Visa + relocation | Sponsorship required everywhere outside India; explicit "no relocation" → reject, relocation offered → bonus | §4.3, §6 |
| 4 | Languages | English only professionally; Spanish A1 (Duolingo, uncertified), disclosed only if asked. Roles **requiring** any other language are rejected | §4.3, profile |
| 5 | Resume theme | Single ATS-friendly theme for all regions (no photo, DOB or nationality) | §8.1 |
| 6 | Email | **`bipin2295@gmail.com`** with `+tenant` aliases. **Initial phase: you sign in manually in the browser** (read-only OAuth, reconnect about weekly); Auth Assist when not connected | §10A.2 |
| 6a | Profile source | **`~/Documents/resume/master_resume.txt`** is the single source of truth; imported deterministically | §3.1 |
| 6b | Firecrawl / Apify | Optional, budget-capped **fallbacks** via SDK (free tiers by default); MCP used only during development | §4.3a |
| 7 | Run mode | **Manual one-click** (▶ Run / `jobagent run`), runs to completion; schedule later | §10C |
| 8 | Phone | Indian mobile from the profile (used on forms; SMS OTP goes to Auth Assist) | profile, §10A.3a |
| 9 | Screening answers | Current CTC from the seed; expected = the job country's §4.4 "Expected" from the seed; assessments: Yes | answer bank §3.2 |
| 10 | Auth help | You're OK doing manual auth when needed → **Auth Assist on** by default | §10A.3a |

Actual values are seeded in `data/profile/answers_seed.yaml` (git-ignored), which Phase 1 imports into `master_profile.yaml`.

**Notes (no action needed unless you disagree):**
- **Blacklist scope:** only Cimpress and Vistaprint/Vista are blocked. Other Cimpress-group brands (National Pen, BuildASign, Pixartprinting, Drukwerkdeal, Exaprint, WIRmachenDRUCK, 99designs) are listed **commented out** in `config/blacklist.yaml`. Uncomment any you want blocked.
- **Lower-pay SEA markets:** MY and VN expected pay in USD is below your India target. They're kept, but deprioritised (§4.4).

---

## 20. What's done and what's next

### ✅ Already done (Phase 0 complete)
- Full project built and Dockerised. The image builds, all tests pass inside the container.
- Master resume imported: 3 jobs, 15 bullets, 5 summaries, 4 recipes, 0 open items.
- All profile questions resolved (gender, DOB, postal code, salary bands for 30+ countries, work authorisation for every target region).

### Your remaining setup steps (~30 min, one-time)

1. **Docker (if not done):** `make build && make up && make pull-models && make import`

2. **Portkey** (required for all LLM features):
   - Create a free account at portkey.ai with your personal email.
   - Set up 3 virtual keys: one for a cheap model (GPT-4o-mini / Gemini Flash), one for a medium model (Claude Haiku), one for a strong-vision model (Claude Sonnet or GPT-4.1).
   - Set a monthly budget limit (e.g. $150).
   - Add `JOBAGENT_PORTKEY_API_KEY=...` to `.env`.
   - Run `make llm-ping` to verify.

3. **Gmail** (required for email verification in account-based ATS):
   - console.cloud.google.com → new project `jobagent` → enable Gmail API.
   - OAuth consent screen: External, Testing mode, add `bipin2295@gmail.com` as test user.
   - Credentials → Create OAuth client ID → Desktop app → download to `data/secrets/client_secret.json`.
   - `make gmail-connect` → browser opens → sign in → done. Reconnect weekly.

4. **Free API keys** (required for discovery):
   - Adzuna: developer.adzuna.com (free, covers IN / EU / UK / AU)
   - Jooble: jooble.org/api (free)
   - Reed: reed.co.uk/developers (free, UK only)
   - Telegram bot: @BotFather → `/newbot` → send it one message so it can find your chat ID.
   - Optional: Apify and Firecrawl free accounts (scraping fallbacks).
   - Add all to `.env`.

5. **Start Phase 1** (discovery collectors) → I build that next.
