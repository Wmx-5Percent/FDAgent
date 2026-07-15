#!/usr/bin/env python3
"""Smoke-check that baseline comparison reports recall regressions before improvements."""
from __future__ import annotations

import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))

from run_eval import (  # noqa: E402
    DEFAULT_COMPARE_LATENCY_TOLERANCE_MS,
    DEFAULT_COMPARE_PASS_RATE_THRESHOLDS,
    DEFAULT_COMPARE_RECALL_TOLERANCE,
    _compare_case,
)


def _thresholds() -> dict[str, dict[str, float]]:
    return {
        "pass_rate": dict(DEFAULT_COMPARE_PASS_RATE_THRESHOLDS),
        "latency_tolerance_ms": dict(DEFAULT_COMPARE_LATENCY_TOLERANCE_MS),
        "recall_tolerance": dict(DEFAULT_COMPARE_RECALL_TOLERANCE),
    }


def _case(*, duration_ms: float, recall_at_k: float) -> dict[str, object]:
    return {
        "id": "rag-recall-latency-precedence-smoke",
        "status": "pass",
        "suite": ["rag"],
        "duration_ms": duration_ms,
        "metrics": {"recall_at_k": recall_at_k},
    }


def main() -> int:
    bucket, reason = _compare_case(
        _case(duration_ms=10_000.0, recall_at_k=1.0),
        _case(duration_ms=1.0, recall_at_k=0.0),
        thresholds=_thresholds(),
    )
    if bucket != "regressed" or "recall_at_k" not in reason:
        print(
            "FAIL: recall regression must win over latency improvement; "
            f"got bucket={bucket!r} reason={reason!r}",
            file=sys.stderr,
        )
        return 1

    bucket, reason = _compare_case(
        _case(duration_ms=10_000.0, recall_at_k=1.0),
        _case(duration_ms=1.0, recall_at_k=1.0),
        thresholds=_thresholds(),
    )
    if bucket != "improved" or "latency" not in reason:
        print(
            "FAIL: pure latency improvement should still be reported; "
            f"got bucket={bucket!r} reason={reason!r}",
            file=sys.stderr,
        )
        return 1

    print("baseline compare regression precedence smoke OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
