"""Backfill recall_label for taxonomy v1 with high-precision deterministic rules.

This is a bootstrap workflow for obvious reason_for_recall prefixes such as
"Lack of Assurance of Sterility" or "cGMP Deviations". It does not replace the
closed-set LLM labeler; it gives the taxonomy track an auditable local backfill
path that works without API quota. Database writes happen only with --apply.

Run:
    .venv/bin/python src/classify/backfill_v1_rules.py
    .venv/bin/python src/classify/backfill_v1_rules.py --apply
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import psycopg
from dotenv import load_dotenv

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from classify import taxonomy_v1  # noqa: E402

load_dotenv()

DEFAULT_DSN = os.environ.get("DATABASE_URL", "postgresql://localhost:5432/fda")
DEFAULT_OUTPUT = "data/processed/taxonomy_v1_rules_backfill_report.json"
DEFAULT_LABELER = "rules:v1-prefix"
DEFAULT_MODEL = "rules:v1-prefix-2026-07-09"


@dataclass(frozen=True)
class Rule:
    node_id: str
    patterns: tuple[str, ...]
    confidence: float


@dataclass(frozen=True)
class ReasonBatch:
    text: str
    text_hash: str
    record_ids: list[str]
    record_count: int


@dataclass(frozen=True)
class Assignment:
    node_id: str
    confidence: float
    evidence: str


RULES: tuple[Rule, ...] = (
    Rule("sterility_assurance", (
        r"\black of assurance (of )?sterility\b",
        r"\black of sterility assurance\b",
        r"\black of assurance sterility\b",
        r"\bnon-sterility\b",
        r"\bnon sterility\b",
    ), 0.96),
    Rule("microbial_contamination", (
        r"\bmicrobial contamination\b",
        r"\bmicrobial contamination of non-sterile products\b",
        r"\bmicrobal contamination\b",
    ), 0.95),
    Rule("particulate_or_foreign_matter", (
        r"\bpresence of particulate matter\b",
        r"\bparticulate matter\b",
        r"\bforeign substance\b",
        r"\bforeign tablets?/capsules?\b",
        r"\bforeign matter\b",
        r"\bglass (fragment|particle|particulate)",
        r"\bvisible particles?\b",
        r"\bembedded particles?\b",
        r"\bprecipitate\b",
    ), 0.94),
    Rule("impurities_or_degradation", (
        r"\bimpurit(y|ies)\b",
        r"\bdegradation\b",
        r"\bchemical contamination\b",
        r"\bnitrosamine\b",
        r"\bndma\b",
    ), 0.90),
    Rule("potency_or_content", (
        r"\bsubpotent\b",
        r"\bsub potent\b",
        r"\bsuperpotent\b",
        r"\bcontent uniformity\b",
        r"\bassay\b",
        r"\bpotency\b",
    ), 0.92),
    Rule("dissolution_or_tablet_specs", (
        r"\bdissolution\b",
        r"\btablet/capsule specifications\b",
        r"\btablet specifications\b",
        r"\bcapsule specifications\b",
        r"\bcrystallization\b",
    ), 0.90),
    Rule("stability_or_expiry", (
        r"\bstability data does not support exp?iry\b",
        r"\bfailed stability specifications\b",
        r"\bstability\b",
        r"\bexpir(y|ation)\b",
    ), 0.88),
    Rule("appearance_or_physical_defect", (
        r"\bdiscoloration\b",
        r"\bcracked\b",
        r"\bvisible defect\b",
    ), 0.86),
    Rule("formulation_or_ingredient_error", (
        r"\bincorrect/ ?undeclared excipients?\b",
        r"\bundeclared excipients?\b",
        r"\bincorrect solvent\b",
        r"\bwrong (active )?ingredient\b",
        r"\bmanufactured with .* instead of\b",
        r"\bincorrect grade of excipient\b",
        r"\bincorrect/ ?undeclared benzyl alcohol\b",
    ), 0.90),
    Rule("other_specification_failure", (
        r"\bout[- ]of[- ]specification\b",
        r"\boos\b",
        r"\bfailed moisture limits\b",
        r"\bdoes not meet monograph\b",
        r"\bfailed excipient specifications\b",
        r"\bidentification testing\b",
    ), 0.84),
    Rule("cgmp_deviation", (
        r"\bcgmp deviations?\b",
        r"\bcgmps deviations?\b",
        r"\bcgmp violations?\b",
        r"^cgmp:",
        r"\black of cgmp\b",
        r"\bgmp deviations?\b",
        r"\bgmp violations?\b",
        r"\bgood manufacturing practice\b",
        r"\bgood manufacturing practices deviations?\b",
    ), 0.92),
    Rule("processing_controls", (
        r"\black of processing controls\b",
        r"\black of processing control\b",
        r"\bprocessing controls\b",
    ), 0.88),
    Rule("cross_contamination", (
        r"\bpenicillin cross contamination\b",
        r"\bcross[- ]+contamination\b",
        r"\bcross contamination\b",
    ), 0.94),
    Rule("labeling_error", (
        r"^labeling\b",
        r"\blabeling error\b",
        r"\bmislabel",
        r"\bmispack",
        r"\bpackaging defects?\b",
        r"\bincorrect label",
        r"\bwrong label",
    ), 0.90),
    Rule("container_or_closure_defect", (
        r"\bdefective container\b",
        r"\bcontainer defect\b",
        r"\bclosure defect\b",
        r"\bdefective closure\b",
        r"\bcontainer/closure\b",
    ), 0.90),
    Rule("delivery_system_defect", (
        r"\bdefective delivery system\b",
        r"\bdelivery system\b",
    ), 0.91),
    Rule("unapproved_drug", (
        r"\bmarketed without an approved nda/anda\b",
        r"\bmarked without an approved nda/anda\b",
        r"\bmarketed without approved nda/anda\b",
        r"\bmarked without approved nda/anda\b",
        r"\bwithout an approved nda\b",
        r"\bwithout an approved anda\b",
        r"\black of drug listing\b",
        r"\bnot approved for sale\b",
        r"\bunapproved (new )?drug\b",
    ), 0.96),
    Rule("temperature_abuse", (
        r"\btemperature abuse\b",
        r"\btemperature excursion\b",
    ), 0.93),
)


def normalize_text(text: str) -> str:
    return re.sub(r"\s+", " ", text.strip()).lower()


def text_hash(text: str) -> str:
    return hashlib.sha256(normalize_text(text).encode("utf-8")).hexdigest()


def fetch_reason_batches(conn: psycopg.Connection, limit: int | None) -> list[ReasonBatch]:
    query = """
        SELECT reason_for_recall,
               array_agg(id ORDER BY id) AS record_ids,
               count(*)::integer AS record_count
        FROM drug_enforcement
        WHERE reason_for_recall IS NOT NULL
          AND length(btrim(reason_for_recall)) > 0
        GROUP BY reason_for_recall
        ORDER BY record_count DESC, reason_for_recall
    """
    params: list[Any] = []
    if limit is not None:
        query += " LIMIT %s"
        params.append(limit)
    with conn.cursor() as cur:
        cur.execute(query, params)
        return [
            ReasonBatch(text=row[0], text_hash=text_hash(row[0]), record_ids=list(row[1]), record_count=row[2])
            for row in cur.fetchall()
        ]


def active_node_ids(conn: psycopg.Connection, version: str) -> set[str]:
    with conn.cursor() as cur:
        cur.execute("SELECT node_id FROM taxonomy WHERE version = %s AND status = 'active'", [version])
        return {row[0] for row in cur.fetchall()}


def classify_text(text: str, *, include_other: bool) -> list[Assignment]:
    normalized = normalize_text(text)
    assignments: list[Assignment] = []
    for rule in RULES:
        if any(re.search(pattern, normalized) for pattern in rule.patterns):
            assignments.append(
                Assignment(
                    node_id=rule.node_id,
                    confidence=rule.confidence,
                    evidence=text[:500],
                )
            )
    if assignments:
        deduped: dict[str, Assignment] = {}
        for assignment in assignments:
            existing = deduped.get(assignment.node_id)
            if existing is None or assignment.confidence > existing.confidence:
                deduped[assignment.node_id] = assignment
        return sorted(deduped.values(), key=lambda item: item.node_id)
    if include_other:
        return [Assignment(node_id="other", confidence=0.0, evidence=text[:500])]
    return []


def validate_rules(active_nodes: set[str]) -> None:
    expected = {rule.node_id for rule in RULES} | {"other"}
    missing = sorted(expected - active_nodes)
    if missing:
        raise ValueError(f"taxonomy version is missing active node(s): {', '.join(missing)}")


def apply_batch(
    conn: psycopg.Connection,
    *,
    batch: ReasonBatch,
    assignments: list[Assignment],
    version: str,
    labeler: str,
    model: str,
) -> int:
    with conn.cursor() as cur:
        cur.execute(
            """
            DELETE FROM recall_label
            WHERE record_id = ANY(%s)
              AND version = %s
              AND labeler = %s
            """,
            (batch.record_ids, version, labeler),
        )
        rows: list[tuple[Any, ...]] = []
        for record_id in batch.record_ids:
            for assignment in assignments:
                rows.append(
                    (
                        record_id,
                        version,
                        assignment.node_id,
                        1 if assignment.node_id != "other" else 0,
                        assignment.confidence,
                        assignment.evidence,
                        batch.text_hash,
                        labeler,
                        model,
                    )
                )
        if rows:
            cur.executemany(
                """
                INSERT INTO recall_label
                    (record_id, version, node_id, level, confidence, evidence,
                     source_text_hash, labeler, model)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (record_id, version, node_id, labeler) DO UPDATE
                    SET level = EXCLUDED.level,
                        confidence = EXCLUDED.confidence,
                        evidence = EXCLUDED.evidence,
                        source_text_hash = EXCLUDED.source_text_hash,
                        model = EXCLUDED.model,
                        updated_at = now()
                """,
                rows,
            )
    conn.commit()
    return len(assignments) * len(batch.record_ids)


def run(
    *,
    dsn: str,
    version: str,
    labeler: str,
    model: str,
    limit: int | None,
    include_other: bool,
    apply: bool,
) -> dict[str, Any]:
    taxonomy_v1.validate()
    with psycopg.connect(dsn) as conn:
        active_nodes = active_node_ids(conn, version)
        validate_rules(active_nodes)
        batches = fetch_reason_batches(conn, limit)
        distinct_matched = 0
        record_matched = 0
        label_counts: Counter[str] = Counter()
        unmatched_examples: list[dict[str, Any]] = []
        applied_rows = 0
        for batch in batches:
            assignments = classify_text(batch.text, include_other=include_other)
            if assignments:
                distinct_matched += 1
                record_matched += batch.record_count
                for assignment in assignments:
                    label_counts[assignment.node_id] += batch.record_count
                if apply:
                    applied_rows += apply_batch(
                        conn,
                        batch=batch,
                        assignments=assignments,
                        version=version,
                        labeler=labeler,
                        model=model,
                    )
            elif len(unmatched_examples) < 25:
                unmatched_examples.append({
                    "text_hash": batch.text_hash,
                    "record_count": batch.record_count,
                    "text": batch.text,
                })
        total_records = sum(batch.record_count for batch in batches)
        return {
            "version": version,
            "labeler": labeler,
            "model": model,
            "dry_run": not apply,
            "include_other": include_other,
            "distinct_reason_count": len(batches),
            "distinct_reasons_matched": distinct_matched,
            "distinct_reasons_unmatched": len(batches) - distinct_matched,
            "record_count": total_records,
            "records_matched": record_matched,
            "records_unmatched": total_records - record_matched,
            "record_coverage": round(record_matched / total_records, 4) if total_records else 0.0,
            "label_counts": dict(label_counts.most_common()),
            "unmatched_examples": unmatched_examples,
            "applied_rows": applied_rows,
        }


def write_report(path: str, payload: dict[str, Any]) -> None:
    out = Path(path)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Backfill taxonomy v1 labels using deterministic prefix rules.")
    parser.add_argument("--dsn", default=DEFAULT_DSN, help="Postgres DSN")
    parser.add_argument("--version", default=taxonomy_v1.VERSION, help="taxonomy version")
    parser.add_argument("--labeler", default=DEFAULT_LABELER, help="labeler stored in recall_label")
    parser.add_argument("--model", default=DEFAULT_MODEL, help="model stored in recall_label")
    parser.add_argument("--limit", type=int, default=None, help="max distinct reason texts to process")
    parser.add_argument("--include-other", action="store_true", help="write unmatched reasons to node_id=other")
    parser.add_argument("--output-file", default=DEFAULT_OUTPUT, help="JSON report path")
    parser.add_argument("--apply", action="store_true", help="write recall_label rows")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    report = run(
        dsn=args.dsn,
        version=args.version,
        labeler=args.labeler,
        model=args.model,
        limit=args.limit,
        include_other=args.include_other,
        apply=args.apply,
    )
    write_report(args.output_file, report)
    if args.apply:
        print(f"applied {report['applied_rows']} recall_label row(s); report={args.output_file}")
    else:
        print(f"dry-run: wrote rules backfill report to {args.output_file}; no DB writes")
    print(
        f"matched {report['records_matched']}/{report['record_count']} records "
        f"({report['record_coverage']:.1%}) across {report['distinct_reasons_matched']} distinct reasons"
    )


if __name__ == "__main__":
    main()
