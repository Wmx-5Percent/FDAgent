"""Hybrid retrieval over the `embeddings` table — Path 2 / slice 2.2.

Embeds a natural-language query with text-embedding-3-small, retrieves both semantic vector
neighbors (pgvector cosine distance) and keyword matches (Postgres FTS over content_tsv), then
fuses the two ranked lists with reciprocal rank fusion (RRF). Results are joined back to
drug_enforcement for metadata + evidence.

Run:
    .venv/bin/python src/retrieval.py "sterility problems"
    .venv/bin/python src/retrieval.py "blood pressure medicine with a cancer-causing impurity" -k 5
    .venv/bin/python src/retrieval.py "children's fever syrup" --field product_description
"""
from __future__ import annotations

import argparse
import os
from dataclasses import dataclass
from typing import Any, Optional, Sequence

import psycopg
from dotenv import load_dotenv
from openai import OpenAI
from psycopg import sql

# reuse the whitelisted filter -> SQL builder so hybrid search can honor hard filters
from analytics import Filter, _conditions

load_dotenv()
DEFAULT_DSN = os.environ.get("DATABASE_URL", "postgresql://localhost:5432/fda")
EMBED_MODEL = os.environ.get("OPENAI_EMBED_MODEL", "text-embedding-3-small")
FIELDS = ("reason_for_recall", "product_description", "both")
RRF_K = 60
MIN_CANDIDATES = 50
CANDIDATE_MULTIPLIER = 5


@dataclass
class Hit:
    recall_number: str
    field: str
    distance: float
    content: str
    recalling_firm: Optional[str]
    classification: Optional[str]
    rrf_score: float = 0.0

    @property
    def similarity(self) -> float:
        return 1.0 - self.distance  # pgvector <=> is cosine distance

    @property
    def retrieval_score(self) -> float:
        return self.similarity


@dataclass(frozen=True)
class _Candidate:
    recall_number: str
    field: str
    distance: float
    content: str
    recalling_firm: Optional[str]
    classification: Optional[str]


def _vec_literal(vec: Sequence[float]) -> str:
    return "[" + ",".join(repr(float(x)) for x in vec) + "]"


def embed_query(client: OpenAI, text: str) -> list[float]:
    return client.embeddings.create(model=EMBED_MODEL, input=[text]).data[0].embedding


def _candidate_limit(k: int) -> int:
    return max(k * CANDIDATE_MULTIPLIER, MIN_CANDIDATES)


def _where(conds: Sequence[sql.Composable]) -> sql.Composable:
    return sql.SQL(" WHERE ") + sql.SQL(" AND ").join(conds)


def _rows_to_candidates(rows: Sequence[tuple[Any, ...]]) -> list[_Candidate]:
    return [
        _Candidate(
            recall_number=r[0],
            field=r[1],
            distance=float(r[2] if r[2] is not None else 1.0),
            content=r[3],
            recalling_firm=r[4],
            classification=r[5],
        )
        for r in rows
    ]


def _vector_candidates(conn: psycopg.Connection, qvec: str, *, source: str, field: str,
                       filters: Sequence[Filter], limit: int) -> list[_Candidate]:
    fconds, fparams = _conditions(filters)  # conditions on the joined source table (d)
    if field == "both":
        # search every field, keep the best-matching row per record (exact scan; fine at ~35k).
        conds = [sql.SQL("e.source = %s"), sql.SQL("e.embedding IS NOT NULL"), *fconds]
        where = _where(conds)
        q = sql.SQL(
            "SELECT source_id, field, dist, content, recalling_firm, classification FROM ("
            "  SELECT DISTINCT ON (e.source_id)"
            "         e.source_id, e.field, (e.embedding <=> %s::vector) AS dist,"
            "         e.content, d.recalling_firm, d.classification"
            "  FROM embeddings e"
            "  JOIN drug_enforcement d ON d.recall_number = e.source_id"
            "  {where}"
            "  ORDER BY e.source_id, dist"
            ") s ORDER BY s.dist LIMIT %s"
        ).format(where=where)
        params: list = [qvec, source, *fparams, limit]
    else:
        # single field -> uses the HNSW index (ORDER BY <=> ... LIMIT).
        conds = [
            sql.SQL("e.source = %s"),
            sql.SQL("e.field = %s"),
            sql.SQL("e.embedding IS NOT NULL"),
            *fconds,
        ]
        where = _where(conds)
        q = sql.SQL(
            "SELECT e.source_id, e.field, (e.embedding <=> %s::vector) AS dist,"
            "       e.content, d.recalling_firm, d.classification"
            "  FROM embeddings e"
            "  JOIN drug_enforcement d ON d.recall_number = e.source_id"
            "  {where}"
            "  ORDER BY e.embedding <=> %s::vector"
            "  LIMIT %s"
        ).format(where=where)
        params = [qvec, source, field, *fparams, qvec, limit]
    with conn.cursor() as cur:
        cur.execute(q, params)
        return _rows_to_candidates(cur.fetchall())


def _fts_candidates(conn: psycopg.Connection, query: str, qvec: str, *, source: str, field: str,
                    filters: Sequence[Filter], limit: int) -> list[_Candidate]:
    fconds, fparams = _conditions(filters)  # conditions on the joined source table (d)
    if field == "both":
        conds = [sql.SQL("e.source = %s"), *fconds]
        where = _where(conds)
        q = sql.SQL(
            "WITH q AS (SELECT websearch_to_tsquery('english', %s) AS tsq) "
            "SELECT source_id, field, dist, content, recalling_firm, classification FROM ("
            "  SELECT DISTINCT ON (e.source_id)"
            "         e.source_id, e.field,"
            "         COALESCE(e.embedding <=> %s::vector, 1.0) AS dist,"
            "         e.content, d.recalling_firm, d.classification,"
            "         ts_rank_cd(e.content_tsv, q.tsq) AS fts_rank"
            "  FROM q"
            "  JOIN embeddings e ON e.content_tsv @@ q.tsq"
            "  JOIN drug_enforcement d ON d.recall_number = e.source_id"
            "  {where}"
            "  ORDER BY e.source_id, fts_rank DESC, dist"
            ") s ORDER BY s.fts_rank DESC, s.dist LIMIT %s"
        ).format(where=where)
        params: list = [query, qvec, source, *fparams, limit]
    else:
        conds = [sql.SQL("e.source = %s"), sql.SQL("e.field = %s"), *fconds]
        where = _where(conds)
        q = sql.SQL(
            "WITH q AS (SELECT websearch_to_tsquery('english', %s) AS tsq) "
            "SELECT e.source_id, e.field,"
            "       COALESCE(e.embedding <=> %s::vector, 1.0) AS dist,"
            "       e.content, d.recalling_firm, d.classification"
            "  FROM q"
            "  JOIN embeddings e ON e.content_tsv @@ q.tsq"
            "  JOIN drug_enforcement d ON d.recall_number = e.source_id"
            "  {where}"
            "  ORDER BY ts_rank_cd(e.content_tsv, q.tsq) DESC, dist"
            "  LIMIT %s"
        ).format(where=where)
        params = [query, qvec, source, field, *fparams, limit]
    with conn.cursor() as cur:
        cur.execute(q, params)
        return _rows_to_candidates(cur.fetchall())


def _fusion_key(candidate: _Candidate, field: str) -> tuple[str, ...]:
    if field == "both":
        return (candidate.recall_number,)
    return (candidate.recall_number, candidate.field)


def _rrf_fuse(vector_hits: Sequence[_Candidate], fts_hits: Sequence[_Candidate], *,
              k: int, field: str) -> list[Hit]:
    scores: dict[tuple[str, ...], float] = {}
    best: dict[tuple[str, ...], _Candidate] = {}
    best_component: dict[tuple[str, ...], float] = {}
    best_is_fts: dict[tuple[str, ...], bool] = {}

    def add(candidates: Sequence[_Candidate], *, is_fts: bool) -> None:
        for rank, candidate in enumerate(candidates, 1):
            key = _fusion_key(candidate, field)
            component = 1.0 / (RRF_K + rank)
            scores[key] = scores.get(key, 0.0) + component
            if (
                key not in best
                or component > best_component[key]
                or (component == best_component[key] and is_fts and not best_is_fts[key])
            ):
                best[key] = candidate
                best_component[key] = component
                best_is_fts[key] = is_fts

    add(vector_hits, is_fts=False)
    add(fts_hits, is_fts=True)
    ranked_keys = sorted(
        scores,
        key=lambda key: (-scores[key], best[key].distance, best[key].recall_number, best[key].field),
    )
    return [
        Hit(
            recall_number=best[key].recall_number,
            field=best[key].field,
            distance=best[key].distance,
            content=best[key].content,
            recalling_firm=best[key].recalling_firm,
            classification=best[key].classification,
            rrf_score=scores[key],
        )
        for key in ranked_keys[:k]
    ]


def search(conn: psycopg.Connection, client: OpenAI, query: str, *,
           k: int = 10, field: str = "reason_for_recall",
           filters: Sequence[Filter] = (), source: str = "drug_enforcement") -> list[Hit]:
    """Hybrid records for ``query``, optionally pre-filtered by hard constraints.

    The vector and FTS halves both honor the same filters on the joined source table.
    ``field='both'`` dedupes to one row per record. ``source`` selects the dataset; v1
    enriches from drug_enforcement (one source).
    """
    if k <= 0:
        return []
    qvec = _vec_literal(embed_query(client, query))
    limit = _candidate_limit(k)
    vector_hits = _vector_candidates(conn, qvec, source=source, field=field,
                                     filters=filters, limit=limit)
    fts_hits = _fts_candidates(conn, query, qvec, source=source, field=field,
                               filters=filters, limit=limit)
    return _rrf_fuse(vector_hits, fts_hits, k=k, field=field)


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Hybrid vector + FTS search over drug-recall text.")
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
