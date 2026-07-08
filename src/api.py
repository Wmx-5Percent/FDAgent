"""FastAPI service exposing the deterministic NL->SQL analytics engine (Path 1, serving half).

A single ``POST /ask`` turns a natural-language question into a validated ``QuerySpec`` (via
``nl_query.NLEngine``), runs it through the SQL analytics engine, and returns a chart-friendly,
evidence-backed JSON payload that the static page at ``web/index.html`` renders. Every number
still comes from SQL — the model only picks the query shape. The OpenAI client and schema
context are warmed ONCE at startup and reused across requests.

Run (from the repo root):
    .venv/bin/python -m uvicorn src.api:app --reload
    # then open http://127.0.0.1:8000/
"""
from __future__ import annotations

import os
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
from openai import OpenAIError
from pydantic import BaseModel, Field

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))  # allow `import nl_query` under uvicorn
import validation  # noqa: E402
from nl_query import Answer, Intent, NLEngine  # noqa: E402
from observability import QueryLogEntry, QueryLogger, response_metadata  # noqa: E402

WEB_DIR = Path(__file__).resolve().parent.parent / "web"
TITLE_WORD_LIMIT = 6
TITLE_MAX_CHARS = 44

# Warmed at startup, reused across requests (see lifespan).
_engine: Optional[NLEngine] = None
_query_logger: Optional[QueryLogger] = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Warm the OpenAI client + cached schema context once, before serving traffic."""
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


def serialize_answer(ans: Answer) -> dict[str, Any]:
    """Shape an :class:`Answer` into a stable, chart-friendly response.

    ``data.kind`` tells the UI how to render: ``scalar`` (one number), ``distribution``
    (bar chart), ``series`` (line chart), or ``rows`` (table). Evidence ``recall_number``s
    ride along so every figure is traceable back to source records.
    """
    spec = ans.spec
    payload: dict[str, Any] = {
        "question": ans.question,
        "intent": spec.intent.value,
        "spec": spec.model_dump(exclude_none=True),
        "summary": ans.summary,
    }
    if isinstance(ans.result, validation.SemanticCountResult):
        result = ans.result
        accepted = [item for item in result.validations if item.accepted]
        evidence_items = [
            {
                "recall_number": item.hit.recall_number,
                "field": item.hit.field,
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
    elif spec.semantic_query:  # concept query -> ranked semantic hits
        payload["data"] = {
            "kind": "retrieval",
            "query": spec.semantic_query,
            "items": [
                {"recall_number": h.recall_number, "field": h.field,
                 "similarity": round(h.similarity, 3),
                 "retrieval_score": round(h.retrieval_score, 3),
                 "rrf_score": round(h.rrf_score, 4),
                 "content": h.content,
                 "recalling_firm": h.recalling_firm, "classification": h.classification}
                for h in ans.result
            ],
        }
    elif spec.intent is Intent.count_total:
        payload["data"] = {"kind": "scalar", "value": int(ans.result)}
    elif spec.intent is Intent.count_by:
        payload["data"] = {
            "kind": "distribution",
            "dimension": spec.group_by,
            "items": [
                {"value": _json_safe(g.value), "count": g.count, "evidence": list(g.evidence)}
                for g in ans.result
            ],
        }
    elif spec.intent is Intent.trend:
        payload["data"] = {
            "kind": "series",
            "grain": spec.grain or "year",
            "points": [{"period": _json_safe(p), "count": n} for p, n in ans.result],
        }
    else:  # sample
        payload["data"] = {
            "kind": "rows",
            "rows": [{k: _json_safe(v) for k, v in row.items()} for row in ans.result],
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


def _has_openai_credentials(engine: NLEngine) -> bool:
    return bool(os.environ.get("OPENAI_API_KEY") or getattr(engine.client, "api_key", None))


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


def _generate_title(req: TitleRequest, engine: NLEngine) -> str:
    completion = engine.client.chat.completions.create(
        model=engine.model,
        temperature=0,
        max_tokens=24,
        messages=[
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
    )
    content = completion.choices[0].message.content or ""
    title = _clean_title(content)
    if not title:
        raise ValueError("empty title response")
    return title


def _request_payload(req: AskRequest) -> dict[str, Any]:
    return req.model_dump(mode="json")


def _log_success(req: AskRequest, ans: Answer, payload: dict[str, Any],
                 *, start: float, model: str) -> None:
    spec = ans.spec.model_dump(mode="json", exclude_none=True)
    metadata = response_metadata(payload, model=model)
    data_kind = metadata.get("data_kind")
    _require_query_logger().write(QueryLogEntry(
        route="/ask",
        question=req.question,
        request=_request_payload(req),
        status_code=200,
        ok=True,
        latency_ms=_elapsed_ms(start),
        query_intent=ans.spec.intent.value,
        data_kind=str(data_kind) if data_kind else None,
        semantic_query=ans.spec.semantic_query,
        query_spec=spec,
        decision={
            "route": "semantic" if ans.spec.semantic_query else "sql",
            "intent": ans.spec.intent.value,
            "data_kind": data_kind,
            "filter_count": len(ans.spec.filters),
        },
        response_metadata=metadata,
    ))


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
        error_detail={"status_code": status_code},
    ))


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok", "engine": "ready" if _engine is not None else "starting"}


@app.post("/title", response_model=TitleResponse)
def title_endpoint(req: TitleRequest) -> TitleResponse:
    engine = _require_engine()
    if not _has_openai_credentials(engine):
        raise HTTPException(
            status_code=503,
            detail="title generation unavailable: OpenAI credentials are not configured",
        )
    try:
        return TitleResponse(title=_generate_title(req, engine))
    except OpenAIError as exc:
        traceback.print_exc()
        raise HTTPException(
            status_code=502,
            detail=f"could not generate title ({type(exc).__name__})",
        ) from exc
    except (IndexError, AttributeError, ValueError) as exc:
        traceback.print_exc()
        raise HTTPException(
            status_code=502,
            detail=f"could not generate title ({type(exc).__name__})",
        ) from exc


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
    except Exception as exc:  # noqa: BLE001 — log server-side, return a safe message
        traceback.print_exc()
        _log_error(req, exc, start=start, status_code=400)
        raise HTTPException(status_code=400,
                            detail=f"could not answer this question ({type(exc).__name__})")
    _log_success(req, ans, payload, start=start, model=_engine.model)
    return payload


@app.get("/")
def index() -> FileResponse:
    return FileResponse(WEB_DIR / "index.html")
