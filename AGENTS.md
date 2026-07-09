# AGENTS.md — Agent Operating Guide

> **Read this first.** It is the always-on entry point of a *progressive-disclosure*
> harness, designed so an agent gets oriented in seconds without loading the whole repo:
>
> **Layer 0** — this file (tiny, always loaded): conventions + commands.
> **Layer 1** — [`PROJECT_INDEX.md`](PROJECT_INDEX.md) (generated map): one-line purpose +
> key symbols for every file. **Use it to locate code instead of grepping the tree.**
> **Layer 2** — the files themselves: open only the ones the index points you to.

## What this project is

`fdaAgent` — a portfolio RAG / tool-calling agent built on **public-domain openFDA**
data. It reproduces an industry-style LLM data-structuring + retrieval pipeline on legal,
public data (no proprietary content). Current dataset: `drug/enforcement` (~17.7k drug-recall
reports) ingested into PostgreSQL. See [README.md](README.md) and [PLAN.md](PLAN.md)
for the roadmap, and [频率查询系统设计-过滤检索校验.md](频率查询系统设计-过滤检索校验.md)
for the frequency/aggregation design.

## Navigation protocol (do this in order)

1. Read this file (always loaded).
2. Read [`PROGRESS.md`](PROGRESS.md) for current state, the next action, and known blockers.
3. Open [`PROJECT_INDEX.md`](PROJECT_INDEX.md) and find the file(s) you need by purpose/symbol.
4. Read those specific files for full detail. Avoid broad tree-wide searches when the index already answers "where".

> **Update ritual:** when you finish a chunk of work, update [`PROGRESS.md`](PROGRESS.md)
> (Now / Next up / Blockers) so the next session starts oriented. Keep it short.
>
> **Parallel-development PROGRESS rule:** `PROGRESS.md` is a **shared multi-workstream
> queue**, not a per-agent scratchpad. A feature/subagent must **not** collapse
> "Next action" / "Next up" to only its own task. Preserve all pending parallel items,
> dependency gates, and merge order. If you only own one feature, update that item's status
> or add a dated note; use your PR body/comments for detailed per-branch progress. The
> coordinator/main agent owns holistic priority/order changes after integration.

## Keep the index reliable (IMPORTANT)

The index is **auto-generated from each file's docstring/heading** — never hand-edit it.

- After **adding / moving / deleting** a file, or changing a file's purpose, run:
  `python scripts/gen_index.py`
- To verify it is in sync (CI / pre-commit): `python scripts/gen_index.py --check` (exits non-zero if stale).
- So the map stays meaningful, **every source file must start with a one-line docstring/heading** stating its job. The generator surfaces any file missing one.

## Keep docs from drifting (why the README went stale once)

Each doc stays current by a *different* mechanism — know which, and **never duplicate project state**:

- **Auto-generated, cannot drift:** [`PROJECT_INDEX.md`](PROJECT_INDEX.md) — rebuilt by `gen_index.py`, enforced by the pre-commit `--check`.
- **Ritual-updated, the single source of truth for state:** [`PROGRESS.md`](PROGRESS.md) — what's done / next / blocked. Update it at the end of each session.
- **Vision + setup, link-only:** [`README.md`](README.md) and [`PLAN.md`](PLAN.md) describe the stable goal and how to run things, and **link to `PROGRESS.md` for live state** — they must not restate "what works / current phase".

When updating `PROGRESS.md` during parallel development, keep "Next up" as the complete
coordinator-approved backlog for all active/planned workstreams. Do not replace it with a
single subagent's local next step; per-agent next steps belong in that PR's body/comments.

**Root cause of the earlier stale README:** it *duplicated* project state (dataset, architecture, "Phase 0 done") into a doc with **no freshness mechanism** — unlike the auto-generated index or the ritual-bound PROGRESS, nothing triggered or checked it, so the drug/enforcement pivot left it behind.

**Prevention — in the same commit as the change:** when you change the **dataset, primary components, or project direction**, update `PROGRESS.md` (always), then fix any sentence in `README.md` / `PLAN.md` that no longer matches (prefer replacing restated state with a link to `PROGRESS.md`).

## Repo facts (verified)

- **Python**: use the venv — `.venv/bin/python ...` (Python 3.13). Install deps: `.venv/bin/python -m pip install -r requirements.txt`.
- **Database**: PostgreSQL (Postgres.app) database `fda`. Extensions enabled: `pgvector` 0.8.1 (vector search) + `hypopg` 1.4.3 (hypothetical-index tuning). DSN `postgresql://localhost:5432/fda` (override via `DATABASE_URL`).
- **Ingest openFDA → Postgres**: `.venv/bin/python src/fetch_openfda.py --endpoint <noun/endpoint> --table <table> [--since auto]`. Generic over any endpoint; idempotent JSONB upsert.
- **Embed text → vectors (Path 2)**: `.venv/bin/python src/embed.py [--dry-run]` — embeds source text fields into the multi-source `embeddings` table (pgvector + FTS); incremental by content hash. Adding a dataset = one `SOURCES` entry in `src/embed.py`.
- **Firm resolution sidecar (Phase 3a, merged)**: `sql/008_firm_resolution.sql` + `src/firm/` create offline company/brand resolution foundations. `src/firm/resolve.py` uses pg_trgm/token/phonetic signals only to recall candidate pairs; automatic identity merges should be verified with OpenRouter web search (`--verification-policy web`, default `OPENROUTER_WEB_MODEL=deepseek/deepseek-v4-pro`). `src/firm/brand.py` returns brand→firm/parent candidates with provenance tiers. These are **not wired into `/ask` yet**.
- **Serve** (FastAPI `/ask` + static UI): `.venv/bin/python -m uvicorn src.api:app` → http://127.0.0.1:8000/. After editing code, fully restart — a lingering uvicorn serves stale code.
- **Inspect data**: `psql -d fda -c "\dt"`. Tables store the full record in a `raw jsonb` column — query logical fields via `raw->>'field'` (the top-level columns are `id, source, report_date, raw, fetched_at`).
- **Read-only DB MCP** (`postgres-fda`, Postgres MCP Pro): a restricted/read-only MCP server for safe schema exploration, `execute_sql` (SELECT), `explain_query` (+ hypothetical indexes), and `analyze_db_health`. Config: `.vscode/mcp.json` (git-ignored; binary at `.venv/bin/postgres-mcp`). Prefer it for reads; do schema changes via versioned scripts, not ad-hoc writes.

## Current firm-resolution handoff for subagents

- **Completed item:** parallel-plan **3a 实体解析离线** is merged as PR #8 (`feat: add firm resolution foundation`). It owns `sql/008_firm_resolution.sql` and `src/firm/{resolve,brand}.py`.
- **Primary firm-track goal now:** productionize **company-name normalization**, not the final Agent yet. Do **3a+ incremental firm normalization** before starting 3b Agent wiring.
- **Why:** production data is not static. New recalls arrive through `fetch_openfda.py --since auto`, so `recalling_firm` aliases must be discovered/updated incrementally, not by a one-time full batch.
- **Next PR shape:** add a run/audit layer (`sql/009_firm_resolution_runs.sql` with `firm_resolution_run` + `firm_match_pair`), then upgrade `src/firm/resolve.py` with `--mode full|incremental`, source table/field options, idempotent alias refresh, candidate-pair audit, golden-set threshold calibration, and OpenRouter web verification for accepted merges.
- **Do not do yet:** do not wire `src/agent.py`, `/ask`, `analytics.py`, or `nl_query.py` to firm resolution until the incremental/audited sidecar is populated and repeatable.
- **Production sequence target:** prefer one command after this PR: `.venv/bin/python src/fetch_openfda.py --endpoint drug/enforcement --table drug_enforcement --since auto --resolve-firms --resolve-firms-verification-policy web --resolve-firms-web-model deepseek/deepseek-v4-pro`. This applies the firm DDL and then runs incremental firm resolution against the ingested table/field. Requires `OPENROUTER_API_KEY`; unknown or ambiguous identities stay in `resolution_log`; never fabricate a firm.

## Conventions

- **Bilingual**: design docs are Chinese (`*.md`); code and code-comments are English.
- **One-time vs scheduled**: DDL/table creation and first full back-fill are one-off; `fetch_openfda.py --since auto` is the scheduled incremental job.
- **Sidecar freshness**: source FDA tables are immutable-ish facts; derived sidecars (`embeddings`, taxonomy labels, firm aliases) must be rerunnable after new ingest. For firm resolution, prefer incremental/idempotent updates with run ids and review logs over manual one-off merges.
- **IP safety**: real company data is git-ignored and **never** committed. Only public-domain
  openFDA or synthetic data is allowed in git.
- **Generated / rebuildable** (git-ignored): `.venv/`, `data/raw/`, `data/processed/`, vector-store files.
- **Worktree handoff gotcha:** `.gitignore`'s `.venv/` pattern ignores a real directory, but
  **does not ignore a `.venv` symlink**. If `git status` shows `?? .venv` and `ls -ld .venv`
  confirms it is a symlink (e.g. to another checkout's venv), remove only the link with
  `rm .venv` before handoff; do not remove the target venv.
- **Secrets** live in `.env` (git-ignored); the template is [.env.example](.env.example).
