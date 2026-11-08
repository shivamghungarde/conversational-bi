# QC implementation — 29 October 2026

Scope: supervised local demo using synthetic data. Shared pilot users will initially have the same access.
The QC report identified both code defects and future deployment requirements. The changes below implement local
regression gates and safeguards; they do not certify production security or make an arbitrary imported model governed.

## Audit coverage

| Finding | Implemented check or safeguard | Remaining work |
|---|---|---|
| Ignored question conditions | Unconsumed meaningful terms trigger clarification; missing/unsupported constraints are not silently dropped | Grow the golden question corpus from real usage; test a live model's interpretation accuracy |
| Incorrect numeric parsing | US thousands/European decimal regression cases; finite-number checks; invalid typed cells reject import | User-selectable import locale for ambiguous values |
| Top-N comparisons | Prior aggregate is matched to the current ranked groups before output; same oracle checked in SQL, CSV and Excel | A separate view for groups present only in the prior period |
| Unsupported attribution | Non-additive metrics without a numerator/denominator return a clear error | Weighted average/distinct-count decompositions require business definitions |
| Zero baseline | Zero-to-positive direction uses absolute change, not an undefined percentage | None for the tested case |
| Source collisions | Path-based immutable IDs, unique cache generations, extension-specific sidecars; reserved built-in ID cannot be replaced by an upload | Dataset lifecycle/deletion administration |
| Cross-source UI state | Each message retains its own model and submission context; drill-down behaviour regression | Full browser automation beyond the manual smoke check |
| Inferred metric semantics | Bill/unit rates average rather than percentage/sum; currency metadata/display; draft warning | Interactive aggregation/currency/date/weight confirmation before real-data use |
| Truncated contributions | An Other group preserves the displayed decomposition; >1,000 drivers are explicitly refused; query truncation is exposed | Paginated or database-side complete decomposition for larger groups |
| Partial periods | Prior month-to-date clamps to prior-period end; ISO week boundaries align; invalid ranges rejected | Typed fiscal/calendar period policy rather than label-derived period intent |
| Invalid API shapes | Strict SemanticQuery constructor, object-only JSON, size/range/list limits, safe HTTP error boundary, unknown-source 404 | FastAPI/Pydantic request schemas now cover the HTTP boundary; response-schema versioning remains future work |
| File replacement/reload | Validate in staging, activate under registry lock; schema changes refuse new cache; snapshots pin active connections | Multi-file crash consistency and explicit schema-remapping UI |
| Misleading charts | Signed bars, valid negative/>100% domains, missing-point gaps and compound series keys | Full visual/accessibility review on target devices |
| Authentication boundary | Localhost default retained; network binding requires explicit opt-in; Host/Origin validation and CSP/frame headers | Authenticated TLS reverse proxy and upload permissions before shared real data; SSO not implemented |
| On-prem model boundary | Private IP pinned at connection; no proxies/redirects/link-local; bounded output/response; clarification wins over model guessing | Network egress policy and a live LLM integration; dimension candidates may contain business names |
| Hostile uploads/exports | Expanded ZIP size, compression, sheet/cell/row/column limits; formula-safe text export | Parsing worker process and broader format fuzzing |
| Sensitive metadata/logs | API source descriptions contain type only; logs omit question/query/error by default; private permissions; 5 MB rotation | Organizational retention/backup policies |
| Workload bounds/snapshots | 16 active HTTP requests, bounded body-read time, SQL deadlines, request connection cleanup, bounded dimension values, repeatable read snapshots | Pool sizing, indexed metadata/freshness cache, realistic load testing |
| Spreadsheet contracts | 1904 epoch and percentage styles tested, all typed values validated, numeric ID text preserved, timezone-aware response times | Explicit date locale/sheet selection, exact-decimal financial storage |
| Test/development gaps | Python matrix + Ruff + frontend checks in CI; optional PostgreSQL job; temporary fixture generation | Live PostgreSQL job must run on GitHub; full static typing and live LLM quality evaluation |
| Frontend failure/accessibility gaps | Empty-source state; upload/rerun error handling; stale rerun protection; keyboard buttons for table sort/drill; normal Tab navigation | Keyboard/touch chart tooltips and complete WCAG audit |

## How to run

```sh
.venv/bin/python -m unittest discover -s tests -v
node tests/test_ui.cjs
ruff check bi tests
```

The PostgreSQL checks skip unless `BI_TEST_POSTGRES_URL` is set. CI provisions a disposable synthetic PostgreSQL
service. The optional integration creates and drops a unique schema, so use a dedicated test database.

The suite includes independent arithmetic oracles, HTTP/import tests, private model transport mocks and frontend
behaviour checks using Node's built-in VM and assertions. It does not claim every phrasing or input is covered.

## Deployment gates

- Keep the current demo local and synthetic.
- Review imported metric definitions; draft inference is not business approval.
- Before a shared pilot, add authenticated TLS access, control uploads, configure Host/Origin and network egress,
  and agree log retention. `BI_ALLOW_NETWORK=1` is an explicit deployment opt-in, not authentication.
- Measure realistic concurrent workloads before increasing limits or introducing queues/pre-aggregation.

## Synthetic-profile architecture update

See [ARCHITECTURE.md](ARCHITECTURE.md). The API now uses FastAPI, Pydantic and Uvicorn with an app factory, explicit startup/shutdown, isolated settings, and framework-independent source registry and use cases. New tests exercise synthetic declarations, marked upload reload, typed requests, streamed body limits, body deadlines, isolated apps and feeder shutdown. The previous engine and cross-format arithmetic tests remain.
