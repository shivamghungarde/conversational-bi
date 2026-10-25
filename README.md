# Conversational BI over a semantic layer

Ask business questions in plain English and get an instant answer, a chart and the result set,
with no SQL. Built for business leaders who want to slice metrics themselves without waiting on an analyst.

```
"Revenue by region vs last year"
→ East leads with $858.1K revenue (29% of the total) year to date. Next is North at $776.0K,
  and South is at the bottom with $597.3K. Biggest gain: South (up 15.5%). Overall revenue is up 13.6%.
```

## Run it

Needs Python 3.9+ and nothing else: no packages, no database server, no API key.

```bash
./run.sh
```

Open http://127.0.0.1:8765. On first run it builds a demo retail dataset (`data/demo.db`, about 25K orders).

Run the tests:

```bash
python3 -m unittest discover -s tests
```

### Optional: Claude for unusual phrasings

The built-in rules engine answers common questions in 1–20 ms. To route low-confidence questions to Claude:

```bash
pip install anthropic
export ANTHROPIC_API_KEY=sk-ant-...
./run.sh
```

| Variable | Default | Purpose |
|---|---|---|
| `BI_LLM` | `on` | `off` disables Claude even when it's configured |
| `BI_MODEL` | `claude-opus-5-5` | Model used to interpret questions |
| `PORT` / `BI_HOST` | `8765` / `127.0.0.1` | Where the server listens |
| `BI_DB` | `data/demo.db` | SQLite database path |
| `BI_MODEL_FILE` | `semantic/retail_sales.json` | Semantic model to load |

## What a user can do

- **Ask in plain English:** "Top 5 products by gross profit", "monthly revenue trend", "gross margin by category in Q2 2025".
- **Follow up in conversation:** "now by channel", "what about South?", "only Online", "compare with last year".
- **Make custom breakdowns instantly:** each answer has controls to change the breakdown, trend grain, metrics and
  comparison. Changes run directly against the semantic layer, with no re-interpretation and no waiting.
- **Drill down:** click any value in the table (for example *East*) to filter to it and break it down by the next dimension.
- **Trust the answer:** chips show exactly what was understood (and can be removed); "How this was calculated" shows the SQL.
- **Start from examples:** sample questions come from the semantic model through `/api/samples`. They rotate as the
  input placeholder (press Tab to accept one) and appear as one-click chips.
- **Export:** download any result set as CSV.

## How it works

```
question ─► Rule parser ──(confident)──────────────┐
              │ (low confidence, if configured)     ▼
              └──► Claude (JSON schema locked ─► SemanticQuery ─► validate ─► compile SQL ─► SQLite
                   to model names)                                (governed)   (parameterised)
                                                                                     │
          answer text (templates) ◄── result set + totals + comparison period ◄──────┘
```

- **The semantic layer** (`semantic/retail_sales.json`) defines metrics (with their SQL), dimensions, joins,
  synonyms and sample questions. It is the only place business logic lives.
- **Nothing writes free SQL.** The rule parser and Claude both produce a structured `SemanticQuery`
  (metrics, dimensions, filters, time range, grain, comparison, top-N). The compiler takes identifiers only from
  the model file and binds every user value as a parameter.
- **Answers are templated from the result set**, so the numbers in the sentence always match the table.
- **Every question is logged** to `data/query_log.jsonl` with the interpretation, confidence and latency.
  Low-confidence and failed questions show you which synonyms or metrics to add next.

## Layout

```
bi/semantic.py     semantic model, SemanticQuery, validator, SQL compiler, comparison logic
bi/parser.py       rule-based plain English → SemanticQuery (time phrases, top-N, filters, follow-ups)
bi/llm.py          optional Claude interpreter (structured output constrained to the model)
bi/narrate.py      plain-English answers, interpretation chips, suggested next breakdowns
bi/engine.py       orchestration, confidence routing, query log
bi/server.py       stdlib HTTP API + static files
bi/data.py         demo dataset generator (replace with a real warehouse connector)
semantic/          semantic model definitions
web/               UI (vanilla HTML/CSS/JS, no build step)
tests/             unittest suite
docs/              best practices and roadmap
```

## API

| Method | Path | Body | Returns |
|---|---|---|---|
| GET | `/api/samples` | | `{samples: [...]}` sample questions (shuffled) |
| GET | `/api/model` | | metrics, dimensions and their values |
| POST | `/api/ask` | `{question, context?}` | answer, interpretation, query, columns, rows, totals, sql |
| POST | `/api/query` | `{query}` | same, for a structured query (custom breakdowns) |

`context` is the `query` from the previous answer, which enables follow-up questions.

See [docs/BEST_PRACTICES.md](docs/BEST_PRACTICES.md) for how to grow this into a production standalone product.
