# Conversational BI over a semantic layer

A local proof of concept for senior stakeholders, using **synthetic data only**. Ask about utilization, revenue and delivery in plain English, with
no SQL. You get an instant answer, a chart and the result set. Ask *why* something changed and it explains what
happened and which measured factors contributed. Driver decompositions do not by themselves prove causation.

```
"Why did utilization drop last month?"

WHAT HAPPENED   Utilization fell 4.6 pts, from 77.7% in August 2026 to 73.1% in September 2026.
                Billable hours were up 13.5% while available hours were up 20.6%.
HOW             Before this, utilization had been steady at 75.7%–77.7% for 5 months, so this is a break
                from the pattern, not a long slide.
WHY             The change is concentrated by region. India (-3.2 pts, 70% of the change): utilization went
                from 79.9% to 70.7%… By practice, the biggest factor is Data & AI (-3.4 pts).
```

## Principles

- **On-premises only.** Rules-only questions stay on this machine. An enabled local model receives questions, structured context and bounded dimension-value candidates. The built-in interpreter is a rules
  engine. An optional LLM must be self-hosted. Its transport pins a validated private IP, disables redirects and proxies, and limits response size.
- **Asks instead of guessing.** Vague measures ("performance", "hours"), values that match more than one thing,
  vague periods ("recently"), and "why" questions with no period all get a clarifying question with one-click
  answers.
- **Real-time.** Every question queries the source live, with no result cache. CSV and Excel files reload as
  soon as they change. Each answer shows when it was computed, and the header shows when the data last changed.
  Use Refresh to rerun an answer; freshness metadata updates automatically.
- **Governed.** Metrics are defined once in a semantic model. Nothing writes free SQL: questions become a
  structured query that is validated and compiled with bound parameters.

## Run it

Use Python 3.12+ for a new environment (3.9 remains tested for existing local setups). The API uses FastAPI, Pydantic and Uvicorn; the built-in demo needs no database server or API key.

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.lock
./run.sh
```

Open http://127.0.0.1:8765. The first run builds a demo professional-services dataset: 132 consultants, 5
practices, 12 clients, and daily timesheets since January 2025. Late timesheets for the last few working days
keep arriving every few seconds, so the numbers move as you watch. Two events are built in so "why" questions
have real answers: a large Data & AI engagement ended on 31 August 2026, and 18 junior hires joined in India
on 1 September.

A second demo source, **Superstore Retail**, holds the fictional Superstore sample: 9,994 order lines and 296
returns from 2018–2021 (`samples/superstore/`). It is a static snapshot, built into SQLite on first run, and its
model adds profit margin, average order value and a per-order return rate (returns join to orders). Its totals
match the published Superstore figures ($2,297,201 sales, $286,397 profit). Ask with explicit years, e.g.
"Why did profit change in 2021 vs last year?".

To run Superstore on **PostgreSQL** instead, set `BI_DEMO_POSTGRES_URL`. The sample is loaded into its own
`bi_demo_superstore` schema (only when missing or changed) and queried read-only. With nothing installed,
`embedded` starts a private local PostgreSQL:

```bash
.venv/bin/python -m pip install "psycopg[binary]>=3.2,<4" pgserver
BI_DEMO_POSTGRES_URL=embedded ./run.sh
```

```bash
.venv/bin/python -m unittest discover -s tests     # PostgreSQL checks run on an embedded server when pgserver is installed
```

`tests/test_edge_cases.py` loads one set of tricky records as a **SQLite database, a CSV export and an Excel
workbook** and asks every question of all three. Each answer must match totals computed straight from the records.
The records cover Unicode and punctuation in names, an injection-shaped value, the same word in two dimensions,
leap day, zero capacity, negative and very large revenue, future dates and empty months. Separate files cover
semicolon/European CSVs, tab-separated files, booleans, SQL-keyword column names, Excel formulas and sparse cells,
and empty or header-only uploads. The fixtures live in `tests/fixtures/edge/`; regenerate them with
`python3 tests/edge_data.py`. Test runs generate their fixtures in temporary directories.

## Connect synthetic data

| Source | How |
|---|---|
| **CSV / Excel** | Open the source button in the header, confirm the file is synthetic and choose **Add a CSV or Excel file**, or start with `BI_SOURCE_CLASSIFICATION=synthetic BI_SOURCE=/path/mock.xlsx ./run.sh`. Types (dates in most formats, `$1,234` amounts, `45%`) and a semantic model are inferred automatically. If the file has billable and available/capacity hours, **utilization** is created as a governed ratio. The draft model is saved as `<file>.semantic.json` for you to refine. |
| **SQLite** | `BI_SOURCE_CLASSIFICATION=synthetic BI_SOURCE=/path/mock.db BI_MODEL_FILE=semantic/your_model.json ./run.sh` |
| **PostgreSQL** | `.venv/bin/python -m pip install "psycopg[binary]>=3.2,<4"`, then `BI_SOURCE_CLASSIFICATION=synthetic BI_SOURCE=postgresql://user@host/mock_db BI_MODEL_FILE=… ./run.sh`. Read-only session. **Live integration runs in CI; locally skipped unless `BI_TEST_POSTGRES_URL` is set.** |

Uploaded files are stored in `data/synthetic/uploads/`. Imports require `X-Data-Classification: synthetic` (the UI sends it after you check the declaration). Only uploads with a synthetic classification sidecar return after a restart. Re-uploading the same name replaces it after validation.

Old `data/uploads/` files are not automatically loaded or labelled synthetic. Re-import mock files through the UI. This declaration does not inspect files for personal data: use fabricated records only. The current profile rejects `BI_DATA_CLASSIFICATION` values other than `synthetic`.

### Configuration

| Variable | Default | Purpose |
|---|---|---|
| `BI_SOURCE` / `BI_MODEL_FILE` | | Synthetic source and its semantic model (see above) |
| `BI_DATA_CLASSIFICATION` | `synthetic` | This release supports the synthetic demo profile only |
| `BI_SOURCE_CLASSIFICATION` | unset | Required value `synthetic` when `BI_SOURCE` is configured |
| `BI_DEMO` | `1` | `0` hides the demo dataset |
| `BI_LIVE` / `BI_LIVE_INTERVAL` | `1` / `8` | Demo live feed of late timesheets, and seconds between batches |
| `BI_LOCAL_LLM_URL` / `BI_LOCAL_LLM_MODEL` | off | Optional **self-hosted** model (Ollama, vLLM, LM Studio) for unusual phrasings. Private-network addresses only. **Tested against a stand-in OpenAI-compatible server only**; no real local model was available while building. |
| `BI_SQL_FALLBACK` | `1` | With a local model set, questions the semantic model can't express may get a **guarded draft**: the model writes one SELECT from table and column names (never data rows), it must pass `bi/sqlguard.py` and the database's own checks, runs read-only with a 200-row cap, and is marked **Unverified**. `0` turns this off. |
| `BI_DEMO_POSTGRES_URL` | off | Runs the Superstore demo on PostgreSQL (a URL, or `embedded`) |
| `PORT` / `BI_HOST` | `8765` / `127.0.0.1` | Where the server listens |
| `BI_DATA_DIR` | `data/synthetic/` | Demo database, uploads, cache and the question log |

## What a user can do

- **Ask:** "Utilization by practice last month", "Top 5 clients by revenue year to date", "Monthly utilization
  trend", "Average bill rate by grade", "How many consultants in India".
- **Ask why:** "Why did utilization drop last month?", "What drove the change in revenue this quarter?". The
  answer is split into what happened, how it happened (trend with the compared periods shaded) and why (a driver
  chart you can switch between practice, region, grade and so on).
- **Follow up:** "now by region", "what about India?", "why?".
- **Answer clarifying questions** with one click.
- **Make custom breakdowns instantly** with the breakdown, trend, metric and compare controls. Click any table
  value to drill into it.
- **Trust it:** chips show what was understood, **Show steps** lists every step taken (source, interpretation,
  compiled SQL, read-only run, narration) followed by the exact SQL, and each answer shows its "as of" time. The
  open/closed choice is remembered.
- **Go beyond the model, carefully:** when no governed measure fits and a local model is configured, a clarifying
  question offers "Something else: let the on-prem model try". The answer is clearly marked Unverified, and a
  blocked draft shows what was refused and why.
- **Start from examples:** sample questions come from the dataset's semantic model through `/api/samples`, rotate
  in the input placeholder (Alt + Enter accepts one) and appear as starter cards on an empty screen.

## How "why" works

The change between two periods is split across the groups of every relevant dimension, and the dimension
where the change is most concentrated leads the explanation.

- **Additive metrics** (revenue, hours): each group's contribution is its own change. The contributions add up
  exactly to the total change.
- **Ratios** (utilization = billable ÷ available, margin, bill rate): each group's contribution is split into
  - a **rate effect**: the group's own utilization moved, and
  - a **mix effect**: capacity shifted toward a group that runs above or below average. New joiners on the bench
    are the classic case.

  These also add up exactly to the total change, in percentage points.

A metric can list the dimensions worth explaining it by (`explain_by` in the model). Utilization is explained by
practice, region, grade and consultant, because "by client" only restates that the bench isn't billed.

## Layout

```
bi/semantic.py      semantic model, SemanticQuery, validator, SQL compiler (SQLite + Postgres dialects)
bi/parser.py        rules interpreter: periods, top-N, filters, follow-ups, why-intent, ambiguity detection
bi/engine.py        orchestration: clarify / query / explain, freshness, question log
bi/explain.py       driver analysis (additive and rate/mix decomposition) and the what/how/why narrative
bi/narrate.py       plain-English answers, interpretation chips, suggestions
bi/sources.py       SQLite, PostgreSQL, CSV/Excel sources (file reload on change)
bi/xlsx.py          stdlib .xlsx reader
bi/autosemantic.py  draft a semantic model from a file's columns
bi/llm.py           optional on-prem LLM adapter with a private-network guard
bi/data.py          demo dataset and live timesheet feed
bi/superstore.py    Superstore sample loader (static snapshot; SQLite or PostgreSQL)
bi/sqlguard.py      safety checks for SQL drafted by the local model (fallback only)
bi/config.py        immutable synthetic-demo settings
bi/contracts.py     Pydantic request contracts
bi/registry.py      source registry and lifecycle bootstrap
bi/service.py       structured query and upload use cases
bi/server.py        FastAPI app factory, request boundary and static UI
semantic/           semantic models
web/                UI (vanilla HTML/CSS/JS, no build step)
```

## API

| Method | Path | Body | Returns |
|---|---|---|---|
| GET | `/api/datasets` | | available datasets |
| GET | `/api/samples?dataset=` | | sample questions (shuffled) |
| GET | `/api/model?dataset=` | | measures, dimensions and values, plus freshness |
| GET | `/api/freshness?dataset=` | | when the data last changed, coverage |
| POST | `/api/ask` | `{dataset, question, context?, context_dataset?, clarifications?}` | `type` of `result`, `explain` or `clarify`, plus the `dataset` that answered. `dataset: "auto"` routes the question to the source whose measures match it, and asks which source to use when several fit. |
| POST | `/api/query` | `{dataset, query}` | runs a structured query (custom breakdowns) |
| POST | `/api/upload` | raw file, `X-Filename` and `X-Data-Classification: synthetic` headers | adds a CSV/Excel dataset |

The versioned API schema is available at `/api/openapi.json`. Invalid request fields return 422; malformed JSON returns 400; oversized bodies return 413. Responses never echo invalid submitted values.

See [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) for the current design and real-data transition boundary, and [docs/BEST_PRACTICES.md](docs/BEST_PRACTICES.md) for taking this from POC to production.


## QC gates

Run the backend, HTTP/import regressions and frontend behaviour checks before merging:

```bash
.venv/bin/python -m unittest discover -s tests -v
node tests/test_ui.cjs
.venv/bin/python -m pip install 'ruff>=0.8,<1'
.venv/bin/ruff check bi tests
```

The GitHub workflow repeats these on Python 3.9, 3.12 and 3.13, and runs a separate synthetic PostgreSQL service.
For local PostgreSQL integration, install `pgserver` (an embedded server starts for the tests) or set
`BI_TEST_POSTGRES_URL` to a **disposable test database** where the test account can create and drop a randomly
named schema. These checks do not certify production capacity or validate a live LLM's
interpretation accuracy. See [docs/QC_IMPLEMENTATION.md](docs/QC_IMPLEMENTATION.md) for the audit-to-check mapping.

Safety and data contracts:

- Unsupported question conditions trigger clarification. Invalid ranges and unknown source IDs fail explicitly.
- Comparisons rank the **current** groups, then look up prior values for those same groups. Prior-only groups are
  outside this view. Driver analysis includes both periods; unsupported non-additive metrics require a denominator.
- Inferred models are **drafts**. Review aggregation, currency, date field and weighting in the generated model
  before business use. Bill/unit prices default to unweighted averages; this may not match your business definition.
- Numeric import treats a single comma followed by three digits as a thousands separator, and a comma followed by
  one/two digits as a decimal separator. Ambiguous locales still need a reviewed export; there is no locale chooser.
- Imports reject invalid typed cells instead of silently converting them to blanks. CSVs are limited to 100,000
  rows and 256 columns. Excel additionally limits sheets, expanded archive size, compression ratio and total cells.
- Uploads validate a staged replacement before activating it. Model sidecars use `<file>.csv.semantic.json` or
  `<file>.xlsx.semantic.json`, so extensions and source IDs stay separate. Older sidecars should be reviewed and
  migrated to these names; they are not automatically reused.
- Questions use coherent read snapshots. SQL work has a 10-second statement/progress limit; HTTP concurrency is
  capped at 16 with request read timeouts. These are local-demo limits, not measured capacity guarantees.
- File schema changes require reimporting/reviewing the model. The last good cache is preserved on failure.
- Default logs omit raw questions, filters and errors, use private permissions and rotate at 5 MB (one previous file).
  `BI_LOG_QUESTIONS=1` enables sensitive question/query logging for local evaluation; manage retention accordingly.
- The local model has a 768-token output cap and a 1 MB response cap; redirects, environment proxies and link-local
  destinations are refused. Network egress rules remain necessary for deployment assurance.
- Non-loopback serving requires explicit `BI_ALLOW_NETWORK=1` and an authenticated TLS reverse proxy. Add allowed
  proxy hostnames through `BI_ALLOWED_HOSTS` (comma-separated). This flag **does not implement authentication**.

Automatic import approval, SSO, measured load capacity and full browser accessibility certification remain future
pilot work. The application remains intended for a supervised local demo with synthetic data.
