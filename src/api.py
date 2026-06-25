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
from typing import Any, Optional

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))  # allow `import nl_query` under uvicorn
from nl_query import Answer, Intent, NLEngine  # noqa: E402

WEB_DIR = Path(__file__).resolve().parent.parent / "web"

# Warmed at startup, reused across requests (see lifespan).
_engine: Optional[NLEngine] = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Warm the OpenAI client + cached schema context once, before serving traffic."""
    global _engine
    _engine = NLEngine()
    yield
    _engine = None


app = FastAPI(
    title="FDAgent — Drug-Recall Intelligence",
    version="0.1.0",
    summary="Natural-language questions over U.S. FDA drug-recall enforcement reports; "
            "every figure is computed in SQL and carries the recall numbers that back it.",
    lifespan=lifespan,
)


class AskRequest(BaseModel):
    question: str = Field(min_length=1, max_length=500,
                          description="A natural-language question about FDA drug recalls.")


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
    if spec.intent is Intent.count_total:
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


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok", "engine": "ready" if _engine is not None else "starting"}


@app.post("/ask")
def ask_endpoint(req: AskRequest) -> dict[str, Any]:
    if _engine is None:
        raise HTTPException(status_code=503, detail="engine not ready")
    try:
        ans = _engine.ask(req.question)
    except Exception as exc:  # noqa: BLE001 — log server-side, return a safe message
        traceback.print_exc()
        raise HTTPException(status_code=400,
                            detail=f"could not answer this question ({type(exc).__name__})")
    return serialize_answer(ans)


@app.get("/")
def index() -> FileResponse:
    return FileResponse(WEB_DIR / "index.html")
