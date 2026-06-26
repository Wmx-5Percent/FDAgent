"""Embed drug_enforcement text fields into recall_embeddings (Path 2, offline half).

Reads reason_for_recall + product_description from drug_enforcement, embeds each non-empty
field with OpenAI text-embedding-3-small (1536-d), and upserts one row per (recall, field)
into recall_embeddings. Idempotent + incremental: a row is (re)embedded only when its text is
new or changed (its md5 differs from the stored content_hash), mirroring fetch_openfda
``--since auto``. content_tsv (for FTS) is a generated column, so it is maintained by Postgres.

Run (needs OPENAI_API_KEY in .env + the recall_embeddings table from sql/003):
    .venv/bin/python src/embed.py                # embed everything new/changed
    .venv/bin/python src/embed.py --limit 100    # small test slice (per field)
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
SOURCE_TABLE = "drug_enforcement"
FIELDS = ("reason_for_recall", "product_description")
BATCH = 256  # texts per OpenAI request + per DB checkpoint commit (well under API limits)

UPSERT = """
    INSERT INTO recall_embeddings (recall_number, field, content, content_hash, embedding)
    VALUES (%s, %s, %s, md5(%s), %s::vector)
    ON CONFLICT (recall_number, field) DO UPDATE
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


def _pending(conn: psycopg.Connection, field: str, limit: Optional[int]) -> list[tuple[str, str]]:
    """Rows whose <field> text is new or changed vs. what's already embedded."""
    col = sql.Identifier(field)
    q = sql.SQL(
        "SELECT d.recall_number, d.{col} AS content "
        "FROM {tbl} d "
        "LEFT JOIN recall_embeddings e "
        "  ON e.recall_number = d.recall_number AND e.field = %s "
        "WHERE d.recall_number IS NOT NULL "
        "  AND d.{col} IS NOT NULL AND length(btrim(d.{col})) > 0 "
        "  AND (e.content_hash IS NULL OR e.content_hash <> md5(d.{col})) "
        "ORDER BY d.recall_number"
    ).format(col=col, tbl=sql.Identifier(SOURCE_TABLE))
    params: list[Any] = [field]
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
        for field in FIELDS:
            rows = _pending(conn, field, limit)
            print(f"{field}: {len(rows)} row(s) need embedding")
            if dry_run or not rows:
                total += len(rows)
                continue
            done = 0
            for chunk in _chunks(rows, BATCH):
                vecs = _embed(client, [content for _, content in chunk])
                with conn.cursor() as cur:
                    cur.executemany(UPSERT, [
                        (rn, field, content, content, _vec_literal(v))
                        for (rn, content), v in zip(chunk, vecs)
                    ])
                conn.commit()  # checkpoint after each batch -> resumable
                done += len(chunk)
                print(f"  embedded {done}/{len(rows)}")
            total += done
    verb = "pending" if dry_run else "written"
    print(f"\nDone. {'(dry-run) ' if dry_run else ''}{total} embedding(s) {verb} (model={MODEL}).")
    return total


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Embed recall text into recall_embeddings (incremental).")
    p.add_argument("--limit", type=int, default=None, help="max rows per field (for testing)")
    p.add_argument("--dry-run", action="store_true", help="count pending only; no API calls or writes")
    return p.parse_args()


if __name__ == "__main__":
    args = parse_args()
    print(f"model    : {MODEL}")
    print(f"database : {DEFAULT_DSN}")
    print(f"fields   : {', '.join(FIELDS)}\n")
    run(DEFAULT_DSN, limit=args.limit, dry_run=args.dry_run)
