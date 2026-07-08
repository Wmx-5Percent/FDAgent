"""Label recall records against a frozen taxonomy version.

Offline Phase 4, P2: load a taxonomy/version, label each distinct
reason_for_recall once with closed-set structured LLM output, cache by content
hash, and optionally backfill all matching records into recall_label. By default
this writes only cache/report files; database writes happen only with --apply.

Run:
    .venv/bin/python src/classify/label.py --version v1 --taxonomy-status active
    .venv/bin/python src/classify/label.py --version v1 --limit 100 --cache-only
    .venv/bin/python src/classify/label.py --version v1 --apply
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import psycopg
from dotenv import load_dotenv
from openai import OpenAI
from pydantic import BaseModel, Field, field_validator
from tenacity import retry, stop_after_attempt, wait_exponential

load_dotenv()

DEFAULT_DSN = os.environ.get("DATABASE_URL", "postgresql://localhost:5432/fda")
DEFAULT_MODEL = os.environ.get("OPENAI_MODEL", "gpt-4o-mini")


@dataclass(frozen=True)
class TaxonomyNode:
    node_id: str
    parent_id: str | None
    label: str
    definition: str
    examples: list[str]
    level: int
    status: str


@dataclass(frozen=True)
class ReasonBatch:
    text: str
    text_hash: str
    record_ids: list[str]
    record_count: int


class LabelAssignment(BaseModel):
    node_id: str
    confidence: float = Field(ge=0, le=1)
    evidence: str = Field(min_length=1)


class LabelResult(BaseModel):
    labels: list[LabelAssignment] = Field(default_factory=list, max_length=3)
    other_reason: str | None = Field(
        default=None,
        description="Why no taxonomy label fits, when labels is empty.",
    )

    @field_validator("labels")
    @classmethod
    def labels_are_unique(cls, labels: list[LabelAssignment]) -> list[LabelAssignment]:
        seen: set[str] = set()
        for label in labels:
            if label.node_id in seen:
                raise ValueError(f"duplicate node_id {label.node_id!r}")
            seen.add(label.node_id)
        return labels


def normalize_text(text: str) -> str:
    return re.sub(r"\s+", " ", text.strip()).lower()


def text_hash(text: str) -> str:
    return hashlib.sha256(normalize_text(text).encode("utf-8")).hexdigest()


def safe_name(value: str) -> str:
    name = re.sub(r"[^a-zA-Z0-9_.-]+", "_", value).strip("_")
    return name or "default"


def default_cache_file(version: str, model: str) -> str:
    return f"data/processed/taxonomy_label_cache_{safe_name(version)}_{safe_name(model)}.jsonl"


def default_output_file(version: str) -> str:
    return f"data/processed/recall_labels_{safe_name(version)}.json"


def load_taxonomy(conn: psycopg.Connection, version: str, status: str) -> list[TaxonomyNode]:
    query = """
        SELECT node_id, parent_id, label, definition, examples, level, status
        FROM taxonomy
        WHERE version = %s
    """
    params: list[Any] = [version]
    if status != "any":
        query += " AND status = %s"
        params.append(status)
    query += " ORDER BY level, node_id"
    with conn.cursor() as cur:
        cur.execute(query, params)
        rows = cur.fetchall()
    if not rows:
        raise ValueError(f"no taxonomy nodes found for version={version!r} status={status!r}")
    return [
        TaxonomyNode(
            node_id=row[0],
            parent_id=row[1],
            label=row[2],
            definition=row[3],
            examples=list(row[4] or []),
            level=int(row[5]),
            status=row[6],
        )
        for row in rows
    ]


def taxonomy_hash(nodes: list[TaxonomyNode]) -> str:
    payload = [
        {
            "node_id": node.node_id,
            "parent_id": node.parent_id,
            "label": node.label,
            "definition": node.definition,
            "examples": node.examples,
            "level": node.level,
            "status": node.status,
        }
        for node in sorted(nodes, key=lambda item: item.node_id)
    ]
    blob = json.dumps(payload, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


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
        rows = cur.fetchall()
    return [
        ReasonBatch(
            text=row[0],
            text_hash=text_hash(row[0]),
            record_ids=list(row[1]),
            record_count=int(row[2]),
        )
        for row in rows
    ]


def cache_key(
    *,
    version: str,
    taxonomy_digest: str,
    model: str,
    reason_hash: str,
    other_node_id: str,
) -> str:
    payload = "|".join([version, taxonomy_digest, model, reason_hash, other_node_id])
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def load_cache(path: str) -> dict[str, dict[str, Any]]:
    cache_path = Path(path)
    if not cache_path.exists():
        return {}
    entries: dict[str, dict[str, Any]] = {}
    for line_no, raw in enumerate(cache_path.read_text(encoding="utf-8").splitlines(), 1):
        if not raw.strip():
            continue
        row = json.loads(raw)
        key = row.get("key")
        if not isinstance(key, str):
            raise ValueError(f"cache line {line_no} in {path} is missing string key")
        entries[key] = row
    return entries


def append_cache(path: str, row: dict[str, Any]) -> None:
    cache_path = Path(path)
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    with cache_path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")


def taxonomy_context(nodes: list[TaxonomyNode]) -> str:
    lines: list[str] = []
    for node in nodes:
        parent = node.parent_id or "-"
        examples = "; ".join(node.examples[:3])
        if examples:
            examples = f" Examples: {examples}"
        lines.append(
            f"- {node.node_id} | label={node.label} | level={node.level} | "
            f"parent={parent} | definition={node.definition}{examples}"
        )
    return "\n".join(lines)


SYSTEM = """You are a closed-set classifier for FDA drug recall reasons.
Use ONLY the taxonomy node_ids provided by the user. Multi-label only when the
reason clearly describes multiple taxonomy categories. If no node fits, return
labels=[] and explain why in other_reason.

Rules:
- Do not invent node_ids.
- Each evidence string must be a short supporting snippet from the recall reason.
- Prefer a precise child node over a broad parent when both fit.
- Use confidence from 0 to 1.
"""


@retry(stop=stop_after_attempt(4), wait=wait_exponential(multiplier=1, min=2, max=20))
def classify_with_llm(
    client: OpenAI,
    *,
    model: str,
    nodes: list[TaxonomyNode],
    reason: str,
) -> LabelResult:
    parse = getattr(client.chat.completions, "parse", None) or client.beta.chat.completions.parse
    completion = parse(
        model=model,
        temperature=0,
        messages=[
            {"role": "system", "content": SYSTEM},
            {
                "role": "user",
                "content": (
                    "Taxonomy:\n"
                    f"{taxonomy_context(nodes)}\n\n"
                    "Recall reason:\n"
                    f"{reason}"
                ),
            },
        ],
        response_format=LabelResult,
    )
    return completion.choices[0].message.parsed


def validate_result(
    result: LabelResult,
    *,
    nodes_by_id: dict[str, TaxonomyNode],
    other_node_id: str,
    reason: str,
) -> LabelResult:
    for label in result.labels:
        if label.node_id not in nodes_by_id:
            raise ValueError(f"LLM returned unknown node_id {label.node_id!r}")
    if result.labels:
        return result
    if other_node_id in nodes_by_id:
        evidence = result.other_reason or reason[:240]
        return LabelResult(labels=[
            LabelAssignment(node_id=other_node_id, confidence=0.0, evidence=evidence[:500])
        ], other_reason=result.other_reason)
    return result


def label_reason(
    *,
    batch: ReasonBatch,
    nodes: list[TaxonomyNode],
    nodes_by_id: dict[str, TaxonomyNode],
    version: str,
    taxonomy_digest: str,
    model: str,
    other_node_id: str,
    cache: dict[str, dict[str, Any]],
    cache_file: str,
    cache_only: bool,
    client: OpenAI | None,
) -> tuple[LabelResult, bool, OpenAI | None]:
    key = cache_key(
        version=version,
        taxonomy_digest=taxonomy_digest,
        model=model,
        reason_hash=batch.text_hash,
        other_node_id=other_node_id,
    )
    if key in cache:
        cached = LabelResult.model_validate(cache[key]["result"])
        return validate_result(
            cached,
            nodes_by_id=nodes_by_id,
            other_node_id=other_node_id,
            reason=batch.text,
        ), True, client
    if cache_only:
        raise ValueError(f"cache miss for text_hash={batch.text_hash}; unset --cache-only to call the LLM")
    if client is None:
        client = OpenAI()
    result = classify_with_llm(client, model=model, nodes=nodes, reason=batch.text)
    result = validate_result(
        result,
        nodes_by_id=nodes_by_id,
        other_node_id=other_node_id,
        reason=batch.text,
    )
    row = {
        "key": key,
        "version": version,
        "taxonomy_hash": taxonomy_digest,
        "model": model,
        "text_hash": batch.text_hash,
        "record_count": batch.record_count,
        "result": result.model_dump(),
    }
    append_cache(cache_file, row)
    cache[key] = row
    return result, False, client


def apply_batch(
    conn: psycopg.Connection,
    *,
    batch: ReasonBatch,
    result: LabelResult,
    nodes_by_id: dict[str, TaxonomyNode],
    version: str,
    labeler: str,
    model: str,
) -> int:
    rows: list[tuple[Any, ...]] = []
    for record_id in batch.record_ids:
        for label in result.labels:
            node = nodes_by_id[label.node_id]
            rows.append(
                (
                    record_id,
                    version,
                    label.node_id,
                    node.level,
                    round(float(label.confidence), 4),
                    label.evidence[:500],
                    batch.text_hash,
                    labeler,
                    model,
                )
            )
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
    return len(rows)


def write_json(path: str, payload: dict[str, Any]) -> None:
    out = Path(path)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Closed-set label recall reasons with a frozen taxonomy.")
    parser.add_argument("--dsn", default=DEFAULT_DSN, help="Postgres DSN")
    parser.add_argument("--version", default="v1", help="taxonomy version to use")
    parser.add_argument("--taxonomy-status", choices=("active", "draft", "deprecated", "any"),
                        default="active", help="taxonomy node status filter")
    parser.add_argument("--model", default=DEFAULT_MODEL, help="OpenAI model for labeling")
    parser.add_argument("--labeler", default=None, help="labeler id stored in recall_label")
    parser.add_argument("--cache-file", default=None, help="JSONL hash-cache path")
    parser.add_argument("--output-file", default=None, help="JSON report path")
    parser.add_argument("--limit", type=int, default=None, help="max distinct reason texts to label")
    parser.add_argument("--other-node-id", default="other", help="taxonomy node used for other/uncertain")
    parser.add_argument("--cache-only", action="store_true", help="never call the LLM; fail on cache miss")
    parser.add_argument("--apply", action="store_true", help="backfill recall_label")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    cache_file = args.cache_file or default_cache_file(args.version, args.model)
    output_file = args.output_file or default_output_file(args.version)
    labeler = args.labeler or f"llm:{args.model}"

    with psycopg.connect(args.dsn) as conn:
        nodes = load_taxonomy(conn, args.version, args.taxonomy_status)
        nodes_by_id = {node.node_id: node for node in nodes}
        digest = taxonomy_hash(nodes)
        batches = fetch_reason_batches(conn, args.limit)
        if not batches:
            raise ValueError("no non-empty reason_for_recall texts found")

        cache = load_cache(cache_file)
        client: OpenAI | None = None
        cache_hits = 0
        applied_rows = 0
        counts: Counter[str] = Counter()
        results: list[dict[str, Any]] = []

        for index, batch in enumerate(batches, 1):
            result, hit, client = label_reason(
                batch=batch,
                nodes=nodes,
                nodes_by_id=nodes_by_id,
                version=args.version,
                taxonomy_digest=digest,
                model=args.model,
                other_node_id=args.other_node_id,
                cache=cache,
                cache_file=cache_file,
                cache_only=args.cache_only,
                client=client,
            )
            if hit:
                cache_hits += 1
            for label in result.labels:
                counts[label.node_id] += batch.record_count
            if args.apply:
                applied_rows += apply_batch(
                    conn,
                    batch=batch,
                    result=result,
                    nodes_by_id=nodes_by_id,
                    version=args.version,
                    labeler=labeler,
                    model=args.model,
                )
            results.append({
                "text_hash": batch.text_hash,
                "record_count": batch.record_count,
                "cache_hit": hit,
                "labels": [label.model_dump() for label in result.labels],
                "other_reason": result.other_reason,
            })
            if index % 50 == 0:
                print(f"processed {index}/{len(batches)} distinct reason text(s)")

        report = {
            "version": args.version,
            "taxonomy_status": args.taxonomy_status,
            "taxonomy_hash": digest,
            "model": args.model,
            "labeler": labeler,
            "cache_file": cache_file,
            "dry_run": not args.apply,
            "distinct_reason_count": len(batches),
            "record_count": sum(batch.record_count for batch in batches),
            "cache_hits": cache_hits,
            "label_counts_by_node": dict(counts.most_common()),
            "applied_rows": applied_rows,
            "results": results,
        }
        write_json(output_file, report)

    if args.apply:
        print(f"applied {applied_rows} recall_label row(s); report={output_file}")
    else:
        print(f"dry-run: wrote label report to {output_file}; no DB writes")
    print(f"cache={cache_file}; cache_hits={cache_hits}/{len(batches)}")


if __name__ == "__main__":
    main()
