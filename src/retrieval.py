"""Hybrid retrieval over the `embeddings` table — Path 2 / slice 2.2.

Embeds a natural-language query with the configured provider-compatible embedding model,
retrieves both semantic vector neighbors (pgvector cosine distance) and keyword matches
(Postgres FTS over content_tsv), then fuses the two ranked lists with reciprocal rank fusion
(RRF). Results are joined back to drug_enforcement for metadata + evidence.

Run:
    .venv/bin/python src/retrieval.py "sterility problems"
    .venv/bin/python src/retrieval.py "blood pressure medicine with a cancer-causing impurity" -k 5
    .venv/bin/python src/retrieval.py "children's fever syrup" --field product_description
"""
from __future__ import annotations

import argparse
import os
from dataclasses import dataclass
from time import perf_counter
from typing import Any, Callable, Optional, Sequence

import psycopg
from dotenv import load_dotenv
from psycopg import sql

import llm
import agent_control
# reuse the whitelisted filter -> SQL builder so hybrid search can honor hard filters
from analytics import Filter, _conditions

load_dotenv()
DEFAULT_DSN = os.environ.get("DATABASE_URL", "postgresql://localhost:5432/fda")
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
    score: float = 0.0
    score_kind: str = "similarity"
    retrieval_mode: str = "hybrid"
    embedding_fallback_reason: Optional[str] = None
    fused_rank: Optional[int] = None
    vector_rank: Optional[int] = None
    vector_distance: Optional[float] = None
    vector_similarity: Optional[float] = None
    fts_rank: Optional[int] = None
    fts_score: Optional[float] = None

    @property
    def similarity(self) -> float:
        return 1.0 - self.distance  # pgvector <=> is cosine distance

    @property
    def retrieval_score(self) -> float:
        return self.score if self.score > 0 else self.similarity


class SearchResult(list[Hit]):
    """List of retrieval hits with degradation metadata even when the list is empty."""

    def __init__(self, hits: Sequence[Hit] = (), *, retrieval_mode: str = "hybrid",
                 embedding_fallback_reason: Optional[str] = None,
                 vector_hit_count: int = 0,
                 fts_hit_count: int = 0,
                 fused_hit_count: int | None = None,
                 timings_ms: dict[str, int] | None = None,
                 fts_queries: Sequence[str] = ()) -> None:
        super().__init__(hits)
        self.retrieval_mode = retrieval_mode
        self.embedding_fallback_reason = embedding_fallback_reason
        self.vector_hit_count = vector_hit_count
        self.fts_hit_count = fts_hit_count
        self.fused_hit_count = len(hits) if fused_hit_count is None else fused_hit_count
        self.timings_ms = dict(timings_ms or {})
        self.fts_queries = list(fts_queries)


@dataclass(frozen=True)
class _Candidate:
    recall_number: str
    field: str
    distance: float
    content: str
    recalling_firm: Optional[str]
    classification: Optional[str]
    rank_score: float = 0.0


def _vec_literal(vec: Sequence[float]) -> str:
    return "[" + ",".join(repr(float(x)) for x in vec) + "]"


def embed_query(client: Any, config: llm.EmbeddingConfig, text: str) -> list[float]:
    return llm.embed_text(client, config, text)


def _embedding_fallback_reason(exc: BaseException) -> str:
    return llm.provider_error_summary(exc)


def _candidate_limit(k: int) -> int:
    return max(k * CANDIDATE_MULTIPLIER, MIN_CANDIDATES)


def _where(conds: Sequence[sql.Composable]) -> sql.Composable:
    return sql.SQL(" WHERE ") + sql.SQL(" AND ").join(conds)


def _rows_to_candidates(rows: Sequence[tuple[Any, ...]]) -> list[_Candidate]:
    out: list[_Candidate] = []
    for r in rows:
        distance = float(r[2] if r[2] is not None else 1.0)
        default_score = max(0.0, 1.0 - distance)
        rank_score = float(r[6]) if len(r) > 6 and r[6] is not None else default_score
        out.append(_Candidate(
            recall_number=r[0],
            field=r[1],
            distance=distance,
            content=r[3],
            recalling_firm=r[4],
            classification=r[5],
            rank_score=rank_score,
        ))
    return out


def _record_debug_sql(
    recorder: Callable[..., None] | None,
    query: sql.Composable,
    params: Sequence[Any],
    *,
    source: str,
    title: str,
    query_id: str,
) -> None:
    if recorder is not None:
        recorder(query, params, source=source, title=title, query_id=query_id)


def _vector_candidates(conn: psycopg.Connection, qvec: str, *, source: str, field: str,
                       filters: Sequence[Filter], limit: int,
                       sql_debug_recorder: Callable[..., None] | None = None) -> list[_Candidate]:
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
        rows = cur.fetchall()
    _record_debug_sql(
        sql_debug_recorder,
        q,
        params,
        source="retrieval.vector_candidates",
        title=f"Vector retrieval candidates ({field})",
        query_id=f"retrieval_vector_{field}",
    )
    return _rows_to_candidates(rows)


def _fts_candidates(conn: psycopg.Connection, query: str, qvec: str | None, *, source: str,
                    field: str, filters: Sequence[Filter], limit: int,
                    sql_debug_recorder: Callable[..., None] | None = None) -> list[_Candidate]:
    fconds, fparams = _conditions(filters)  # conditions on the joined source table (d)
    if field == "both":
        conds = [sql.SQL("e.source = %s"), *fconds]
        where = _where(conds)
        if qvec is None:
            q = sql.SQL(
                "WITH q AS (SELECT websearch_to_tsquery('english', %s) AS tsq) "
                "SELECT source_id, field, dist, content, recalling_firm, classification, fts_rank "
                "FROM ("
                "  SELECT DISTINCT ON (e.source_id)"
                "         e.source_id, e.field, 1.0 AS dist,"
                "         e.content, d.recalling_firm, d.classification,"
                "         ts_rank_cd(e.content_tsv, q.tsq) AS fts_rank"
                "  FROM q"
                "  JOIN embeddings e ON e.content_tsv @@ q.tsq"
                "  JOIN drug_enforcement d ON d.recall_number = e.source_id"
                "  {where}"
                "  ORDER BY e.source_id, fts_rank DESC"
                ") s ORDER BY s.fts_rank DESC LIMIT %s"
            ).format(where=where)
            params: list = [query, source, *fparams, limit]
        else:
            q = sql.SQL(
                "WITH q AS (SELECT websearch_to_tsquery('english', %s) AS tsq) "
                "SELECT source_id, field, dist, content, recalling_firm, classification, fts_rank "
                "FROM ("
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
            params = [query, qvec, source, *fparams, limit]
    else:
        conds = [sql.SQL("e.source = %s"), sql.SQL("e.field = %s"), *fconds]
        where = _where(conds)
        if qvec is None:
            q = sql.SQL(
                "WITH q AS (SELECT websearch_to_tsquery('english', %s) AS tsq) "
                "SELECT e.source_id, e.field, 1.0 AS dist,"
                "       e.content, d.recalling_firm, d.classification,"
                "       ts_rank_cd(e.content_tsv, q.tsq) AS fts_rank"
                "  FROM q"
                "  JOIN embeddings e ON e.content_tsv @@ q.tsq"
                "  JOIN drug_enforcement d ON d.recall_number = e.source_id"
                "  {where}"
                "  ORDER BY fts_rank DESC"
                "  LIMIT %s"
            ).format(where=where)
            params = [query, source, field, *fparams, limit]
        else:
            q = sql.SQL(
                "WITH q AS (SELECT websearch_to_tsquery('english', %s) AS tsq) "
                "SELECT e.source_id, e.field,"
                "       COALESCE(e.embedding <=> %s::vector, 1.0) AS dist,"
                "       e.content, d.recalling_firm, d.classification,"
                "       ts_rank_cd(e.content_tsv, q.tsq) AS fts_rank"
                "  FROM q"
                "  JOIN embeddings e ON e.content_tsv @@ q.tsq"
                "  JOIN drug_enforcement d ON d.recall_number = e.source_id"
                "  {where}"
                "  ORDER BY fts_rank DESC, dist"
                "  LIMIT %s"
            ).format(where=where)
            params = [query, qvec, source, field, *fparams, limit]
    with conn.cursor() as cur:
        cur.execute(q, params)
        rows = cur.fetchall()
    _record_debug_sql(
        sql_debug_recorder,
        q,
        params,
        source="retrieval.fts_candidates",
        title=f"FTS retrieval candidates ({field})",
        query_id=f"retrieval_fts_{field}",
    )
    return _rows_to_candidates(rows)


def _fusion_key(candidate: _Candidate, field: str) -> tuple[str, ...]:
    if field == "both":
        return (candidate.recall_number,)
    return (candidate.recall_number, candidate.field)


def _elapsed_ms(start: float) -> int:
    return max(0, round((perf_counter() - start) * 1000))


def _rrf_fuse(vector_hits: Sequence[_Candidate], fts_hits: Sequence[_Candidate], *,
              k: int, field: str, retrieval_mode: str = "hybrid",
              fallback_reason: str | None = None,
              timings_ms: dict[str, int] | None = None,
              fts_queries: Sequence[str] = ()) -> SearchResult:
    scores: dict[tuple[str, ...], float] = {}
    best: dict[tuple[str, ...], _Candidate] = {}
    best_component: dict[tuple[str, ...], float] = {}
    best_is_fts: dict[tuple[str, ...], bool] = {}
    vector_meta: dict[tuple[str, ...], tuple[int, float, float]] = {}
    fts_meta: dict[tuple[str, ...], tuple[int, float]] = {}

    def add(candidates: Sequence[_Candidate], *, is_fts: bool) -> None:
        for rank, candidate in enumerate(candidates, 1):
            key = _fusion_key(candidate, field)
            component = 1.0 / (RRF_K + rank)
            scores[key] = scores.get(key, 0.0) + component
            if is_fts:
                fts_meta.setdefault(key, (rank, candidate.rank_score))
            else:
                distance = candidate.distance
                vector_meta.setdefault(key, (rank, distance, max(0.0, 1.0 - distance)))
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
    hits = []
    for fused_rank, key in enumerate(ranked_keys[:k], 1):
        vector_rank: int | None = None
        vector_distance: float | None = None
        vector_similarity: float | None = None
        if key in vector_meta:
            vector_rank, vector_distance, vector_similarity = vector_meta[key]
        fts_rank: int | None = None
        fts_score: float | None = None
        if key in fts_meta:
            fts_rank, fts_score = fts_meta[key]
        hits.append(
            Hit(
                recall_number=best[key].recall_number,
                field=best[key].field,
                distance=best[key].distance,
                content=best[key].content,
                recalling_firm=best[key].recalling_firm,
                classification=best[key].classification,
                rrf_score=scores[key],
                score=best[key].rank_score if retrieval_mode == "fts_only"
                else max(0.0, 1.0 - best[key].distance),
                score_kind="fts_rank" if retrieval_mode == "fts_only" else "similarity",
                retrieval_mode=retrieval_mode,
                embedding_fallback_reason=fallback_reason,
                fused_rank=fused_rank,
                vector_rank=vector_rank,
                vector_distance=vector_distance,
                vector_similarity=vector_similarity,
                fts_rank=fts_rank,
                fts_score=fts_score,
            )
        )
    return SearchResult(
        hits,
        retrieval_mode=retrieval_mode,
        embedding_fallback_reason=fallback_reason,
        vector_hit_count=len(vector_hits),
        fts_hit_count=len(fts_hits),
        fused_hit_count=len(ranked_keys),
        timings_ms=timings_ms,
        fts_queries=fts_queries,
    )


def search(conn: psycopg.Connection, client: Any | None, query: str, *,
           k: int = 10, field: str = "reason_for_recall",
           filters: Sequence[Filter] = (), source: str = "drug_enforcement",
           embed_config: llm.EmbeddingConfig | None = None,
           embedding_error: BaseException | None = None,
           fts_queries: Sequence[str] = (),
           sql_debug_recorder: Callable[..., None] | None = None) -> SearchResult:
    """Hybrid records for ``query``, optionally pre-filtered by hard constraints.

    The vector and FTS halves both honor the same filters on the joined source table.
    ``field='both'`` dedupes to one row per record. ``source`` selects the dataset; v1
    enriches from drug_enforcement (one source).
    """
    if k <= 0:
        return SearchResult()
    embed_config = embed_config or llm.embedding_config()
    limit = _candidate_limit(k)
    timings_ms: dict[str, int] = {}
    search_started = perf_counter()
    used_fts_queries: list[str] = []
    try:
        embedding_started = perf_counter()
        if embedding_error is not None:
            raise embedding_error
        if client is None:
            raise llm.ProviderMissingKeyError(
                "embedding client is not configured",
                provider=embed_config.provider,
                model=embed_config.model,
                operation="embedding",
            )
        qvec = _vec_literal(embed_query(client, embed_config, query))
        timings_ms["embedding"] = _elapsed_ms(embedding_started)
    except Exception as exc:
        timings_ms["embedding"] = _elapsed_ms(embedding_started)
        if not llm.can_fallback_to_fts(exc):
            raise
        fts_hits: list[_Candidate] = []
        fts_started = perf_counter()
        for fts_query in agent_control.broad_fts_queries(query, list(fts_queries)):
            used_fts_queries.append(fts_query)
            fts_hits.extend(_fts_candidates(conn, fts_query, None, source=source, field=field,
                                            filters=filters, limit=limit,
                                            sql_debug_recorder=sql_debug_recorder))
            if fts_hits:
                break
        timings_ms["fts"] = _elapsed_ms(fts_started)
        fusion_started = perf_counter()
        result = _rrf_fuse(
            [],
            fts_hits,
            k=k,
            field=field,
            retrieval_mode="fts_only",
            fallback_reason=_embedding_fallback_reason(exc),
            timings_ms=timings_ms,
            fts_queries=used_fts_queries,
        )
        timings_ms["fusion"] = _elapsed_ms(fusion_started)
        timings_ms["total"] = _elapsed_ms(search_started)
        result.timings_ms = dict(timings_ms)
        return result
    vector_started = perf_counter()
    vector_hits = _vector_candidates(conn, qvec, source=source, field=field,
                                     filters=filters, limit=limit,
                                     sql_debug_recorder=sql_debug_recorder)
    timings_ms["vector"] = _elapsed_ms(vector_started)
    fts_hits: list[_Candidate] = []
    fts_started = perf_counter()
    for fts_query in agent_control.broad_fts_queries(query, list(fts_queries)):
        used_fts_queries.append(fts_query)
        fts_hits.extend(_fts_candidates(conn, fts_query, qvec, source=source, field=field,
                                        filters=filters, limit=limit,
                                        sql_debug_recorder=sql_debug_recorder))
    timings_ms["fts"] = _elapsed_ms(fts_started)
    fusion_started = perf_counter()
    result = _rrf_fuse(
        vector_hits,
        fts_hits,
        k=k,
        field=field,
        timings_ms=timings_ms,
        fts_queries=used_fts_queries,
    )
    timings_ms["fusion"] = _elapsed_ms(fusion_started)
    timings_ms["total"] = _elapsed_ms(search_started)
    result.timings_ms = dict(timings_ms)
    return result


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Hybrid vector + FTS search over drug-recall text.")
    p.add_argument("query", help="natural-language query")
    p.add_argument("-k", type=int, default=10, help="number of results (default 10)")
    p.add_argument("--field", choices=FIELDS, default="reason_for_recall",
                   help="which embedded field to search (default reason_for_recall)")
    return p.parse_args()


def main() -> None:
    args = parse_args()
    embed_config = llm.embedding_config()
    embedding_error: llm.ProviderError | None = None
    try:
        client = llm.create_embedding_client(embed_config)
    except llm.ProviderError as exc:
        client = None
        embedding_error = exc
    with psycopg.connect(DEFAULT_DSN) as conn:
        hits = search(conn, client, args.query, k=args.k, field=args.field,
                      embed_config=embed_config, embedding_error=embedding_error)
        print(f"Q: {args.query!r}  (field={args.field}, k={args.k})\n")
        print(
            f"Embedding: provider={embed_config.provider} model={embed_config.model} "
            f"dimension={embed_config.dimension} retrieval_mode={hits.retrieval_mode}"
        )
        if hits.embedding_fallback_reason:
            print(f"Embedding fallback: {hits.embedding_fallback_reason}")
        print()
        for i, h in enumerate(hits, 1):
            print(f"{i:>2}. [{h.recall_number}] {h.score_kind}={h.retrieval_score:.3f}  "
                  f"{h.retrieval_mode}  {h.classification or '-'}  {h.recalling_firm or '-'}")
            print(f"    {(h.content or '')[:150]}")


if __name__ == "__main__":
    main()
