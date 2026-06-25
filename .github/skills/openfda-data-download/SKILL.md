---
name: openfda-data-download
description: "Download or incrementally ingest data from any openFDA endpoint (drug / device / food / animal & veterinary / tobacco / other) into a local store, with a decision guide on dataset size (live API vs bulk download) and measured record counts per dataset. Use when: pulling a new openFDA dataset; deciding which endpoint or size fits a task; wiring an openFDA-to-Postgres ingest; estimating how large a dataset is before committing. Not for: non-openFDA APIs, or downstream modeling/RAG."
---

# Downloading / ingesting openFDA data

## Metadata
- **Type**: API Guide + Workflow
- **Use when**: pulling any openFDA dataset locally, right-sizing the choice of endpoint, or wiring an openFDA → DB ingest.
- **Output**: rows in a local store. In this repo: PostgreSQL via [src/fetch_openfda.py](../../../src/fetch_openfda.py).
- **Created**: 2026-06-25.

## Goal
Get any openFDA dataset into a local store reliably, using the access pattern (live API vs bulk download) that fits the dataset's size and the task.

## How the API works (the three things you must know)
- **One endpoint per query:** `https://api.fda.gov/<noun>/<endpoint>.json`. Every response is `meta` (includes `results.total`) + `results[]` (JSON records).
- **Query params:** `search=<field>:<value>` (`+AND+`, range `[A TO B]`, exact phrase via the `.exact` suffix); `count=<field>` for **server-side aggregation** (use it instead of downloading everything just to count); `limit` (≤ 1000); `skip` (≤ 25000); `sort`; `api_key`.
- **Authoritative schema per endpoint:** `https://open.fda.gov/fields/<endpoint>.yaml` (every field + definition + enum values). Bulk zipped-JSON files and the catalog: `https://open.fda.gov/data/downloads/`.

## Dataset catalog — measured record counts (as-of 2026-06-24)
> Counts only grow. **Never hardcode a total in logic** — re-query it:
> `curl -s "https://api.fda.gov/<endpoint>.json?limit=1" | python3 -c "import sys,json;print(json.load(sys.stdin)['meta']['results']['total'])"`

| Endpoint | Records | Endpoint | Records |
|---|--:|---|--:|
| device/event | 25,039,198 | food/event | 149,945 |
| drug/event | 20,328,575 | drug/ndc | 136,167 |
| device/udi | 5,083,948 | cosmetic/event | 85,511 |
| animalandveterinary/event | 1,340,077 | device/recall | 58,533 |
| other/nsde | 660,911 | device/pma | 56,607 |
| device/registrationlisting | 327,885 | device/enforcement | 39,225 |
| drug/label | 259,616 | drug/drugsfda | 29,159 |
| device/510k | 175,299 | food/enforcement | 29,034 |
| other/unii | 174,260 | drug/enforcement | 17,723 |
| other/substance | 167,385 | device/covid19serology | 13,420 |
| | | other/historicaldocument | 8,858 |
| | | device/classification | 7,071 |
| | | drug/shortages | 1,637 |
| | | tobacco/problem | 1,337 |
| | | transparency/crl | 439 |

## Verified natural keys / incremental date fields
Used for idempotent upserts and `--since` windows (from [src/fetch_openfda.py](../../../src/fetch_openfda.py) `ENDPOINT_DEFAULTS`). For any endpoint not listed, look the key up in that endpoint's `fields/<endpoint>.yaml` — **do not guess it**.

| Endpoint | Natural key | Date field |
|---|---|---|
| drug/enforcement, food/enforcement, device/enforcement | `recall_number` | `report_date` |
| device/recall | `cfres_id` | `event_date_posted` |
| drug/event | `safetyreportid` | `receivedate` |
| device/event | `mdr_report_key` | `date_received` |
| drug/label | `id` | `effective_time` |
| drug/ndc | `product_ndc` | — |
| drug/drugsfda | `application_number` | — |
| device/510k | `k_number` | `decision_date` |
| device/pma | `pma_number` | `decision_date` |
| device/classification | `product_code` | — |

## Decision guide: API vs bulk download (size-driven)
- **≤ ~25k records, or you need filtering / incremental refresh** → live API + `skip` pagination (this repo's ingester). `skip` caps at 25000, so ~26k is the deepest reachable by paging.
- **> 25k and you want the whole set** → the skip cap blocks deep paging. Either **partition by a date field** (`search=<date>:[A TO B]` windows) or **download the bulk JSON** from the downloads page. Do not brute-force `skip` past 25000.
- **Millions (device/event, drug/event, device/udi)** → too big for a local full-load RAG. Use the API for `count=` aggregations, or bulk-download into a warehouse. Not a starter dataset.
- **Good right-sized choices** for a demo/RAG: `drug/enforcement` (17.7k), `drug/shortages` (1.6k), or a filtered slice of `drug/label`.

## This repo's ingester (output spec)
```bash
.venv/bin/python src/fetch_openfda.py --endpoint <noun/endpoint> --table <table> [--since auto] [--search '<filter>']
```
Stores each record as JSONB `raw` plus an extracted `id` (the endpoint's natural key) and a `report_date`; columns are `(id, source, report_date, raw, fetched_at)`. Generic over endpoints, idempotent upsert (`ON CONFLICT (id)`), and `--since auto` pulls only rows newer than `MAX(report_date)` already stored.

## Acceptance criteria
- For a full load, the loaded row count equals the endpoint's `meta.results.total`; for a filtered/incremental load, it equals the filtered subset's `total`.
- Re-running is idempotent — no duplicate rows (primary key = natural key).
- For a > 26k full load you used date-partitioning or bulk files, **not** a silently truncated `skip` loop.

## Known pitfalls (each one actually happened building this repo)
- **Date-range encoding bug.** Build `search` ranges with literal spaces — `<field>:[20240101 TO 20260624]`, never `+`. The HTTP layer encodes a space to `+` (which openFDA reads as a space), but encodes a literal `+` to `%2B` (a literal plus) → malformed Lucene range → **HTTP 500**.
- **`skip` hard cap = 25000.** Paging past it errors; see the decision guide.
- **HTTP 404 means "No matches found"**, not a failure — treat a 404 search response as zero results instead of crashing.
- **Rate limits:** ~240/min and 1000/day without a key; 240/min and 120k/day with a free `api_key` (env `OPENFDA_API_KEY`). Sleep briefly between pages and retry 429/5xx with backoff.
- **Counts are stale the instant you record them** — re-query `meta.results.total`; the table above is a snapshot, not a constant.
- **`fetch_webpage` reformats/wraps** the downloads and data-dictionary pages — hit the API or pull the structured `fields/*.yaml` directly instead.
