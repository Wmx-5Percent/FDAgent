"""Semantic (vector) retrieval over recall_embeddings — Path 2 / slice 2.2 (v1: vector core).

Embeds a natural-language query with text-embedding-3-small and returns the nearest recalls by
cosine distance (pgvector HNSW), joined back to drug_enforcement for metadata + evidence. This
is the vector core that fixes the literal-`ilike` recall gap; the hybrid half (Postgres FTS +
RRF) and the QuerySpec router are the next increments (2.2 / 2.3).

Run:
    .venv/bin/python src/retrieval.py "sterility problems"
    .venv/bin/python src/retrieval.py "blood pressure medicine with a cancer-causing impurity" -k 5
    .venv/bin/python src/retrieval.py "children's fever syrup" --field product_description
"""
from __future__ import annotations

import argparse
import os
from dataclasses import dataclass
from typing import Optional, Sequence

import psycopg
from dotenv import load_dotenv
from openai import OpenAI
from psycopg import sql

# reuse the whitelisted filter -> SQL builder so semantic search can honor hard filters
from analytics import Filter, _conditions

load_dotenv()
DEFAULT_DSN = os.environ.get("DATABASE_URL", "postgresql://localhost:5432/fda")
EMBED_MODEL = os.environ.get("OPENAI_EMBED_MODEL", "text-embedding-3-small")
FIELDS = ("reason_for_recall", "product_description", "both")


@dataclass
class Hit:
    recall_number: str
    field: str
    distance: float
    content: str
    recalling_firm: Optional[str]
    classification: Optional[str]

    @property
    def similarity(self) -> float:
        return 1.0 - self.distance  # pgvector <=> is cosine distance


def _vec_literal(vec: Sequence[float]) -> str:
    return "[" + ",".join(repr(float(x)) for x in vec) + "]"


def embed_query(client: OpenAI, text: str) -> list[float]:
    return client.embeddings.create(model=EMBED_MODEL, input=[text]).data[0].embedding


def search(conn: psycopg.Connection, client: OpenAI, query: str, *,
           k: int = 10, field: str = "reason_for_recall",
           filters: Sequence[Filter] = ()) -> list[Hit]:
    """Nearest recalls to ``query`` by cosine distance, optionally pre-filtered by hard
    constraints (Tier-A columns on drug_enforcement). field='both' dedupes to one row/recall."""
    qvec = _vec_literal(embed_query(client, query))
    fconds, fparams = _conditions(filters)  # conditions on the joined drug_enforcement (d)
    if field == "both":
        # search every field, keep the best-matching row per recall (exact scan; fine at ~35k).
        where = (sql.SQL(" WHERE ") + sql.SQL(" AND ").join(fconds)) if fconds else sql.SQL("")
        q = sql.SQL(
            "SELECT recall_number, field, dist, content, recalling_firm, classification FROM ("
            "  SELECT DISTINCT ON (e.recall_number)"
            "         e.recall_number, e.field, (e.embedding <=> %s::vector) AS dist,"
            "         e.content, d.recalling_firm, d.classification"
            "  FROM recall_embeddings e"
            "  JOIN drug_enforcement d ON d.recall_number = e.recall_number"
            "  {where}"
            "  ORDER BY e.recall_number, dist"
            ") s ORDER BY s.dist LIMIT %s"
        ).format(where=where)
        params: list = [qvec, *fparams, k]
    else:
        # single field -> uses the HNSW index (ORDER BY <=> ... LIMIT).
        where = sql.SQL(" WHERE ") + sql.SQL(" AND ").join([sql.SQL("e.field = %s"), *fconds])
        q = sql.SQL(
            "SELECT e.recall_number, e.field, (e.embedding <=> %s::vector) AS dist,"
            "       e.content, d.recalling_firm, d.classification"
            "  FROM recall_embeddings e"
            "  JOIN drug_enforcement d ON d.recall_number = e.recall_number"
            "  {where}"
            "  ORDER BY e.embedding <=> %s::vector"
            "  LIMIT %s"
        ).format(where=where)
        params = [qvec, field, *fparams, qvec, k]
    with conn.cursor() as cur:
        cur.execute(q, params)
        return [Hit(*r) for r in cur.fetchall()]


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Semantic vector search over drug-recall text.")
    p.add_argument("query", help="natural-language query")
    p.add_argument("-k", type=int, default=10, help="number of results (default 10)")
    p.add_argument("--field", choices=FIELDS, default="reason_for_recall",
                   help="which embedded field to search (default reason_for_recall)")
    return p.parse_args()


def main() -> None:
    args = parse_args()
    client = OpenAI()
    with psycopg.connect(DEFAULT_DSN) as conn:
        hits = search(conn, client, args.query, k=args.k, field=args.field)
        print(f"Q: {args.query!r}  (field={args.field}, k={args.k})\n")
        for i, h in enumerate(hits, 1):
            print(f"{i:>2}. [{h.recall_number}] sim={h.similarity:.3f}  "
                  f"{h.classification or '-'}  {h.recalling_firm or '-'}")
            print(f"    {(h.content or '')[:150]}")


if __name__ == "__main__":
    main()
