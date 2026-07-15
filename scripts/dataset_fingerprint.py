#!/usr/bin/env python3
"""Compute and check stable openFDA dataset fingerprints for eval fixtures."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

import psycopg

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DSN = os.environ.get("DATABASE_URL", "postgresql://localhost:5432/fda")
DEFAULT_TABLE = "drug_enforcement"
DEFAULT_BASELINE = REPO_ROOT / "evals" / "baselines" / "drug_enforcement_fingerprint.json"

DATASET_NAME = "openFDA drug/enforcement"
FINGERPRINT_VERSION = "drug_enforcement_raw_v1"
STABLE_FIELDS = ("id", "source", "report_date", "raw")
EXCLUDED_FIELDS = ("fetched_at",)
COMPARE_FIELDS = (
    "dataset",
    "source_table",
    "fingerprint_version",
    "stable_fields",
    "excluded_fields",
    "row_count",
    "report_date_min",
    "report_date_max",
    "distinct_recall_numbers",
    "distinct_event_ids",
    "taxonomy_label_rows",
    "taxonomy_covered_records",
    "taxonomy_versions",
    "taxonomy_labelers",
    "embedding_rows",
    "embedding_covered_records",
    "embedding_fields",
    "source_column_count",
    "source_index_count",
    "source_schema_sha256",
    "sha256",
)
DATASET_DRIFT_EXIT_CODE = 3

_IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


class DatasetFingerprintError(RuntimeError):
    """Raised when a dataset fingerprint cannot be computed or checked."""


class DatasetFingerprintMismatch(DatasetFingerprintError):
    """Raised when the active dataset differs from the pinned baseline."""


@dataclass(frozen=True)
class FingerprintCheck:
    expected: Mapping[str, Any]
    actual: Mapping[str, Any]


def _quote_identifier(name: str) -> str:
    parts = name.split(".")
    if not parts or any(not _IDENTIFIER.fullmatch(part) for part in parts):
        raise DatasetFingerprintError(
            f"invalid SQL identifier {name!r}; use simple identifiers like drug_enforcement"
        )
    return ".".join(f'"{part}"' for part in parts)


def _canonical_json(value: Mapping[str, Any]) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _normalize_raw(raw: Any) -> Any:
    if isinstance(raw, str):
        return json.loads(raw)
    return raw


def _render_json(payload: Mapping[str, Any]) -> str:
    text = json.dumps(payload, indent=2, ensure_ascii=False)
    if text.startswith('{\n  "description": '):
        text = text.replace('{\n  "description": ', '{"description": ', 1)
    return text + "\n"


def _table_exists(cur: psycopg.Cursor[Any], table: str) -> bool:
    cur.execute("SELECT to_regclass(%s) IS NOT NULL", (table,))
    row = cur.fetchone()
    return bool(row and row[0])


def _sidecar_counts(cur: psycopg.Cursor[Any], *, source_table: str) -> dict[str, int | None]:
    counts: dict[str, int | None] = {
        "taxonomy_label_rows": None,
        "taxonomy_covered_records": None,
        "taxonomy_versions": None,
        "taxonomy_labelers": None,
        "embedding_rows": None,
        "embedding_covered_records": None,
        "embedding_fields": None,
    }
    if _table_exists(cur, "recall_label"):
        cur.execute(
            """
            SELECT
                count(*)::bigint,
                count(DISTINCT record_id)::bigint,
                count(DISTINCT version)::bigint,
                count(DISTINCT labeler)::bigint
            FROM recall_label
            """
        )
        row = cur.fetchone()
        if row is not None:
            (
                counts["taxonomy_label_rows"],
                counts["taxonomy_covered_records"],
                counts["taxonomy_versions"],
                counts["taxonomy_labelers"],
            ) = [int(value) for value in row]
    if _table_exists(cur, "embeddings"):
        cur.execute(
            """
            SELECT
                count(*)::bigint,
                count(DISTINCT source_id)::bigint,
                count(DISTINCT field)::bigint
            FROM embeddings
            WHERE source = %s
            """,
            (source_table,),
        )
        row = cur.fetchone()
        if row is not None:
            (
                counts["embedding_rows"],
                counts["embedding_covered_records"],
                counts["embedding_fields"],
            ) = [int(value) for value in row]
    return counts


def _schema_counts(cur: psycopg.Cursor[Any], *, table: str) -> dict[str, int | str]:
    parts = table.split(".")
    schema = parts[0] if len(parts) == 2 else "public"
    table_name = parts[-1]
    cur.execute(
        """
        SELECT column_name, data_type, udt_name, is_nullable, column_default
        FROM information_schema.columns
        WHERE table_schema = %s AND table_name = %s
        ORDER BY ordinal_position
        """,
        (schema, table_name),
    )
    columns = cur.fetchall()
    cur.execute(
        """
        SELECT indexname, indexdef
        FROM pg_indexes
        WHERE schemaname = %s AND tablename = %s
        ORDER BY indexname
        """,
        (schema, table_name),
    )
    indexes = cur.fetchall()
    schema_payload = {
        "columns": columns,
        "indexes": indexes,
    }
    return {
        "source_column_count": len(columns),
        "source_index_count": len(indexes),
        "source_schema_sha256": hashlib.sha256(
            _canonical_json(schema_payload).encode("utf-8")
        ).hexdigest(),
    }


def compute_fingerprint(*, dsn: str = DEFAULT_DSN, table: str = DEFAULT_TABLE) -> dict[str, Any]:
    qualified_table = _quote_identifier(table)
    hasher = hashlib.sha256()

    try:
        with psycopg.connect(dsn) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    f"""
                    SELECT
                        count(*)::bigint,
                        to_char(min(report_date), 'YYYY-MM-DD'),
                        to_char(max(report_date), 'YYYY-MM-DD'),
                        count(DISTINCT raw->>'recall_number')::bigint,
                        count(DISTINCT raw->>'event_id')::bigint
                    FROM {qualified_table}
                    """
                )
                stats = cur.fetchone()
                if stats is None:
                    raise DatasetFingerprintError(f"no stats returned for {table}")

                cur.execute(
                    f"""
                    SELECT id, source, to_char(report_date, 'YYYY-MM-DD'), raw
                    FROM {qualified_table}
                    ORDER BY id
                    """
                )
                for row_id, source, report_date, raw in cur:
                    row_payload = {
                        "id": row_id,
                        "source": source,
                        "report_date": report_date,
                        "raw": _normalize_raw(raw),
                    }
                    hasher.update(_canonical_json(row_payload).encode("utf-8"))
                    hasher.update(b"\n")
                sidecar_counts = _sidecar_counts(cur, source_table=table)
                schema_counts = _schema_counts(cur, table=table)
    except (psycopg.Error, OSError, json.JSONDecodeError) as exc:
        raise DatasetFingerprintError(
            f"could not compute dataset fingerprint for {table}: {exc}"
        ) from exc

    row_count, date_min, date_max, recall_count, event_count = stats
    return {
        "description": "Pinned stable fixture fingerprint for FDAgent eval preflight.",
        "dataset": DATASET_NAME,
        "source_table": table,
        "fingerprint_version": FINGERPRINT_VERSION,
        "stable_fields": list(STABLE_FIELDS),
        "excluded_fields": list(EXCLUDED_FIELDS),
        "row_count": int(row_count),
        "report_date_min": date_min,
        "report_date_max": date_max,
        "distinct_recall_numbers": int(recall_count),
        "distinct_event_ids": int(event_count),
        **sidecar_counts,
        **schema_counts,
        "sha256": hasher.hexdigest(),
    }


def load_baseline(path: Path) -> dict[str, Any]:
    try:
        with path.open("r", encoding="utf-8") as fh:
            baseline = json.load(fh)
    except FileNotFoundError as exc:
        raise DatasetFingerprintError(
            f"dataset fingerprint baseline is missing: {path}"
        ) from exc
    except (OSError, json.JSONDecodeError) as exc:
        raise DatasetFingerprintError(
            f"could not load dataset fingerprint baseline {path}: {exc}"
        ) from exc
    if not isinstance(baseline, dict):
        raise DatasetFingerprintError(f"dataset fingerprint baseline must be an object: {path}")
    return baseline


def write_baseline(path: Path, payload: Mapping[str, Any]) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(_render_json(payload), encoding="utf-8")
    except OSError as exc:
        raise DatasetFingerprintError(
            f"could not write dataset fingerprint baseline {path}: {exc}"
        ) from exc


def fingerprint_mismatches(
    expected: Mapping[str, Any],
    actual: Mapping[str, Any],
) -> list[str]:
    mismatches: list[str] = []
    for field in COMPARE_FIELDS:
        if expected.get(field) != actual.get(field):
            mismatches.append(
                f"{field}: expected {expected.get(field)!r}, actual {actual.get(field)!r}"
            )
    return mismatches


def check_fingerprint(
    *,
    dsn: str = DEFAULT_DSN,
    table: str = DEFAULT_TABLE,
    baseline_path: Path = DEFAULT_BASELINE,
) -> FingerprintCheck:
    expected = load_baseline(baseline_path)
    actual = compute_fingerprint(dsn=dsn, table=table)
    mismatches = fingerprint_mismatches(expected, actual)
    if mismatches:
        details = "\n  ".join(mismatches)
        raise DatasetFingerprintMismatch(
            "DATASET DRIFT: dataset fingerprint mismatch; review fixture drift before blessing a new "
            f"baseline with scripts/dataset_fingerprint.py --write-baseline\n  {details}"
        )
    return FingerprintCheck(expected=expected, actual=actual)


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Compute or check the FDAgent dataset fingerprint.")
    p.add_argument("--dsn", default=DEFAULT_DSN,
                   help="Postgres DSN for reading the source fixture table")
    p.add_argument("--table", default=DEFAULT_TABLE,
                   help=f"source table to fingerprint (default: {DEFAULT_TABLE})")
    p.add_argument("--baseline", type=Path, default=DEFAULT_BASELINE,
                   help=f"baseline path (default: {DEFAULT_BASELINE})")
    mode = p.add_mutually_exclusive_group()
    mode.add_argument("--check", action="store_true",
                      help="compare the current dataset fingerprint to the baseline")
    mode.add_argument("--write-baseline", action="store_true",
                      help="explicitly write the current fingerprint as the baseline")
    return p.parse_args()


def main() -> int:
    args = parse_args()
    try:
        if args.write_baseline:
            actual = compute_fingerprint(dsn=args.dsn, table=args.table)
            write_baseline(args.baseline, actual)
            print(f"Wrote dataset fingerprint baseline: {args.baseline}")
            print(f"sha256={actual['sha256']} rows={actual['row_count']}")
            return 0
        if args.check:
            result = check_fingerprint(
                dsn=args.dsn,
                table=args.table,
                baseline_path=args.baseline,
            )
            actual = result.actual
            print(f"Dataset fingerprint OK: sha256={actual['sha256']} rows={actual['row_count']}")
            return 0
        print(_render_json(compute_fingerprint(dsn=args.dsn, table=args.table)), end="")
        return 0
    except DatasetFingerprintMismatch as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return DATASET_DRIFT_EXIT_CODE
    except DatasetFingerprintError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
