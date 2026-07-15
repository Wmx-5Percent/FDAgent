"""FastAPI service exposing the deterministic NL->SQL analytics engine (Path 1, serving half).

A single ``POST /ask`` turns a natural-language question into a validated ``QuerySpec`` (via
``nl_query.NLEngine``), runs it through the SQL analytics engine, and returns a chart-friendly,
evidence-backed JSON payload that the static page at ``web/index.html`` renders. Every number
still comes from SQL — the model only picks the query shape. Provider clients and schema
context are warmed ONCE at startup and reused across requests.

Run (from the repo root):
    .venv/bin/python -m uvicorn src.api:app --reload
    # then open http://127.0.0.1:8000/
"""
from __future__ import annotations

import json
import os
import re
import sys
import traceback
from contextlib import asynccontextmanager
from datetime import date, datetime
from html import escape
from pathlib import Path
from time import perf_counter
from typing import Any, Literal, Optional
from urllib.parse import quote

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))  # allow `import nl_query` under uvicorn
import llm  # noqa: E402
import agent_control  # noqa: E402
import retrieval  # noqa: E402
import validation  # noqa: E402
from analytics import CATALOG, OPS, Filter, Kind, RawFirmExposureLeaderboard, RecallAnalytics  # noqa: E402
from nl_query import Answer, Intent, MultiSectionResult, NLEngine, TaxonomyExplanation  # noqa: E402
from observability import (  # noqa: E402
    HybridSearchLogEntry,
    HybridSearchLogger,
    QueryLogEntry,
    QueryLogger,
    response_metadata,
)

WEB_DIR = Path(__file__).resolve().parent.parent / "web"
TITLE_WORD_LIMIT = 6
TITLE_MAX_CHARS = 44
TITLE_MAX_TOKENS = 64
TITLE_FALLBACK_STOPWORDS = {
    "a", "about", "are", "been", "did", "do", "does", "for", "give", "has", "have",
    "how", "in", "is", "many", "me", "of", "on", "show", "tell", "the", "there",
    "to", "was", "were", "what", "when", "which", "who", "with",
}
OPENFDA_DRUG_ENFORCEMENT_URL = "https://api.fda.gov/drug/enforcement.json"
OPENFDA_TERMS_URL = "https://open.fda.gov/terms/"
OPENFDA_DISCLAIMER_URL = "https://open.fda.gov/terms/#disclaimer-of-warranties"
OPENFDA_LICENSE_URL = "https://open.fda.gov/license/"
RECALL_NUMBER_RE = re.compile(r"^[A-Z]-\d{3,4}-\d{4}$")
RECALL_DETAIL_COLUMNS = [
    "recall_number",
    "classification",
    "status",
    "product_type",
    "recalling_firm",
    "city",
    "state",
    "country",
    "product_description",
    "reason_for_recall",
    "report_date",
    "recall_initiation_date",
    "center_classification_date",
    "termination_date",
    "distribution_pattern",
    "code_info",
    "product_quantity",
    "voluntary_mandated",
    "initial_firm_notification",
    "event_id",
    "raw",
]
RECALL_DETAIL_SECTIONS = [
    (
        "Product and recall reason",
        [
            ("product_description", "Product description"),
            ("reason_for_recall", "Reason for recall"),
        ],
    ),
    (
        "Firm and location",
        [
            ("recalling_firm", "Recalling firm"),
            ("city", "City"),
            ("state", "State"),
            ("country", "Country"),
        ],
    ),
    (
        "Dates",
        [
            ("report_date", "Report date"),
            ("recall_initiation_date", "Recall initiation date"),
            ("center_classification_date", "Center classification date"),
            ("termination_date", "Termination date"),
        ],
    ),
    (
        "Distribution and code information",
        [
            ("distribution_pattern", "Distribution pattern"),
            ("code_info", "Code / lot information"),
            ("product_quantity", "Product quantity"),
        ],
    ),
    (
        "FDA administrative fields",
        [
            ("product_type", "Product type"),
            ("voluntary_mandated", "Voluntary / mandated"),
            ("initial_firm_notification", "Initial firm notification"),
            ("event_id", "Event ID"),
        ],
    ),
]
RECALL_DETAIL_CORE_FIELDS = [
    ("recall_number", "recall number"),
    ("classification", "classification"),
    ("status", "status"),
    ("recalling_firm", "recalling firm"),
    ("product_description", "product description"),
    ("reason_for_recall", "reason for recall"),
]
RECALL_DETAIL_LONG_FIELDS = {
    "product_description",
    "reason_for_recall",
    "distribution_pattern",
    "code_info",
    "product_quantity",
}

# Warmed at startup, reused across requests (see lifespan).
_engine: Optional[NLEngine] = None
_query_logger: Optional[QueryLogger] = None
_hybrid_search_logger: Optional[HybridSearchLogger] = None
HYBRID_FILTER_ALLOWED_OPS: dict[Kind, set[str]] = {
    # Lab filters are hard constraints, so categorical/id fields stay exact-match only.
    Kind.DIMENSION: {"eq", "ne", "in"},
    Kind.ID: {"eq", "ne", "in"},
    # Free-text fields may use ILIKE in addition to exact matches.
    Kind.TEXT: {"eq", "ne", "in", "ilike"},
    # Date fields support chronological comparisons; ILIKE is intentionally rejected.
    Kind.DATE: {"eq", "ne", "in", "gte", "lte", "between"},
}
HYBRID_FILTER_STRING_MAX_CHARS = 200
HYBRID_FILTER_IN_MAX_ITEMS = 25
HYBRID_FILTER_MAX_SERIALIZED_BYTES = 4096


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Warm provider clients + cached schema context once, before serving traffic."""
    global _engine, _query_logger, _hybrid_search_logger
    _engine = NLEngine()
    _query_logger = QueryLogger(_engine.dsn)
    _hybrid_search_logger = HybridSearchLogger(_engine.dsn)
    yield
    _engine = None
    _query_logger = None
    _hybrid_search_logger = None


app = FastAPI(
    title="FDAgent — Drug-Recall Intelligence",
    version="0.1.0",
    summary="Natural-language questions over U.S. FDA drug-recall enforcement reports; "
            "every figure is computed in SQL and carries the recall numbers that back it.",
    lifespan=lifespan,
)
app.mount("/static", StaticFiles(directory=WEB_DIR), name="static")


class AskRequest(BaseModel):
    question: str = Field(min_length=1, max_length=500,
                          description="A natural-language question about FDA drug recalls.")


class TitleRequest(BaseModel):
    question: str = Field(min_length=1, max_length=500,
                          description="The first user question in a local chat conversation.")


class TitleResponse(BaseModel):
    title: str = Field(description="A concise chat title, capped at six words.")


class HybridSearchRequest(BaseModel):
    query: str = Field(min_length=1, max_length=500,
                       description="Natural-language retrieval query to inspect.")
    field: Literal["reason_for_recall", "product_description", "both"] = Field(
        default="both",
        description="Which embedded recall text field to search.",
    )
    k: int = Field(default=20, ge=1, le=100,
                   description="Number of fused retrieval rows to return.")
    filters: dict[str, Any] = Field(
        default_factory=dict,
        description=(
            "Hard filters over whitelisted drug_enforcement columns. Accepts either "
            "{column: value}, {column: [values]}, {column: {op, value}}, or "
            "{column: {gte/lte/between/...}}."
        ),
    )
    aliases: list[str] = Field(
        default_factory=list,
        max_length=20,
        description="Optional FTS aliases/synonyms to compare alongside the query.",
    )


def _json_safe(v: Any) -> Any:
    """Make a single SQL value JSON-serializable (dates -> ISO strings)."""
    if isinstance(v, (date, datetime)):
        return v.isoformat()
    return v


def _normalized_recall_number(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    recall_number = value.strip().upper()
    if not RECALL_NUMBER_RE.fullmatch(recall_number):
        return None
    return recall_number


def recall_verification_url(value: Any) -> str | None:
    """Raw openFDA API verification URL for a syntactically valid recall number."""
    recall_number = _normalized_recall_number(value)
    if recall_number is None:
        return None
    search = quote(f'recall_number:"{recall_number}"', safe="")
    return f"{OPENFDA_DRUG_ENFORCEMENT_URL}?search={search}&limit=1"


def recall_detail_url(value: Any) -> str | None:
    """FDAgent-hosted readable detail page URL for a syntactically valid recall number."""
    recall_number = _normalized_recall_number(value)
    if recall_number is None:
        return None
    return f"/recalls/{quote(recall_number, safe='')}"


def _recall_link(value: Any) -> dict[str, str] | None:
    detail_url = recall_detail_url(value)
    source_url = recall_verification_url(value)
    if detail_url is None or source_url is None:
        return None
    return {
        "recall_number": _normalized_recall_number(value) or str(value).strip(),
        "url": detail_url,
        "source_url": source_url,
        "source": "FDAgent recall detail (openFDA drug enforcement)",
    }


def _recall_links(values: list[Any]) -> list[dict[str, str]]:
    return [link for value in values if (link := _recall_link(value)) is not None]


def _attach_recall_url(item: dict[str, Any], recall_number: Any, *,
                       key: str = "url") -> dict[str, Any]:
    detail_url = recall_detail_url(recall_number)
    source_url = recall_verification_url(recall_number)
    if detail_url:
        item[key] = detail_url
    if source_url and key == "url":
        item["source_url"] = source_url
    return item


def _serialize_group(g: Any) -> dict[str, Any]:
    item = {
        "value": _json_safe(g.value),
        "count": int(g.count),
        "evidence": list(g.evidence),
    }
    evidence_links = _recall_links(item["evidence"])
    if evidence_links:
        item["evidence_links"] = evidence_links
    label = getattr(g, "label", None)
    if label:
        item["label"] = _json_safe(label)
    metadata = getattr(g, "metadata", None)
    if isinstance(metadata, dict):
        for key, value in metadata.items():
            if value is not None:
                item[key] = _json_safe(value)
    return item


def _serialize_raw_firm_exposure(result: RawFirmExposureLeaderboard) -> dict[str, Any]:
    items: list[dict[str, Any]] = []
    for item in result.items:
        row = {
            "rank": item.rank,
            "recalling_firm": item.recalling_firm,
            "exposure_score": item.exposure_score,
            "total_recalls": item.total_recalls,
            "class_i_recalls": item.class_i_recalls,
            "class_ii_recalls": item.class_ii_recalls,
            "class_iii_recalls": item.class_iii_recalls,
            "unclassified_recalls": item.unclassified_recalls,
            "top_reason_category": item.top_reason_category,
            "top_reason_node_id": item.top_reason_node_id,
            "top_reason_count": item.top_reason_count,
            "evidence": list(item.evidence),
            "evidence_links": _recall_links(list(item.evidence)),
        }
        items.append({k: _json_safe(v) for k, v in row.items() if v is not None})
    return {
        "kind": "raw_firm_exposure",
        "metric": result.metric,
        "metric_label": (
            "Severity-weighted exposure score"
            if result.metric == "severity_weighted" else "Raw recall count"
        ),
        "formula_version": result.formula_version,
        "formula": result.formula,
        "scope": result.scope,
        "caveats": list(result.caveats),
        "items": items,
    }


def _serialize_multi_section(result: MultiSectionResult) -> dict[str, Any]:
    sections = []
    for section in result.sections:
        section_data: dict[str, Any] = {
            "id": section.id,
            "title": section.title,
            "kind": section.data_kind,
            "dimension": section.dimension,
            "source": section.source,
            "items": [_serialize_group(g) for g in section.result],
            "spec": section.spec.model_dump(mode="json", exclude_none=True),
        }
        metadata = {k: v for k, v in section.metadata.items() if v is not None}
        if metadata:
            section_data["metadata"] = metadata
        sections.append(section_data)
    return {
        "kind": "multi_section",
        "sections": sections,
    }


def serialize_answer(ans: Answer) -> dict[str, Any]:
    """Shape an :class:`Answer` into a stable, chart-friendly response.

    ``data.kind`` tells the UI how to render: ``scalar`` (one number), ``distribution``
    (bar chart), ``series`` (line chart), ``rows`` (table), or a message/explanation kind.
    Evidence ``recall_number``s ride along so every figure is traceable back to source records.
    """
    spec = ans.spec
    response_intent = ans.metadata.get("intent") or (
        spec.intent.value if spec is not None else ans.metadata.get("control_route", "message")
    )
    spec_payload: dict[str, Any] = (
        spec.model_dump(mode="json", exclude_none=True) if spec is not None else {}
    )
    if ans.metadata.get("sub_specs"):
        spec_payload = {
            "intent": response_intent,
            "base_spec": spec_payload,
            "sub_specs": ans.metadata["sub_specs"],
        }
    payload: dict[str, Any] = {
        "question": ans.question,
        "intent": response_intent,
        "spec": spec_payload,
        "summary": ans.summary,
    }
    if ans.highlights:
        payload["highlights"] = list(ans.highlights)
    if isinstance(ans.result, agent_control.AgentControlResult):
        payload["data"] = ans.result.as_data()
    elif isinstance(ans.result, TaxonomyExplanation):
        node = ans.result.node
        payload["data"] = {
            "kind": "taxonomy_explanation",
            "route": "explanation",
            "node_id": node.node_id,
            "label": node.label,
            "definition": node.definition,
            "parent_id": node.parent_id,
            "parent_label": node.parent_label,
            "explanation": ans.result.answer,
            "examples": [
                _attach_recall_url({
                    "recall_number": item.get("recall_number"),
                    "classification": item.get("classification"),
                    "reason_for_recall": item.get("reason_for_recall"),
                }, item.get("recall_number"))
                for item in ans.result.examples
            ],
            "source": "taxonomy",
        }
    elif isinstance(ans.result, MultiSectionResult):
        payload["data"] = _serialize_multi_section(ans.result)
    elif isinstance(ans.result, RawFirmExposureLeaderboard):
        payload["data"] = _serialize_raw_firm_exposure(ans.result)
    elif isinstance(ans.result, validation.SemanticCountResult):
        result = ans.result
        accepted = [item for item in result.validations if item.accepted]
        evidence_items = []
        for item in accepted:
            evidence_items.append(_attach_recall_url({
                "recall_number": item.hit.recall_number,
                "field": item.hit.field,
                "retrieval_mode": item.hit.retrieval_mode,
                "score_kind": item.hit.score_kind,
                "retrieval_score": round(item.hit.retrieval_score, 3),
                "rrf_score": round(item.hit.rrf_score, 4),
                "similarity": round(item.hit.similarity, 3),
                "validation_confidence": round(item.validation.confidence, 3),
                "supporting_snippet": item.validation.supporting_snippet,
                "rationale": item.validation.rationale,
                "content": item.hit.content,
                "recalling_firm": item.hit.recalling_firm,
                "classification": item.hit.classification,
            }, item.hit.recall_number))
        base_data: dict[str, Any] = {
            "query": result.query,
            "retrieval_mode": result.retrieval_mode,
            "embedding_fallback_reason": result.embedding_fallback_reason,
            "estimated_count": result.estimated_count,
            "confidence_interval": result.confidence_interval,
            "confidence": result.confidence,
            "verified_count": result.verified_count,
            "candidate_count": result.candidate_count,
            "validated_count": result.validated_count,
            "retrieval_pool_count": result.retrieval_pool_count,
            "verified_ratio": (
                result.verified_count / result.validated_count
                if result.validated_count else 0.0
            ),
            "verified": f"{result.verified_count}/{result.validated_count}",
            "thresholds": result.thresholds,
            "evidence": list(result.evidence),
            "evidence_links": _recall_links(list(result.evidence)),
            "evidence_items": evidence_items,
        }
        if result.group_by:
            payload["data"] = {
                **base_data,
                "kind": "semantic_distribution",
                "dimension": result.group_by,
                "items": [
                    _serialize_group(g)
                    for g in result.groups
                ],
            }
        else:
            payload["data"] = {**base_data, "kind": "semantic_count"}
    elif spec is not None and spec.semantic_query:  # concept query -> ranked semantic hits
        retrieval_mode = (
            ans.result[0].retrieval_mode
            if ans.result else ans.metadata.get("retrieval_mode", "hybrid")
        )
        fallback_reason = (
            ans.result[0].embedding_fallback_reason
            if ans.result else ans.metadata.get("embedding_fallback_reason")
        )
        items = []
        for h in ans.result:
            items.append(_attach_recall_url({
                "recall_number": h.recall_number,
                "field": h.field,
                "retrieval_mode": h.retrieval_mode,
                "score_kind": h.score_kind,
                "similarity": round(h.similarity, 3),
                "retrieval_score": round(h.retrieval_score, 3),
                "rrf_score": round(h.rrf_score, 4),
                "content": h.content,
                "recalling_firm": h.recalling_firm,
                "classification": h.classification,
            }, h.recall_number))
        payload["data"] = {
            "kind": "retrieval",
            "query": spec.semantic_query,
            "retrieval_mode": retrieval_mode,
            "embedding_fallback_reason": fallback_reason,
            "degraded": bool(ans.metadata.get("degraded")),
            "items": items,
        }
    elif spec is not None and spec.intent is Intent.count_total:
        payload["data"] = {"kind": "scalar", "value": int(ans.result)}
    elif spec is not None and spec.intent is Intent.count_by:
        payload["data"] = {
            "kind": "distribution",
            "dimension": spec.group_by,
            "items": [
                _serialize_group(g)
                for g in ans.result
            ],
        }
    elif spec is not None and spec.intent is Intent.count_by_taxonomy:
        payload["data"] = {
            "kind": "distribution",
            "dimension": "recall_reason_category",
            "items": [
                _serialize_group(g)
                for g in ans.result
            ],
        }
    elif spec is not None and spec.intent is Intent.trend:
        payload["data"] = {
            "kind": "series",
            "grain": spec.grain or "year",
            "points": [{"period": _json_safe(p), "count": n} for p, n in ans.result],
        }
    elif spec is not None:  # sample
        rows = []
        for row in ans.result:
            serialized = {k: _json_safe(v) for k, v in row.items()}
            _attach_recall_url(serialized, row.get("recall_number"), key="recall_url")
            rows.append(serialized)
        payload["data"] = {
            "kind": "rows",
            "rows": rows,
        }
    else:
        payload["data"] = {
            "kind": "message",
            "route": ans.metadata.get("control_route", "message"),
            "message": ans.summary,
        }
    return payload


def _elapsed_ms(start: float) -> int:
    return max(0, round((perf_counter() - start) * 1000))


def _require_query_logger() -> QueryLogger:
    if _query_logger is None:
        raise RuntimeError("query logger not initialized")
    return _query_logger


def _require_hybrid_search_logger() -> HybridSearchLogger:
    if _hybrid_search_logger is None:
        raise RuntimeError("hybrid search logger not initialized")
    return _hybrid_search_logger


def _require_engine() -> NLEngine:
    if _engine is None:
        raise HTTPException(status_code=503, detail="engine not ready")
    return _engine


def _coerce_filter_atom(column: str, value: Any) -> Any:
    kind = CATALOG[column]
    if kind is Kind.DATE:
        if isinstance(value, date) and not isinstance(value, datetime):
            return value
        if not isinstance(value, str):
            raise ValueError(f"date filter {column!r} must be a string")
        text = value.strip()
        if len(text) > HYBRID_FILTER_STRING_MAX_CHARS:
            raise ValueError(
                f"filter {column!r} exceeds {HYBRID_FILTER_STRING_MAX_CHARS} characters"
            )
        for fmt in ("%Y-%m-%d", "%Y%m%d", "%Y/%m/%d"):
            try:
                return datetime.strptime(text, fmt).date()
            except ValueError:
                continue
        raise ValueError(f"date filter {column!r} must be YYYY-MM-DD")
    if not isinstance(value, str):
        raise ValueError(f"filter {column!r} must be a string")
    text = value.strip()
    if not text:
        raise ValueError(f"filter {column!r} cannot be empty")
    if len(text) > HYBRID_FILTER_STRING_MAX_CHARS:
        raise ValueError(
            f"filter {column!r} exceeds {HYBRID_FILTER_STRING_MAX_CHARS} characters"
        )
    return text


def _clean_aliases(values: list[str]) -> list[str]:
    aliases: list[str] = []
    seen: set[str] = set()
    for value in values:
        if not isinstance(value, str):
            continue
        alias = " ".join(value.split())
        key = alias.casefold()
        if alias and key not in seen:
            aliases.append(alias[:120])
            seen.add(key)
    return aliases[:20]


def _filters_for_request(raw_filters: dict[str, Any]) -> list[Filter]:
    if not isinstance(raw_filters, dict):
        raise ValueError("filters must be an object")
    _validate_filter_payload_size(raw_filters)
    filters: list[Filter] = []
    for column, raw in raw_filters.items():
        if column not in CATALOG:
            raise ValueError(f"unknown filter column {column!r}")
        filters.extend(_filters_for_column(column, raw))
    return filters


def _validate_filter_payload_size(raw_filters: dict[str, Any]) -> None:
    try:
        serialized = json.dumps(
            raw_filters,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
    except (TypeError, ValueError) as exc:
        raise ValueError("filters must be JSON-serializable") from exc
    size = len(serialized.encode("utf-8"))
    if size > HYBRID_FILTER_MAX_SERIALIZED_BYTES:
        raise ValueError(
            f"filters payload exceeds {HYBRID_FILTER_MAX_SERIALIZED_BYTES} bytes"
        )


def _filters_for_column(column: str, raw: Any) -> list[Filter]:
    if raw is None or raw == "" or raw == []:
        return []
    if isinstance(raw, dict):
        if "op" in raw:
            op = str(raw.get("op") or "").strip()
            value = raw.get("value", raw.get("values"))
            return [_make_filter(column, op, value)]
        out: list[Filter] = []
        for op in ("eq", "ne", "in", "gte", "lte", "between", "ilike"):
            if op in raw and raw[op] not in (None, "", []):
                out.append(_make_filter(column, op, raw[op]))
        if out:
            return out
        raise ValueError(f"filter object for {column!r} needs an op/value")
    if isinstance(raw, list):
        return [_make_filter(column, "in", raw)]
    return [_make_filter(column, "eq", raw)]


def _make_filter(column: str, op: str, raw_value: Any) -> Filter:
    if op not in OPS:
        raise ValueError(f"unknown filter op {op!r}")
    kind = CATALOG[column]
    allowed_ops = HYBRID_FILTER_ALLOWED_OPS[kind]
    if op not in allowed_ops:
        allowed = ", ".join(sorted(allowed_ops))
        raise ValueError(
            f"filter op {op!r} is not allowed for {kind.value} column {column!r}; "
            f"allowed: {allowed}"
        )
    if op == "in":
        if not isinstance(raw_value, list) or not raw_value:
            raise ValueError(f"filter {column!r}.in needs a non-empty array")
        if len(raw_value) > HYBRID_FILTER_IN_MAX_ITEMS:
            raise ValueError(
                f"filter {column!r}.in accepts at most {HYBRID_FILTER_IN_MAX_ITEMS} items"
            )
        return Filter(column, op, [_coerce_filter_atom(column, item) for item in raw_value])
    if op == "between":
        if not isinstance(raw_value, list) or len(raw_value) != 2:
            raise ValueError(f"filter {column!r}.between needs [start, end]")
        return Filter(
            column,
            op,
            (
                _coerce_filter_atom(column, raw_value[0]),
                _coerce_filter_atom(column, raw_value[1]),
            ),
        )
    if isinstance(raw_value, list):
        if len(raw_value) != 1:
            raise ValueError(f"filter {column!r}.{op} needs one value")
        raw_value = raw_value[0]
    return Filter(column, op, _coerce_filter_atom(column, raw_value))


def _filter_debug_payload(filters: list[Filter]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for item in filters:
        value = item.value
        if isinstance(value, tuple):
            value = [_json_safe(v) for v in value]
        elif isinstance(value, list):
            value = [_json_safe(v) for v in value]
        else:
            value = _json_safe(value)
        out.append({"column": item.column, "op": item.op, "value": value})
    return out


def _round_float(value: Any, digits: int = 4) -> float | None:
    if value is None:
        return None
    try:
        return round(float(value), digits)
    except (TypeError, ValueError):
        return None


def _hybrid_request_payload(
    req: HybridSearchRequest,
    *,
    normalized_filters: list[dict[str, Any]],
    filters_valid: bool,
) -> dict[str, Any]:
    filters_payload: dict[str, Any] = {"items": normalized_filters}
    if not filters_valid:
        filters_payload.update({"valid": False, "omitted_raw": True})
    return {
        "query": req.query,
        "field": req.field,
        "k": req.k,
        "aliases": _clean_aliases(req.aliases),
        "filters": filters_payload,
    }


def _load_hybrid_hit_details(conn: Any, recall_numbers: list[str]) -> dict[str, dict[str, Any]]:
    if not recall_numbers:
        return {}
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT recall_number, status, recalling_firm, classification,
                   product_description, reason_for_recall, report_date
            FROM drug_enforcement
            WHERE recall_number = ANY(%s)
            """,
            [recall_numbers],
        )
        return {
            row[0]: {
                "recall_number": row[0],
                "status": row[1],
                "recalling_firm": row[2],
                "classification": row[3],
                "product_description": row[4],
                "reason_for_recall": row[5],
                "report_date": _json_safe(row[6]),
            }
            for row in cur.fetchall()
        }


def _serialize_hybrid_hit(hit: retrieval.Hit, rank: int,
                          details: dict[str, Any]) -> dict[str, Any]:
    recall_number = hit.recall_number
    detail_url = recall_detail_url(recall_number)
    source_url = recall_verification_url(recall_number)
    row = {
        "rank": hit.fused_rank or rank,
        "recall_number": recall_number,
        "field": hit.field,
        "retrieval_mode": hit.retrieval_mode,
        "score_kind": hit.score_kind,
        "retrieval_score": _round_float(hit.retrieval_score),
        "rrf_score": _round_float(hit.rrf_score, 6),
        "vector_rank": hit.vector_rank,
        "vector_distance": _round_float(hit.vector_distance),
        "vector_similarity": _round_float(hit.vector_similarity),
        "fts_rank": hit.fts_rank,
        "fts_score": _round_float(hit.fts_score),
        "classification": details.get("classification") or hit.classification,
        "status": details.get("status"),
        "recalling_firm": details.get("recalling_firm") or hit.recalling_firm,
        "product_description": details.get("product_description"),
        "reason_for_recall": details.get("reason_for_recall"),
        "report_date": details.get("report_date"),
        "content": hit.content,
    }
    if detail_url:
        row["url"] = detail_url
        row["evidence_link"] = detail_url
    if source_url:
        row["source_url"] = source_url
    return row


def _hybrid_response_metadata(
    req: HybridSearchRequest,
    hits: retrieval.SearchResult,
    *,
    aliases: list[str],
    rows: list[dict[str, Any]],
    embed_config: llm.EmbeddingConfig,
    embedding_available: bool,
) -> dict[str, Any]:
    return {
        "query": req.query,
        "field": req.field,
        "k": req.k,
        "aliases": aliases,
        "fts_queries": list(getattr(hits, "fts_queries", [])),
        "retrieval_mode": getattr(hits, "retrieval_mode", "hybrid"),
        "fallback_reason": getattr(hits, "embedding_fallback_reason", None),
        "embedding_provider": embed_config.provider,
        "embedding_model": embed_config.model,
        "embedding_available": embedding_available,
        "vector_hit_count": int(getattr(hits, "vector_hit_count", 0)),
        "fts_hit_count": int(getattr(hits, "fts_hit_count", 0)),
        "fused_hit_count": int(getattr(hits, "fused_hit_count", len(hits))),
        "returned_count": len(rows),
        "top_recall_numbers": [row["recall_number"] for row in rows[:10]],
    }


def _log_hybrid_search(
    req: HybridSearchRequest,
    *,
    filters: list[Filter],
    hits: retrieval.SearchResult | None,
    timings_ms: dict[str, Any],
    response_meta: dict[str, Any],
    error_type: str | None = None,
    error_message: str | None = None,
) -> int:
    engine = _require_engine()
    retrieval_mode = (
        getattr(hits, "retrieval_mode", None)
        if hits is not None else response_meta.get("retrieval_mode")
    ) or ("error" if error_type else "hybrid")
    fallback_reason = (
        getattr(hits, "embedding_fallback_reason", None)
        if hits is not None else response_meta.get("fallback_reason")
    )
    top_recall_numbers = list(response_meta.get("top_recall_numbers") or [])
    normalized_filters = _filter_debug_payload(filters)
    filters_valid = not (error_type == "ValueError" and bool(req.filters) and not filters)
    return _require_hybrid_search_logger().write(HybridSearchLogEntry(
        query=req.query,
        field=req.field,
        k=req.k,
        filters={"items": normalized_filters},
        embedding_provider=engine.embed_config.provider,
        embedding_model=engine.embed_config.model,
        retrieval_mode=str(retrieval_mode),
        fallback_reason=fallback_reason,
        vector_hit_count=int(getattr(hits, "vector_hit_count", 0)) if hits is not None else 0,
        fts_hit_count=int(getattr(hits, "fts_hit_count", 0)) if hits is not None else 0,
        fused_hit_count=int(getattr(hits, "fused_hit_count", 0)) if hits is not None else 0,
        top_recall_numbers=top_recall_numbers,
        timings_ms=timings_ms,
        request=_hybrid_request_payload(
            req,
            normalized_filters=normalized_filters,
            filters_valid=filters_valid,
        ),
        response_metadata=response_meta,
        error_type=error_type,
        error_message=error_message,
    ))


def _clean_title(raw: str) -> str:
    title = " ".join(raw.replace("\n", " ").split()).strip(" \"'`")
    for prefix in ("Title:", "title:"):
        if title.startswith(prefix):
            title = title[len(prefix):].strip(" \"'`")
    while title.startswith(("-", "*")):
        title = title[1:].lstrip()
    if len(title) > 2 and title[0].isdigit() and title[1] == ".":
        title = title[2:].lstrip()

    words = title.split()
    title = " ".join(words[:TITLE_WORD_LIMIT])
    if len(title) > TITLE_MAX_CHARS:
        title = f"{title[:TITLE_MAX_CHARS - 3].rstrip()}..."
    return title.rstrip(" .,:;-")


def _fallback_title(question: str) -> str:
    words = re.findall(r"[A-Za-z0-9]+(?:[-'][A-Za-z0-9]+)?", question)
    kept: list[str] = []
    for word in words:
        if word.casefold() in TITLE_FALLBACK_STOPWORDS:
            continue
        kept.append(_title_word(word))
        if len(kept) >= TITLE_WORD_LIMIT:
            break
    if not kept:
        kept = ["FDA", "Recall", "Question"]
    return _clean_title(" ".join(kept)) or "FDA Recall Question"


def _title_word(word: str) -> str:
    if word.isupper() or word.isdigit() or re.fullmatch(r"[IVXLCDM]+", word):
        return word
    return word[:1].upper() + word[1:]


def _generate_title(req: TitleRequest, engine: NLEngine) -> str:
    if engine.title_client is None:
        raise engine.title_error or llm.ProviderMissingKeyError(
            "title client is not configured",
            provider=engine.title_config.provider,
            model=engine.title_config.model,
            operation="chat_completion",
        )
    content = llm.chat_completion_text(
        engine.title_client,
        engine.title_config,
        [
            {
                "role": "system",
                "content": (
                    "Generate a concise title for a chat conversation about FDA drug recalls. "
                    "Use only the user's first question. Return only the title, with no quotes, "
                    "no period, and no more than six words."
                ),
            },
            {"role": "user", "content": req.question},
        ],
        temperature=0,
        max_tokens=TITLE_MAX_TOKENS,
    )
    title = _clean_title(content)
    if not title:
        raise ValueError("empty title response")
    return title


def _request_payload(req: AskRequest) -> dict[str, Any]:
    return req.model_dump(mode="json")


def _log_success(req: AskRequest, ans: Answer, payload: dict[str, Any],
                 *, start: float, model: str) -> None:
    spec = ans.spec.model_dump(mode="json", exclude_none=True) if ans.spec is not None else None
    provider = _engine.chat_config.provider if _engine is not None else None
    metadata = response_metadata(payload, model=model, provider=provider)
    data_kind = metadata.get("data_kind")
    control = ans.control.as_dict() if ans.control is not None else None
    answer_intent = ans.metadata.get("intent") or (
        ans.spec.intent.value if ans.spec is not None else None
    )
    if control is not None and control["route"] != "in_domain":
        route = control["route"]
    elif ans.spec is not None and ans.spec.intent is Intent.explain_taxonomy_node:
        route = "explanation"
    elif isinstance(ans.result, MultiSectionResult):
        route = "sql"
    elif isinstance(ans.result, RawFirmExposureLeaderboard):
        route = "sql"
    elif ans.spec is not None and ans.spec.semantic_query:
        route = "semantic"
    else:
        route = "sql"
    if route == "semantic" and _engine is not None:
        metadata.update({
            "embedding_provider": _engine.embed_config.provider,
            "embedding_model": _engine.embed_config.model,
            "embedding_available": _engine.embedding_error is None,
        })
    decision: dict[str, Any] = {
        "route": route,
        "intent": answer_intent or route,
        "data_kind": data_kind,
        "filter_count": len(ans.spec.filters) if ans.spec is not None else 0,
        "taxonomy_node_id": ans.spec.taxonomy_node_id if ans.spec is not None else None,
        "control": control,
    }
    if ans.metadata.get("sections"):
        decision["sections"] = ans.metadata["sections"]
    if ans.metadata.get("sub_specs"):
        decision["sub_specs"] = ans.metadata["sub_specs"]
    if isinstance(ans.result, RawFirmExposureLeaderboard):
        decision.update({
            "formula_version": ans.result.formula_version,
            "formula": ans.result.formula,
            "scope": ans.result.scope,
            "exposure_metric": ans.result.metric,
        })
    if route == "semantic" and _engine is not None:
        decision["embedding_provider"] = _engine.embed_config.provider
        decision["embedding_model"] = _engine.embed_config.model
        decision["embedding_available"] = _engine.embedding_error is None
    _require_query_logger().write(QueryLogEntry(
        route="/ask",
        question=req.question,
        request=_request_payload(req),
        status_code=200,
        ok=True,
        latency_ms=_elapsed_ms(start),
        query_intent=answer_intent or route,
        data_kind=str(data_kind) if data_kind else None,
        semantic_query=ans.spec.semantic_query if ans.spec is not None else None,
        query_spec=spec,
        decision=decision,
        response_metadata=metadata,
    ))


def _error_detail(exc: Exception, status_code: int) -> dict[str, Any]:
    detail: dict[str, Any] = {"status_code": status_code}
    if isinstance(exc, llm.ProviderError):
        detail.update({
            "provider": exc.provider,
            "model": exc.model,
            "operation": exc.operation,
            "retryable": exc.retryable,
        })
    return detail


def _log_error(req: AskRequest, exc: Exception, *, start: float, status_code: int) -> None:
    _require_query_logger().write(QueryLogEntry(
        route="/ask",
        question=req.question,
        request=_request_payload(req),
        status_code=status_code,
        ok=False,
        latency_ms=_elapsed_ms(start),
        decision={"route": "error"},
        error_type=type(exc).__name__,
        error_message=str(exc),
        error_detail=_error_detail(exc, status_code),
    ))


def _load_recall_record(recall_number: str) -> dict[str, Any] | None:
    """Load one recall detail record from the local openFDA-backed table."""
    engine = _require_engine()
    columns = ", ".join(RECALL_DETAIL_COLUMNS)
    with RecallAnalytics(engine.dsn) as analytics:
        with analytics.conn.cursor() as cur:
            cur.execute(
                f"SELECT {columns} FROM drug_enforcement WHERE recall_number = %s LIMIT 1",
                [recall_number],
            )
            row = cur.fetchone()
    if row is None:
        return None
    return dict(zip(RECALL_DETAIL_COLUMNS, row))


def _is_missing(value: Any) -> bool:
    return value is None or value == "" or value == [] or value == {}


def _record_value(record: dict[str, Any], key: str) -> Any:
    value = record.get(key)
    raw = record.get("raw")
    if _is_missing(value) and isinstance(raw, dict):
        value = raw.get(key)
    return value


def _detail_text(value: Any) -> str:
    value = _json_safe(value)
    if _is_missing(value):
        return ""
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True)
    return str(value).strip()


def _detail_html_text(value: Any) -> str:
    return escape(_detail_text(value)).replace("\n", "<br>")


def _detail_field_html(record: dict[str, Any], key: str, label: str) -> str:
    text = _detail_html_text(_record_value(record, key))
    if not text:
        return ""
    extra = " detail-field-long" if key in RECALL_DETAIL_LONG_FIELDS else ""
    return (
        f'<div class="detail-field{extra}">'
        f"<dt>{escape(label)}</dt>"
        f"<dd>{text}</dd>"
        "</div>"
    )


def _detail_badge_html(record: dict[str, Any], key: str, label: str) -> str:
    text = _detail_html_text(_record_value(record, key))
    if not text:
        return ""
    return (
        '<span class="badge detail-badge">'
        f"<span>{escape(label)}:</span> {text}"
        "</span>"
    )


def _detail_section_html(record: dict[str, Any], title: str, fields: list[tuple[str, str]]) -> str:
    field_html = "".join(
        _detail_field_html(record, key, label)
        for key, label in fields
    )
    if not field_html:
        return ""
    return (
        '<section class="detail-card">'
        f"<h2>{escape(title)}</h2>"
        f'<dl class="detail-grid">{field_html}</dl>'
        "</section>"
    )


def _source_links_html(recall_number: str) -> str:
    raw_url = recall_verification_url(recall_number) or OPENFDA_DRUG_ENFORCEMENT_URL
    return (
        '<section class="detail-card source-card">'
        "<h2>Source and verification</h2>"
        "<p>"
        "This page renders the local FDAgent copy of the public openFDA "
        "<code>drug/enforcement</code> record. Use the source links below to audit the "
        "original API response and openFDA data terms."
        "</p>"
        '<div class="detail-source-links">'
        f'<a href="{escape(raw_url, quote=True)}" target="_blank" rel="noopener noreferrer">'
        "Raw openFDA API result</a>"
        f'<a href="{escape(OPENFDA_TERMS_URL, quote=True)}" target="_blank" rel="noopener noreferrer">'
        "openFDA terms</a>"
        f'<a href="{escape(OPENFDA_DISCLAIMER_URL, quote=True)}" target="_blank" rel="noopener noreferrer">'
        "openFDA disclaimer</a>"
        f'<a href="{escape(OPENFDA_LICENSE_URL, quote=True)}" target="_blank" rel="noopener noreferrer">'
        "openFDA license</a>"
        "</div>"
        "</section>"
    )


def _html_page(title: str, body_html: str, *, status_code: int = 200) -> HTMLResponse:
    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>{escape(title)} - FDAgent</title>
  <link rel="stylesheet" href="/static/styles.css" />
</head>
<body class="detail-page">
  {body_html}
</body>
</html>
"""
    return HTMLResponse(html, status_code=status_code)


def _recall_not_found_page(recall_number: str, message: str) -> HTMLResponse:
    normalized = _normalized_recall_number(recall_number)
    source = _source_links_html(normalized) if normalized else ""
    body = (
        '<main class="detail-shell">'
        '<a class="detail-back-link" href="/">← Back to FDAgent</a>'
        '<section class="detail-card detail-hero">'
        "<p class=\"detail-kicker\">Recall detail</p>"
        "<h1>Recall not found</h1>"
        f"<p>{escape(message)}</p>"
        '<p class="muted">FDAgent only renders syntactically valid recall numbers that exist '
        "in the local openFDA drug-enforcement table; it does not fabricate missing records.</p>"
        "</section>"
        f"{source}"
        "</main>"
    )
    return _html_page("Recall not found", body, status_code=404)


def _recall_detail_page(record: dict[str, Any]) -> HTMLResponse:
    recall_number = _detail_text(_record_value(record, "recall_number"))
    missing = [
        label for key, label in RECALL_DETAIL_CORE_FIELDS
        if not _detail_text(_record_value(record, key))
    ]
    missing_html = ""
    if missing:
        missing_html = (
            '<div class="detail-warning" role="note">'
            "Some expected FDA fields are absent from this source record: "
            f"{escape(', '.join(missing))}."
            "</div>"
        )
    sections = "".join(
        _detail_section_html(record, title, fields)
        for title, fields in RECALL_DETAIL_SECTIONS
    )
    badges = "".join([
        _detail_badge_html(record, "classification", "Classification"),
        _detail_badge_html(record, "status", "Status"),
        _detail_badge_html(record, "product_type", "Product type"),
    ])
    body = (
        '<main class="detail-shell">'
        '<a class="detail-back-link" href="/">← Back to FDAgent</a>'
        '<section class="detail-card detail-hero">'
        "<p class=\"detail-kicker\">FDA drug enforcement recall</p>"
        f"<h1>{escape(recall_number)}</h1>"
        f'<div class="badge-row">{badges}</div>'
        f"{missing_html}"
        "</section>"
        f"{sections}"
        f"{_source_links_html(recall_number)}"
        "</main>"
    )
    return _html_page(f"Recall {recall_number}", body)


@app.get("/health")
def health() -> dict[str, Any]:
    provider_status = _engine.provider_status() if _engine is not None else llm.provider_status()
    return {
        "status": "ok",
        "engine": "ready" if _engine is not None else "starting",
        **provider_status,
    }


@app.get("/hybrid-search")
def hybrid_search_page() -> FileResponse:
    return FileResponse(WEB_DIR / "hybrid-search.html")


@app.post("/hybrid-search")
def hybrid_search_endpoint(req: HybridSearchRequest) -> dict[str, Any]:
    start = perf_counter()
    engine = _require_engine()
    filters: list[Filter] = []
    hits: retrieval.SearchResult | None = None
    response_meta: dict[str, Any] = {}
    timings_ms: dict[str, Any] = {}
    try:
        aliases = _clean_aliases(req.aliases)
        filters = _filters_for_request(req.filters)
        with RecallAnalytics(engine.dsn) as analytics:
            hits = retrieval.search(
                analytics.conn,
                engine.embed_client,
                req.query.strip(),
                k=req.k,
                field=req.field,
                filters=filters,
                embed_config=engine.embed_config,
                embedding_error=engine.embedding_error,
                fts_queries=aliases,
            )
            details_by_recall = _load_hybrid_hit_details(
                analytics.conn,
                [hit.recall_number for hit in hits],
            )
        timings_ms = dict(getattr(hits, "timings_ms", {}))
        rows = [
            _serialize_hybrid_hit(hit, rank, details_by_recall.get(hit.recall_number, {}))
            for rank, hit in enumerate(hits, 1)
        ]
        response_meta = _hybrid_response_metadata(
            req,
            hits,
            aliases=aliases,
            rows=rows,
            embed_config=engine.embed_config,
            embedding_available=engine.embedding_error is None,
        )
        timings_ms["api_total"] = _elapsed_ms(start)
        log_started = perf_counter()
        log_id = _log_hybrid_search(
            req,
            filters=filters,
            hits=hits,
            timings_ms=timings_ms,
            response_meta=response_meta,
        )
        timings_ms["log_write"] = _elapsed_ms(log_started)
        response_meta["log_id"] = log_id
        payload = {
            "query": req.query,
            "field": req.field,
            "k": req.k,
            "filters": _filter_debug_payload(filters),
            "aliases": aliases,
            "retrieval_mode": getattr(hits, "retrieval_mode", "hybrid"),
            "embedding_provider": engine.embed_config.provider,
            "embedding_model": engine.embed_config.model,
            "embedding_available": engine.embedding_error is None,
            "fallback_reason": getattr(hits, "embedding_fallback_reason", None),
            "timings_ms": timings_ms,
            "counts": {
                "vector_hit_count": int(getattr(hits, "vector_hit_count", 0)),
                "fts_hit_count": int(getattr(hits, "fts_hit_count", 0)),
                "fused_hit_count": int(getattr(hits, "fused_hit_count", len(hits))),
                "returned_count": len(rows),
            },
            "fts_queries": list(getattr(hits, "fts_queries", [])),
            "top_recall_numbers": [row["recall_number"] for row in rows[:10]],
            "log_id": log_id,
            "rows": rows,
        }
        return payload
    except ValueError as exc:
        timings_ms["api_total"] = _elapsed_ms(start)
        response_meta = {
            **response_meta,
            "retrieval_mode": "error",
            "fallback_reason": None,
            "top_recall_numbers": [],
        }
        _log_hybrid_search(
            req,
            filters=filters,
            hits=hits,
            timings_ms=timings_ms,
            response_meta=response_meta,
            error_type=type(exc).__name__,
            error_message=str(exc),
        )
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except llm.ProviderError as exc:
        timings_ms["api_total"] = _elapsed_ms(start)
        response_meta = {
            **response_meta,
            "retrieval_mode": "error",
            "fallback_reason": llm.provider_error_summary(exc),
            "top_recall_numbers": [],
        }
        _log_hybrid_search(
            req,
            filters=filters,
            hits=hits,
            timings_ms=timings_ms,
            response_meta=response_meta,
            error_type=type(exc).__name__,
            error_message=llm.public_error_detail(exc),
        )
        raise HTTPException(status_code=llm.http_status(exc),
                            detail=llm.public_error_detail(exc)) from exc
    except Exception as exc:  # noqa: BLE001 — log server-side, return a safe message
        traceback.print_exc()
        timings_ms["api_total"] = _elapsed_ms(start)
        response_meta = {
            **response_meta,
            "retrieval_mode": "error",
            "fallback_reason": None,
            "top_recall_numbers": [],
        }
        _log_hybrid_search(
            req,
            filters=filters,
            hits=hits,
            timings_ms=timings_ms,
            response_meta=response_meta,
            error_type=type(exc).__name__,
            error_message=str(exc),
        )
        raise HTTPException(status_code=500, detail="could not run hybrid search") from exc


@app.post("/title", response_model=TitleResponse)
def title_endpoint(req: TitleRequest) -> TitleResponse:
    engine = _require_engine()
    try:
        return TitleResponse(title=_generate_title(req, engine))
    except llm.ProviderError:
        return TitleResponse(title=_fallback_title(req.question))
    except (IndexError, AttributeError, ValueError) as exc:
        return TitleResponse(title=_fallback_title(req.question))


@app.post("/ask")
def ask_endpoint(req: AskRequest) -> dict[str, Any]:
    start = perf_counter()
    if _engine is None:
        exc = RuntimeError("engine not ready")
        _log_error(req, exc, start=start, status_code=503)
        raise HTTPException(status_code=503, detail="engine not ready")
    try:
        ans = _engine.ask(req.question)
        payload = serialize_answer(ans)
    except llm.ProviderError as exc:
        status_code = llm.http_status(exc)
        _log_error(req, exc, start=start, status_code=status_code)
        raise HTTPException(status_code=status_code, detail=llm.public_error_detail(exc)) from exc
    except validation.SemanticValidationError as exc:
        traceback.print_exc()
        _log_error(req, exc, start=start, status_code=502)
        raise HTTPException(
            status_code=502,
            detail=f"semantic validation failed ({type(exc).__name__})",
        ) from exc
    except ValueError as exc:
        traceback.print_exc()
        _log_error(req, exc, start=start, status_code=400)
        raise HTTPException(status_code=400,
                            detail=f"could not answer this question ({type(exc).__name__})")
    except Exception as exc:  # noqa: BLE001 — log server-side, return a safe message
        traceback.print_exc()
        _log_error(req, exc, start=start, status_code=500)
        raise HTTPException(status_code=500,
                            detail=f"could not answer this question ({type(exc).__name__})")
    _log_success(req, ans, payload, start=start, model=_engine.model)
    return payload


@app.get("/recalls/{recall_number}", response_class=HTMLResponse)
def recall_detail(recall_number: str) -> HTMLResponse:
    normalized = _normalized_recall_number(recall_number)
    if normalized is None:
        return _recall_not_found_page(
            recall_number,
            f"{recall_number!r} is not a valid FDA drug recall number.",
        )
    try:
        record = _load_recall_record(normalized)
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001 — log server-side, return a safe message
        traceback.print_exc()
        raise HTTPException(status_code=500, detail="could not load recall detail") from exc
    if record is None:
        return _recall_not_found_page(
            normalized,
            f"No FDAgent/openFDA drug-enforcement record was found for {normalized}.",
        )
    return _recall_detail_page(record)


@app.get("/")
def index() -> FileResponse:
    return FileResponse(WEB_DIR / "index.html")
