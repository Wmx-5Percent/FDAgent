"""Embed FDA source text fields into the shared `embeddings` table (Path 2, offline half).

For each source in SOURCES (currently just drug_enforcement), embeds its declared text fields
with OpenAI text-embedding-3-small (1536-d) and upserts one row per (source, source_id, field)
into `embeddings`. Idempotent + incremental: a row is (re)embedded only when its text is new or
changed (its md5 differs from the stored content_hash), mirroring fetch_openfda ``--since auto``.
content_tsv (for FTS) is a generated column, maintained by Postgres. Adding a new FDA dataset =
add one SOURCES entry (no pipeline changes).

Run (needs OPENAI_API_KEY in .env + the `embeddings` table from sql/003 + sql/004):
    .venv/bin/python src/embed.py                # embed everything new/changed
    .venv/bin/python src/embed.py --limit 100    # small test slice (per source/field)
    .venv/bin/python src/embed.py --dry-run      # count what's pending; no API calls / writes
"""
from __future__ import annotations

import argparse
import os
from typing import Any, Optional, Sequence

import psycopg
from dotenv import load_dotenv
from openai import OpenAI
from psycopg import sql
from tenacity import retry, stop_after_attempt, wait_exponential

load_dotenv()
DEFAULT_DSN = os.environ.get("DATABASE_URL", "postgresql://localhost:5432/fda")
MODEL = os.environ.get("OPENAI_EMBED_MODEL", "text-embedding-3-small")

# Registry of data sources to embed: source name -> its table, id column, and text fields.
# Adding a new FDA dataset = add one entry here (the pipeline stays unchanged).
SOURCES: dict[str, dict[str, Any]] = {
    "drug_enforcement": {
        "table": "drug_enforcement",
        "id": "recall_number",
        "text_fields": ("reason_for_recall", "product_description"),
    },
}
BATCH = 256  # texts per OpenAI request + per DB checkpoint commit (well under API limits)

UPSERT = """
    INSERT INTO embeddings (source, source_id, field, content, content_hash, embedding)
    VALUES (%s, %s, %s, %s, md5(%s), %s::vector)
    ON CONFLICT (source, source_id, field) DO UPDATE
        SET content = EXCLUDED.content,
            content_hash = EXCLUDED.content_hash,
            embedding = EXCLUDED.embedding
"""


def _vec_literal(vec: Sequence[float]) -> str:
    """pgvector text input form: '[0.1,0.2,...]'."""
    return "[" + ",".join(repr(float(x)) for x in vec) + "]"


@retry(stop=stop_after_attempt(5), wait=wait_exponential(multiplier=1, min=2, max=30))
def _embed(client: OpenAI, texts: list[str]) -> list[list[float]]:
    resp = client.embeddings.create(model=MODEL, input=texts)
    return [d.embedding for d in resp.data]


def _pending(conn: psycopg.Connection, source: str, cfg: dict, field: str,
             limit: Optional[int]) -> list[tuple[str, str]]:
    """Rows of <source> whose <field> text is new or changed vs. what's already embedded."""
    tbl, idcol, col = sql.Identifier(cfg["table"]), sql.Identifier(cfg["id"]), sql.Identifier(field)
    q = sql.SQL(
        "SELECT d.{idcol} AS source_id, d.{col} AS content "
        "FROM {tbl} d "
        "LEFT JOIN embeddings e "
        "  ON e.source = %s AND e.source_id = d.{idcol} AND e.field = %s "
        "WHERE d.{idcol} IS NOT NULL "
        "  AND d.{col} IS NOT NULL AND length(btrim(d.{col})) > 0 "
        "  AND (e.content_hash IS NULL OR e.content_hash <> md5(d.{col})) "
        "ORDER BY d.{idcol}"
    ).format(idcol=idcol, col=col, tbl=tbl)
    params: list[Any] = [source, field]
    if limit:
        q = q + sql.SQL(" LIMIT %s")
        params.append(limit)
    with conn.cursor() as cur:
        cur.execute(q, params)
        return cur.fetchall()


def _chunks(seq: list, n: int):
    for i in range(0, len(seq), n):
        yield seq[i:i + n]


def run(dsn: str = DEFAULT_DSN, *, limit: Optional[int] = None, dry_run: bool = False) -> int:
    client = None if dry_run else OpenAI()
    total = 0
    with psycopg.connect(dsn) as conn:
        for source, cfg in SOURCES.items():
            for field in cfg["text_fields"]:
                rows = _pending(conn, source, cfg, field, limit)
                print(f"{source}.{field}: {len(rows)} row(s) need embedding")
                if dry_run or not rows:
                    total += len(rows)
                    continue
                assert client is not None  # past the dry-run guard
                done = 0
                for chunk in _chunks(rows, BATCH):
                    vecs = _embed(client, [content for _, content in chunk])
                    with conn.cursor() as cur:
                        cur.executemany(UPSERT, [
                            (source, sid, field, content, content, _vec_literal(v))
                            for (sid, content), v in zip(chunk, vecs)
                        ])
                    conn.commit()  # checkpoint after each batch -> resumable
                    done += len(chunk)
                    print(f"  embedded {done}/{len(rows)}")
                total += done
    verb = "pending" if dry_run else "written"
    print(f"\nDone. {'(dry-run) ' if dry_run else ''}{total} embedding(s) {verb} (model={MODEL}).")
    return total


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Embed FDA source text into the embeddings table (incremental).")
    p.add_argument("--limit", type=int, default=None, help="max rows per source/field (for testing)")
    p.add_argument("--dry-run", action="store_true", help="count pending only; no API calls or writes")
    return p.parse_args()


if __name__ == "__main__":
    args = parse_args()
    print(f"model    : {MODEL}")
    print(f"database : {DEFAULT_DSN}")
    print(f"sources  : {', '.join(SOURCES)}\n")
    run(DEFAULT_DSN, limit=args.limit, dry_run=args.dry_run)
