"""Focused smoke checks for /hybrid-search bad-filter handling.

These cases exercise the invalid inputs called out in PR review without needing a database:
the endpoint should reject them during request validation and raise HTTP 400 before any SQL
search is attempted.

Run from repo root:
    .venv/bin/python scripts/check_hybrid_filter_validation.py
"""
from __future__ import annotations

from dataclasses import dataclass
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


def _assert_bad_filter(name: str, filters: dict[str, Any], expected_detail: str) -> None:
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
    print("hybrid filter validation smoke checks passed")


if __name__ == "__main__":
    main()
