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

import os
import re
import sys
import traceback
from contextlib import asynccontextmanager
from datetime import date, datetime
from pathlib import Path
from time import perf_counter
from typing import Any, Optional

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))  # allow `import nl_query` under uvicorn
import llm  # noqa: E402
import agent_control  # noqa: E402
import validation  # noqa: E402
from nl_query import Answer, Intent, MultiSectionResult, NLEngine, TaxonomyExplanation  # noqa: E402
from observability import QueryLogEntry, QueryLogger, response_metadata  # noqa: E402

WEB_DIR = Path(__file__).resolve().parent.parent / "web"
TITLE_WORD_LIMIT = 6
TITLE_MAX_CHARS = 44
TITLE_MAX_TOKENS = 64
TITLE_FALLBACK_STOPWORDS = {
    "a", "about", "are", "been", "did", "do", "does", "for", "give", "has", "have",
    "how", "in", "is", "many", "me", "of", "on", "show", "tell", "the", "there",
    "to", "was", "were", "what", "when", "which", "who", "with",
}

# Warmed at startup, reused across requests (see lifespan).
_engine: Optional[NLEngine] = None
_query_logger: Optional[QueryLogger] = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Warm provider clients + cached schema context once, before serving traffic."""
    global _engine, _query_logger
    _engine = NLEngine()
    _query_logger = QueryLogger(_engine.dsn)
    yield
    _engine = None
    _query_logger = None


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


def _json_safe(v: Any) -> Any:
    """Make a single SQL value JSON-serializable (dates -> ISO strings)."""
    if isinstance(v, (date, datetime)):
        return v.isoformat()
    return v


def _serialize_group(g: Any) -> dict[str, Any]:
    item = {
        "value": _json_safe(g.value),
        "count": int(g.count),
        "evidence": list(g.evidence),
    }
    label = getattr(g, "label", None)
    if label:
        item["label"] = _json_safe(label)
    metadata = getattr(g, "metadata", None)
    if isinstance(metadata, dict):
        for key, value in metadata.items():
            if value is not None:
                item[key] = _json_safe(value)
    return item


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
                {
                    "recall_number": item.get("recall_number"),
                    "classification": item.get("classification"),
                    "reason_for_recall": item.get("reason_for_recall"),
                }
                for item in ans.result.examples
            ],
            "source": "taxonomy",
        }
    elif isinstance(ans.result, MultiSectionResult):
        payload["data"] = _serialize_multi_section(ans.result)
    elif isinstance(ans.result, validation.SemanticCountResult):
        result = ans.result
        accepted = [item for item in result.validations if item.accepted]
        evidence_items = [
            {
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
            }
            for item in accepted
        ]
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
            "evidence_items": evidence_items,
        }
        if result.group_by:
            payload["data"] = {
                **base_data,
                "kind": "semantic_distribution",
                "dimension": result.group_by,
                "items": [
                    {"value": _json_safe(g.value), "count": g.count, "evidence": list(g.evidence)}
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
        payload["data"] = {
            "kind": "retrieval",
            "query": spec.semantic_query,
            "retrieval_mode": retrieval_mode,
            "embedding_fallback_reason": fallback_reason,
            "degraded": bool(ans.metadata.get("degraded")),
            "items": [
                {"recall_number": h.recall_number, "field": h.field,
                 "retrieval_mode": h.retrieval_mode,
                 "score_kind": h.score_kind,
                 "similarity": round(h.similarity, 3),
                 "retrieval_score": round(h.retrieval_score, 3),
                 "rrf_score": round(h.rrf_score, 4),
                 "content": h.content,
                 "recalling_firm": h.recalling_firm, "classification": h.classification}
                for h in ans.result
            ],
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
        payload["data"] = {
            "kind": "rows",
            "rows": [{k: _json_safe(v) for k, v in row.items()} for row in ans.result],
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


def _require_engine() -> NLEngine:
    if _engine is None:
        raise HTTPException(status_code=503, detail="engine not ready")
    return _engine


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
    elif ans.spec is not None and ans.spec.semantic_query:
        route = "semantic"
    else:
        route = "sql"
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


@app.get("/health")
def health() -> dict[str, Any]:
    provider_status = _engine.provider_status() if _engine is not None else llm.provider_status()
    return {
        "status": "ok",
        "engine": "ready" if _engine is not None else "starting",
        **provider_status,
    }


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


@app.get("/")
def index() -> FileResponse:
    return FileResponse(WEB_DIR / "index.html")
