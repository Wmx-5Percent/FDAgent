"""Seed the frozen v1 recall-reason taxonomy into the taxonomy sidecar table.

This is the human-governed freeze point for Phase 4 taxonomy v1. By default it
prints/writes a dry-run report only. Database writes happen only with --apply.

Run:
    .venv/bin/python src/classify/seed_v1.py
    .venv/bin/python src/classify/seed_v1.py --apply
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any

import psycopg
from dotenv import load_dotenv

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from classify import taxonomy_v1  # noqa: E402

load_dotenv()

DEFAULT_DSN = os.environ.get("DATABASE_URL", "postgresql://localhost:5432/fda")
DEFAULT_OUTPUT = "data/processed/taxonomy_v1_seed_report.json"


def node_rows(version: str, status: str) -> list[tuple[Any, ...]]:
    taxonomy_v1.validate()
    return [
        (
            version,
            node.node_id,
            node.parent_id,
            node.label,
            node.definition,
            list(node.examples),
            node.level,
            status,
        )
        for node in sorted(taxonomy_v1.NODES, key=lambda item: (item.level, item.parent_id or "", item.node_id))
    ]


def existing_nodes(conn: psycopg.Connection, version: str) -> dict[str, str]:
    with conn.cursor() as cur:
        cur.execute("SELECT node_id, status FROM taxonomy WHERE version = %s ORDER BY node_id", [version])
        return {node_id: status for node_id, status in cur.fetchall()}


def apply_seed(conn: psycopg.Connection, *, version: str, status: str) -> int:
    rows = node_rows(version, status)
    with conn.cursor() as cur:
        cur.executemany(
            """
            INSERT INTO taxonomy
                (version, node_id, parent_id, label, definition, examples, level, status)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (version, node_id) DO UPDATE
                SET parent_id = EXCLUDED.parent_id,
                    label = EXCLUDED.label,
                    definition = EXCLUDED.definition,
                    examples = EXCLUDED.examples,
                    level = EXCLUDED.level,
                    status = EXCLUDED.status,
                    updated_at = now()
            """,
            rows,
        )
    conn.commit()
    return len(rows)


def write_report(path: str, payload: dict[str, Any]) -> None:
    out = Path(path)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def report_payload(
    *,
    version: str,
    status: str,
    dry_run: bool,
    before: dict[str, str],
    applied_nodes: int,
) -> dict[str, Any]:
    parents = [node for node in taxonomy_v1.NODES if node.level == 0]
    children = [node for node in taxonomy_v1.NODES if node.level > 0]
    return {
        "version": version,
        "status": status,
        "dry_run": dry_run,
        "existing_nodes_before": before,
        "node_count": len(taxonomy_v1.NODES),
        "parent_count": len(parents),
        "child_count": len(children),
        "applied_nodes": applied_nodes,
        "nodes": [
            {
                "node_id": node.node_id,
                "parent_id": node.parent_id,
                "label": node.label,
                "definition": node.definition,
                "examples": list(node.examples),
                "level": node.level,
            }
            for node in taxonomy_v1.NODES
        ],
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Seed the frozen taxonomy v1 into the taxonomy table.")
    parser.add_argument("--dsn", default=DEFAULT_DSN, help="Postgres DSN")
    parser.add_argument("--version", default=taxonomy_v1.VERSION, help="taxonomy version to seed")
    parser.add_argument("--status", choices=("draft", "active"), default="active",
                        help="status to write when --apply is set")
    parser.add_argument("--output-file", default=DEFAULT_OUTPUT, help="JSON report path")
    parser.add_argument("--apply", action="store_true", help="write taxonomy rows")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    taxonomy_v1.validate()
    with psycopg.connect(args.dsn) as conn:
        before = existing_nodes(conn, args.version)
        applied_nodes = apply_seed(conn, version=args.version, status=args.status) if args.apply else 0
    payload = report_payload(
        version=args.version,
        status=args.status,
        dry_run=not args.apply,
        before=before,
        applied_nodes=applied_nodes,
    )
    write_report(args.output_file, payload)
    if args.apply:
        print(f"applied {applied_nodes} taxonomy node(s) to version={args.version} status={args.status}")
    else:
        print(f"dry-run: wrote seed report to {args.output_file}; no DB writes")
    print(f"taxonomy v1 nodes: {payload['node_count']} ({payload['parent_count']} parent, {payload['child_count']} child)")


if __name__ == "__main__":
    main()
