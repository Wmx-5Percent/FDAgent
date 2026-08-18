"""Postgres-backed query logging for the /ask API."""
from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any, Mapping

import psycopg
from psycopg.types.json import Jsonb

DEFAULT_DSN = os.environ.get("DATABASE_URL", "postgresql://localhost:5432/fda")


@dataclass(frozen=True)
class QueryLogEntry:
    route: str
    question: str
    request: dict[str, Any]
    status_code: int
    ok: bool
    latency_ms: int
    query_intent: str | None = None
    data_kind: str | None = None
    semantic_query: str | None = None
    query_spec: dict[str, Any] | None = None
    decision: dict[str, Any] | None = None
    response_metadata: dict[str, Any] | None = None
    error_type: str | None = None
    error_message: str | None = None
    error_detail: dict[str, Any] | None = None


@dataclass(frozen=True)
class HybridSearchLogEntry:
    query: str
    field: str
    k: int
    filters: dict[str, Any]
    embedding_provider: str
    embedding_model: str
    retrieval_mode: str
    fallback_reason: str | None
    vector_hit_count: int
    fts_hit_count: int
    fused_hit_count: int
    top_recall_numbers: list[str]
    timings_ms: dict[str, Any]
    request: dict[str, Any]
    response_metadata: dict[str, Any]
    fusion_config: dict[str, Any] | None = None
    alias_generation: dict[str, Any] | None = None
    vector_trace: list[dict[str, Any]] | None = None
    fts_trace: list[dict[str, Any]] | None = None
    fused_trace: list[dict[str, Any]] | None = None
    error_type: str | None = None
    error_message: str | None = None


def _jsonb(value: dict[str, Any] | None) -> Jsonb | None:
    return Jsonb(value) if value is not None else None


class QueryLogger:
    """Writes one query_log row per handled API request."""

    def __init__(self, dsn: str = DEFAULT_DSN) -> None:
        self.dsn = dsn

    def write(self, entry: QueryLogEntry) -> int:
        with psycopg.connect(self.dsn, autocommit=True) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO query_log (
                        route, question, request, status_code, ok, latency_ms,
                        query_intent, data_kind, semantic_query, query_spec, decision,
                        response_metadata, error_type, error_message, error_detail
                    )
                    VALUES (
                        %s, %s, %s, %s, %s, %s,
                        %s, %s, %s, %s, %s,
                        %s, %s, %s, %s
                    )
                    RETURNING id
                    """,
                    [
                        entry.route,
                        entry.question,
                        Jsonb(entry.request),
                        entry.status_code,
                        entry.ok,
                        entry.latency_ms,
                        entry.query_intent,
                        entry.data_kind,
                        entry.semantic_query,
                        _jsonb(entry.query_spec),
                        _jsonb(entry.decision),
                        _jsonb(entry.response_metadata),
                        entry.error_type,
                        entry.error_message,
                        _jsonb(entry.error_detail),
                    ],
                )
                row = cur.fetchone()
        if row is None:
            raise RuntimeError("query_log insert did not return an id")
        return int(row[0])


class HybridSearchLogger:
    """Writes one hybrid_search_log row per lab/debug retrieval run."""

    def __init__(self, dsn: str = DEFAULT_DSN) -> None:
        self.dsn = dsn

    def write(self, entry: HybridSearchLogEntry) -> int:
        with psycopg.connect(self.dsn, autocommit=True) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO hybrid_search_log (
                        query, field, k, filters,
                        embedding_provider, embedding_model, retrieval_mode, fallback_reason,
                        vector_hit_count, fts_hit_count, fused_hit_count,
                        top_recall_numbers, timings_ms, request, response_metadata,
                        fusion_config, alias_generation, vector_trace, fts_trace, fused_trace,
                        error_type, error_message
                    )
                    VALUES (
                        %s, %s, %s, %s,
                        %s, %s, %s, %s,
                        %s, %s, %s,
                        %s, %s, %s, %s,
                        %s, %s, %s, %s, %s,
                        %s, %s
                    )
                    RETURNING id
                    """,
                    [
                        entry.query,
                        entry.field,
                        entry.k,
                        Jsonb(entry.filters),
                        entry.embedding_provider,
                        entry.embedding_model,
                        entry.retrieval_mode,
                        entry.fallback_reason,
                        entry.vector_hit_count,
                        entry.fts_hit_count,
                        entry.fused_hit_count,
                        Jsonb(entry.top_recall_numbers),
                        Jsonb(entry.timings_ms),
                        Jsonb(entry.request),
                        Jsonb(entry.response_metadata),
                        Jsonb(entry.fusion_config or {}),
                        Jsonb(entry.alias_generation or {}),
                        Jsonb(entry.vector_trace or []),
                        Jsonb(entry.fts_trace or []),
                        Jsonb(entry.fused_trace or []),
                        entry.error_type,
                        entry.error_message,
                    ],
                )
                row = cur.fetchone()
        if row is None:
            raise RuntimeError("hybrid_search_log insert did not return an id")
        return int(row[0])

    def list_runs(self, *, limit: int = 25, before_id: int | None = None) -> list[dict[str, Any]]:
        limit = min(50, max(1, int(limit)))
        where = "WHERE id < %s" if before_id is not None else ""
        params: list[Any] = [before_id, limit] if before_id is not None else [limit]
        with psycopg.connect(self.dsn) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    f"""
                    SELECT id, created_at, query, field, k, retrieval_mode, fallback_reason,
                           vector_hit_count, fts_hit_count, fused_hit_count, top_recall_numbers,
                           timings_ms, request, response_metadata, fusion_config,
                           alias_generation, error_type, error_message
                    FROM hybrid_search_log
                    {where}
                    ORDER BY id DESC
                    LIMIT %s
                    """,
                    params,
                )
                rows = cur.fetchall()
        return [
            {
                "id": row[0],
                "created_at": row[1].isoformat() if row[1] is not None else None,
                "query": row[2],
                "field": row[3],
                "k": row[4],
                "retrieval_mode": row[5],
                "fallback_reason": row[6],
                "vector_hit_count": row[7],
                "fts_hit_count": row[8],
                "fused_hit_count": row[9],
                "top_recall_numbers": row[10],
                "timings_ms": row[11],
                "request": row[12],
                "response_metadata": row[13],
                "fusion_config": row[14],
                "alias_generation": row[15],
                "error_type": row[16],
                "error_message": row[17],
            }
            for row in rows
        ]

    def get_run(self, run_id: int) -> dict[str, Any] | None:
        with psycopg.connect(self.dsn) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT id, created_at, query, field, k, filters,
                           embedding_provider, embedding_model, retrieval_mode, fallback_reason,
                           vector_hit_count, fts_hit_count, fused_hit_count, top_recall_numbers,
                           timings_ms, request, response_metadata, fusion_config, alias_generation,
                           vector_trace, fts_trace, fused_trace, error_type, error_message
                    FROM hybrid_search_log
                    WHERE id = %s
                    """,
                    [run_id],
                )
                row = cur.fetchone()
        if row is None:
            return None
        return {
            "id": row[0],
            "created_at": row[1].isoformat() if row[1] is not None else None,
            "query": row[2],
            "field": row[3],
            "k": row[4],
            "filters": row[5],
            "embedding_provider": row[6],
            "embedding_model": row[7],
            "retrieval_mode": row[8],
            "fallback_reason": row[9],
            "vector_hit_count": row[10],
            "fts_hit_count": row[11],
            "fused_hit_count": row[12],
            "top_recall_numbers": row[13],
            "timings_ms": row[14],
            "request": row[15],
            "response_metadata": row[16],
            "fusion_config": row[17],
            "alias_generation": row[18],
            "vector_trace": row[19],
            "fts_trace": row[20],
            "fused_trace": row[21],
            "error_type": row[22],
            "error_message": row[23],
        }


def response_metadata(response: Mapping[str, Any], *, model: str | None = None,
                      provider: str | None = None) -> dict[str, Any]:
    data = response.get("data")
    data_kind = data.get("kind") if isinstance(data, Mapping) else None
    metadata: dict[str, Any] = {
        "data_kind": data_kind,
        "summary_chars": len(str(response.get("summary") or "")),
    }
    if provider:
        metadata["provider"] = provider
    if model:
        metadata["model"] = model
    if not isinstance(data, Mapping):
        return metadata

    if data_kind == "multi_section":
        sections = data.get("sections")
        if isinstance(sections, list):
            section_metadata: list[dict[str, Any]] = []
            total = 0
            for section in sections:
                if not isinstance(section, Mapping):
                    continue
                items = section.get("items")
                rows = section.get("rows")
                result_count = 0
                if isinstance(items, list):
                    result_count = len(items)
                elif isinstance(rows, list):
                    result_count = len(rows)
                total += result_count
                section_metadata.append({
                    "id": section.get("id"),
                    "kind": section.get("kind"),
                    "dimension": section.get("dimension"),
                    "source": section.get("source"),
                    "result_count": result_count,
                })
            metadata["section_count"] = len(section_metadata)
            metadata["result_count"] = total
            metadata["sections"] = section_metadata
        return metadata

    count_fields = {
        "retrieval": "items",
        "distribution": "items",
        "semantic_count": "evidence_items",
        "semantic_distribution": "items",
        "series": "points",
        "rows": "rows",
        "message": "suggestions",
        "clarification": "suggestions",
    }
    field = count_fields.get(str(data_kind))
    if field and isinstance(data.get(field), list):
        metadata["result_count"] = len(data[field])
    if str(data_kind) in {"semantic_count", "semantic_distribution"}:
        for key in ("estimated_count", "verified_count", "validated_count", "candidate_count"):
            value = data.get(key)
            if isinstance(value, int):
                metadata[key] = value
    for key in ("retrieval_mode", "embedding_fallback_reason"):
        value = data.get(key)
        if value:
            metadata[key] = value
    if data.get("degraded"):
        metadata["degraded"] = True
    if data_kind in {"message", "clarification"}:
        for key in ("route", "reason"):
            value = data.get(key)
            if value:
                metadata[key] = value
    return metadata
