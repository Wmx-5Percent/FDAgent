"""Audit taxonomy coverage and exact offline recall-label counts.

This is a taxonomy-side validation/reporting CLI only. It reads the sidecar
tables and can answer exact label-backed counts such as "sterility by raw
recalling_firm" after recall_label is populated, without wiring /ask.

Run:
    .venv/bin/python src/classify/audit.py coverage --version v1
    .venv/bin/python src/classify/audit.py count-by --version v1 --node-id sterility_assurance --dimension recalling_firm
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
from typing import Any

import psycopg
from dotenv import load_dotenv
from psycopg import sql

load_dotenv()

DEFAULT_DSN = os.environ.get("DATABASE_URL", "postgresql://localhost:5432/fda")
DEFAULT_LABELER = os.environ.get("TAXONOMY_LABELER", "llm:gpt-4o-mini")

DIMENSIONS = {
    "classification",
    "status",
    "product_type",
    "voluntary_mandated",
    "initial_firm_notification",
    "recalling_firm",
    "state",
    "country",
    "city",
    "distribution_pattern",
}


def _labeler_clause(labeler: str | None) -> tuple[str, list[Any]]:
    if labeler:
        return "AND labeler = %s", [labeler]
    return "", []


def taxonomy_nodes(conn: psycopg.Connection, version: str) -> list[dict[str, Any]]:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT node_id, parent_id, label, level, status
            FROM taxonomy
            WHERE version = %s
            ORDER BY level, node_id
            """,
            [version],
        )
        return [
            {
                "node_id": row[0],
                "parent_id": row[1],
                "label": row[2],
                "level": row[3],
                "status": row[4],
            }
            for row in cur.fetchall()
        ]


def descendant_node_ids(conn: psycopg.Connection, *, version: str, node_id: str,
                        include_descendants: bool) -> list[str]:
    if not include_descendants:
        return [node_id]
    with conn.cursor() as cur:
        cur.execute(
            """
            WITH RECURSIVE subtree AS (
                SELECT node_id
                FROM taxonomy
                WHERE version = %s AND node_id = %s
              UNION ALL
                SELECT child.node_id
                FROM taxonomy child
                JOIN subtree parent ON child.parent_id = parent.node_id
                WHERE child.version = %s
            )
            SELECT node_id FROM subtree ORDER BY node_id
            """,
            [version, node_id, version],
        )
        rows = [row[0] for row in cur.fetchall()]
    if not rows:
        raise ValueError(f"unknown taxonomy node {node_id!r} for version {version!r}")
    return rows


def coverage_report(conn: psycopg.Connection, *, version: str, labeler: str | None,
                    min_confidence: float) -> dict[str, Any]:
    labeler_sql, labeler_params = _labeler_clause(labeler)
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT count(*)::integer,
                   count(DISTINCT reason_for_recall)::integer
            FROM drug_enforcement
            WHERE reason_for_recall IS NOT NULL AND length(btrim(reason_for_recall)) > 0
            """
        )
        total_records, distinct_reasons = cur.fetchone()
        cur.execute(
            f"""
            SELECT count(DISTINCT record_id)::integer
            FROM recall_label
            WHERE version = %s
              AND confidence >= %s
              {labeler_sql}
            """,
            [version, min_confidence, *labeler_params],
        )
        labeled_records = cur.fetchone()[0]
        cur.execute(
            f"""
            SELECT rl.node_id, t.label, count(DISTINCT rl.record_id)::integer AS n
            FROM recall_label rl
            LEFT JOIN taxonomy t ON t.version = rl.version AND t.node_id = rl.node_id
            WHERE rl.version = %s
              AND rl.confidence >= %s
              {labeler_sql}
            GROUP BY rl.node_id, t.label
            ORDER BY n DESC, rl.node_id
            """,
            [version, min_confidence, *labeler_params],
        )
        label_counts = [
            {"node_id": row[0], "label": row[1], "count": row[2]}
            for row in cur.fetchall()
        ]
    return {
        "version": version,
        "labeler": labeler,
        "min_confidence": min_confidence,
        "taxonomy_nodes": taxonomy_nodes(conn, version),
        "source_records_with_reason": total_records,
        "distinct_reason_texts": distinct_reasons,
        "labeled_records": labeled_records,
        "unlabeled_records": max(total_records - labeled_records, 0),
        "coverage": round(labeled_records / total_records, 4) if total_records else 0.0,
        "label_counts": label_counts,
    }


def count_by_report(
    conn: psycopg.Connection,
    *,
    version: str,
    node_id: str,
    dimension: str,
    labeler: str | None,
    min_confidence: float,
    include_descendants: bool,
    limit: int,
    evidence_n: int,
) -> dict[str, Any]:
    if dimension not in DIMENSIONS:
        raise ValueError(f"unsupported dimension {dimension!r}; allowed: {sorted(DIMENSIONS)}")
    node_ids = descendant_node_ids(
        conn,
        version=version,
        node_id=node_id,
        include_descendants=include_descendants,
    )
    labeler_sql, labeler_params = _labeler_clause(labeler)
    q = sql.SQL(
        """
        WITH labeled AS (
            SELECT DISTINCT record_id
            FROM recall_label
            WHERE version = %s
              AND node_id = ANY(%s)
              AND confidence >= %s
              {labeler_clause}
        )
        SELECT d.{dimension} AS value,
               count(*)::integer AS n,
               (array_agg(d.recall_number ORDER BY d.recall_initiation_date DESC NULLS LAST))[1:%s] AS evidence
        FROM labeled l
        JOIN drug_enforcement d ON d.id = l.record_id
        GROUP BY d.{dimension}
        ORDER BY n DESC, value NULLS LAST
        LIMIT %s
        """
    ).format(
        dimension=sql.Identifier(dimension),
        labeler_clause=sql.SQL(labeler_sql),
    )
    params = [version, node_ids, min_confidence, *labeler_params, evidence_n, limit]
    with conn.cursor() as cur:
        cur.execute(q, params)
        rows = cur.fetchall()
    return {
        "version": version,
        "node_id": node_id,
        "included_node_ids": node_ids,
        "dimension": dimension,
        "labeler": labeler,
        "min_confidence": min_confidence,
        "include_descendants": include_descendants,
        "groups": [
            {
                "value": row[0],
                "count": row[1],
                "evidence": list(row[2] or []),
            }
            for row in rows
        ],
    }


def write_or_print(payload: dict[str, Any], output_file: str | None) -> None:
    text = json.dumps(payload, indent=2, ensure_ascii=False, default=str) + "\n"
    if output_file:
        out = Path(output_file)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(text, encoding="utf-8")
        print(f"wrote {output_file}")
        return
    print(text, end="")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Audit taxonomy coverage and exact offline label counts.")
    parser.add_argument("--dsn", default=DEFAULT_DSN, help="Postgres DSN")
    sub = parser.add_subparsers(dest="command", required=True)

    coverage = sub.add_parser("coverage", help="report taxonomy/label coverage")
    coverage.add_argument("--version", default="v1", help="taxonomy version")
    coverage.add_argument("--labeler", default=DEFAULT_LABELER,
                          help="labeler to inspect; use empty string for all labelers")
    coverage.add_argument("--min-confidence", type=float, default=0.0,
                          help="minimum label confidence")
    coverage.add_argument("--output-file", default=None, help="optional JSON output path")

    count_by = sub.add_parser("count-by", help="count labeled records by a source dimension")
    count_by.add_argument("--version", default="v1", help="taxonomy version")
    count_by.add_argument("--node-id", required=True, help="taxonomy node_id to count")
    count_by.add_argument("--dimension", default="recalling_firm", choices=sorted(DIMENSIONS),
                          help="drug_enforcement dimension")
    count_by.add_argument("--labeler", default=DEFAULT_LABELER,
                          help="labeler to inspect; use empty string for all labelers")
    count_by.add_argument("--min-confidence", type=float, default=0.0,
                          help="minimum label confidence")
    count_by.add_argument("--include-descendants", action=argparse.BooleanOptionalAction, default=True,
                          help="include descendant taxonomy nodes")
    count_by.add_argument("--limit", type=int, default=20, help="max groups")
    count_by.add_argument("--evidence-n", type=int, default=3, help="recall_number evidence per group")
    count_by.add_argument("--output-file", default=None, help="optional JSON output path")

    return parser.parse_args()


def clean_labeler(labeler: str | None) -> str | None:
    if labeler is None:
        return None
    labeler = labeler.strip()
    return labeler or None


def main() -> None:
    args = parse_args()
    with psycopg.connect(args.dsn) as conn:
        if args.command == "coverage":
            payload = coverage_report(
                conn,
                version=args.version,
                labeler=clean_labeler(args.labeler),
                min_confidence=args.min_confidence,
            )
            write_or_print(payload, args.output_file)
            return
        if args.command == "count-by":
            payload = count_by_report(
                conn,
                version=args.version,
                node_id=args.node_id,
                dimension=args.dimension,
                labeler=clean_labeler(args.labeler),
                min_confidence=args.min_confidence,
                include_descendants=args.include_descendants,
                limit=args.limit,
                evidence_n=args.evidence_n,
            )
            write_or_print(payload, args.output_file)
            return
    raise ValueError(f"unknown command {args.command!r}")


if __name__ == "__main__":
    main()
