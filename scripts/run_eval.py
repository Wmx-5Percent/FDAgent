#!/usr/bin/env python3
"""Run local contract-tagged eval suites for /ask and retrieval behavior."""
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
from nl_query import (  # noqa: E402
    MODEL,
    NLEngine,
    _class_filter_label,
    _maybe_raw_firm_exposure_spec,
    _maybe_simple_class_count_spec,
    _safe_hard_filter_specs_or_defer,
)

DEFAULT_DSN = os.environ.get("DATABASE_URL", "postgresql://localhost:5432/fda")
DEFAULT_GOLDEN = REPO_ROOT / "evals" / "golden" / "v1.json"


@dataclass(frozen=True)
class EvalResult:
    case_id: str
    passed: bool
    detail: str
    skipped: bool = False


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
    suites = golden.get("suites") or {}
    _require(isinstance(suites, Mapping), "golden suites must be an object when provided")
    known_suites = {str(name) for name in suites}
    for case in golden["cases"]:
        _validate_case_metadata(case, known_suites=known_suites)
    return golden


def _case_suites(case: Mapping[str, Any]) -> set[str]:
    raw = case.get("suite")
    case_id = str(case.get("id", "<missing-id>"))
    if isinstance(raw, str):
        values = [raw]
    elif isinstance(raw, list):
        values = raw
    else:
        raise EvalFailure(f"{case_id}: suite must be a string or list of strings")
    if not all(isinstance(value, str) for value in values):
        raise EvalFailure(f"{case_id}: suite entries must be strings")
    suites = {value.strip() for value in values if value.strip()}
    _require(bool(suites), f"{case_id}: suite must not be empty")
    return suites


def _validate_case_metadata(case: Mapping[str, Any], *, known_suites: set[str]) -> None:
    case_id = case.get("id")
    _require(isinstance(case_id, str) and bool(case_id.strip()),
             "every eval case must include a non-empty string id")
    _require(isinstance(case.get("kind"), str) and bool(str(case.get("kind")).strip()),
             f"{case_id}: kind must be a non-empty string")
    suites = _case_suites(case)
    if known_suites:
        unknown = sorted(suites - known_suites)
        _require(not unknown, f"{case_id}: unknown suite tag(s): {', '.join(unknown)}")
    _require(isinstance(case.get("risk"), str) and bool(str(case.get("risk")).strip()),
             f"{case_id}: risk must be a non-empty string")
    for field in ("requires_llm", "requires_embedding", "requires_db"):
        _require(isinstance(case.get(field), bool),
                 f"{case_id}: {field} must be a boolean")
    _require(isinstance(case.get("assert"), Mapping),
             f"{case_id}: assert must be an object")


def _csv_values(values: list[str] | None) -> set[str]:
    selected: set[str] = set()
    for raw in values or []:
        for part in raw.split(","):
            value = part.strip()
            if value:
                selected.add(value)
    return selected


def _select_cases(
    golden: Mapping[str, Any],
    *,
    suite_filters: set[str],
    case_filters: set[str],
) -> list[Mapping[str, Any]]:
    cases = golden["cases"]
    known_case_ids = {str(case["id"]) for case in cases}
    missing_cases = sorted(case_filters - known_case_ids)
    _require(not missing_cases, f"unknown case id(s): {', '.join(missing_cases)}")

    known_suites = set(golden.get("suites") or {})
    if not known_suites:
        known_suites = set().union(*(_case_suites(case) for case in cases))
    missing_suites = sorted(suite_filters - known_suites)
    _require(not missing_suites, f"unknown suite tag(s): {', '.join(missing_suites)}")

    selected: list[Mapping[str, Any]] = []
    for case in cases:
        case_id = str(case["id"])
        if suite_filters and _case_suites(case).isdisjoint(suite_filters):
            continue
        if case_filters and case_id not in case_filters:
            continue
        selected.append(case)
    _require(bool(selected), "eval selection matched no cases")
    return selected


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


def _assert_highlights(assertions: Mapping[str, Any], answer: Mapping[str, Any]) -> None:
    if not (
        assertions.get("requires_highlights")
        or "min_highlights" in assertions
        or "max_highlights" in assertions
        or assertions.get("highlights_contains_any")
        or assertions.get("highlights_not_contains_any")
    ):
        return
    highlights = answer.get("highlights")
    _require(isinstance(highlights, list), "expected answer.highlights to be a list")
    cleaned = [str(item).strip() for item in highlights if str(item).strip()]
    if assertions.get("requires_highlights"):
        _require(bool(cleaned), "expected non-empty answer.highlights")
    if "min_highlights" in assertions:
        _require(len(cleaned) >= int(assertions["min_highlights"]),
                 f"expected at least {assertions['min_highlights']} highlights, got {len(cleaned)}")
    if "max_highlights" in assertions:
        _require(len(cleaned) <= int(assertions["max_highlights"]),
                 f"expected at most {assertions['max_highlights']} highlights, got {len(cleaned)}")
    haystack = "\n".join(cleaned).casefold()
    needles = [str(v).casefold() for v in assertions.get("highlights_contains_any", [])]
    if needles:
        _require(any(needle in haystack for needle in needles),
                 f"highlights did not contain any of {needles}: {cleaned!r}")
    banned = [str(v).casefold() for v in assertions.get("highlights_not_contains_any", [])]
    if banned:
        _require(not any(needle in haystack for needle in banned),
                 f"highlights unexpectedly contained one of {banned}: {cleaned!r}")


def _assert_item_matches(actual: Mapping[str, Any], expected: Mapping[str, Any],
                         context: str) -> None:
    for key, expected_value in expected.items():
        if key.endswith("_contains"):
            actual_key = key[:-len("_contains")]
            actual_value = actual.get(actual_key)
            _require(
                str(expected_value).casefold() in str(actual_value).casefold(),
                f"{context}.{actual_key} expected to contain {expected_value!r}, got {actual_value!r}",
            )
        elif key == "firm_equals":
            actual_value = actual.get("recalling_firm", actual.get("value"))
            _require(actual_value == expected_value,
                     f"{context}.firm expected {expected_value!r}, got {actual_value!r}")
        elif key == "recall_count_equals":
            actual_value = actual.get("total_recalls", actual.get("count"))
            _require(actual_value == expected_value,
                     f"{context}.recall_count expected {expected_value!r}, got {actual_value!r}")
        else:
            actual_value = actual.get(key)
            _require(actual_value == expected_value,
                     f"{context}.{key} expected {expected_value!r}, got {actual_value!r}")


def _assert_top_items(expected_top_items: list[Any], items: Any, context: str) -> None:
    _require(isinstance(items, list), f"{context}.top_items requires items to be a list")
    _require(len(items) >= len(expected_top_items),
             f"{context} expected at least {len(expected_top_items)} items, got {len(items)}")
    for idx, expected in enumerate(expected_top_items):
        _require(isinstance(expected, Mapping), f"{context}.top_items entries must be objects")
        actual = items[idx]
        _require(isinstance(actual, Mapping),
                 f"{context}.items[{idx}] must be an object, got {type(actual).__name__}")
        _assert_item_matches(actual, expected, f"{context}.items[{idx}]")


def _assert_sections(assertions: Mapping[str, Any], data: Mapping[str, Any]) -> None:
    if "section_count" not in assertions and not assertions.get("sections"):
        return
    sections = data.get("sections")
    _require(isinstance(sections, list), "section assertions require data.sections to be a list")
    if "section_count" in assertions:
        expected_count = int(assertions["section_count"])
        _require(len(sections) == expected_count,
                 f"expected {expected_count} sections, got {len(sections)}")
    for idx, expected in enumerate(assertions.get("sections") or []):
        _require(isinstance(expected, Mapping), "sections entries must be objects")
        expected_id = expected.get("id")
        if expected_id:
            matches = [s for s in sections if isinstance(s, Mapping) and s.get("id") == expected_id]
            _require(bool(matches), f"expected section id {expected_id!r}, got {sections!r}")
            section = matches[0]
        else:
            _require(idx < len(sections), f"expected section at index {idx}, got {len(sections)}")
            section = sections[idx]
            _require(isinstance(section, Mapping),
                     f"data.sections[{idx}] must be an object, got {type(section).__name__}")
        for key in ("id", "kind", "dimension", "source"):
            if key in expected:
                actual_value = section.get(key)
                _require(actual_value == expected[key],
                         f"section {expected_id or idx}.{key} expected {expected[key]!r}, got {actual_value!r}")
        if "title_contains" in expected:
            title = str(section.get("title") or "")
            needle = str(expected["title_contains"])
            _require(needle.casefold() in title.casefold(),
                     f"section {expected_id or idx}.title expected to contain {needle!r}, got {title!r}")
        if "min_items" in expected:
            items = section.get("items")
            _require(isinstance(items, list), f"section {expected_id or idx}.items must be a list")
            _require(len(items) >= int(expected["min_items"]),
                     f"section {expected_id or idx} expected at least {expected['min_items']} items, got {len(items)}")
        expected_top_items = expected.get("top_items") or []
        if expected_top_items:
            _assert_top_items(
                expected_top_items,
                section.get("items"),
                f"section {expected_id or idx}",
            )


def _assert_filters(assertions: Mapping[str, Any], spec: Mapping[str, Any]) -> None:
    expected_filters = assertions.get("filters_include") or []
    if not expected_filters:
        return
    filters = _spec_filters(spec)
    for expected in expected_filters:
        _require(isinstance(expected, Mapping), "filters_include entries must be objects")
        expected_column = expected.get("column")
        expected_op = expected.get("op")
        expected_values = [str(v).casefold() for v in expected.get("values", [])]
        matched = False
        for actual in filters:
            if expected_column and actual.get("column") != expected_column:
                continue
            if expected_op and _plain(actual.get("op")) != expected_op:
                continue
            actual_values = [str(v).casefold() for v in (actual.get("values") or [])]
            if expected_values and not all(v in actual_values for v in expected_values):
                continue
            matched = True
            break
        _require(matched, f"expected filter {dict(expected)!r}, got {filters!r}")


def _assert_ask_case(case: Mapping[str, Any], answer: Mapping[str, Any]) -> EvalResult:
    assertions = case.get("assert") or {}
    _require(isinstance(assertions, Mapping), "ask case assert must be an object")
    spec = answer.get("spec") or {}
    data = answer.get("data") or {}
    _require(isinstance(spec, Mapping), "answer.spec must be an object")
    _require(isinstance(data, Mapping), "answer.data must be an object")

    data_kind = _plain(data.get("kind"))
    intent = _plain(answer.get("intent") if data_kind == "multi_section" else (
        spec.get("intent") or answer.get("intent")
    ))
    expected_intents = assertions.get("intents") or []
    expected_kinds = assertions.get("data_kinds") or []
    if expected_intents:
        _require(intent in expected_intents,
                 f"expected intent in {expected_intents}, got {intent!r}")
    if expected_kinds:
        _require(data_kind in expected_kinds,
                 f"expected data.kind in {expected_kinds}, got {data_kind!r}")
    banned_kinds = assertions.get("data_not_kinds") or []
    if banned_kinds:
        _require(data_kind not in banned_kinds,
                 f"expected data.kind not in {banned_kinds}, got {data_kind!r}")
    if assertions.get("no_ilike"):
        _assert_no_ilike(spec)
    if assertions.get("spec_empty"):
        _require(not spec, f"expected empty spec for guarded response, got {spec!r}")
    _assert_filters(assertions, spec)
    if data_kind in {"semantic_count", "semantic_distribution"}:
        _assert_semantic_count(assertions, data)
    _assert_highlights(assertions, answer)
    _assert_sections(assertions, data)
    if "taxonomy_node_id_equals" in assertions:
        expected_node = assertions["taxonomy_node_id_equals"]
        actual_node = spec.get("taxonomy_node_id") or data.get("node_id")
        _require(actual_node == expected_node,
                 f"expected taxonomy_node_id {expected_node!r}, got {actual_node!r}")
    for key, expected_value in (assertions.get("data_fields") or {}).items():
        actual_value = data.get(key)
        _require(actual_value == expected_value,
                 f"data.{key} expected {expected_value!r}, got {actual_value!r}")
    summary_needles = [str(v).lower() for v in assertions.get("summary_contains_any", [])]
    if summary_needles:
        summary = str(answer.get("summary") or "").lower()
        _require(any(n in summary for n in summary_needles),
                 f"summary did not contain any of {summary_needles}: {summary!r}")
    summary_banned = [str(v).lower() for v in assertions.get("summary_not_contains_any", [])]
    if summary_banned:
        summary = str(answer.get("summary") or "").lower()
        _require(not any(n in summary for n in summary_banned),
                 f"summary unexpectedly contained one of {summary_banned}: {summary!r}")

    if "value_equals" in assertions:
        _require(data_kind == "scalar",
                 f"value_equals requires scalar data.kind, got {data_kind!r}")
        actual_value = data.get("value")
        expected_value = assertions["value_equals"]
        _require(actual_value == expected_value,
                 f"expected scalar value {expected_value!r}, got {actual_value!r}")

    expected_top_items = assertions.get("top_items") or []
    if expected_top_items:
        _assert_top_items(expected_top_items, data.get("items"), "data")

    route = assertions.get("route")
    semantic_query = spec.get("semantic_query")
    banned_needles = [str(v).lower() for v in assertions.get("semantic_query_not_contains_any", [])]
    if banned_needles and semantic_query:
        haystack = str(semantic_query).lower()
        _require(not any(n in haystack for n in banned_needles),
                 f"semantic_query={semantic_query!r} unexpectedly contained one of {banned_needles}")
    if route == "sql":
        _require(not semantic_query, f"numeric/SQL case unexpectedly used semantic_query={semantic_query!r}")
        _require(data_kind in {"scalar", "distribution", "series", "rows", "multi_section", "raw_firm_exposure"},
                 f"SQL-backed case returned non-SQL data.kind={data_kind!r}")
    elif route == "explanation":
        _require(not semantic_query,
                 f"explanation case unexpectedly emitted semantic_query={semantic_query!r}")
        _require(data_kind == "taxonomy_explanation",
                 f"explanation case returned unexpected data.kind={data_kind!r}")
    elif route == "semantic":
        _require(bool(semantic_query), "fuzzy concept case did not emit semantic_query")
        needles = [str(v).lower() for v in assertions.get("semantic_query_contains_any", [])]
        if needles:
            haystack = str(semantic_query).lower()
            _require(any(n in haystack for n in needles),
                     f"semantic_query={semantic_query!r} did not contain any of {needles}")
    elif route in {"chitchat_meta", "out_of_domain", "ambiguous"}:
        _require(not semantic_query,
                 f"guarded case unexpectedly emitted semantic_query={semantic_query!r}")
        _require(data.get("route") == route,
                 f"expected data.route={route!r}, got {data.get('route')!r}")
        _require(data_kind in {"message", "clarification"},
                 f"guarded case returned unexpected data.kind={data_kind!r}")
    return EvalResult(str(case["id"]), True,
                      f"intent={intent} data.kind={data_kind} route={route or '-'}")


def _assert_deterministic_helper_case(case: Mapping[str, Any]) -> EvalResult:
    assertions = case.get("assert") or {}
    _require(isinstance(assertions, Mapping),
             "deterministic_helper case assert must be an object")
    question = str(case["question"])
    simple_spec = _maybe_simple_class_count_spec(question)
    raw_spec = _maybe_raw_firm_exposure_spec(question)
    specs = {
        "simple_class_count": simple_spec,
        "raw_firm_exposure": raw_spec,
    }
    if assertions.get("simple_class_count_spec_empty"):
        _require(
            simple_spec is None,
            "expected simple Class-count fast path to defer, got "
            f"{simple_spec.model_dump(mode='json', exclude_none=True) if simple_spec else None}",
        )
    if assertions.get("raw_firm_exposure_spec_empty"):
        _require(
            raw_spec is None,
            "expected raw firm exposure fast path to defer, got "
            f"{raw_spec.model_dump(mode='json', exclude_none=True) if raw_spec else None}",
        )
    for helper_name, spec in specs.items():
        expected_filters = assertions.get(f"{helper_name}_filters_include") or []
        if expected_filters:
            _require(spec is not None, f"expected {helper_name} fast path to match")
            _assert_filters(
                {"filters_include": expected_filters},
                spec.model_dump(mode="json", exclude_none=True),
            )
    if assertions.get("helper_filter_invariant"):
        hard_filters, defer = _safe_hard_filter_specs_or_defer(question)
        class_label = _class_filter_label(question)
        expected = [
            {"column": f.column, "op": f.op.value, "values": f.values}
            for f in hard_filters
        ]
        if class_label:
            expected.insert(0, {"column": "classification", "op": "eq", "values": [class_label]})
        for helper_name, spec in specs.items():
            if spec is None:
                continue
            _require(not defer, f"{helper_name} matched although hard-filter parser requested deferral")
            _assert_filters(
                {"filters_include": expected},
                spec.model_dump(mode="json", exclude_none=True),
            )
    return EvalResult(
        str(case["id"]),
        True,
        "simple_class_count="
        f"{'deferred' if simple_spec is None else 'matched'} "
        "raw_firm_exposure="
        f"{'deferred' if raw_spec is None else 'matched'}",
    )


def _run_retrieval_case(case: Mapping[str, Any], *, dsn: str) -> EvalResult:
    assertions = case.get("assert") or {}
    _require(isinstance(assertions, Mapping),
             "retrieval case assert must be an object")
    expected = {
        str(v) for v in assertions.get(
            "expected_recall_numbers",
            case.get("expected_recall_numbers", []),
        )
    }
    _require(bool(expected), "retrieval case needs expected_recall_numbers")
    query = str(case["query"])
    k = int(case.get("k", 10))
    field = str(case.get("field", "both"))
    threshold = float(assertions.get("min_recall_at_k", case.get("min_recall_at_k", 1.0)))
    expected_mode = assertions.get("retrieval_mode", case.get("retrieval_mode"))

    embed_config = llm.embedding_config()
    embedding_error: llm.ProviderError | None = None
    try:
        client = llm.create_embedding_client(embed_config)
        llm.embed_text(client, embed_config, query)
    except llm.ProviderError as exc:
        return EvalResult(
            str(case["id"]),
            True,
            f"skipped vector recall@{k}: embedding unavailable ({type(exc).__name__}: {exc})",
            skipped=True,
        )
    with psycopg.connect(dsn) as conn:
        hits = retrieval.search(conn, client, query, k=k, field=field,
                                embed_config=embed_config, embedding_error=embedding_error)
    modes = {h.retrieval_mode for h in hits}
    if expected_mode:
        _require(modes == {expected_mode},
                 f"expected retrieval_mode={expected_mode!r}, got {sorted(modes)!r}")
    returned = [h.recall_number for h in hits]
    matched = expected.intersection(returned)
    recall = len(matched) / len(expected)
    _require(recall >= threshold,
             f"recall@{k}={recall:.2f} below {threshold:.2f}; expected={sorted(expected)} got={returned}")
    return EvalResult(str(case["id"]), True,
                      f"provider={embed_config.provider} model={embed_config.model} "
                      f"retrieval_mode={','.join(sorted(modes)) or '-'} "
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
    p = argparse.ArgumentParser(description="Run FDAgent contract-tagged golden eval suites.")
    p.add_argument("--golden", type=Path, default=DEFAULT_GOLDEN,
                   help=f"golden set path (default: {DEFAULT_GOLDEN})")
    p.add_argument("--suite", action="append",
                   help="run only cases tagged with this suite; repeat or comma-separate")
    p.add_argument("--case", dest="case_ids", action="append",
                   help="run only this case id; repeat or comma-separate")
    p.add_argument("--list-cases", action="store_true",
                   help="list selected cases with suite/risk metadata and exit")
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
    try:
        golden = _load_golden(args.golden)
        suite_filters = _csv_values(args.suite)
        case_filters = _csv_values(args.case_ids)
        cases = _select_cases(
            golden,
            suite_filters=suite_filters,
            case_filters=case_filters,
        )
    except EvalFailure as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2

    if args.list_cases:
        for case in cases:
            suites = ",".join(sorted(_case_suites(case)))
            print(f"{case['id']}\t{suites}\t{case['kind']}\t{case['risk']}")
        return 0

    ask_fn: Callable[[str], dict[str, Any]] | None = None
    results: list[EvalResult] = []

    selected_suites = ",".join(sorted(suite_filters)) if suite_filters else "all"
    selected_cases = ",".join(sorted(case_filters)) if case_filters else "all"
    print(
        f"Running {len(cases)} eval case(s) from {args.golden} "
        f"(suite={selected_suites}; case={selected_cases})"
    )
    for case in cases:
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
            elif kind == "deterministic_helper":
                results.append(_assert_deterministic_helper_case(case))
            elif kind == "retrieval_recall":
                results.append(_run_retrieval_case(case, dsn=args.dsn))
            else:
                raise EvalFailure(f"unknown case kind {kind!r}")
        except Exception as exc:  # noqa: BLE001 - eval runner must report every case failure
            results.append(EvalResult(case_id, False, f"{type(exc).__name__}: {exc}"))

    failed = [r for r in results if not r.passed and not r.skipped]
    skipped = [r for r in results if r.skipped]
    for result in results:
        status = "SKIP" if result.skipped else ("PASS" if result.passed else "FAIL")
        print(f"{status} {result.case_id}: {result.detail}")
    passed = len(results) - len(failed) - len(skipped)
    print(f"\nSummary: {passed} passed, {len(failed)} failed, {len(skipped)} skipped")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
