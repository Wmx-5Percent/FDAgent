# FDAgent — openFDA Drug-Recall Intelligence Agent

Ask natural-language questions about U.S. **FDA drug-recall enforcement reports** and get
**evidence-backed** answers — frequencies, trends, and distributions computed in SQL (never
guessed by the model), each result carrying the recall numbers that back it.

> **Portfolio note.** This reproduces — on 100% **public-domain openFDA data** — the same
> applied-AI pipeline I built in industry (large-scale LLM structuring + analytics over noisy
> operational records). No proprietary data or code is used.

> **Live state → [PROGRESS.md](PROGRESS.md)** (single source of truth). Roadmap → [PLAN.md](PLAN.md);
> query design → [频率查询系统设计-过滤检索校验.md](频率查询系统设计-过滤检索校验.md);
> agent entry point + conventions → [AGENTS.md](AGENTS.md).

---

## What works today

- **Ingest** — [src/fetch_openfda.py](src/fetch_openfda.py): any openFDA endpoint → PostgreSQL (idempotent JSONB upsert, `--since auto` incremental). Loaded: `drug/enforcement`, ~17.7k recall reports.
- **Parse** — [sql/001_parse_drug_enforcement.sql](sql/001_parse_drug_enforcement.sql): the `raw` JSONB → 23 typed, indexed columns, documented with verbatim openFDA definitions ([sql/002](sql/002_drug_enforcement_comments.sql)).
- **Analytics engine** — [src/analytics.py](src/analytics.py): safe, parameterized, read-only `count / group-by / trend / sample`, returning evidence `recall_number`s.
- **NL→SQL layer** — [src/nl_query.py](src/nl_query.py): an LLM turns a question into a *validated* `QuerySpec` (columns/values whitelisted, schema + column comments injected) that runs through the analytics engine — so every number comes from SQL.

```text
openFDA API ─▶ src/fetch_openfda.py ─▶ Postgres (raw JSONB + parsed columns)
                                            │
        question ─▶ src/nl_query.py (LLM → QuerySpec) ─▶ src/analytics.py (SQL) ─▶ answer + evidence
```

**Next:** a FastAPI `/ask` endpoint + small chart UI, then hybrid semantic retrieval (Path 2) and an eval harness — see [PROGRESS.md](PROGRESS.md).

---

## Data: openFDA drug recall enforcement (public domain)

Source: [openFDA Drug Enforcement API](https://open.fda.gov/apis/drug/enforcement/) — U.S.-government
drug-recall reports, no PII. Each row keeps the full original record as `raw` JSONB plus parsed
columns (`classification`, `status`, `reason_for_recall`, `recalling_firm`, `state`, dates, …).

---

## Quick start

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env            # add OPENAI_API_KEY and DATABASE_URL

# Postgres (Postgres.app): create the db + enable pgvector
createdb fda && psql -d fda -c "CREATE EXTENSION IF NOT EXISTS vector;"

# load data, then try the two engines
.venv/bin/python src/fetch_openfda.py --endpoint drug/enforcement --table drug_enforcement
.venv/bin/python src/analytics.py                                            # deterministic demo (no API key)
.venv/bin/python src/nl_query.py "Which firms had the most Class I recalls?"  # NL→SQL demo
```

---

## Tech stack

Python 3.13 · PostgreSQL + `pgvector` + `hypopg` · `psycopg` 3 · OpenAI (Pydantic structured
output) · FastAPI (next) · a read-only Postgres MCP for safe schema exploration.

---

## IP safety

Real company data is **git-ignored** and never committed; everything here is public-domain
openFDA (or synthetic). Secrets live in `.env` (git-ignored; template in [.env.example](.env.example)).
