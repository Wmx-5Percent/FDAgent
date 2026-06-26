# Progress — fdaAgent

> Live project state for fast session pickup. This is the **dynamic** doc; the static
> roadmap is [PLAN.md](PLAN.md), commands/conventions are [AGENTS.md](AGENTS.md), and the
> file map is [PROJECT_INDEX.md](PROJECT_INDEX.md). To avoid drift, this file **links** to
> those rather than repeating them.
> **Maintenance:** at the end of each work session, update *Now / Next up / Blockers*. Keep it short.
> Last updated: 2026-06-26

## Goal (end state)
A deployed, demoable agent that answers natural-language questions about FDA drug recalls with **evidence-backed** results — **Path 1**: deterministic NL→SQL analytics (frequencies / trends / distributions, every number from SQL); **Path 2** (later): hybrid semantic retrieval for ad-hoc questions; served via FastAPI + a small UI, with an eval harness. Reproduces an industry LLM ticket-intelligence pipeline on 100% public-domain data (portfolio for NA AI/ML roles). Full roadmap in [PLAN.md](PLAN.md).

## Now
- **State:** Path 1 served + containerized. **Path 2 slices 2.1 + 2.3 done** — 35,446 embeddings in the multi-source `embeddings` table; `/ask` now routes fuzzy concepts through `semantic_query` → semantic retrieval (vector, filter-aware), no more `ilike`. Verified on web ("pills too strong" → Superpotent; "Class I glass fragments" → Class I particulate).
- ▶️ **Next action:** **frontend redesign** — a ChatGPT-style chat UI (thread + conversation sidebar + edit-message + stop-generation), then **2.2** hybrid (FTS + RRF). Plan in *Next up*.

## Works now (verified)
1. **Ingest** — [src/fetch_openfda.py](src/fetch_openfda.py): generic openFDA→Postgres, idempotent JSONB upsert, `--since auto` incremental.
2. **Data** — table `drug_enforcement`, 17,723 rows: JSONB `raw` + 23 parsed STORED columns + indexes ([sql/001_parse_drug_enforcement.sql](sql/001_parse_drug_enforcement.sql)).
3. **Schema docs** — verbatim openFDA column comments ([sql/002_drug_enforcement_comments.sql](sql/002_drug_enforcement_comments.sql)).
4. **Analytics engine** — [src/analytics.py](src/analytics.py): `count_total` / `count_by` / `trend` / `sample`, read-only + parameterized, returns evidence `recall_number`s.
5. **NL→SQL layer** — [src/nl_query.py](src/nl_query.py): question → LLM → validated Pydantic `QuerySpec` (columns/values whitelisted; schema + column comments + value-index injected) → `analytics.py`. All numbers come from SQL. Verified: count_total / count_by / trend / sample.
6. **Serving** — [src/api.py](src/api.py): FastAPI `/ask` (+ `/health`) warms the engine once, returns a chart-friendly, evidence-backed payload; static UI [web/index.html](web/index.html) renders scalar / bar / line / table. Smoke-tested across all four intents.
7. **Harness** — [AGENTS.md](AGENTS.md) + auto-generated [PROJECT_INDEX.md](PROJECT_INDEX.md) ([scripts/gen_index.py](scripts/gen_index.py)) + pre-commit hook ([scripts/hooks/pre-commit](scripts/hooks/pre-commit)).
8. **DB** — Postgres.app 17, db `fda`; extensions pgvector 0.8.1 / hypopg 1.4.3 / pg_stat_statements.
9. **Read-only DB MCP** — `postgres-fda` ([.vscode/mcp.json](.vscode/mcp.json), restricted mode).
10. **Skills** — under [.github/skills/](.github/skills/): db-column-docs-from-dictionary, openfda-data-download, skill-writing, learning-session-notes.
11. **Containerized (local)** — [Dockerfile](Dockerfile) (lean serving image; base-registry + PyPI mirrors are build-args) + [.dockerignore](.dockerignore) + [requirements-serve.txt](requirements-serve.txt). `docker run` serves `/ask` + UI, reaching host Postgres via `host.docker.internal`. Verified.
12. **Path 2 — semantic retrieval wired into `/ask` (2.1 + 2.3)** — multi-source `embeddings` table ([sql/003](sql/003_recall_embeddings.sql) + [sql/004](sql/004_embeddings_multisource.sql), keyed `(source, source_id, field)`, every column documented in [sql/005](sql/005_embeddings_comments.sql)) + [src/embed.py](src/embed.py) (`SOURCES` registry; 35,446 vectors) + [src/retrieval.py](src/retrieval.py) (vector search, filter-aware); [src/nl_query.py](src/nl_query.py) routes `semantic_query` (concepts → retrieval, never `ilike`), rendered as a ranked list in the UI. Hybrid (FTS + RRF) is the remaining **2.2** increment.

## Next up (ordered)
**Frontend — ChatGPT-style chat UI** (before 2.2; **plan ready**):
1. Replace the single input box with a chat **thread** (user/assistant bubbles, per-message result rendering) + a left **conversation sidebar** (new / switch / rename / delete). Stay vanilla JS (zero-build → single Docker image); split into `web/index.html` + `app.js` + `styles.css` (api.py serves `web/` as static).
2. **Edit a sent message** (→ truncate the thread after it → re-ask) and **Stop generation** (`AbortController`, client-side cancel). History in **localStorage** (no backend change). v1 = each turn is an independent `/ask` (no cross-turn context); conversational context + server-side history (tied to `query_log`) are v2.

**Path 2 — finish hybrid retrieval:**
3. **2.2 Hybrid** — add the Postgres-FTS keyword half (`content_tsv` ready) + **RRF** fusion to [src/retrieval.py](src/retrieval.py); fixes exact-term misses (NDMA, child-resistant). *(2.1 embed + 2.3 router/UI render already done — see Works now #12.)*
4. **2.4 Per-item validation** — LLM yes/no + supporting snippet + threshold; **semantic counting** lands here (estimate + confidence).

**Observability + eval:**
5. **`query_log` (L1, Postgres)** → **Langfuse (L2)**; **eval harness** — versioned golden set; deterministic asserts where numbers come from SQL (e.g. `sterility-related` MUST emit `semantic_query`, not `ilike`); recall@k for retrieval; LLM-as-judge for faithfulness.

**Phase 3 — agentic capstone** ("is this company safe?"): firm entity-resolution (`pg_trgm` fuzzy + known-subsidiary expansion + LLM verify) + tool-calling agent; reuses Path 2. **Public deploy (optional):** push the image to HF Spaces / Render + a managed Postgres + pgvector (Supabase / Neon).

## Blockers & gotchas
- ⚠️ **The venv is not relocatable** — it broke once after the folder was renamed (`find-jobs/ticket agent` → `fdaAgent`); recreated. Always run `.venv/bin/python …`, or re-`source .venv/bin/activate` after any move.
- ⚠️ **`hypopg` is built into the Postgres.app bundle** — rebuild it after a Postgres.app major-version upgrade.
- ⚠️ **Postgres MCP quirks** — `get_object_details` does not render column comments, and restricted-mode `execute_sql` rejects catalog queries (`col_description`, `::regclass`). Read comments via `psql \d+` or a direct connection; normal data `SELECT`s through the MCP are fine.
- ℹ️ **`state` stores 2-letter codes** (`CA`, not `California`). The NL layer enumerates a column's allowed values into the prompt ("value index") so the LLM uses real codes — keep that pattern when adding categorical filters.
- 🐳 **Docker build on this machine (CN network):** Docker Hub + PyPI time out — build with mirror build-args (`REGISTRY=docker.m.daocloud.io`, `PIP_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple`). Don't hand-edit `~/.docker/daemon.json` (Docker Desktop overwrites it); the base-image registry is parameterized in the [Dockerfile](Dockerfile) instead.
- 🐳 **Container → host Postgres:** use `DATABASE_URL=postgresql://<role>@host.docker.internal:5432/fda`. `host.docker.internal` reaches Postgres.app even though it only listens on `localhost`, but you MUST set the role — the container runs as `root` (no such Postgres role); the host role is `waywei`.

## Decisions (settled — don't re-litigate)
- **Dataset = `drug/enforcement`** — right-sized (~17.7k), clean, single-file download.
- **Storage = JSONB `raw` + STORED generated columns** — parsed columns auto-recompute on re-ingest, so they never drift from `raw`.
- **Deterministic engine first, LLM on top** — every number comes from SQL, never from the model.
- **One store = Postgres + pgvector** — no separate vector DB.
- **MCP is read-only; schema changes go through versioned `sql/` scripts**, not ad-hoc writes.
- **Column docs = verbatim openFDA text**; anything not from openFDA is marked `[inferred]`.
- **Path 2 retrieval = hybrid, in Postgres** — pgvector (semantic) ⊕ Postgres FTS `ts_rank` (keyword) fused via RRF. True BM25 (`pg_search`) only if FTS recall proves insufficient.
- **Embeddings = `text-embedding-3-small` (1536-d)** in a separate, **multi-source** `embeddings` table keyed `(source, source_id, field)` — one row per (record, field), e.g. `drug_enforcement` × {reason_for_recall, product_description} — NOT extra columns on the source table. One HNSW + one GIN index cover all sources/fields; adding an FDA dataset = one `SOURCES` entry in `embed.py` (no schema change). Incremental re-embed only new/changed text.
- **Concepts route via `semantic_query`, never `ilike`** — hard facts go in `filters` (Tier-A columns), fuzzy concepts in `semantic_query`.
- **Semantic counting is an estimate** — deferred to validation (2.4): retrieve-above-threshold → per-item verify → count + confidence. v1 retrieval returns top-K only.
- **Observability before scale** — `query_log` (Postgres, L1) first, then Langfuse (L2); every `/ask` is a trace and the `QuerySpec` is the materialized, inspectable "reasoning".
- **"Is this company safe?" = Phase 3 capstone, after Path 2** — brand→parent is `[inferred]` (LLM / Wikidata / NDC labeler, marked + confirmable); company→`recalling_firm` is entity resolution (`pg_trgm` fuzzy + known-subsidiary expansion + LLM verify), since firms are fragmented (1,634 distinct; Pfizer/Teva/McNeil appear under many names/subsidiaries).
- **Cosmetics ≈ out of scope** — openFDA has cosmetic *adverse events* (`/food/event`), not a clean recall endpoint; the architecture generalizes to `device`/`food/enforcement` (same firm structure).
