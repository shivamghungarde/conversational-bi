# Best practices for building this as a standalone product

## 1. Repository and project hygiene

- **Give it its own repository.** Today it lives in `conversational-bi/` on its own branch so it can be lifted out
  cleanly with its history:
  ```bash
  git subtree split --prefix=conversational-bi -b conversational-bi-standalone
  # then push that branch to a new, empty GitHub repo as its main branch
  ```
- **Keep it self-contained:** no imports from or into the profile site, its own README, tests, run script and
  dependency file. That is already true.
- **Use trunk-based development** with short feature branches, PRs and CI that runs the tests and a linter (ruff)
  on every push.
- **Version the semantic model like code.** Changes to metric definitions go through PR review, because a changed
  `revenue` definition changes every answer.

## 2. The semantic layer is the product

- **Govern metrics in one place.** Every metric has one owner, one SQL definition, a description, a format and
  synonyms. The NL layer never invents logic.
- **Mark metrics additive or not.** Shares of total and summed totals only make sense for additive metrics
  (revenue yes, margin % no). The narrator already respects this flag.
- **Add synonyms from real usage.** Review `data/query_log.jsonl` each week. Each low-confidence question means a
  synonym, metric or dimension is missing.
- **When you outgrow JSON, adopt a standard.** dbt Semantic Layer (MetricFlow), Cube or LookML each give you
  metrics, joins, caching and access control. Keep `SemanticQuery` as the internal contract and write an adapter
  so the NL and UI layers don't change.

## 3. Natural language: safe, fast, honest

- **Never let an LLM write raw SQL against the warehouse.** Have it fill a structured query constrained by
  JSON-schema enums (see `bi/llm.py`), then validate and compile it yourself. This prevents injection, hallucinated
  columns and ungoverned definitions.
- **Use tiered interpretation for speed:** deterministic rules first (milliseconds, free), LLM only on low
  confidence. Most BI questions are repetitive, so this keeps the median answer instant.
- **Always show the interpretation** as editable chips and keep the SQL one click away. Leaders trust what they can
  check.
- **Template numbers, don't generate them.** If you later want richer LLM-written narratives, pass the computed
  result in and have the model only rephrase. Then check that every number in its output appears in the result.
- **Ask, don't guess, when a question is ambiguous** (for example "best customers" by revenue or by orders). That is
  the next feature to add: a clarifying-question response type.

## 4. Data access and performance

- **Use a read-only service account** with a statement timeout and row limits (the compiler already caps `LIMIT`).
- **Cache by compiled SQL plus parameters**, invalidated when data refreshes. Repeat questions should return in
  under 50 ms.
- **Pre-aggregate hot paths** (daily metrics by the main dimensions) once the fact table gets large. DuckDB or
  warehouse materialized views both work.
- **Add row-level security in the compiler,** for example by injecting `region IN (user's regions)` from the
  user's identity, rather than trusting the UI.

## 5. Quality: evals before features

- **Build a golden set.** Turn real questions plus the expected `SemanticQuery` into tests (`tests/test_engine.py`
  shows the pattern). Run it in CI, and measure the parser and the LLM path separately.
- **Track metrics:** interpretation accuracy, the share of questions answered without the LLM, p50/p95 latency,
  "couldn't answer" rate and thumbs-down rate.
- **Add a thumbs up/down control on each answer** and log it next to the query. That gives you an eval set for free.

## 6. Security and operations

- Put secrets (API keys, warehouse credentials) in a secret manager or environment variables. Never commit them.
  `.env` files are git-ignored.
- Add SSO (OIDC) before anyone outside your team uses it. Log who asked what.
- Containerise (one `Dockerfile`, health check at `/api/health`) and deploy behind a reverse proxy with TLS.
- Treat question logs as potentially sensitive business data: set a retention policy.

## 7. Suggested roadmap

| Phase | Scope |
|---|---|
| **0 (this MVP)** | One semantic model, rules + optional Claude, breakdowns, trends, comparisons, drill-down, CSV |
| **1. Real data** | Warehouse connector (Postgres/Snowflake/BigQuery), dbt/Cube adapter, caching, auth |
| **2. Trust** | Clarifying questions, feedback buttons, golden-set CI, metric glossary pages |
| **3. Reach** | Slack/Teams bot over the same `/api/ask`, scheduled digests, saved/pinned questions |
| **4. Insight** | "Why did revenue drop?" driver analysis, anomaly alerts, forecasts |
