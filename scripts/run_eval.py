#!/usr/bin/env python3
"""Run local contract-tagged eval suites for /ask and retrieval behavior."""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable, Mapping

REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_DIR = REPO_ROOT / "src"
sys.path.insert(0, str(SRC_DIR))

import psycopg  # noqa: E402

import llm  # noqa: E402
import retrieval  # noqa: E402
from dataset_fingerprint import (  # noqa: E402
    DATASET_DRIFT_EXIT_CODE,
    DEFAULT_BASELINE as DEFAULT_DATASET_FINGERPRINT_BASELINE,
    DEFAULT_TABLE as DEFAULT_DATASET_FINGERPRINT_TABLE,
    DatasetFingerprintError,
    DatasetFingerprintMismatch,
    check_fingerprint,
)
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
REPORT_SCHEMA_VERSION = "fdaagent_eval_report_v1"
CORE_PR_GATE_SUITE = "core"
DEFAULT_COMPARE_PASS_RATE_THRESHOLDS = {"core": 1.0}
DEFAULT_COMPARE_LATENCY_TOLERANCE_MS = {"*": 1000.0, "core": 500.0, "rag": 2000.0}
DEFAULT_COMPARE_RECALL_TOLERANCE = {"rag": 0.0}


@dataclass(frozen=True)
class EvalResult:
    case_id: str
    passed: bool
    detail: str
    skipped: bool = False
    duration_ms: float | None = None
    metadata: Mapping[str, Any] = field(default_factory=dict)


class EvalFailure(AssertionError):
    pass


def _plain(value: Any) -> Any:
    return getattr(value, "value", value)


def _round_ms(value: float | None) -> float | None:
    return None if value is None else round(value, 3)


def _result_status(result: EvalResult) -> str:
    if result.skipped:
        return "skip"
    return "pass" if result.passed else "fail"


def _first_observation(value: Any, keys: set[str]) -> Any:
    if isinstance(value, Mapping):
        for key in keys:
            if key in value and value[key] not in (None, ""):
                return _plain(value[key])
        for child in value.values():
            observed = _first_observation(child, keys)
            if observed not in (None, ""):
                return observed
    elif isinstance(value, list):
        for child in value:
            observed = _first_observation(child, keys)
            if observed not in (None, ""):
                return observed
    return None


def _answer_report_metadata(
    case: Mapping[str, Any],
    answer: Mapping[str, Any],
    *,
    intent: Any,
    data_kind: Any,
) -> dict[str, Any]:
    spec = answer.get("spec") or {}
    data = answer.get("data") or {}
    assertions = case.get("assert") or {}
    route = (
        _plain(data.get("route")) if isinstance(data, Mapping) else None
    ) or assertions.get("route") or ("semantic" if spec.get("semantic_query") else "sql")
    fallback_reason = _first_observation(
        answer,
        {"embedding_fallback_reason", "fallback_reason"},
    )
    metadata = {
        "route": route,
        "intent": _plain(intent),
        "data_kind": _plain(data_kind),
        "retrieval_mode": _first_observation(answer, {"retrieval_mode"}),
        "fallback_reason": fallback_reason,
    }
    return {key: value for key, value in metadata.items() if value not in (None, "")}


def _is_timeout_exception(exc: BaseException) -> bool:
    return isinstance(exc, TimeoutError) or "timed out" in str(exc).casefold()


def _is_provider_unavailable(exc: BaseException) -> bool:
    return isinstance(exc, llm.ProviderError) and bool(getattr(exc, "fallback_allowed", False))


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
    if "allow_provider_unavailable_skip" in case:
        _require(isinstance(case.get("allow_provider_unavailable_skip"), bool),
                 f"{case_id}: allow_provider_unavailable_skip must be a boolean")
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


def _case_by_base_result_id(
    cases: list[Mapping[str, Any]],
) -> dict[str, Mapping[str, Any]]:
    return {str(case["id"]): case for case in cases}


def _allows_provider_unavailable_skip(case: Mapping[str, Any]) -> bool:
    assertions = case.get("assert") or {}
    return bool(
        case.get("allow_provider_unavailable_skip")
        or (
            isinstance(assertions, Mapping)
            and assertions.get("allow_provider_unavailable_skip")
        )
    )


def _provider_unavailable_skip(result: EvalResult) -> bool:
    detail = result.detail.casefold()
    metadata = result.metadata
    return bool(metadata.get("fallback_reason")) or any(
        marker in detail
        for marker in (
            "provider",
            "embedding unavailable",
            "embedding degraded",
            "missing key",
            "auth",
            "quota",
            "rate limit",
            "fallback",
        )
    )


def _allowed_core_gate_skip(
    result: EvalResult,
    case: Mapping[str, Any] | None,
) -> bool:
    if not result.skipped or case is None:
        return False
    requires_provider = bool(case.get("requires_llm") or case.get("requires_embedding"))
    return (
        requires_provider
        and _allows_provider_unavailable_skip(case)
        and _provider_unavailable_skip(result)
    )


def _core_case_ids(golden: Mapping[str, Any]) -> set[str]:
    return {
        str(case["id"])
        for case in golden["cases"]
        if CORE_PR_GATE_SUITE in _case_suites(case)
    }


def _validate_core_pr_gate_args(
    args: argparse.Namespace,
    golden: Mapping[str, Any],
    *,
    suite_filters: set[str],
    case_filters: set[str],
    selected_cases: list[Mapping[str, Any]],
) -> None:
    if not args.core_pr_gate:
        return
    _require(
        not args.skip_dataset_fingerprint,
        "--core-pr-gate cannot be combined with --skip-dataset-fingerprint",
    )
    _require(
        not case_filters,
        "--core-pr-gate must run the complete core suite; do not pass --case",
    )
    _require(
        suite_filters == {CORE_PR_GATE_SUITE},
        "--core-pr-gate must select exactly --suite core",
    )
    suites = golden.get("suites") or {}
    core_suite = suites.get(CORE_PR_GATE_SUITE)
    _require(
        isinstance(core_suite, Mapping) and core_suite.get("blocking") is True,
        "golden suite 'core' must be marked blocking=true for --core-pr-gate",
    )
    selected_ids = {str(case["id"]) for case in selected_cases}
    expected_ids = _core_case_ids(golden)
    missing = sorted(expected_ids - selected_ids)
    extra = sorted(selected_ids - expected_ids)
    _require(
        not missing and not extra,
        "--core-pr-gate selection mismatch: "
        f"missing={missing or '-'} extra={extra or '-'}",
    )


def _core_pr_gate_failed(
    cases: list[Mapping[str, Any]],
    results: list[EvalResult],
) -> bool:
    cases_by_id = _case_by_base_result_id(cases)
    failed = [result for result in results if not result.passed and not result.skipped]
    skipped = [result for result in results if result.skipped]
    disallowed_skips = [
        result
        for result in skipped
        if not _allowed_core_gate_skip(
            result,
            cases_by_id.get(result.case_id.split(":", 1)[0]),
        )
    ]
    allowed_skips = len(skipped) - len(disallowed_skips)

    if failed or disallowed_skips:
        print("\nCore PR gate: FAIL")
        for result in failed:
            print(f"FAIL {result.case_id}: {result.detail}")
        for result in disallowed_skips:
            print(f"DISALLOWED SKIP {result.case_id}: {result.detail}")
        if disallowed_skips:
            print(
                "Only provider-unavailable skips explicitly marked with "
                "allow_provider_unavailable_skip=true are allowed in the core gate."
            )
        return True

    passed = len(results) - len(skipped)
    print(
        "\nCore PR gate: PASS "
        f"({passed} passed, {allowed_skips} provider-unavailable skips excluded)"
    )
    return False


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
    return EvalResult(
        str(case["id"]),
        True,
        f"intent={intent} data.kind={data_kind} route={route or '-'}",
        metadata=_answer_report_metadata(
            case,
            answer,
            intent=intent,
            data_kind=data_kind,
        ),
    )


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


def _assert_embedding_provider_config_case(case: Mapping[str, Any]) -> EvalResult:
    assertions = case.get("assert") or {}
    _require(isinstance(assertions, Mapping),
             "embedding_provider_config case assert must be an object")
    env = {str(k): str(v) for k, v in (case.get("env") or {}).items()}
    unset_env = [str(v) for v in (case.get("unset_env") or [])]
    managed_env = set(env) | set(unset_env)
    old_env = {key: os.environ.get(key) for key in managed_env}
    old_openai = llm.OpenAI
    client_kwargs: dict[str, Any] = {}
    request_kwargs: dict[str, Any] = {}

    class FakeOpenAI:
        def __init__(self, **kwargs: Any) -> None:
            client_kwargs.update(kwargs)

    class FakeEmbeddings:
        def create(self, **kwargs: Any) -> Any:
            request_kwargs.update(kwargs)
            return SimpleNamespace(
                data=[SimpleNamespace(embedding=[0.0] * int(config.dimension or 0))]
            )

    try:
        for key in unset_env:
            os.environ.pop(key, None)
        for key, value in env.items():
            os.environ[key] = value

        config = llm.embedding_config(model=case.get("model"))
        _require(config.provider == assertions.get("provider"),
                 f"provider expected {assertions.get('provider')!r}, got {config.provider!r}")
        _require(config.model == assertions.get("model"),
                 f"model expected {assertions.get('model')!r}, got {config.model!r}")
        if "base_url" in assertions:
            _require(config.base_url == assertions["base_url"],
                     f"base_url expected {assertions['base_url']!r}, got {config.base_url!r}")
        if "dimension" in assertions:
            _require(config.dimension == int(assertions["dimension"]),
                     f"dimension expected {assertions['dimension']!r}, got {config.dimension!r}")
        if "configured" in assertions:
            _require(config.configured is bool(assertions["configured"]),
                     f"configured expected {assertions['configured']!r}, got {config.configured!r}")

        llm.OpenAI = FakeOpenAI  # type: ignore[assignment]
        llm.create_embedding_client(config)
        if "client_api_key" in assertions:
            _require(client_kwargs.get("api_key") == assertions["client_api_key"],
                     "embedding client used the wrong API key")
        if "client_not_api_key" in assertions:
            _require(client_kwargs.get("api_key") != assertions["client_not_api_key"],
                     "embedding client fell back to a forbidden API key")
        if "client_base_url" in assertions:
            _require(str(client_kwargs.get("base_url")) == str(assertions["client_base_url"]),
                     f"client base_url expected {assertions['client_base_url']!r}, "
                     f"got {client_kwargs.get('base_url')!r}")
        default_headers = client_kwargs.get("default_headers") or {}
        for header in assertions.get("client_default_headers_include") or []:
            _require(header in default_headers,
                     f"client default_headers missing {header!r}: {default_headers!r}")

        fake_client = SimpleNamespace(embeddings=FakeEmbeddings())
        llm.embed_text(fake_client, config, str(case.get("query", "superpotent")))
        if "request_model" in assertions:
            _require(request_kwargs.get("model") == assertions["request_model"],
                     f"embedding request model expected {assertions['request_model']!r}, "
                     f"got {request_kwargs.get('model')!r}")
        if "request_encoding_format" in assertions:
            _require(request_kwargs.get("encoding_format") == assertions["request_encoding_format"],
                     f"embedding request encoding_format expected "
                     f"{assertions['request_encoding_format']!r}, "
                     f"got {request_kwargs.get('encoding_format')!r}")
        extra_headers = request_kwargs.get("extra_headers") or {}
        for header in assertions.get("request_extra_headers_include") or []:
            _require(header in extra_headers,
                     f"embedding request extra_headers missing {header!r}: {extra_headers!r}")
    finally:
        llm.OpenAI = old_openai  # type: ignore[assignment]
        for key, old_value in old_env.items():
            if old_value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = old_value

    return EvalResult(
        str(case["id"]),
        True,
        f"provider={config.provider} model={config.model} base_url={config.base_url}",
        metadata={
            "embedding_provider": config.provider,
            "embedding_model": config.model,
            "embedding_dimension": config.dimension,
        },
    )


def _ranked_positions(returned: list[str], expected: set[str]) -> list[int]:
    return [idx + 1 for idx, recall_number in enumerate(returned) if recall_number in expected]


def _mrr_at_k(positions: list[int]) -> float:
    return 0.0 if not positions else 1.0 / min(positions)


def _ndcg_at_k(positions: list[int], *, ideal_relevant: int) -> float:
    if not positions or ideal_relevant <= 0:
        return 0.0
    from math import log2

    dcg = sum(1.0 / log2(position + 1) for position in positions)
    ideal = sum(1.0 / log2(rank + 2) for rank in range(ideal_relevant))
    return 0.0 if ideal == 0 else dcg / ideal


def _assert_min_float(assertions: Mapping[str, Any], key: str, actual: float,
                      *, label: str) -> None:
    if key not in assertions:
        return
    expected = float(assertions[key])
    _require(actual >= expected, f"{label}={actual:.3f} below {expected:.3f}")


_SIMULATED_EMBEDDING_ERRORS: Mapping[str, type[llm.ProviderError]] = {
    "ProviderMissingKeyError": llm.ProviderMissingKeyError,
    "ProviderAuthError": llm.ProviderAuthError,
    "ProviderQuotaError": llm.ProviderQuotaError,
    "ProviderRateLimitError": llm.ProviderRateLimitError,
    "ProviderConnectionError": llm.ProviderConnectionError,
}


def _simulated_embedding_error(name: str, config: llm.EmbeddingConfig) -> llm.ProviderError:
    error_type = _SIMULATED_EMBEDDING_ERRORS.get(name)
    _require(
        error_type is not None,
        f"unknown simulated embedding error {name!r}; expected one of "
        f"{sorted(_SIMULATED_EMBEDDING_ERRORS)}",
    )
    return error_type(
        f"simulated {name} for retrieval benchmark",
        provider=config.provider,
        model=config.model,
        operation="embedding",
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
    simulated_error_name = (
        assertions.get("simulate_embedding_error")
        or case.get("simulate_embedding_error")
    )
    simulate_fallback = bool(
        assertions.get("simulate_embedding_fallback")
        or case.get("simulate_embedding_fallback")
        or simulated_error_name
    )

    embed_config = llm.embedding_config()
    embedding_error: llm.ProviderError | None = None
    if simulate_fallback:
        client = None
        embedding_error = _simulated_embedding_error(
            str(simulated_error_name or "ProviderMissingKeyError"),
            embed_config,
        )
    else:
        try:
            client = llm.create_embedding_client(embed_config)
        except llm.ProviderError as exc:
            fallback_reason = llm.provider_error_summary(exc)
            return EvalResult(
                str(case["id"]),
                True,
                "skipped hybrid recall@"
                f"{k}: embedding unavailable; provider={embed_config.provider} "
                f"model={embed_config.model} dimension={embed_config.dimension} "
                f"fallback={fallback_reason}; not counted as zero recall",
                skipped=True,
                metadata={
                    "embedding_provider": embed_config.provider,
                    "embedding_model": embed_config.model,
                    "embedding_dimension": embed_config.dimension,
                    "fallback_reason": fallback_reason,
                    "metrics": {"recall_at_k": None, "k": k},
                },
            )
    with psycopg.connect(dsn) as conn:
        hits = retrieval.search(conn, client, query, k=k, field=field,
                                embed_config=embed_config, embedding_error=embedding_error)
    retrieval_mode = getattr(hits, "retrieval_mode", None) or (
        hits[0].retrieval_mode if hits else "-"
    )
    fallback_reason = getattr(hits, "embedding_fallback_reason", None)
    if fallback_reason and case.get("requires_embedding") and not simulate_fallback:
        return EvalResult(
            str(case["id"]),
            True,
            "skipped hybrid recall@"
            f"{k}: embedding degraded; provider={embed_config.provider} "
            f"model={embed_config.model} dimension={embed_config.dimension} "
            f"retrieval_mode={retrieval_mode} fallback={fallback_reason}; "
            "not counted as zero recall",
            skipped=True,
            metadata={
                "embedding_provider": embed_config.provider,
                "embedding_model": embed_config.model,
                "embedding_dimension": embed_config.dimension,
                "retrieval_mode": retrieval_mode,
                "fallback_reason": fallback_reason,
                "metrics": {"recall_at_k": None, "k": k},
            },
        )
    if expected_mode:
        _require(retrieval_mode == expected_mode,
                 f"expected retrieval_mode={expected_mode!r}, got {retrieval_mode!r}")
    if "min_returned_hits" in assertions:
        _require(len(hits) >= int(assertions["min_returned_hits"]),
                 f"expected at least {assertions['min_returned_hits']} hits, got {len(hits)}")
    returned = [h.recall_number for h in hits]
    matched = expected.intersection(returned)
    recall = len(matched) / len(expected)
    _require(recall >= threshold,
             f"recall@{k}={recall:.2f} below {threshold:.2f}; expected={sorted(expected)} got={returned}")
    positions = _ranked_positions(returned, expected)
    mrr = _mrr_at_k(positions)
    ndcg = _ndcg_at_k(positions, ideal_relevant=min(len(expected), k))
    _assert_min_float(assertions, "min_mrr_at_k", mrr, label=f"mrr@{k}")
    _assert_min_float(assertions, "min_ndcg_at_k", ndcg, label=f"ndcg@{k}")
    if "embedding_fallback_reason" in assertions:
        expected_reason = str(assertions["embedding_fallback_reason"])
        _require(
            str(fallback_reason) == expected_reason or expected_reason in str(fallback_reason),
            f"embedding_fallback_reason expected {expected_reason!r}, got {fallback_reason!r}",
        )
    if assertions.get("requires_fallback_reason"):
        _require(bool(fallback_reason), "expected non-empty embedding_fallback_reason")
    for needle in assertions.get("fallback_reason_contains_all") or []:
        _require(str(needle) in str(fallback_reason),
                 f"fallback_reason expected to contain {needle!r}, got {fallback_reason!r}")
    for attr, key in (
        ("vector_hit_count", "min_vector_hit_count"),
        ("fts_hit_count", "min_fts_hit_count"),
        ("fused_hit_count", "min_fused_hit_count"),
    ):
        if key in assertions:
            actual = int(getattr(hits, attr, 0))
            expected_min = int(assertions[key])
            _require(actual >= expected_min,
                     f"{attr} expected >= {expected_min}, got {actual}")
    vector_hit_count = getattr(hits, "vector_hit_count", None)
    fts_hit_count = getattr(hits, "fts_hit_count", None)
    fused_hit_count = getattr(hits, "fused_hit_count", None)
    return EvalResult(
        str(case["id"]),
        True,
        f"provider={embed_config.provider} model={embed_config.model} "
        f"dimension={embed_config.dimension} retrieval_mode={retrieval_mode} "
        f"fallback={fallback_reason or '-'} "
        f"vector_hits={vector_hit_count if vector_hit_count is not None else '-'} "
        f"fts_hits={fts_hit_count if fts_hit_count is not None else '-'} "
        f"fused_hits={fused_hit_count if fused_hit_count is not None else '-'} "
        f"recall@{k}={recall:.2f} mrr@{k}={mrr:.2f} "
        f"ndcg@{k}={ndcg:.2f} matched={sorted(matched)} returned={returned}",
        metadata={
            "embedding_provider": embed_config.provider,
            "embedding_model": embed_config.model,
            "embedding_dimension": embed_config.dimension,
            "retrieval_mode": retrieval_mode,
            "fallback_reason": fallback_reason,
            "metrics": {
                "recall_at_k": recall,
                "mrr_at_k": mrr,
                "ndcg_at_k": ndcg,
                "k": k,
                "matched_expected": sorted(matched),
                "expected_count": len(expected),
                "returned": returned,
                "vector_hit_count": vector_hit_count,
                "fts_hit_count": fts_hit_count,
                "fused_hit_count": fused_hit_count,
            },
        },
    )


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


def _provider_unavailable_skip_result(
    case: Mapping[str, Any],
    exc: BaseException,
) -> EvalResult | None:
    if not case.get("allow_provider_unavailable_skip") or not _is_provider_unavailable(exc):
        return None
    provider = getattr(exc, "provider", None)
    model = getattr(exc, "model", None)
    operation = getattr(exc, "operation", None)
    summary = llm.provider_error_summary(exc) if isinstance(exc, llm.ProviderError) else str(exc)
    return EvalResult(
        str(case["id"]),
        True,
        "provider-unavailable skip: "
        f"{type(exc).__name__}: {summary}; provider={provider or '-'} "
        f"model={model or '-'} operation={operation or '-'}",
        skipped=True,
        metadata={
            "skip_category": "provider_unavailable",
            "provider_error": type(exc).__name__,
            "provider": provider,
            "model": model,
            "operation": operation,
            "fallback_reason": summary,
        },
    )


def _requires_dataset_fingerprint(cases: list[Mapping[str, Any]]) -> bool:
    return any(bool(case.get("requires_db")) for case in cases)


def _run_dataset_fingerprint_preflight(
    args: argparse.Namespace,
    cases: list[Mapping[str, Any]],
) -> tuple[int, Mapping[str, Any] | None]:
    if args.skip_dataset_fingerprint:
        print("SKIP dataset fingerprint preflight (--skip-dataset-fingerprint)")
        return 0, {"skipped": True, "required": _requires_dataset_fingerprint(cases)}
    if not _requires_dataset_fingerprint(cases):
        return 0, {"required": False}
    try:
        result = check_fingerprint(
            dsn=args.dsn,
            table=args.dataset_fingerprint_table,
            baseline_path=args.dataset_fingerprint_baseline,
        )
    except DatasetFingerprintMismatch as exc:
        print(f"ERROR: dataset fingerprint preflight failed: {exc}", file=sys.stderr)
        return DATASET_DRIFT_EXIT_CODE, None
    except DatasetFingerprintError as exc:
        print(f"ERROR: dataset fingerprint preflight failed: {exc}", file=sys.stderr)
        return 2, None
    actual = result.actual
    print(
        "Dataset fingerprint OK: "
        f"sha256={actual['sha256']} rows={actual['row_count']} "
        f"report_date={actual['report_date_min']}..{actual['report_date_max']} "
        f"taxonomy_labels={actual.get('taxonomy_label_rows')} "
        f"taxonomy_covered={actual.get('taxonomy_covered_records')} "
        f"embeddings={actual.get('embedding_rows')} "
        f"schema_sha256={actual.get('source_schema_sha256')}"
    )
    return 0, actual


def _git_output(*args: str) -> str | None:
    try:
        completed = subprocess.run(
            ("git", *args),
            cwd=REPO_ROOT,
            check=True,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
        )
    except (OSError, subprocess.CalledProcessError):
        return None
    return completed.stdout.strip() or None


def _git_metadata() -> dict[str, Any]:
    return {
        "sha": _git_output("rev-parse", "HEAD"),
        "branch": _git_output("branch", "--show-current"),
        "dirty": bool(_git_output("status", "--short")),
    }


def _provider_metadata(args: argparse.Namespace) -> dict[str, Any]:
    metadata: dict[str, Any] = {}
    try:
        metadata.update(llm.chat_config(model=args.model).status())
    except Exception as exc:  # noqa: BLE001 - report config errors without aborting reporting
        metadata["llm_config_error"] = f"{type(exc).__name__}: {exc}"
    try:
        metadata.update(llm.embedding_config().status())
    except Exception as exc:  # noqa: BLE001
        metadata["embed_config_error"] = f"{type(exc).__name__}: {exc}"
    return metadata


def _numeric_override_map(
    values: list[str] | None,
    *,
    defaults: Mapping[str, float],
    arg_name: str,
) -> dict[str, float]:
    parsed = dict(defaults)
    for raw in values or []:
        if "=" not in raw:
            raise EvalFailure(f"{arg_name} must use SUITE=NUMBER, got {raw!r}")
        suite, value = raw.split("=", 1)
        suite = suite.strip()
        if not suite:
            raise EvalFailure(f"{arg_name} requires a non-empty suite name")
        try:
            parsed[suite] = float(value)
        except ValueError as exc:
            raise EvalFailure(f"{arg_name} value for {suite!r} is not numeric: {value!r}") from exc
    return parsed


def _compare_thresholds(args: argparse.Namespace) -> dict[str, dict[str, float]]:
    return {
        "pass_rate": _numeric_override_map(
            args.compare_pass_rate_threshold,
            defaults=DEFAULT_COMPARE_PASS_RATE_THRESHOLDS,
            arg_name="--compare-pass-rate-threshold",
        ),
        "latency_tolerance_ms": _numeric_override_map(
            args.compare_latency_tolerance_ms,
            defaults=DEFAULT_COMPARE_LATENCY_TOLERANCE_MS,
            arg_name="--compare-latency-tolerance-ms",
        ),
        "recall_tolerance": _numeric_override_map(
            args.compare_recall_tolerance,
            defaults=DEFAULT_COMPARE_RECALL_TOLERANCE,
            arg_name="--compare-recall-tolerance",
        ),
    }


def _case_report(result: EvalResult, case: Mapping[str, Any] | None) -> dict[str, Any]:
    case = case or {}
    status = _result_status(result)
    suites = sorted(_case_suites(case)) if case else []
    metadata = dict(result.metadata)
    report: dict[str, Any] = {
        "id": result.case_id,
        "status": status,
        "passed": result.passed,
        "skipped": result.skipped,
        "detail": result.detail,
        "duration_ms": _round_ms(result.duration_ms),
        "timeout": bool(metadata.pop("timeout", False)),
        "suite": suites,
        "kind": case.get("kind"),
        "risk": case.get("risk"),
        "requires_llm": case.get("requires_llm"),
        "requires_embedding": case.get("requires_embedding"),
        "requires_db": case.get("requires_db"),
    }
    if status == "fail":
        report["failure_detail"] = result.detail
    metrics = metadata.pop("metrics", None)
    if metrics is not None:
        report["metrics"] = metrics
    for key in (
        "route",
        "intent",
        "data_kind",
        "retrieval_mode",
        "fallback_reason",
        "embedding_provider",
        "embedding_model",
        "embedding_dimension",
    ):
        if key in metadata and metadata[key] not in (None, ""):
            report[key] = metadata[key]
    if metadata:
        report["extra"] = metadata
    return report


def _summary_for_reports(reports: list[Mapping[str, Any]]) -> dict[str, Any]:
    total = len(reports)
    skipped = sum(1 for report in reports if report.get("status") == "skip")
    failed = sum(1 for report in reports if report.get("status") == "fail")
    passed = total - skipped - failed
    evaluated = total - skipped
    return {
        "total": total,
        "passed": passed,
        "failed": failed,
        "skipped": skipped,
        "pass_rate": round(passed / evaluated, 6) if evaluated else None,
    }


def _suite_summary_for_reports(reports: list[Mapping[str, Any]]) -> dict[str, Any]:
    summaries: dict[str, dict[str, Any]] = {}
    for report in reports:
        for suite in report.get("suite") or []:
            summaries.setdefault(suite, {"cases": []})["cases"].append(report)
    rendered: dict[str, Any] = {}
    for suite, payload in sorted(summaries.items()):
        suite_reports = payload["cases"]
        summary = _summary_for_reports(suite_reports)
        durations = [
            float(report["duration_ms"])
            for report in suite_reports
            if isinstance(report.get("duration_ms"), (int, float))
        ]
        summary["duration_ms_total"] = round(sum(durations), 3)
        summary["duration_ms_max"] = round(max(durations), 3) if durations else None
        rendered[suite] = summary
    return rendered


def _build_report(
    args: argparse.Namespace,
    *,
    golden: Mapping[str, Any],
    cases: list[Mapping[str, Any]],
    results: list[EvalResult],
    dataset_fingerprint: Mapping[str, Any] | None,
    thresholds: Mapping[str, Mapping[str, float]],
) -> dict[str, Any]:
    cases_by_id = {str(case["id"]): case for case in cases}
    case_reports = [
        _case_report(result, cases_by_id.get(result.case_id.split(":", 1)[0]))
        for result in results
    ]
    return {
        "schema_version": REPORT_SCHEMA_VERSION,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "git": _git_metadata(),
        "golden": {
            "path": str(args.golden),
            "version": golden.get("version"),
        },
        "selection": {
            "suite": sorted(_csv_values(args.suite)),
            "case": sorted(_csv_values(args.case_ids)),
            "case_count": len(cases),
        },
        "provider": _provider_metadata(args),
        "dataset_fingerprint": dataset_fingerprint,
        "thresholds": thresholds,
        "summary": _summary_for_reports(case_reports),
        "suite_summary": _suite_summary_for_reports(case_reports),
        "cases": case_reports,
    }


def _write_report(report: Mapping[str, Any], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as fh:
        json.dump(report, fh, indent=2, ensure_ascii=False)
        fh.write("\n")
    print(f"Wrote eval report JSON: {path}")


def _suite_threshold_value(
    suites: list[str],
    thresholds: Mapping[str, float],
    *,
    default: float,
) -> float:
    values = [thresholds[suite] for suite in suites if suite in thresholds]
    if values:
        return max(values)
    if "*" in thresholds:
        return thresholds["*"]
    return default


def _case_metric(report: Mapping[str, Any], name: str) -> float | None:
    value = (report.get("metrics") or {}).get(name)
    return float(value) if isinstance(value, (int, float)) else None


def _compare_case(
    baseline: Mapping[str, Any],
    current: Mapping[str, Any],
    *,
    thresholds: Mapping[str, Mapping[str, float]],
) -> tuple[str, str]:
    rank = {"fail": 0, "skip": 1, "pass": 2}
    before = str(baseline.get("status"))
    after = str(current.get("status"))
    regressions: list[str] = []
    improvements: list[str] = []
    if rank.get(after, 0) < rank.get(before, 0):
        regressions.append(f"status {before} -> {after}: {current.get('detail')}")
    elif rank.get(after, 0) > rank.get(before, 0):
        improvements.append(f"status {before} -> {after}")

    suites = [str(suite) for suite in current.get("suite") or baseline.get("suite") or []]
    latency_tolerance = _suite_threshold_value(
        suites,
        thresholds["latency_tolerance_ms"],
        default=0.0,
    )
    before_ms = baseline.get("duration_ms")
    after_ms = current.get("duration_ms")
    if isinstance(before_ms, (int, float)) and isinstance(after_ms, (int, float)):
        delta = float(after_ms) - float(before_ms)
        if delta > latency_tolerance:
            regressions.append(
                f"latency +{delta:.1f}ms exceeds tolerance {latency_tolerance:.1f}ms",
            )
        elif delta < -latency_tolerance:
            improvements.append(
                f"latency {delta:.1f}ms beats tolerance {latency_tolerance:.1f}ms",
            )

    recall_tolerance = _suite_threshold_value(
        suites,
        thresholds["recall_tolerance"],
        default=0.0,
    )
    before_recall = _case_metric(baseline, "recall_at_k")
    after_recall = _case_metric(current, "recall_at_k")
    if before_recall is not None and after_recall is not None:
        delta = after_recall - before_recall
        if delta < -recall_tolerance:
            regressions.append(
                f"recall_at_k {before_recall:.3f} -> {after_recall:.3f}",
            )
        elif delta > recall_tolerance:
            improvements.append(
                f"recall_at_k {before_recall:.3f} -> {after_recall:.3f}",
            )

    if regressions:
        return "regressed", "; ".join(regressions)
    if improvements:
        return "improved", "; ".join(improvements)
    return "unchanged", "within configured thresholds"


def _compare_reports(
    current_report: Mapping[str, Any],
    baseline_path: Path,
    *,
    thresholds: Mapping[str, Mapping[str, float]],
) -> bool:
    with baseline_path.open("r", encoding="utf-8") as fh:
        baseline_report = json.load(fh)

    baseline_cases = {str(case["id"]): case for case in baseline_report.get("cases", [])}
    current_cases = {str(case["id"]): case for case in current_report.get("cases", [])}
    buckets: dict[str, list[tuple[str, str]]] = {
        "improved": [],
        "unchanged": [],
        "regressed": [],
        "added": [],
        "removed": [],
    }
    for case_id, current in sorted(current_cases.items()):
        baseline = baseline_cases.get(case_id)
        if baseline is None:
            buckets["added"].append((case_id, "not present in baseline report"))
            continue
        bucket, reason = _compare_case(baseline, current, thresholds=thresholds)
        buckets[bucket].append((case_id, reason))
    for case_id in sorted(set(baseline_cases) - set(current_cases)):
        buckets["removed"].append((case_id, "not present in current report"))

    suite_violations: list[str] = []
    suite_summary = current_report.get("suite_summary") or {}
    for suite, threshold in sorted(thresholds["pass_rate"].items()):
        if suite == "*":
            summary = current_report.get("summary") or {}
        else:
            summary = suite_summary.get(suite)
        if not summary:
            continue
        pass_rate = summary.get("pass_rate")
        if isinstance(pass_rate, (int, float)) and float(pass_rate) < threshold:
            suite_violations.append(
                f"{suite}: pass_rate {float(pass_rate):.3f} < {threshold:.3f}"
            )

    print(f"\nBaseline comparison: {baseline_path}")
    print(
        "Comparison summary: "
        f"{len(buckets['improved'])} improved, "
        f"{len(buckets['unchanged'])} unchanged, "
        f"{len(buckets['regressed'])} regressed, "
        f"{len(buckets['added'])} added, "
        f"{len(buckets['removed'])} removed"
    )
    for bucket in ("regressed", "improved", "added", "removed"):
        if not buckets[bucket]:
            continue
        print(f"{bucket.upper()}:")
        for case_id, reason in buckets[bucket][:20]:
            print(f"  - {case_id}: {reason}")
        if len(buckets[bucket]) > 20:
            print(f"  ... {len(buckets[bucket]) - 20} more")
    if suite_violations:
        print("SUITE THRESHOLD VIOLATIONS:")
        for violation in suite_violations:
            print(f"  - {violation}")
    return bool(buckets["regressed"] or buckets["removed"] or suite_violations)


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Run FDAgent contract-tagged golden eval suites.")
    p.add_argument("--golden", type=Path, default=DEFAULT_GOLDEN,
                   help=f"golden set path (default: {DEFAULT_GOLDEN})")
    p.add_argument("--core-pr-gate", action="store_true",
                   help="run the full PR-blocking core suite and enforce gate rules")
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
    p.add_argument("--dataset-fingerprint-baseline", type=Path,
                   default=DEFAULT_DATASET_FINGERPRINT_BASELINE,
                   help="expected stable fixture fingerprint baseline path")
    p.add_argument("--dataset-fingerprint-table", default=DEFAULT_DATASET_FINGERPRINT_TABLE,
                   help="source table checked by the stable fixture fingerprint preflight")
    p.add_argument("--skip-dataset-fingerprint", action="store_true",
                   help="explicitly skip the stable fixture fingerprint preflight")
    p.add_argument("--report-json", type=Path,
                   help="write a durable machine-readable eval baseline/report artifact")
    p.add_argument("--compare-baseline", type=Path,
                   help="compare this run against a prior --report-json artifact")
    p.add_argument("--compare-pass-rate-threshold", action="append",
                   help="suite pass-rate floor for comparison, as SUITE=FLOAT; repeatable")
    p.add_argument("--compare-latency-tolerance-ms", action="append",
                   help="latency delta tolerance for comparison, as SUITE=MS or *=MS; repeatable")
    p.add_argument("--compare-recall-tolerance", action="append",
                   help="recall_at_k delta tolerance for comparison, as SUITE=FLOAT; repeatable")
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
        thresholds = _compare_thresholds(args)
        golden = _load_golden(args.golden)
        suite_filters = _csv_values(args.suite)
        case_filters = _csv_values(args.case_ids)
        if args.core_pr_gate and not suite_filters:
            suite_filters = {CORE_PR_GATE_SUITE}
            args.suite = [CORE_PR_GATE_SUITE]
        cases = _select_cases(
            golden,
            suite_filters=suite_filters,
            case_filters=case_filters,
        )
        _validate_core_pr_gate_args(
            args,
            golden,
            suite_filters=suite_filters,
            case_filters=case_filters,
            selected_cases=cases,
        )
    except EvalFailure as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2

    if args.list_cases:
        for case in cases:
            suites = ",".join(sorted(_case_suites(case)))
            print(f"{case['id']}\t{suites}\t{case['kind']}\t{case['risk']}")
        return 0

    preflight_exit, dataset_fingerprint = _run_dataset_fingerprint_preflight(args, cases)
    if preflight_exit:
        return preflight_exit

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
        started = time.monotonic()
        case_results: list[EvalResult] = []
        try:
            kind = case.get("kind")
            if kind == "ask":
                if ask_fn is None:
                    ask_fn = _build_ask_fn(args)
                answer = ask_fn(str(case["question"]))
                case_results.append(_assert_ask_case(case, answer))
                judge_result = _maybe_judge(case, answer, enabled=args.llm_judge)
                if judge_result:
                    case_results.append(judge_result)
            elif kind == "deterministic_helper":
                case_results.append(_assert_deterministic_helper_case(case))
            elif kind == "embedding_provider_config":
                case_results.append(_assert_embedding_provider_config_case(case))
            elif kind == "retrieval_recall":
                case_results.append(_run_retrieval_case(case, dsn=args.dsn))
            else:
                raise EvalFailure(f"unknown case kind {kind!r}")
        except Exception as exc:  # noqa: BLE001 - eval runner must report every case failure
            skipped = _provider_unavailable_skip_result(case, exc)
            if skipped is not None:
                case_results.append(skipped)
            else:
                case_results.append(EvalResult(
                    case_id,
                    False,
                    f"{type(exc).__name__}: {exc}",
                    metadata={"timeout": _is_timeout_exception(exc)},
                ))
        duration_ms = (time.monotonic() - started) * 1000
        results.extend(replace(result, duration_ms=duration_ms) for result in case_results)

    failed = [r for r in results if not r.passed and not r.skipped]
    skipped = [r for r in results if r.skipped]
    for result in results:
        status = "SKIP" if result.skipped else ("PASS" if result.passed else "FAIL")
        print(f"{status} {result.case_id}: {result.detail}")
    passed = len(results) - len(failed) - len(skipped)
    print(f"\nSummary: {passed} passed, {len(failed)} failed, {len(skipped)} skipped")
    report = _build_report(
        args,
        golden=golden,
        cases=cases,
        results=results,
        dataset_fingerprint=dataset_fingerprint,
        thresholds=thresholds,
    )
    if args.report_json:
        _write_report(report, args.report_json)
    compare_failed = False
    if args.compare_baseline:
        try:
            compare_failed = _compare_reports(
                report,
                args.compare_baseline,
                thresholds=thresholds,
            )
        except (OSError, json.JSONDecodeError, EvalFailure) as exc:
            print(f"ERROR: baseline comparison failed: {exc}", file=sys.stderr)
            return 2
    core_gate_failed = _core_pr_gate_failed(cases, results) if args.core_pr_gate else False
    return 1 if failed or compare_failed or core_gate_failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
