#!/usr/bin/env python3
"""Run local golden evals for /ask routing and retrieval recall@k."""
from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping

REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_DIR = REPO_ROOT / "src"
sys.path.insert(0, str(SRC_DIR))

import psycopg  # noqa: E402

import llm  # noqa: E402
import retrieval  # noqa: E402
from api import serialize_answer  # noqa: E402
from nl_query import MODEL, NLEngine  # noqa: E402

DEFAULT_DSN = os.environ.get("DATABASE_URL", "postgresql://localhost:5432/fda")
DEFAULT_GOLDEN = REPO_ROOT / "evals" / "golden" / "v1.json"


@dataclass(frozen=True)
class EvalResult:
    case_id: str
    passed: bool
    detail: str


class EvalFailure(AssertionError):
    pass


def _plain(value: Any) -> Any:
    return getattr(value, "value", value)


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise EvalFailure(message)


def _load_golden(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as fh:
        golden = json.load(fh)
    _require("version" in golden and isinstance(golden.get("cases"), list),
             "golden set must include version and cases")
    return golden


def _post_ask(base_url: str, question: str, timeout: float) -> dict[str, Any]:
    url = base_url.rstrip("/") + "/ask"
    body = json.dumps({"question": question}).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise EvalFailure(f"POST {url} returned HTTP {exc.code}: {detail}") from exc


def _spec_filters(spec: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    filters = spec.get("filters") or []
    return [f for f in filters if isinstance(f, Mapping)]


def _assert_no_ilike(spec: Mapping[str, Any]) -> None:
    offenders = [f for f in _spec_filters(spec) if _plain(f.get("op")) == "ilike"]
    _require(not offenders, f"expected no ilike filters, got {offenders}")


def _assert_semantic_count(assertions: Mapping[str, Any], data: Mapping[str, Any]) -> None:
    if assertions.get("requires_estimate"):
        _require(isinstance(data.get("estimated_count"), int),
                 "expected integer data.estimated_count")
    if "min_estimated_count" in assertions:
        value = data.get("estimated_count")
        _require(isinstance(value, int),
                 f"min_estimated_count requires integer estimated_count, got {value!r}")
        _require(value >= int(assertions["min_estimated_count"]),
                 f"expected estimated_count >= {assertions['min_estimated_count']}, got {value}")
    if "min_verified_count" in assertions:
        value = data.get("verified_count")
        _require(isinstance(value, int),
                 f"min_verified_count requires integer verified_count, got {value!r}")
        _require(value >= int(assertions["min_verified_count"]),
                 f"expected verified_count >= {assertions['min_verified_count']}, got {value}")
    if assertions.get("requires_confidence"):
        confidence = data.get("confidence")
        interval = data.get("confidence_interval")
        thresholds = data.get("thresholds")
        _require(isinstance(confidence, Mapping), "expected data.confidence object")
        _require(isinstance(interval, Mapping), "expected data.confidence_interval object")
        _require(isinstance(thresholds, Mapping), "expected data.thresholds object")
    if assertions.get("requires_evidence"):
        evidence = data.get("evidence")
        _require(isinstance(evidence, list) and bool(evidence),
                 "expected non-empty data.evidence list")


def _assert_ask_case(case: Mapping[str, Any], answer: Mapping[str, Any]) -> EvalResult:
    assertions = case.get("assert") or {}
    _require(isinstance(assertions, Mapping), "ask case assert must be an object")
    spec = answer.get("spec") or {}
    data = answer.get("data") or {}
    _require(isinstance(spec, Mapping), "answer.spec must be an object")
    _require(isinstance(data, Mapping), "answer.data must be an object")

    intent = _plain(spec.get("intent") or answer.get("intent"))
    data_kind = _plain(data.get("kind"))
    expected_intents = assertions.get("intents") or []
    expected_kinds = assertions.get("data_kinds") or []
    if expected_intents:
        _require(intent in expected_intents,
                 f"expected intent in {expected_intents}, got {intent!r}")
    if expected_kinds:
        _require(data_kind in expected_kinds,
                 f"expected data.kind in {expected_kinds}, got {data_kind!r}")
    if assertions.get("no_ilike"):
        _assert_no_ilike(spec)
    if data_kind in {"semantic_count", "semantic_distribution"}:
        _assert_semantic_count(assertions, data)

    if "value_equals" in assertions:
        _require(data_kind == "scalar",
                 f"value_equals requires scalar data.kind, got {data_kind!r}")
        actual_value = data.get("value")
        expected_value = assertions["value_equals"]
        _require(actual_value == expected_value,
                 f"expected scalar value {expected_value!r}, got {actual_value!r}")

    expected_top_items = assertions.get("top_items") or []
    if expected_top_items:
        items = data.get("items")
        _require(isinstance(items, list), "top_items requires data.items to be a list")
        _require(len(items) >= len(expected_top_items),
                 f"expected at least {len(expected_top_items)} items, got {len(items)}")
        for idx, expected in enumerate(expected_top_items):
            _require(isinstance(expected, Mapping), "top_items entries must be objects")
            actual = items[idx]
            _require(isinstance(actual, Mapping),
                     f"data.items[{idx}] must be an object, got {type(actual).__name__}")
            for key, expected_value in expected.items():
                actual_value = actual.get(key)
                _require(actual_value == expected_value,
                         f"data.items[{idx}].{key} expected {expected_value!r}, got {actual_value!r}")

    route = assertions.get("route")
    semantic_query = spec.get("semantic_query")
    if route == "sql":
        _require(not semantic_query, f"numeric/SQL case unexpectedly used semantic_query={semantic_query!r}")
        _require(data_kind in {"scalar", "distribution", "series", "rows"},
                 f"SQL-backed case returned non-SQL data.kind={data_kind!r}")
    elif route == "semantic":
        _require(bool(semantic_query), "fuzzy concept case did not emit semantic_query")
        needles = [str(v).lower() for v in assertions.get("semantic_query_contains_any", [])]
        if needles:
            haystack = str(semantic_query).lower()
            _require(any(n in haystack for n in needles),
                     f"semantic_query={semantic_query!r} did not contain any of {needles}")
    return EvalResult(str(case["id"]), True,
                      f"intent={intent} data.kind={data_kind} route={route or '-'}")


def _run_retrieval_case(case: Mapping[str, Any], *, dsn: str) -> EvalResult:
    expected = {str(v) for v in case.get("expected_recall_numbers", [])}
    _require(bool(expected), "retrieval case needs expected_recall_numbers")
    query = str(case["query"])
    k = int(case.get("k", 10))
    field = str(case.get("field", "both"))
    threshold = float(case.get("min_recall_at_k", 1.0))

    embed_config = llm.embedding_config()
    embedding_error: llm.ProviderError | None = None
    try:
        client = llm.create_embedding_client(embed_config)
    except llm.ProviderError as exc:
        client = None
        embedding_error = exc
    with psycopg.connect(dsn) as conn:
        hits = retrieval.search(conn, client, query, k=k, field=field,
                                embed_config=embed_config, embedding_error=embedding_error)
    returned = [h.recall_number for h in hits]
    matched = expected.intersection(returned)
    recall = len(matched) / len(expected)
    _require(recall >= threshold,
             f"recall@{k}={recall:.2f} below {threshold:.2f}; expected={sorted(expected)} got={returned}")
    return EvalResult(str(case["id"]), True,
                      f"recall@{k}={recall:.2f} matched={sorted(matched)}")


def _maybe_judge(case: Mapping[str, Any], answer: Mapping[str, Any], *, enabled: bool) -> EvalResult | None:
    judge = case.get("judge")
    if not enabled or not judge:
        return None
    return EvalResult(f"{case['id']}:judge", True,
                      "LLM-as-judge hook is configured but disabled in v1 golden cases")


def _build_ask_fn(args: argparse.Namespace) -> Callable[[str], dict[str, Any]]:
    if args.base_url:
        return lambda question: _post_ask(args.base_url, question, args.timeout)

    engine = NLEngine(dsn=args.dsn, model=args.model)

    def ask_local(question: str) -> dict[str, Any]:
        return serialize_answer(engine.ask(question))

    return ask_local


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Run FDAgent golden evals.")
    p.add_argument("--golden", type=Path, default=DEFAULT_GOLDEN,
                   help=f"golden set path (default: {DEFAULT_GOLDEN})")
    p.add_argument("--base-url",
                   help="optional running API base URL, e.g. http://127.0.0.1:8003")
    p.add_argument("--dsn", default=DEFAULT_DSN,
                   help="Postgres DSN for in-process evals and retrieval recall@k")
    p.add_argument("--model", default=MODEL,
                   help="chat model for in-process /ask evals")
    p.add_argument("--timeout", type=float, default=60.0,
                   help="HTTP timeout in seconds for --base-url mode")
    p.add_argument("--llm-judge", action="store_true",
                   help="enable optional judge hooks when a golden case defines one")
    return p.parse_args()


def main() -> int:
    args = parse_args()
    golden = _load_golden(args.golden)
    ask_fn: Callable[[str], dict[str, Any]] | None = None
    results: list[EvalResult] = []

    for case in golden["cases"]:
        case_id = str(case.get("id", "<missing-id>"))
        try:
            kind = case.get("kind")
            if kind == "ask":
                if ask_fn is None:
                    ask_fn = _build_ask_fn(args)
                answer = ask_fn(str(case["question"]))
                results.append(_assert_ask_case(case, answer))
                judge_result = _maybe_judge(case, answer, enabled=args.llm_judge)
                if judge_result:
                    results.append(judge_result)
            elif kind == "retrieval_recall":
                results.append(_run_retrieval_case(case, dsn=args.dsn))
            else:
                raise EvalFailure(f"unknown case kind {kind!r}")
        except Exception as exc:  # noqa: BLE001 - eval runner must report every case failure
            results.append(EvalResult(case_id, False, f"{type(exc).__name__}: {exc}"))

    failed = [r for r in results if not r.passed]
    for result in results:
        status = "PASS" if result.passed else "FAIL"
        print(f"{status} {result.case_id}: {result.detail}")
    print(f"\nSummary: {len(results) - len(failed)} passed, {len(failed)} failed")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
