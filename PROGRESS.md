# Progress — fdaAgent

> Live project state for fast session pickup. This is the **dynamic** doc; the static
> roadmap is [PLAN.md](PLAN.md), commands/conventions are [AGENTS.md](AGENTS.md), and the
> file map is [PROJECT_INDEX.md](PROJECT_INDEX.md). To avoid drift, this file **links** to
> those rather than repeating them.
> **Maintenance:** at the end of each work session, update *Now / Next up / Blockers*. Keep it short.
> Last updated: 2026-07-08

## Goal (end state)
A deployed, demoable agent that answers natural-language questions about FDA drug recalls with **evidence-backed** results — **Path 1**: deterministic NL→SQL analytics (frequencies / trends / distributions, every number from SQL); **Path 2**: hybrid semantic retrieval + validated semantic-count estimates for fuzzy concepts; served via FastAPI + a small UI, with an eval harness. Reproduces an industry-style LLM structuring + retrieval pipeline on 100% public-domain data (portfolio for NA AI/ML roles). Full roadmap in [PLAN.md](PLAN.md).

## Now
- **State:** Path 1 served + containerized. **Path 2.4 semantic validation/counting, Phase 3/4 sidecar foundations, and Frontend v2 are merged** — `/ask` can route fuzzy concepts through retrieval → LLM yes/no validation → estimated counts with evidence/confidence; offline taxonomy and firm-resolution schemas/CLIs are in place; the UI has one-time server title generation and icon sidebar controls.
- ▶️ **Next action:** run/freeze a Phase 4 taxonomy v1, label/backfill `recall_label`, then wire taxonomy `count_by` into `/ask`; after that populate firm aliases and build the Phase 3 “is this company safe?” agent.

## Works now (verified)
1. **Ingest** — [src/fetch_openfda.py](src/fetch_openfda.py): generic openFDA→Postgres, idempotent JSONB upsert, `--since auto` incremental.
2. **Data** — table `drug_enforcement`, 17,723 rows: JSONB `raw` + 23 parsed STORED columns + indexes ([sql/001_parse_drug_enforcement.sql](sql/001_parse_drug_enforcement.sql)).
3. **Schema docs** — verbatim openFDA column comments ([sql/002_drug_enforcement_comments.sql](sql/002_drug_enforcement_comments.sql)).
4. **Analytics engine** — [src/analytics.py](src/analytics.py): `count_total` / `count_by` / `trend` / `sample`, read-only + parameterized, returns evidence `recall_number`s.
5. **NL→SQL layer** — [src/nl_query.py](src/nl_query.py): question → LLM → validated Pydantic `QuerySpec` (columns/values whitelisted; schema + column comments + value-index injected) → `analytics.py`. All numbers come from SQL. Verified: count_total / count_by / trend / sample.
6. **Serving + UI** — [src/api.py](src/api.py): FastAPI `/ask` (+ `/health`) warms the engine once, returns a chart-friendly, evidence-backed payload, and serves a zero-build ChatGPT-style UI ([web/index.html](web/index.html), [web/app.js](web/app.js), [web/styles.css](web/styles.css)) with a localStorage conversation sidebar, edit-message, stop-generation, scalar / bar / line / table / retrieval / semantic-count rendering, one-time server-generated titles (`POST /title`), and inline-SVG rename/delete controls.
7. **Harness** — [AGENTS.md](AGENTS.md) + auto-generated [PROJECT_INDEX.md](PROJECT_INDEX.md) ([scripts/gen_index.py](scripts/gen_index.py)) + pre-commit hook ([scripts/hooks/pre-commit](scripts/hooks/pre-commit)).
8. **DB** — Postgres.app 17, db `fda`; extensions pgvector 0.8.1 / hypopg 1.4.3 / pg_stat_statements.
9. **Read-only DB MCP** — `postgres-fda` ([.vscode/mcp.json](.vscode/mcp.json), restricted mode).
10. **Skills** — under [.github/skills/](.github/skills/): db-column-docs-from-dictionary, openfda-data-download, skill-writing, learning-session-notes, parallel-agent-pr-coordination, parallel-agent-prompt-pack.
11. **Containerized (local)** — [Dockerfile](Dockerfile) (lean serving image; base-registry + PyPI mirrors are build-args) + [.dockerignore](.dockerignore) + [requirements-serve.txt](requirements-serve.txt). `docker run` serves `/ask` + UI, reaching host Postgres via `host.docker.internal`. Verified.
12. **Path 2 — hybrid retrieval + validated semantic counting (2.1–2.4)** — multi-source `embeddings` table ([sql/003](sql/003_recall_embeddings.sql) + [sql/004](sql/004_embeddings_multisource.sql), keyed `(source, source_id, field)`, every column documented in [sql/005](sql/005_embeddings_comments.sql)) + [src/embed.py](src/embed.py) (`SOURCES` registry; 35,446 vectors) + [src/retrieval.py](src/retrieval.py) (pgvector semantic candidates + Postgres FTS over `content_tsv`, fused with RRF and filter-aware); [src/nl_query.py](src/nl_query.py) routes `semantic_query` (concepts → retrieval, never `ilike`) and can combine it with `count_total` / `count_by`; [src/validation.py](src/validation.py) performs structured LLM yes/no validation with snippets/confidence so semantic counts are returned as estimates with evidence.
13. **Observability + eval (L1)** — [sql/006_query_log.sql](sql/006_query_log.sql) creates the idempotent `query_log` trace table; [src/observability.py](src/observability.py) logs request, QuerySpec, routing decision, compact response metadata, latency, and handled errors for `/ask`; [scripts/run_eval.py](scripts/run_eval.py) runs golden evals from [evals/golden/v1.json](evals/golden/v1.json), now including a semantic-count case.
14. **Phase 4 classification foundation** — [sql/007_taxonomy.sql](sql/007_taxonomy.sql) + [src/classify/](src/classify/) provide sidecar taxonomy tables and dry-run-first CLIs for taxonomy induction, closed-set labeling, and residual discovery. Not yet wired into `/ask`.
15. **Phase 3 firm-resolution foundation** — [sql/008_firm_resolution.sql](sql/008_firm_resolution.sql) + [src/firm/](src/firm/) provide sidecar firm / alias / brand / resolution-log tables and dry-run-first CLIs for firm normalization, candidate generation, provenance, and unknown handling. Not yet wired into `/ask`.

## Next up (ordered)
> Each item lists **concrete deliverables + done-when**. Design rationale lives in [PLAN.md](PLAN.md); don't duplicate it here.

1. **Phase 4 — run + freeze taxonomy v1, then exact taxonomy counts.**
   - Run `src/classify/induce.py` on distinct `reason_for_recall` texts, review/freeze taxonomy v1, then run `src/classify/label.py --apply` to populate `recall_label`.
   - Wire `GROUP BY recall_label` in [src/analytics.py](src/analytics.py) / [src/nl_query.py](src/nl_query.py) so “sterility by firm” returns exact counts for known categories.
   - **Done when:** `recall_label` is populated + a `count_by` over a taxonomy node works end-to-end in `/ask`.
2. **Phase 3 — populate firm aliases + tool-calling agent.**
   - Run `src/firm/resolve.py` / `src/firm/brand.py` with reviewed thresholds/provenance, then add the OpenAI function-calling agent over analytics/retrieval/firm tools.
   - **Done when:** “is <brand> safe?” resolves brand→firm set and returns an evidence-backed profile with provenance tiers.
3. **Conversational context v2.** Server-side conversation history keyed to `query_log`; `/ask` accepts a conversation id + prior turns so follow-ups carry context. **Done when:** a follow-up question resolves pronouns/ellipsis using prior turns.
4. **Observability L2 — Langfuse.** Only after L1 `query_log` shows a concrete gap. Wrap the `/ask` LLM calls as Langfuse traces/spans; keep `query_log` as the SQL-queryable source of truth. **Done when:** a traced `/ask` is inspectable in Langfuse without losing the `query_log` row.
5. **Public deploy (optional).** Build/push the image to HF Spaces or Render + a managed Postgres with pgvector (Supabase / Neon); load `sql/` + embeddings; set `DATABASE_URL` / `OPENAI_API_KEY` as secrets. **Done when:** a public URL serves `/ask` against the managed DB.

## Backlog (unscheduled ideas)
- **Company Exposure Index** — per-product-category ranking of firms/brands by openFDA "exposure" (à la Karpathy's AI-exposure), with a category dropdown → ranked chart. **Coupled, not standalone:** reuses generic ingest + Path 1 `count_by`, but **depends on Phase 3 entity resolution** for correct per-firm numbers; decoupled only from the LLM/agent/chat layer. Key nuance: exposure ≠ raw count → needs normalization (per product/NDC count) + severity weight (Class I/II/III) + recency. Full design in [PLAN.md](PLAN.md) 附录 A1.

## Blockers & gotchas
- ⚠️ **The venv is not relocatable** — it broke once after the project folder was renamed; recreated. Always run `.venv/bin/python …`, or re-`source .venv/bin/activate` after any move.
- ⚠️ **`hypopg` is built into the Postgres.app bundle** — rebuild it after a Postgres.app major-version upgrade.
- ⚠️ **Postgres MCP quirks** — `get_object_details` does not render column comments, and restricted-mode `execute_sql` rejects catalog queries (`col_description`, `::regclass`). Read comments via `psql \d+` or a direct connection; normal data `SELECT`s through the MCP are fine.
- ℹ️ **`state` stores 2-letter codes** (`CA`, not `California`). The NL layer enumerates a column's allowed values into the prompt ("value index") so the LLM uses real codes — keep that pattern when adding categorical filters.
- 🐳 **Docker build on this machine (CN network):** Docker Hub + PyPI time out — build with mirror build-args (`REGISTRY=docker.m.daocloud.io`, `PIP_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple`). Don't hand-edit `~/.docker/daemon.json` (Docker Desktop overwrites it); the base-image registry is parameterized in the [Dockerfile](Dockerfile) instead.
- 🐳 **Container → host Postgres:** use `DATABASE_URL=postgresql://<role>@host.docker.internal:5432/fda`. `host.docker.internal` reaches Postgres.app even though it only listens on `localhost`, but you MUST set the role — the container runs as `root` (no such Postgres role); the host role is `waywei`.
- 🔑 **OpenAI quota:** full `scripts/run_eval.py` currently hits OpenAI `insufficient_quota`; targeted compile/index/frontend checks pass, but live LLM eval needs quota restored.

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
- **Semantic counting is an estimate** — retrieve-above-threshold → per-item validate → count + confidence/evidence. Exact counts should come from taxonomy labels once Phase 4 is populated.
- **Observability before scale** — `query_log` (Postgres, L1) first, then Langfuse (L2); every `/ask` is a trace and the `QuerySpec` is the materialized, inspectable "reasoning".
- **"Is this company safe?" = Phase 3 capstone, after Path 2** — brand→parent is `[inferred]` (LLM / Wikidata / NDC labeler, marked + confirmable); company→`recalling_firm` is entity resolution (`pg_trgm` fuzzy + known-subsidiary expansion + LLM verify), since firms are fragmented (1,634 distinct; Pfizer/Teva/McNeil appear under many names/subsidiaries).
- **Vision = FDA-grounded vertical agent** (not just NL→DB): FDA = ground-truth fact layer; web/Wikidata = augmentation layer, strictly isolated (web numbers never enter counts; negative = "not found in FDA", not "safe"). Two scenarios: personal pull (Q&A) + enterprise push (monitor watched firms/device types; reuses `--since auto`).
- **Automated classification (Phase 4) = induce + closed-set label + discovery loop; human governs, never hand-labels** (TnT-LLM). Closed-set classifier assigns known taxonomy; open-set residual clustering surfaces new categories for approval. Initial taxonomy is data-induced (cluster + prefix mining), not hand-written. Label distinct text only; offline + hash-cached; optional distilled classifier.
- **Entity resolution: recall via multi-signal union (not normalization alone), precision via verify** — identity ≠ FDA footprint (`fda_present`; external/zero-recall firms exist; truly-unknown → `resolution_log`, never fabricate). NDC = 18% verifier, names = 100% path. Tables are a versioned side-car; source table stays immutable.
- **Cosmetics ≈ out of scope** — openFDA has cosmetic *adverse events* (`/food/event`), not a clean recall endpoint; the architecture generalizes to `device`/`food/enforcement` (same firm structure).
