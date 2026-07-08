# Progress — fdaAgent

> Live project state for fast session pickup. This is the **dynamic** doc; the static
> roadmap is [PLAN.md](PLAN.md), commands/conventions are [AGENTS.md](AGENTS.md), and the
> file map is [PROJECT_INDEX.md](PROJECT_INDEX.md). To avoid drift, this file **links** to
> those rather than repeating them.
> **Maintenance:** at the end of each work session, update *Now / Next up / Blockers*. Keep it short.
> Last updated: 2026-07-08

## Goal (end state)
A deployed, demoable agent that answers natural-language questions about FDA drug recalls with **evidence-backed** results — **Path 1**: deterministic NL→SQL analytics (frequencies / trends / distributions, every number from SQL); **Path 2** (later): hybrid semantic retrieval for ad-hoc questions; served via FastAPI + a small UI, with an eval harness. Reproduces an industry-style LLM structuring + retrieval pipeline on 100% public-domain data (portfolio for NA AI/ML roles). Full roadmap in [PLAN.md](PLAN.md).

## Now
- **State:** Path 1 served + containerized. **Frontend redesign, Path 2 hybrid retrieval (2.1/2.2/2.3), and L1 observability/eval are merged and verified** — `/ask` routes fuzzy concepts through `semantic_query` into hybrid pgvector + Postgres FTS with RRF; the static UI is now a ChatGPT-style local conversation client; every handled `/ask` request is logged to `query_log`; golden eval v1 passes.
- ▶️ **Next action:** **2.4 per-item validation** — LLM yes/no + supporting snippet + threshold for semantic matches; semantic counting lands there. Then add Langfuse L2 if useful.

## Works now (verified)
1. **Ingest** — [src/fetch_openfda.py](src/fetch_openfda.py): generic openFDA→Postgres, idempotent JSONB upsert, `--since auto` incremental.
2. **Data** — table `drug_enforcement`, 17,723 rows: JSONB `raw` + 23 parsed STORED columns + indexes ([sql/001_parse_drug_enforcement.sql](sql/001_parse_drug_enforcement.sql)).
3. **Schema docs** — verbatim openFDA column comments ([sql/002_drug_enforcement_comments.sql](sql/002_drug_enforcement_comments.sql)).
4. **Analytics engine** — [src/analytics.py](src/analytics.py): `count_total` / `count_by` / `trend` / `sample`, read-only + parameterized, returns evidence `recall_number`s.
5. **NL→SQL layer** — [src/nl_query.py](src/nl_query.py): question → LLM → validated Pydantic `QuerySpec` (columns/values whitelisted; schema + column comments + value-index injected) → `analytics.py`. All numbers come from SQL. Verified: count_total / count_by / trend / sample.
6. **Serving + UI** — [src/api.py](src/api.py): FastAPI `/ask` (+ `/health`) warms the engine once, returns a chart-friendly, evidence-backed payload, and serves a zero-build ChatGPT-style UI ([web/index.html](web/index.html), [web/app.js](web/app.js), [web/styles.css](web/styles.css)) with a localStorage conversation sidebar, edit-message, stop-generation, and scalar / bar / line / table / retrieval rendering.
7. **Harness** — [AGENTS.md](AGENTS.md) + auto-generated [PROJECT_INDEX.md](PROJECT_INDEX.md) ([scripts/gen_index.py](scripts/gen_index.py)) + pre-commit hook ([scripts/hooks/pre-commit](scripts/hooks/pre-commit)).
8. **DB** — Postgres.app 17, db `fda`; extensions pgvector 0.8.1 / hypopg 1.4.3 / pg_stat_statements.
9. **Read-only DB MCP** — `postgres-fda` ([.vscode/mcp.json](.vscode/mcp.json), restricted mode).
10. **Skills** — under [.github/skills/](.github/skills/): db-column-docs-from-dictionary, openfda-data-download, skill-writing, learning-session-notes.
11. **Containerized (local)** — [Dockerfile](Dockerfile) (lean serving image; base-registry + PyPI mirrors are build-args) + [.dockerignore](.dockerignore) + [requirements-serve.txt](requirements-serve.txt). `docker run` serves `/ask` + UI, reaching host Postgres via `host.docker.internal`. Verified.
12. **Path 2 — hybrid retrieval wired into `/ask` (2.1 + 2.2 + 2.3)** — multi-source `embeddings` table ([sql/003](sql/003_recall_embeddings.sql) + [sql/004](sql/004_embeddings_multisource.sql), keyed `(source, source_id, field)`, every column documented in [sql/005](sql/005_embeddings_comments.sql)) + [src/embed.py](src/embed.py) (`SOURCES` registry; 35,446 vectors) + [src/retrieval.py](src/retrieval.py) (pgvector semantic candidates + Postgres FTS over `content_tsv`, fused with RRF and filter-aware); [src/nl_query.py](src/nl_query.py) routes `semantic_query` (concepts → retrieval, never `ilike`), rendered as a ranked list in the UI.
13. **Observability + eval (L1)** — [sql/006_query_log.sql](sql/006_query_log.sql) creates the idempotent `query_log` trace table; [src/observability.py](src/observability.py) logs request, QuerySpec, routing decision, compact response metadata, latency, and handled errors for `/ask`; [scripts/run_eval.py](scripts/run_eval.py) runs golden evals from [evals/golden/v1.json](evals/golden/v1.json). Verified: 5/5 golden cases pass, including SQL routing assertions and retrieval recall@10.

## Next up (ordered)
> Each item lists **concrete deliverables + done-when**. Design rationale lives in [PLAN.md](PLAN.md); don't duplicate it here.

1. **Path 2.4 — per-item validation + semantic counting.** Turn a fuzzy-concept query from a top-K list into a *verified count with confidence*.
   - **Verify step** (new `src/validation.py`, or extend [src/retrieval.py](src/retrieval.py)): after `retrieval.search()`, for each candidate ask the LLM **yes/no “does this recall actually describe <concept>?”** + a **supporting snippet** + confidence (Pydantic structured output).
   - **Threshold**: keep items above a similarity/confidence cutoff; calibrate the cutoff on a small hand-labeled set (record precision/recall at the chosen cutoff).
   - **Enable semantic + aggregate**: let `QuerySpec` carry `semantic_query` *together with* `intent=count_total/count_by` ([src/nl_query.py](src/nl_query.py)); `run_spec` runs the funnel (retrieve → verify → count) and returns **estimate + confidence interval + evidence `recall_number`s**.
   - **Serve/UI**: [src/api.py](src/api.py) returns estimate + CI; UI renders “~N recalls (estimated, verified k/K)”.
   - **Done when:** “how many sterility recalls?” returns a verified estimate with evidence, and a new golden case in [evals/golden/v1.json](evals/golden/v1.json) asserts it.

2. **Phase 4 — automated recall classification** (design: PLAN §4). Make recall problems a **structured label** so counting is exact.
   - **Schema** (`sql/007_taxonomy.sql`): `taxonomy(node_id,parent_id,label,definition,examples[],version,status)` + `recall_label(record_id,node_id,level,confidence,evidence,version,labeler)` + `taxonomy_candidate`.
   - **P1 induce** (new `src/classify/induce.py`): HDBSCAN over the 4,390 distinct `reason_for_recall` embeddings + prefix mining → LLM names each cluster → draft 2-level taxonomy → **you review & freeze v1**.
   - **P2 label** (new `src/classify/label.py`): closed-set LLM labeling of the 4,390 distinct texts (multi-label + confidence + evidence + `other`), hash-cached → write `recall_label`, backfill to 17.7k rows.
   - **P3 discovery** (new `src/classify/discover.py`): cluster the `other`/low-confidence residual → LLM-name candidates → dedup vs taxonomy → emit a **“candidate new categories” report** (size/growth/coherence).
   - **Wire query**: expose `GROUP BY recall_label` in [src/analytics.py](src/analytics.py) / [src/nl_query.py](src/nl_query.py) so “sterility by firm” returns **exact** counts (fills the semantic×aggregate gap; supersedes 2.4's estimate for known categories).
   - **Done when:** `recall_label` populated + a `count_by` over a taxonomy node works end-to-end in `/ask`.

3. **Phase 3 — firm entity resolution + tool-calling agent** (design: PLAN Phase 3). The capstone “is this company safe?”
   - **Schema** (`sql/008_firm_resolution.sql`): `firm(…,fda_present,source,confidence)` + `firm_alias(raw_firm→firm_id)` + `parent_group` + `brand_alias` + `resolution_log`.
   - **Offline build** (new `src/firm/resolve.py`): normalize the 1,634 firm strings → multi-signal candidate pairs (**pg_trgm + token-set + phonetic via `fuzzystrmatch` + name-embeddings**) → union-find / community cluster → **LLM verify** → materialize `firm`/`firm_alias`. Seed canonical firms from the NDC labeler for the 18% that have it.
   - **Brand→parent** (new `src/firm/brand.py`): NDC labeler > Wikidata > LLM, each link marked `source`+`confidence`, user-confirmable.
   - **Agent** (new `src/agent.py`): OpenAI function-calling over tools {analytics, retrieval, firm-resolution, web-search}; “is <brand> safe?” → resolve brand→firm set → **risk profile** (count by class / trend / top problems) with **3-tier provenance** (✅ FDA fact / ⚠️ inferred / 🌐 web).
   - **Done when:** “is <brand> safe?” resolves brand→firm set and returns an evidence-backed profile with provenance tiers.

4. **Observability L2 — Langfuse.** Only after L1 `query_log` shows a concrete gap. Wrap the `/ask` LLM calls as Langfuse traces/spans ([src/observability.py](src/observability.py)); keep `query_log` as the SQL-queryable source of truth. **Done when:** a traced `/ask` is inspectable in Langfuse without losing the `query_log` row.

5. **Frontend v2 (one PR) — session titles + sidebar icons.** Self-contained UI PR; history stays in `localStorage` (server-side persistence is item 6).
   - **Auto-summary titles:** new server-side `POST /title` (`gpt-4o-mini`) → ≤6-word title from the **first question**, called once per conversation (manual rename still wins); replaces the `titleFromQuestion` truncation in [web/app.js](web/app.js); OpenAI key stays server-side; optionally log `route='/title'` in `query_log`.
   - **Sidebar icons:** replace the “R”/“D” text buttons with inline-SVG **pencil = rename** / **trash = delete** (existing `renameConversation` / `deleteConversation`); zero-build, no new deps.
   - **Done when:** new conversations auto-title once and the sidebar shows icon controls.

6. **Conversational context v2.** Server-side conversation history keyed to `query_log`; `/ask` accepts a conversation id + prior turns so follow-ups carry context (UI currently sends each turn independently). **Done when:** a follow-up question resolves pronouns/ellipsis using prior turns.

7. **Public deploy (optional).** Build/push the image to HF Spaces or Render + a managed Postgres with pgvector (Supabase / Neon); load `sql/` + embeddings; set `DATABASE_URL` / `OPENAI_API_KEY` as secrets. **Done when:** a public URL serves `/ask` against the managed DB.

## Backlog (unscheduled ideas)
- **Company Exposure Index** — per-product-category ranking of firms/brands by openFDA "exposure" (à la Karpathy's AI-exposure), with a category dropdown → ranked chart. **Coupled, not standalone:** reuses generic ingest + Path 1 `count_by`, but **depends on Phase 3 entity resolution** for correct per-firm numbers; decoupled only from the LLM/agent/chat layer. Key nuance: exposure ≠ raw count → needs normalization (per product/NDC count) + severity weight (Class I/II/III) + recency. Full design in [PLAN.md](PLAN.md) 附录 A1.

## Blockers & gotchas
- ⚠️ **The venv is not relocatable** — it broke once after the project folder was renamed; recreated. Always run `.venv/bin/python …`, or re-`source .venv/bin/activate` after any move.
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
- **Vision = FDA-grounded vertical agent** (not just NL→DB): FDA = ground-truth fact layer; web/Wikidata = augmentation layer, strictly isolated (web numbers never enter counts; negative = "not found in FDA", not "safe"). Two scenarios: personal pull (Q&A) + enterprise push (monitor watched firms/device types; reuses `--since auto`).
- **Automated classification (Phase 4) = induce + closed-set label + discovery loop; human governs, never hand-labels** (TnT-LLM). Closed-set classifier assigns known taxonomy; open-set residual clustering surfaces new categories for approval. Initial taxonomy is data-induced (cluster + prefix mining), not hand-written. Label distinct text only; offline + hash-cached; optional distilled classifier.
- **Entity resolution: recall via multi-signal union (not normalization alone), precision via verify** — identity ≠ FDA footprint (`fda_present`; external/zero-recall firms exist; truly-unknown → `resolution_log`, never fabricate). NDC = 18% verifier, names = 100% path. Tables are a versioned side-car; source table stays immutable.
- **Cosmetics ≈ out of scope** — openFDA has cosmetic *adverse events* (`/food/event`), not a clean recall endpoint; the architecture generalizes to `device`/`food/enforcement` (same firm structure).
