# Best practices for building this as a standalone product

## 1. Repository and project hygiene

- **Its own repository (done).** This project was split out of the personal-site repo with its history
  intact (`git subtree split`). Keep it independent: no shared code with other projects.
- **Keep it self-contained:** its own README, tests, run script and dependency file.
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

## 3. Natural language: on-prem, fast, honest

- **Keep questions on the network.** The rules engine needs no model at all. If you add one, self-host it
  (Ollama or vLLM on an internal GPU box) and keep the private-network guard in `bi/llm.py`. Log only locally.
- **Never let a model write raw SQL against the warehouse.** Have it fill a structured query constrained by
  JSON-schema enums, then validate and compile it yourself.
- **Use tiered interpretation for speed:** rules first (under a millisecond), the local model only when
  confidence is low.
- **Ask, don't guess.** Clarify vague measures, values that match several things, vague periods, and "why"
  questions with no period. Grow the `ambiguous_terms` list in the model from the question log.
- **Always show the interpretation** as chips, keep the SQL one click away, and stamp every answer with its
  "as of" time.
- **Template numbers, don't generate them.** If a local model later writes richer narratives, give it the computed
  result and check that every number it outputs appears in that result.

## 3a. "Why" questions

- Agree the **decomposition** with Finance and Delivery up front. Rate versus mix for utilization is standard, but
  leaders must recognise it. Show "Rate effect" and "Mix effect" in the table and tooltips, as the POC does.
- Choose `explain_by` dimensions per metric so explanations are causal, not circular.
- Watch out for **partial periods**: compare month-to-date with the same days of last month, never with a full month.
  The POC does this automatically.

## 4. Data access and real-time freshness

- **Query the system of record live**, through a read-only account with a statement timeout and the compiler's row
  caps. For real-time numbers, don't add a result cache. If load grows, cache for a few seconds at most, keyed on
  the data's last-change time.
- **Show freshness everywhere:** when the data last changed (`freshness_sql` in the model, e.g. the latest timesheet
  submission) and when each answer was computed.
- **Files:** point `BI_SOURCE` at a file that an export job overwrites. The app reloads it when it changes.
- **Later:** pre-aggregate hot paths (daily utilization by practice and region) with incremental refresh once the fact
  table is large. Add row-level security in the compiler from the user's identity.

## 5. Quality: evals before features

- **Build a golden set.** Turn real questions plus the expected `SemanticQuery` into tests (`tests/test_engine.py`
  shows the pattern). Run it in CI, and measure the parser and the LLM path separately.
- **Track metrics:** interpretation accuracy, the share of questions answered without the LLM, p50/p95 latency,
  "couldn't answer" rate and thumbs-down rate.
- **Add a thumbs up/down control on each answer** and log it next to the query. That gives you an eval set for free.

## 6. Security and operations

- Put secrets (warehouse credentials) in a secret manager or environment variables. Never commit them.
  `.env` files are git-ignored.
- Add SSO (OIDC) before anyone outside your team uses it. Log who asked what.
- Containerise (one `Dockerfile`, health check at `/api/health`) and deploy behind a reverse proxy with TLS.
- Treat question logs as potentially sensitive business data: set a retention policy.

## 7. Suggested roadmap

| Phase | Scope |
|---|---|
| **0 (this POC)** | Utilization and revenue model, CSV/Excel/SQLite sources, clarifying questions, what/how/why, live data, on-prem only |
| **1. Real data** | Connect the real timesheet/finance database, agree metric definitions, SSO and row-level security |
| **2. Trust** | Feedback buttons, golden-set CI, metric glossary pages, a self-hosted LLM for unusual phrasings |
| **3. Reach** | Slack/Teams bot over the same `/api/ask`, scheduled digests, saved/pinned questions |
| **4. Insight** | "Why did revenue drop?" driver analysis, anomaly alerts, forecasts |
