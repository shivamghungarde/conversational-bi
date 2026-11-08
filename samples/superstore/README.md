# Superstore sample

The Superstore retail sample: fictional US orders, 2018–2021. It was received as a PostgreSQL dump in a third-party
POC and is stored here as two gzipped CSVs, loaded into SQLite by `bi/superstore.py` at startup.

| File | Rows | Grain |
|---|---|---|
| `orders.csv.gz` | 9,994 | one row per **order line** (5,009 orders) |
| `returns.csv.gz` | 296 | one row per **returned order** (all present in `orders`) |

Changes from the dump: US ZIP codes are restored to five digits (438 had lost a leading zero, e.g. `1841` → `01841`)
and `row_id`/`quantity` are integers. The 11 Burlington, Vermont lines have no ZIP in the source and are
kept empty. Values are otherwise unchanged.

Returns are recorded per order while sales are per line, so the semantic model defines return rate per order
(`returned orders ÷ orders`), not per line.

The customers, products and figures are fabricated sample data, so the dataset fits the synthetic-only profile.
Before sharing outside the organisation, check the sample's licence terms.
