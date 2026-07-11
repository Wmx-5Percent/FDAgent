"""Label recall records against a frozen taxonomy version.

Offline Phase 4, P2: load a taxonomy/version, label each distinct
reason_for_recall once with closed-set structured LLM output, cache by content
hash, and optionally backfill all matching records into recall_label. By default
this writes only cache/report files; database writes happen only with --apply.

Run:
    .venv/bin/python src/classify/label.py --version v1 --taxonomy-status active
    .venv/bin/python src/classify/label.py --version v1 --limit 100 --cache-only
    .venv/bin/python src/classify/label.py --taxonomy-file data/processed/taxonomy_draft_v1.json --draft-prefix-match
    .venv/bin/python src/classify/label.py --version v1 --apply
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import psycopg
from dotenv import load_dotenv
from pydantic import BaseModel, Field, field_validator
from tenacity import retry, stop_after_attempt, wait_exponential

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import llm  # noqa: E402  (OpenAI-compatible provider gateway)

load_dotenv()

DEFAULT_DSN = os.environ.get("DATABASE_URL", "postgresql://localhost:5432/fda")


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


def _node_from_payload(row: dict[str, Any], *, status: str) -> TaxonomyNode:
    return TaxonomyNode(
        node_id=str(row["node_id"]),
        parent_id=None if row.get("parent_id") is None else str(row["parent_id"]),
        label=str(row["label"]),
        definition=str(row["definition"]),
        examples=[str(example) for example in row.get("examples", [])],
        level=int(row["level"]),
        status=str(row.get("status") or status),
    )


def load_taxonomy_file(path: str, status: str) -> tuple[str | None, list[TaxonomyNode]]:
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    if isinstance(payload.get("draft"), dict):
        source = payload["draft"]
        version = source.get("version") or payload.get("version")
        node_rows = source.get("nodes", [])
    else:
        version = payload.get("version")
        node_rows = payload.get("nodes", [])
    if not isinstance(node_rows, list) or not node_rows:
        raise ValueError(f"{path} does not contain taxonomy nodes")
    nodes = [_node_from_payload(row, status=status) for row in node_rows]
    validate_taxonomy_nodes(nodes)
    return None if version is None else str(version), nodes


def validate_taxonomy_nodes(nodes: list[TaxonomyNode]) -> None:
    node_ids = [node.node_id for node in nodes]
    duplicates = [node_id for node_id, count in Counter(node_ids).items() if count > 1]
    if duplicates:
        raise ValueError(f"duplicate taxonomy node_id(s): {', '.join(sorted(duplicates))}")
    known = set(node_ids)
    for node in nodes:
        if node.parent_id and node.parent_id not in known:
            raise ValueError(f"{node.node_id!r} references unknown parent_id {node.parent_id!r}")
        if node.level == 0 and node.parent_id is not None:
            raise ValueError(f"root node {node.node_id!r} must not have parent_id")
        if node.level > 0 and not node.parent_id:
            raise ValueError(f"child node {node.node_id!r} must have parent_id")


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


def extract_prefix(text: str) -> str | None:
    raw = re.sub(r"\s+", " ", text.strip())
    if not raw:
        return None
    head = re.split(r":|;|\s+-\s+|\s+--\s+|\s+–\s+|\s+—\s+", raw, maxsplit=1)[0]
    head = head.strip(" .,-")
    words = head.split()
    if not 1 <= len(words) <= 8:
        return None
    if len(head) > 90:
        return None
    return normalize_text(head)


def draft_match_terms(nodes: list[TaxonomyNode]) -> list[tuple[str, str]]:
    nodes_by_id = {node.node_id: node for node in nodes}
    seen: set[tuple[str, str]] = set()
    terms: list[tuple[str, str]] = []
    for node in nodes:
        if node.node_id == "other":
            continue
        raw_terms = [node.label, node.node_id.replace("_", " ")]
        raw_terms.extend(prefix for example in node.examples if (prefix := extract_prefix(example)))
        for raw in raw_terms:
            term = normalize_text(raw)
            if not term or len(term) > 90:
                continue
            key = (term, node.node_id)
            if key in seen:
                continue
            seen.add(key)
            terms.append(key)
    terms.sort(key=lambda item: (nodes_by_id[item[1]].level == 0, -len(item[0]), item[1]))
    return terms


def _matches_term(reason: str, prefix: str | None, term: str) -> bool:
    if prefix == term:
        return True
    if reason == term:
        return True
    separators = (":", ";", ".", " -", " --", " –", " —")
    if any(reason.startswith(f"{term}{separator}") for separator in separators):
        return True
    if prefix is None:
        return False
    if prefix.startswith(term) or term.startswith(prefix):
        return True
    return len(term) >= 6 and term in prefix


def label_with_draft_prefix_match(
    *,
    batch: ReasonBatch,
    terms: list[tuple[str, str]],
    other_node_id: str,
) -> LabelResult:
    reason = normalize_text(batch.text)
    prefix = extract_prefix(batch.text)
    for term, node_id in terms:
        if _matches_term(reason, prefix, term):
            evidence = prefix or batch.text[:240]
            return LabelResult(labels=[
                LabelAssignment(node_id=node_id, confidence=1.0, evidence=evidence[:500])
            ])
    return LabelResult(
        labels=[LabelAssignment(node_id=other_node_id, confidence=0.0, evidence=batch.text[:500])],
        other_reason="No draft prefix rule matched this reason.",
    )


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
    client: Any,
    *,
    config: llm.ChatConfig,
    nodes: list[TaxonomyNode],
    reason: str,
) -> LabelResult:
    return llm.structured_completion(
        client,
        config,
        [
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
        LabelResult,
        temperature=0,
    )


def validate_result(
    result: LabelResult,
    *,
    nodes_by_id: dict[str, TaxonomyNode],
    other_node_id: str,
    reason: str,
) -> LabelResult:
    # Closed-set guard: keep only labels whose node_id exists in the taxonomy.
    # Models can hallucinate an out-of-taxonomy node_id; drop it and record the
    # drop instead of raising, so one stray label cannot crash the whole run.
    valid_labels = [label for label in result.labels if label.node_id in nodes_by_id]
    dropped = sorted({label.node_id for label in result.labels if label.node_id not in nodes_by_id})
    other_reason = result.other_reason
    if dropped:
        note = f"dropped unknown node_id(s): {', '.join(dropped)}"
        other_reason = f"{other_reason}; {note}" if other_reason else note
    if valid_labels:
        return LabelResult(labels=valid_labels, other_reason=other_reason)
    if other_node_id in nodes_by_id:
        evidence = other_reason or reason[:240]
        return LabelResult(labels=[
            LabelAssignment(node_id=other_node_id, confidence=0.0, evidence=evidence[:500])
        ], other_reason=other_reason)
    return LabelResult(labels=[], other_reason=other_reason)


def label_reason(
    *,
    batch: ReasonBatch,
    nodes: list[TaxonomyNode],
    nodes_by_id: dict[str, TaxonomyNode],
    version: str,
    taxonomy_digest: str,
    model: str,
    chat_config: llm.ChatConfig,
    other_node_id: str,
    cache: dict[str, dict[str, Any]],
    cache_file: str,
    cache_only: bool,
    client: Any | None,
) -> tuple[LabelResult, bool, Any | None]:
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
        client = llm.create_chat_client(chat_config)
    result = classify_with_llm(client, config=chat_config, nodes=nodes, reason=batch.text)
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
    parser.add_argument("--model", default=None, help="chat model override for labeling")
    parser.add_argument("--labeler", default=None, help="labeler id stored in recall_label")
    parser.add_argument("--taxonomy-file", default=None,
                        help="taxonomy draft/seed JSON file to use instead of taxonomy table; dry-run only")
    parser.add_argument("--cache-file", default=None, help="JSONL hash-cache path")
    parser.add_argument("--output-file", default=None, help="JSON report path")
    parser.add_argument("--limit", type=int, default=None, help="max distinct reason texts to label")
    parser.add_argument("--other-node-id", default="other", help="taxonomy node used for other/uncertain")
    parser.add_argument("--sample-reasons", type=int, default=3,
                        help="max sample reason texts to include per node in the JSON report")
    parser.add_argument("--draft-prefix-match", action="store_true",
                        help="estimate a draft taxonomy distribution by prefix matching; no LLM or cache")
    parser.add_argument("--cache-only", action="store_true", help="never call the LLM; fail on cache miss")
    parser.add_argument("--concurrency", type=int, default=12,
                        help="max concurrent LLM requests for cache-miss reasons (>=1)")
    parser.add_argument("--apply", action="store_true", help="backfill recall_label")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    if args.taxonomy_file and args.apply:
        raise ValueError("--taxonomy-file is dry-run only; refusing to use it with --apply")
    if args.draft_prefix_match and not args.taxonomy_file:
        raise ValueError("--draft-prefix-match requires --taxonomy-file")
    if args.sample_reasons < 0:
        raise ValueError("--sample-reasons must be non-negative")

    chat_config = llm.chat_config(model=args.model)
    model = chat_config.model
    version = args.version

    with psycopg.connect(args.dsn) as conn:
        if args.taxonomy_file:
            file_version, nodes = load_taxonomy_file(args.taxonomy_file, status="draft")
            version = file_version or version
        else:
            nodes = load_taxonomy(conn, version, args.taxonomy_status)
        nodes_by_id = {node.node_id: node for node in nodes}
        if args.other_node_id not in nodes_by_id:
            raise ValueError(f"other node {args.other_node_id!r} is not present in the taxonomy")
        digest = taxonomy_hash(nodes)
        cache_file = args.cache_file or default_cache_file(version, model)
        output_file = args.output_file or default_output_file(version)
        labeler = args.labeler or (
            "draft-prefix-match" if args.draft_prefix_match else f"llm:{model}"
        )
        batches = fetch_reason_batches(conn, args.limit)
        if not batches:
            raise ValueError("no non-empty reason_for_recall texts found")

        cache = {} if args.draft_prefix_match else load_cache(cache_file)
        match_terms = draft_match_terms(nodes) if args.draft_prefix_match else []
        client: Any | None = None
        cache_hits = 0
        applied_rows = 0
        counts: Counter[str] = Counter()
        sample_reasons: dict[str, list[str]] = defaultdict(list)
        results: list[dict[str, Any]] = []

        total = len(batches)
        processed = 0

        def record(batch: ReasonBatch, result: LabelResult, hit: bool) -> None:
            nonlocal cache_hits, applied_rows, processed
            if hit:
                cache_hits += 1
            for label in result.labels:
                counts[label.node_id] += batch.record_count
                if len(sample_reasons[label.node_id]) < args.sample_reasons:
                    sample_reasons[label.node_id].append(batch.text)
            if args.apply:
                applied_rows += apply_batch(
                    conn,
                    batch=batch,
                    result=result,
                    nodes_by_id=nodes_by_id,
                    version=version,
                    labeler=labeler,
                    model=model,
                )
            results.append({
                "text_hash": batch.text_hash,
                "record_count": batch.record_count,
                "cache_hit": hit,
                "labels": [label.model_dump() for label in result.labels],
                "other_reason": result.other_reason,
            })
            processed += 1
            if processed % 50 == 0:
                print(f"processed {processed}/{total} distinct reason text(s)")

        if args.draft_prefix_match:
            for batch in batches:
                result = label_with_draft_prefix_match(
                    batch=batch,
                    terms=match_terms,
                    other_node_id=args.other_node_id,
                )
                record(batch, result, hit=False)
        else:
            # Serve cache hits inline; send only cache misses to the LLM, concurrently.
            pending: list[tuple[ReasonBatch, str]] = []
            for batch in batches:
                key = cache_key(
                    version=version,
                    taxonomy_digest=digest,
                    model=model,
                    reason_hash=batch.text_hash,
                    other_node_id=args.other_node_id,
                )
                if key in cache:
                    cached = LabelResult.model_validate(cache[key]["result"])
                    result = validate_result(
                        cached,
                        nodes_by_id=nodes_by_id,
                        other_node_id=args.other_node_id,
                        reason=batch.text,
                    )
                    record(batch, result, hit=True)
                elif args.cache_only:
                    raise ValueError(
                        f"cache miss for text_hash={batch.text_hash}; unset --cache-only to call the LLM"
                    )
                else:
                    pending.append((batch, key))

            if pending:
                client = llm.create_chat_client(chat_config)
                workers = max(1, args.concurrency)

                def call_llm(target: ReasonBatch) -> LabelResult:
                    raw = classify_with_llm(client, config=chat_config, nodes=nodes, reason=target.text)
                    return validate_result(
                        raw,
                        nodes_by_id=nodes_by_id,
                        other_node_id=args.other_node_id,
                        reason=target.text,
                    )

                print(f"labeling {len(pending)} cache-miss reason(s) with concurrency={workers}")
                with ThreadPoolExecutor(max_workers=workers) as executor:
                    future_map = {
                        executor.submit(call_llm, batch): (batch, key)
                        for batch, key in pending
                    }
                    for future in as_completed(future_map):
                        batch, key = future_map[future]
                        try:
                            result = future.result()
                        except Exception as exc:  # noqa: BLE001 - keep going on a single failed reason
                            print(f"skip reason text_hash={batch.text_hash}: {exc}")
                            continue
                        row = {
                            "key": key,
                            "version": version,
                            "taxonomy_hash": digest,
                            "model": model,
                            "text_hash": batch.text_hash,
                            "record_count": batch.record_count,
                            "result": result.model_dump(),
                        }
                        append_cache(cache_file, row)
                        cache[key] = row
                        record(batch, result, hit=False)

        report = {
            "version": version,
            "taxonomy_status": args.taxonomy_status,
            "taxonomy_source": "file" if args.taxonomy_file else "database",
            "taxonomy_file": args.taxonomy_file,
            "taxonomy_hash": digest,
            "provider": chat_config.provider,
            "model": model,
            "labeler": labeler,
            "labeling_mode": "draft_prefix_match" if args.draft_prefix_match else "llm_closed_set",
            "cache_file": cache_file,
            "dry_run": not args.apply,
            "distinct_reason_count": len(batches),
            "record_count": sum(batch.record_count for batch in batches),
            "cache_hits": cache_hits,
            "label_counts_by_node": dict(counts.most_common()),
            "sample_reasons_by_node": dict(sorted(sample_reasons.items())),
            "applied_rows": applied_rows,
            "results": results,
        }
        write_json(output_file, report)

    if args.apply:
        print(f"applied {applied_rows} recall_label row(s); report={output_file}")
    else:
        print(f"dry-run: wrote label report to {output_file}; no DB writes")
    if args.draft_prefix_match:
        print(f"draft-prefix-match: no LLM/cache used; matched {len(match_terms)} term(s)")
    else:
        print(f"cache={cache_file}; cache_hits={cache_hits}/{len(batches)}")


if __name__ == "__main__":
    main()
