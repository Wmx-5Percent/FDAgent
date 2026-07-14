"""Focused smoke checks for /hybrid-search bad-filter handling.

These cases exercise the invalid inputs called out in PR review without needing a database:
the endpoint should reject them during request validation and raise HTTP 400 before any SQL
search is attempted.

Run from repo root:
    .venv/bin/python scripts/check_hybrid_filter_validation.py
"""
from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
import sys
from typing import Any

from fastapi import HTTPException

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import src.api as api


@dataclass(frozen=True)
class _FakeEmbedConfig:
    provider: str = "test"
    model: str = "test-embedding-model"


class _FakeEngine:
    embed_config = _FakeEmbedConfig()


class _FakeHybridSearchLogger:
    def __init__(self) -> None:
        self.entries: list[Any] = []

    def write(self, entry: Any) -> int:
        self.entries.append(entry)
        return len(self.entries)


def _assert_bad_filter(
    name: str,
    filters: dict[str, Any],
    expected_detail: str,
    *,
    forbidden_log_fragment: str | None = None,
) -> None:
    old_engine = api._engine
    old_logger = api._hybrid_search_logger
    fake_logger = _FakeHybridSearchLogger()
    api._engine = _FakeEngine()  # type: ignore[assignment]
    api._hybrid_search_logger = fake_logger  # type: ignore[assignment]
    try:
        req = api.HybridSearchRequest(
            query="pills too strong",
            field="both",
            k=3,
            filters=filters,
        )
        try:
            api.hybrid_search_endpoint(req)
        except HTTPException as exc:
            if exc.status_code != 400:
                raise AssertionError(
                    f"{name}: expected HTTP 400, got {exc.status_code}: {exc.detail}"
                ) from exc
            detail = str(exc.detail)
            if expected_detail not in detail:
                raise AssertionError(
                    f"{name}: expected detail containing {expected_detail!r}, got {detail!r}"
                ) from exc
        else:
            raise AssertionError(f"{name}: expected HTTPException")
        if not fake_logger.entries:
            raise AssertionError(f"{name}: expected invalid request to be logged")
        if fake_logger.entries[-1].error_type != "ValueError":
            raise AssertionError(
                f"{name}: expected ValueError log, got {fake_logger.entries[-1].error_type!r}"
            )
        request_payload = fake_logger.entries[-1].request
        if request_payload.get("filters", {}).get("omitted_raw") is not True:
            raise AssertionError(f"{name}: expected raw filters to be omitted from log request")
        if fake_logger.entries[-1].filters != {"items": []}:
            raise AssertionError(f"{name}: expected normalized log filters to be empty")
        if forbidden_log_fragment:
            serialized_log = json.dumps({
                "request": request_payload,
                "filters": fake_logger.entries[-1].filters,
            }, ensure_ascii=False)
            if forbidden_log_fragment in serialized_log:
                raise AssertionError(
                    f"{name}: oversized raw filter fragment leaked into log payload"
                )
    finally:
        api._engine = old_engine
        api._hybrid_search_logger = old_logger


def main() -> None:
    _assert_bad_filter(
        "non_string_dimension_value",
        {"classification": 123},
        "filter 'classification' must be a string",
    )
    _assert_bad_filter(
        "invalid_date_ilike",
        {"report_date": {"ilike": "2024-01-01"}},
        "not allowed for date column 'report_date'",
    )
    _assert_bad_filter(
        "too_long_filter_string",
        {"classification": "x" * (api.HYBRID_FILTER_STRING_MAX_CHARS + 1)},
        f"exceeds {api.HYBRID_FILTER_STRING_MAX_CHARS} characters",
        forbidden_log_fragment="x" * (api.HYBRID_FILTER_STRING_MAX_CHARS + 1),
    )
    _assert_bad_filter(
        "too_many_in_items",
        {"classification": [f"Class {i}" for i in range(api.HYBRID_FILTER_IN_MAX_ITEMS + 1)]},
        f"at most {api.HYBRID_FILTER_IN_MAX_ITEMS} items",
    )
    _assert_bad_filter(
        "too_large_filter_payload",
        {"classification": ["x" * api.HYBRID_FILTER_STRING_MAX_CHARS
                            for _ in range(api.HYBRID_FILTER_IN_MAX_ITEMS)]},
        f"exceeds {api.HYBRID_FILTER_MAX_SERIALIZED_BYTES} bytes",
        forbidden_log_fragment="x" * api.HYBRID_FILTER_STRING_MAX_CHARS,
    )
    print("hybrid filter validation smoke checks passed")


if __name__ == "__main__":
    main()
