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
from dataclasses import dataclass, field as dataclass_field
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
TRACE_LIMIT = 100


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
    vector_rrf_contribution: float = 0.0
    fts_rrf_contribution: float = 0.0
    vector_contribution_percent: float = 0.0
    fts_contribution_percent: float = 0.0
    matched_fts_queries: list[str] = dataclass_field(default_factory=list)

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
                 fts_queries: Sequence[str] = (),
                 fusion_config: dict[str, Any] | None = None,
                 vector_trace: Sequence[dict[str, Any]] = (),
                 fts_trace: Sequence[dict[str, Any]] = (),
                 fused_trace: Sequence[dict[str, Any]] = ()) -> None:
        super().__init__(hits)
        self.retrieval_mode = retrieval_mode
        self.embedding_fallback_reason = embedding_fallback_reason
        self.vector_hit_count = vector_hit_count
        self.fts_hit_count = fts_hit_count
        self.fused_hit_count = len(hits) if fused_hit_count is None else fused_hit_count
        self.timings_ms = dict(timings_ms or {})
        self.fts_queries = list(fts_queries)
        self.fusion_config = dict(fusion_config or default_fusion_config())
        self.vector_trace = list(vector_trace)
        self.fts_trace = list(fts_trace)
        self.fused_trace = list(fused_trace)


@dataclass(frozen=True)
class _Candidate:
    recall_number: str
    field: str
    distance: float
    content: str
    recalling_firm: Optional[str]
    classification: Optional[str]
    rank_score: float = 0.0
    matched_fts_queries: tuple[str, ...] = ()


def _weights(vector_weight_percent: int | float | None) -> tuple[float, float, int]:
    percent = 50 if vector_weight_percent is None else int(round(float(vector_weight_percent)))
    percent = min(100, max(0, percent))
    vector_weight = percent / 100.0
    return vector_weight, 1.0 - vector_weight, percent


def default_fusion_config(*, vector_weight_percent: int | float | None = 50,
                          candidate_limit: int | None = None) -> dict[str, Any]:
    vector_weight, fts_weight, percent = _weights(vector_weight_percent)
    config: dict[str, Any] = {
        "rrf_k": RRF_K,
        "vector_weight_percent": percent,
        "fts_weight_percent": 100 - percent,
        "vector_weight": vector_weight,
        "fts_weight": fts_weight,
        "min_candidates": MIN_CANDIDATES,
        "candidate_multiplier": CANDIDATE_MULTIPLIER,
        "trace_limit": TRACE_LIMIT,
        "formula": (
            "rrf_score = vector_weight / (rrf_k + vector_rank) + "
            "fts_weight / (rrf_k + fts_rank)"
        ),
    }
    if candidate_limit is not None:
        config["candidate_limit"] = candidate_limit
    return config


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


def _rows_to_candidates(rows: Sequence[tuple[Any, ...]], *,
                        matched_fts_query: str | None = None) -> list[_Candidate]:
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
            matched_fts_queries=((matched_fts_query,) if matched_fts_query else ()),
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
            "  ORDER BY e.source_id, dist, e.field"
            ") s ORDER BY s.dist, s.source_id, s.field LIMIT %s"
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
            "  ORDER BY e.embedding <=> %s::vector, e.source_id, e.field"
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
                "  ORDER BY e.source_id, fts_rank DESC, dist, e.field"
                ") s ORDER BY s.fts_rank DESC, s.dist, s.source_id, s.field LIMIT %s"
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
                "  ORDER BY fts_rank DESC, dist, e.source_id, e.field"
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
    return _rows_to_candidates(rows, matched_fts_query=query)


def _fusion_key(candidate: _Candidate, field: str) -> tuple[str, ...]:
    if field == "both":
        return (candidate.recall_number,)
    return (candidate.recall_number, candidate.field)


def _elapsed_ms(start: float) -> int:
    return max(0, round((perf_counter() - start) * 1000))


def _dedupe_fts_candidates(candidates: Sequence[_Candidate], *, field: str) -> list[_Candidate]:
    best: dict[tuple[str, ...], _Candidate] = {}
    matched_queries: dict[tuple[str, ...], set[str]] = {}
    for candidate in candidates:
        key = _fusion_key(candidate, field)
        matched_queries.setdefault(key, set()).update(candidate.matched_fts_queries)
        current = best.get(key)
        if current is None or (
            candidate.rank_score,
            -candidate.distance,
            candidate.recall_number,
            candidate.field,
        ) > (
            current.rank_score,
            -current.distance,
            current.recall_number,
            current.field,
        ):
            best[key] = candidate
    out = []
    for key, candidate in best.items():
        out.append(_Candidate(
            recall_number=candidate.recall_number,
            field=candidate.field,
            distance=candidate.distance,
            content=candidate.content,
            recalling_firm=candidate.recalling_firm,
            classification=candidate.classification,
            rank_score=candidate.rank_score,
            matched_fts_queries=tuple(sorted(matched_queries.get(key, set()))),
        ))
    return sorted(
        out,
        key=lambda c: (-c.rank_score, c.distance, c.recall_number, c.field),
    )


def _trace_key(candidate: _Candidate) -> dict[str, Any]:
    return {"recall_number": candidate.recall_number, "field": candidate.field}


def _rrf_fuse(vector_hits: Sequence[_Candidate], fts_hits: Sequence[_Candidate], *,
              k: int, field: str, retrieval_mode: str = "hybrid",
              fallback_reason: str | None = None,
              timings_ms: dict[str, int] | None = None,
              fts_queries: Sequence[str] = (),
              vector_weight_percent: int | float | None = 50,
              candidate_limit: int | None = None) -> SearchResult:
    scores: dict[tuple[str, ...], float] = {}
    best: dict[tuple[str, ...], _Candidate] = {}
    best_component: dict[tuple[str, ...], float] = {}
    best_is_fts: dict[tuple[str, ...], bool] = {}
    vector_meta: dict[tuple[str, ...], tuple[int, float, float]] = {}
    fts_meta: dict[tuple[str, ...], tuple[int, float]] = {}
    vector_contrib: dict[tuple[str, ...], float] = {}
    fts_contrib: dict[tuple[str, ...], float] = {}
    fts_queries_by_key: dict[tuple[str, ...], tuple[str, ...]] = {}
    vector_weight, fts_weight, vector_percent = _weights(vector_weight_percent)
    fusion_config = default_fusion_config(
        vector_weight_percent=vector_percent,
        candidate_limit=candidate_limit,
    )

    def add(candidates: Sequence[_Candidate], *, is_fts: bool) -> None:
        for rank, candidate in enumerate(candidates, 1):
            key = _fusion_key(candidate, field)
            weight = fts_weight if is_fts else vector_weight
            component = weight / (RRF_K + rank) if weight > 0 else 0.0
            scores[key] = scores.get(key, 0.0) + component
            if is_fts:
                fts_meta.setdefault(key, (rank, candidate.rank_score))
                fts_contrib[key] = component
                fts_queries_by_key[key] = candidate.matched_fts_queries
            else:
                distance = candidate.distance
                vector_meta.setdefault(key, (rank, distance, max(0.0, 1.0 - distance)))
                vector_contrib[key] = component
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
        key=lambda key: (
            -scores[key],
            best[key].distance,
            best[key].recall_number,
            best[key].field,
        ),
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
        vector_part = vector_contrib.get(key, 0.0)
        fts_part = fts_contrib.get(key, 0.0)
        total_part = vector_part + fts_part
        vector_part_percent = (vector_part / total_part * 100.0) if total_part else 0.0
        fts_part_percent = (fts_part / total_part * 100.0) if total_part else 0.0
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
                vector_rrf_contribution=vector_part,
                fts_rrf_contribution=fts_part,
                vector_contribution_percent=vector_part_percent,
                fts_contribution_percent=fts_part_percent,
                matched_fts_queries=list(fts_queries_by_key.get(key, ())),
            )
        )
    vector_trace = []
    for rank, candidate in enumerate(vector_hits[:TRACE_LIMIT], 1):
        key = _fusion_key(candidate, field)
        distance = candidate.distance
        vector_trace.append({
            **_trace_key(candidate),
            "rank": rank,
            "distance": distance,
            "similarity": max(0.0, 1.0 - distance),
            "rrf_contribution": vector_contrib.get(key, 0.0),
        })
    fts_trace = []
    for rank, candidate in enumerate(fts_hits[:TRACE_LIMIT], 1):
        key = _fusion_key(candidate, field)
        fts_trace.append({
            **_trace_key(candidate),
            "rank": rank,
            "fts_score": candidate.rank_score,
            "matched_fts_queries": list(candidate.matched_fts_queries),
            "rrf_contribution": fts_contrib.get(key, 0.0),
        })
    fused_trace = []
    for hit in hits[:TRACE_LIMIT]:
        fused_trace.append({
            "recall_number": hit.recall_number,
            "field": hit.field,
            "rank": hit.fused_rank,
            "rrf_score": hit.rrf_score,
            "vector_rrf_contribution": hit.vector_rrf_contribution,
            "fts_rrf_contribution": hit.fts_rrf_contribution,
            "vector_contribution_percent": hit.vector_contribution_percent,
            "fts_contribution_percent": hit.fts_contribution_percent,
            "vector_rank": hit.vector_rank,
            "fts_rank": hit.fts_rank,
            "matched_fts_queries": list(hit.matched_fts_queries),
        })
    return SearchResult(
        hits,
        retrieval_mode=retrieval_mode,
        embedding_fallback_reason=fallback_reason,
        vector_hit_count=len(vector_hits),
        fts_hit_count=len(fts_hits),
        fused_hit_count=len(ranked_keys),
        timings_ms=timings_ms,
        fts_queries=fts_queries,
        fusion_config=fusion_config,
        vector_trace=vector_trace,
        fts_trace=fts_trace,
        fused_trace=fused_trace,
    )


def search(conn: psycopg.Connection, client: Any | None, query: str, *,
           k: int = 10, field: str = "reason_for_recall",
           filters: Sequence[Filter] = (), source: str = "drug_enforcement",
           embed_config: llm.EmbeddingConfig | None = None,
           embedding_error: BaseException | None = None,
           fts_queries: Sequence[str] = (),
           sql_debug_recorder: Callable[..., None] | None = None,
           vector_weight_percent: int | float | None = 50) -> SearchResult:
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
    fusion_config = default_fusion_config(
        vector_weight_percent=vector_weight_percent,
        candidate_limit=limit,
    )
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
        fts_hits_raw: list[_Candidate] = []
        fts_started = perf_counter()
        for fts_query in agent_control.broad_fts_queries(query, list(fts_queries)):
            used_fts_queries.append(fts_query)
            query_hits = _fts_candidates(conn, fts_query, None, source=source, field=field,
                                         filters=filters, limit=limit,
                                         sql_debug_recorder=sql_debug_recorder)
            fts_hits_raw.extend(query_hits)
            if query_hits:
                break
        fts_hits = _dedupe_fts_candidates(fts_hits_raw, field=field) if fts_queries else fts_hits_raw
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
            vector_weight_percent=vector_weight_percent,
            candidate_limit=limit,
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
    fts_hits_raw: list[_Candidate] = []
    fts_started = perf_counter()
    for fts_query in agent_control.broad_fts_queries(query, list(fts_queries)):
        used_fts_queries.append(fts_query)
        fts_hits_raw.extend(_fts_candidates(conn, fts_query, qvec, source=source, field=field,
                                            filters=filters, limit=limit,
                                            sql_debug_recorder=sql_debug_recorder))
    fts_hits = _dedupe_fts_candidates(fts_hits_raw, field=field)
    timings_ms["fts"] = _elapsed_ms(fts_started)
    fusion_started = perf_counter()
    result = _rrf_fuse(
        vector_hits,
        fts_hits,
        k=k,
        field=field,
        timings_ms=timings_ms,
        fts_queries=used_fts_queries,
        vector_weight_percent=vector_weight_percent,
        candidate_limit=limit,
    )
    timings_ms["fusion"] = _elapsed_ms(fusion_started)
    timings_ms["total"] = _elapsed_ms(search_started)
    result.timings_ms = dict(timings_ms)
    result.fusion_config = fusion_config
    return result


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Hybrid vector + FTS search over drug-recall text.")
    p.add_argument("query", help="natural-language query")
    p.add_argument("-k", type=int, default=10, help="number of results (default 10)")
    p.add_argument("--field", choices=FIELDS, default="reason_for_recall",
                   help="which embedded field to search (default reason_for_recall)")
    p.add_argument("--vector-weight-percent", type=int, default=50,
                   help="weighted RRF vector contribution percent, 0-100 (default 50)")
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
                      embed_config=embed_config, embedding_error=embedding_error,
                      vector_weight_percent=args.vector_weight_percent)
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
