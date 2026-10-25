# Conversational BI over a semantic layer

A proof of concept for senior stakeholders. Ask about utilization, revenue and delivery in plain English, with
no SQL. You get an instant answer, a chart and the result set. Ask *why* something changed and it explains what
happened, how it happened and why.

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

- **On-premises only.** Questions and data never leave this machine. The built-in interpreter is a rules
  engine. An optional LLM must be self-hosted, and the app refuses any endpoint that isn't on a private network.
- **Asks instead of guessing.** Vague measures ("performance", "hours"), values that match more than one thing,
  vague periods ("recently"), and "why" questions with no period all get a clarifying question with one-click
  answers.
- **Real-time.** Every question queries the source live, with no result cache. CSV and Excel files reload as
  soon as they change. Each answer shows when it was computed, and the header shows when the data last changed.
  Answers can auto-refresh.
- **Governed.** Metrics are defined once in a semantic model. Nothing writes free SQL: questions become a
  structured query that is validated and compiled with bound parameters.

## Run it

Needs Python 3.9+ and nothing else: no packages, no database server, no API key.

```bash
./run.sh
```

Open http://127.0.0.1:8765. The first run builds a demo professional-services dataset: 132 consultants, 5
practices, 12 clients, and daily timesheets since January 2025. Late timesheets for the last few working days
keep arriving every few seconds, so the numbers move as you watch. Two events are built in so "why" questions
have real answers: a large Data & AI engagement ended on 31 August 2026, and 18 junior hires joined in India
on 1 September.

```bash
python3 -m unittest discover -s tests     # 32 tests
```

## Bring your own data

| Source | How |
|---|---|
| **CSV / Excel** | Click **+ Add file** in the header, or start with `BI_SOURCE=/path/report.xlsx ./run.sh`. Types (dates in most formats, `$1,234` amounts, `45%`) and a semantic model are inferred automatically. If the file has billable and available/capacity hours, **utilization** is created as a governed ratio. The draft model is saved as `<file>.semantic.json` for you to refine. |
| **SQLite** | `BI_SOURCE=/path/warehouse.db BI_MODEL_FILE=semantic/your_model.json ./run.sh` |
| **PostgreSQL** | `pip install "psycopg[binary]"`, then `BI_SOURCE=postgresql://user@host/db BI_MODEL_FILE=… ./run.sh`. Read-only session. **Not yet tested against a live server.** |

Uploaded files are stored in `data/uploads/` and come back after a restart. Re-uploading a file with the same name
replaces it.

### Configuration

| Variable | Default | Purpose |
|---|---|---|
| `BI_SOURCE` / `BI_MODEL_FILE` | | Your own data source and its semantic model (see above) |
| `BI_DEMO` | `1` | `0` hides the demo dataset |
| `BI_LIVE` / `BI_LIVE_INTERVAL` | `1` / `8` | Demo live feed of late timesheets, and seconds between batches |
| `BI_LOCAL_LLM_URL` / `BI_LOCAL_LLM_MODEL` | off | Optional **self-hosted** model (Ollama, vLLM, LM Studio) for unusual phrasings. Private-network addresses only. **Untested**: no local model was available while building. |
| `PORT` / `BI_HOST` | `8765` / `127.0.0.1` | Where the server listens |
| `BI_DATA_DIR` | `data/` | Demo database, uploads, cache and the question log |

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
- **Trust it:** chips show what was understood, "How this was calculated" shows the SQL, and each answer shows its
  "as of" time.
- **Start from examples:** sample questions come from the dataset's semantic model through `/api/samples`, rotate
  in the input placeholder (Tab accepts one) and appear as chips.

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
bi/server.py        stdlib HTTP API, datasets and uploads
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
| POST | `/api/upload` | raw file, `X-Filename` header | adds a CSV/Excel dataset |

See [docs/BEST_PRACTICES.md](docs/BEST_PRACTICES.md) for taking this from POC to production.
