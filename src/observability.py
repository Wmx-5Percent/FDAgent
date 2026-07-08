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

    count_fields = {
        "retrieval": "items",
        "distribution": "items",
        "semantic_count": "evidence_items",
        "semantic_distribution": "items",
        "series": "points",
        "rows": "rows",
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
    return metadata
