"""Induce a draft recall-reason taxonomy from distinct reason_for_recall text.

Offline Phase 4, P1: read distinct recall reasons, mine repeated prefixes,
optionally cluster existing reason_for_recall embeddings, and ask the LLM for a
structured two-level taxonomy draft. By default this writes only an output JSON
file; database writes to taxonomy happen only with --apply.

Run:
    .venv/bin/python src/classify/induce.py --output-file data/processed/taxonomy_draft_v1.json
    .venv/bin/python src/classify/induce.py --limit 200 --no-llm
    .venv/bin/python src/classify/induce.py --version v1 --apply
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
from collections import Counter, defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Sequence

import psycopg
from dotenv import load_dotenv
from psycopg import errors
from pydantic import BaseModel, Field, field_validator
from tenacity import retry, stop_after_attempt, wait_exponential

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import llm  # noqa: E402  (OpenAI-compatible provider gateway)

load_dotenv()

DEFAULT_DSN = os.environ.get("DATABASE_URL", "postgresql://localhost:5432/fda")
DEFAULT_OUTPUT = "data/processed/taxonomy_draft_v1.json"
SOURCE = "drug_enforcement"
FIELD = "reason_for_recall"

PARENT_DEFINITIONS: dict[str, tuple[str, str]] = {
    "quality_and_potency": (
        "Quality and potency",
        "Recall reasons describing failed product quality attributes, potency, specifications, contamination, sterility, or stability.",
    ),
    "manufacturing_controls": (
        "Manufacturing controls",
        "Recall reasons describing manufacturing practice, process-control, or cross-contamination control failures.",
    ),
    "labeling_and_packaging": (
        "Labeling and packaging",
        "Recall reasons describing labeling, package, container, closure, or delivery-system defects.",
    ),
    "regulatory_status": (
        "Regulatory status",
        "Recall reasons describing products marketed without the required approval, application, or regulatory clearance.",
    ),
    "storage_distribution": (
        "Storage and distribution",
        "Recall reasons describing temperature abuse, storage, shipment, or distribution conditions that may affect product quality.",
    ),
    "other": (
        "Other",
        "Reasons that do not clearly fit the current taxonomy.",
    ),
}

CHILD_RULES: tuple[tuple[tuple[str, ...], str, str, str, str], ...] = (
    (
        ("label",),
        "labeling_and_packaging",
        "labeling_error",
        "Labeling error",
        "Incorrect, missing, misleading, or otherwise defective labeling, including lot, expiration, strength, or warning text problems.",
    ),
    (
        ("defective container", "container", "closure", "pouch", "seal"),
        "labeling_and_packaging",
        "container_or_closure_defect",
        "Container or closure defect",
        "Defective containers, closures, packaging integrity, or similar package failures.",
    ),
    (
        ("delivery system",),
        "labeling_and_packaging",
        "delivery_system_defect",
        "Delivery system defect",
        "Defective drug delivery devices or delivery-system components.",
    ),
    (
        ("cgmp", "gmp deviation", "gmp deviations"),
        "manufacturing_controls",
        "cgmp_deviation",
        "cGMP deviation",
        "cGMP/GMP deviations or broad manufacturing-quality-system failures.",
    ),
    (
        ("processing control", "processing controls", "process control"),
        "manufacturing_controls",
        "processing_controls",
        "Processing controls",
        "Missing, inadequate, or failed production or process controls.",
    ),
    (
        ("cross contamination", "penicillin cross"),
        "manufacturing_controls",
        "cross_contamination",
        "Cross contamination",
        "Cross-contamination between products, ingredients, or drug classes.",
    ),
    (
        ("marketed without", "approved nda", "approved anda", "unapproved", "misbranded"),
        "regulatory_status",
        "unapproved_drug",
        "Unapproved drug",
        "Products marketed without an approved NDA/ANDA or equivalent required approval.",
    ),
    (
        ("temperature", "storage", "shipment", "distribution"),
        "storage_distribution",
        "temperature_or_storage_abuse",
        "Temperature or storage abuse",
        "Improper temperature, storage, shipment, or distribution conditions that may affect product quality.",
    ),
    (
        ("lack of assurance of sterility", "lack of sterility", "non-sterility", "sterility"),
        "quality_and_potency",
        "sterility_assurance",
        "Sterility assurance",
        "Sterile or intended-sterile products recalled for lack of sterility assurance, non-sterility, or sterility-process concerns.",
    ),
    (
        ("microbial contamination", "bacterial", "mold", "yeast"),
        "quality_and_potency",
        "microbial_contamination",
        "Microbial contamination",
        "Actual or potential microbial contamination in sterile or non-sterile products.",
    ),
    (
        ("particulate", "foreign substance", "foreign tablets", "foreign capsules", "foreign matter"),
        "quality_and_potency",
        "particulate_or_foreign_matter",
        "Particulate or foreign matter",
        "Particulate matter, foreign substances, or foreign tablets/capsules in the product.",
    ),
    (
        ("impurities", "degradation", "chemical contamination", "benzene", "nitros"),
        "quality_and_potency",
        "impurities_or_degradation",
        "Impurities or degradation",
        "Failed impurity, degradation, chemical-contamination, or related chemistry specifications.",
    ),
    (
        ("subpotent", "superpotent", "potency", "assay", "content uniformity"),
        "quality_and_potency",
        "potency_or_content",
        "Potency or content",
        "Subpotent, superpotent, content-uniformity, or assay-strength failures.",
    ),
    (
        ("dissolution",),
        "quality_and_potency",
        "dissolution_or_tablet_specs",
        "Dissolution or tablet specifications",
        "Failed dissolution, tablet, capsule, crystallization, or other physical dosage-form specifications.",
    ),
    (
        ("tablet/capsule", "tablet", "capsule", "crystallization"),
        "quality_and_potency",
        "dissolution_or_tablet_specs",
        "Dissolution or tablet specifications",
        "Failed dissolution, tablet, capsule, crystallization, or other physical dosage-form specifications.",
    ),
    (
        ("stability", "expiry", "expiration"),
        "quality_and_potency",
        "stability_or_expiry",
        "Stability or expiry",
        "Failed stability specifications or insufficient data to support expiration dating.",
    ),
    (
        ("discoloration", "appearance"),
        "quality_and_potency",
        "appearance_or_physical_defect",
        "Appearance or physical defect",
        "Discoloration or visible physical defects not better captured by foreign matter or dosage-form specifications.",
    ),
)


@dataclass
class ReasonText:
    text: str
    text_hash: str
    record_count: int
    first_report_date: str | None
    last_report_date: str | None
    vector: list[float] | None = None


@dataclass
class ClusterSummary:
    cluster_key: str
    text_count: int
    record_count: int
    coherence: float | None
    top_prefixes: list[dict[str, Any]]
    examples: list[str]
    member_hashes: list[str]


class DraftNode(BaseModel):
    node_id: str = Field(description="Stable lowercase snake_case id.")
    parent_id: str | None = Field(default=None, description="Parent node_id, or null for roots.")
    label: str
    definition: str
    examples: list[str] = Field(default_factory=list)
    level: int = Field(ge=0, le=2)

    @field_validator("node_id")
    @classmethod
    def node_id_is_slug(cls, value: str) -> str:
        if not re.fullmatch(r"[a-z][a-z0-9_]{1,80}", value):
            raise ValueError("node_id must be lowercase snake_case and start with a letter")
        return value


class TaxonomyDraft(BaseModel):
    version: str
    nodes: list[DraftNode]
    notes: list[str] = Field(default_factory=list)


def normalize_text(text: str) -> str:
    return re.sub(r"\s+", " ", text.strip()).lower()


def text_hash(text: str) -> str:
    return hashlib.sha256(normalize_text(text).encode("utf-8")).hexdigest()


def slugify(value: str, *, fallback: str = "node") -> str:
    slug = re.sub(r"[^a-z0-9]+", "_", value.lower()).strip("_")
    slug = re.sub(r"_+", "_", slug)
    if not slug:
        slug = fallback
    if slug[0].isdigit():
        slug = f"n_{slug}"
    return slug[:80].rstrip("_") or fallback


def parse_vector(raw: str | None) -> list[float] | None:
    if not raw:
        return None
    value = raw.strip()
    if value.startswith("[") and value.endswith("]"):
        value = value[1:-1]
    if not value:
        return None
    return [float(part) for part in value.split(",")]


def fetch_reason_texts(conn: psycopg.Connection, limit: int | None) -> list[ReasonText]:
    query = """
        SELECT reason_for_recall,
               count(*)::integer AS record_count,
               min(report_date)::text AS first_report_date,
               max(report_date)::text AS last_report_date
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
        ReasonText(
            text=row[0],
            text_hash=text_hash(row[0]),
            record_count=int(row[1]),
            first_report_date=row[2],
            last_report_date=row[3],
        )
        for row in rows
    ]


def load_reason_vectors(conn: psycopg.Connection) -> dict[str, list[float]]:
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT DISTINCT ON (content) content, embedding::text
                FROM embeddings
                WHERE source = %s
                  AND field = %s
                  AND embedding IS NOT NULL
                ORDER BY content
                """,
                (SOURCE, FIELD),
            )
            rows = cur.fetchall()
    except (errors.UndefinedTable, errors.UndefinedColumn):
        conn.rollback()
        print("warning: embeddings table/columns are unavailable; using prefix mining only")
        return {}
    return {content: vec for content, raw in rows if (vec := parse_vector(raw))}


def attach_vectors(reasons: list[ReasonText], vectors: dict[str, list[float]]) -> int:
    attached = 0
    for reason in reasons:
        reason.vector = vectors.get(reason.text)
        if reason.vector is not None:
            attached += 1
    return attached


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
    return head.lower()


def prefix_summaries(
    reasons: Sequence[ReasonText],
    *,
    min_count: int,
    max_prefixes: int,
) -> list[dict[str, Any]]:
    counts: Counter[str] = Counter()
    examples: dict[str, list[str]] = defaultdict(list)
    records: Counter[str] = Counter()
    for reason in reasons:
        prefix = extract_prefix(reason.text)
        if not prefix:
            continue
        counts[prefix] += 1
        records[prefix] += reason.record_count
        if len(examples[prefix]) < 5:
            examples[prefix].append(reason.text)
    out: list[dict[str, Any]] = []
    for prefix, count in counts.most_common():
        if count < min_count:
            continue
        out.append({
            "prefix": prefix,
            "distinct_texts": count,
            "record_count": records[prefix],
            "examples": examples[prefix],
        })
        if len(out) >= max_prefixes:
            break
    return out


def cluster_reasons(
    reasons: Sequence[ReasonText],
    *,
    max_clusters: int,
    min_cluster_size: int,
) -> list[ClusterSummary]:
    vector_reasons = [reason for reason in reasons if reason.vector is not None]
    if len(vector_reasons) < max(min_cluster_size * 2, 4):
        return []

    import numpy as np
    from sklearn.cluster import MiniBatchKMeans

    matrix = np.array([reason.vector for reason in vector_reasons], dtype=np.float32)
    norms = np.linalg.norm(matrix, axis=1, keepdims=True)
    norms[norms == 0] = 1
    matrix = matrix / norms

    n_clusters = min(max_clusters, max(2, len(vector_reasons) // min_cluster_size))
    model = MiniBatchKMeans(
        n_clusters=n_clusters,
        random_state=13,
        n_init=10,
        batch_size=min(512, max(32, len(vector_reasons))),
    )
    labels = model.fit_predict(matrix)
    centers = model.cluster_centers_
    center_norms = np.linalg.norm(centers, axis=1, keepdims=True)
    center_norms[center_norms == 0] = 1
    centers = centers / center_norms

    grouped: dict[int, list[tuple[int, ReasonText]]] = defaultdict(list)
    for idx, label in enumerate(labels):
        grouped[int(label)].append((idx, vector_reasons[idx]))

    summaries: list[ClusterSummary] = []
    for label, members in grouped.items():
        if len(members) < min_cluster_size:
            continue
        member_reasons = [reason for _, reason in members]
        sims = [float(matrix[idx].dot(centers[label])) for idx, _ in members]
        coherence = round(max(0.0, min(1.0, sum(sims) / len(sims))), 4)
        member_reasons.sort(key=lambda item: (-item.record_count, item.text))
        member_hashes = sorted(reason.text_hash for reason in member_reasons)
        cluster_key = hashlib.sha256("|".join(member_hashes[:50]).encode("utf-8")).hexdigest()[:16]
        summaries.append(
            ClusterSummary(
                cluster_key=cluster_key,
                text_count=len(member_reasons),
                record_count=sum(reason.record_count for reason in member_reasons),
                coherence=coherence,
                top_prefixes=prefix_summaries(member_reasons, min_count=2, max_prefixes=8),
                examples=[reason.text for reason in member_reasons[:8]],
                member_hashes=member_hashes[:20],
            )
        )
    summaries.sort(key=lambda item: (-item.record_count, -(item.coherence or 0), item.cluster_key))
    return summaries


def induction_payload(
    reasons: Sequence[ReasonText],
    prefixes: Sequence[dict[str, Any]],
    clusters: Sequence[ClusterSummary],
) -> dict[str, Any]:
    return {
        "source": SOURCE,
        "field": FIELD,
        "distinct_reason_count": len(reasons),
        "record_count": sum(reason.record_count for reason in reasons),
        "top_prefixes": list(prefixes),
        "clusters": [
            {
                "cluster_key": cluster.cluster_key,
                "text_count": cluster.text_count,
                "record_count": cluster.record_count,
                "coherence": cluster.coherence,
                "top_prefixes": cluster.top_prefixes,
                "examples": cluster.examples,
                "member_hashes": cluster.member_hashes,
            }
            for cluster in clusters
        ],
    }


SYSTEM = """You design a compact two-level taxonomy for FDA drug recall reasons.
Use the supplied prefix and embedding-cluster evidence. Return only categories that
are useful for closed-set labeling and SQL aggregation.

Rules:
- Use lowercase snake_case node_ids.
- Parent nodes have level=0 and parent_id=null.
- Child nodes have level=1 and parent_id equal to an existing parent node_id.
- Include a top-level node_id "other" for unclear or out-of-taxonomy reasons.
- Keep labels short and definitions operational enough for a labeler.
- Prefer broad stable categories over tiny one-off categories.
"""


@retry(stop=stop_after_attempt(4), wait=wait_exponential(multiplier=1, min=2, max=20))
def draft_with_llm(
    client: Any,
    *,
    config: llm.ChatConfig,
    version: str,
    payload: dict[str, Any],
) -> TaxonomyDraft:
    draft = llm.structured_completion(
        client,
        config,
        [
            {"role": "system", "content": SYSTEM},
            {
                "role": "user",
                "content": (
                    f"Taxonomy version: {version}\n"
                    "Evidence JSON:\n"
                    f"{json.dumps(payload, ensure_ascii=False)}"
                ),
            },
        ],
        TaxonomyDraft,
        temperature=0,
    )
    draft.version = version
    return draft


def deterministic_child(prefix: str) -> tuple[str, str, str, str] | None:
    normalized = normalize_text(prefix)
    for needles, parent_id, node_id, label, definition in CHILD_RULES:
        if any(needle in normalized for needle in needles):
            return parent_id, node_id, label, definition
    return None


def _extend_examples(current: list[str], examples: Sequence[str], *, limit: int = 8) -> None:
    seen = {normalize_text(example) for example in current}
    for example in examples:
        key = normalize_text(example)
        if key in seen:
            continue
        current.append(example)
        seen.add(key)
        if len(current) >= limit:
            break


def deterministic_draft(version: str, prefixes: Sequence[dict[str, Any]]) -> TaxonomyDraft:
    child_examples: dict[str, list[str]] = defaultdict(list)
    child_specs: dict[str, tuple[str, str, str, str]] = {}
    residual_examples: list[str] = []

    for item in prefixes:
        match = deterministic_child(item["prefix"])
        if match is None:
            _extend_examples(residual_examples, item.get("examples", []), limit=8)
            continue
        parent_id, node_id, label, definition = match
        child_specs[node_id] = (parent_id, node_id, label, definition)
        _extend_examples(child_examples[node_id], item.get("examples", []), limit=8)

    used_parents = {spec[0] for spec in child_specs.values()}
    ordered_parent_ids = [
        "quality_and_potency",
        "manufacturing_controls",
        "labeling_and_packaging",
        "regulatory_status",
        "storage_distribution",
    ]
    nodes: list[DraftNode] = []
    for parent_id in ordered_parent_ids:
        if parent_id not in used_parents:
            continue
        label, definition = PARENT_DEFINITIONS[parent_id]
        examples: list[str] = []
        for child_id, spec in child_specs.items():
            if spec[0] == parent_id:
                _extend_examples(examples, child_examples[child_id], limit=5)
        nodes.append(
            DraftNode(
                node_id=parent_id,
                parent_id=None,
                label=label,
                definition=definition,
                examples=examples[:5],
                level=0,
            )
        )

    for parent_id in ordered_parent_ids:
        children = sorted(
            (spec for spec in child_specs.values() if spec[0] == parent_id),
            key=lambda spec: spec[1],
        )
        for _, node_id, label, definition in children:
            nodes.append(
                DraftNode(
                    node_id=node_id,
                    parent_id=parent_id,
                    label=label,
                    definition=definition,
                    examples=child_examples[node_id][:5],
                    level=1,
                )
            )

    other_label, other_definition = PARENT_DEFINITIONS["other"]
    nodes.append(
        DraftNode(
            node_id="other",
            parent_id=None,
            label=other_label,
            definition=other_definition,
            examples=residual_examples[:5],
            level=0,
        )
    )
    return TaxonomyDraft(
        version=version,
        nodes=nodes,
        notes=[
            "Deterministic prefix-rule draft because --no-llm was used.",
            "Prefix rules create reviewable parent/child nodes from openFDA reason_for_recall evidence; human review must freeze v1 before any --apply.",
        ],
    )


def validate_draft(draft: TaxonomyDraft) -> None:
    node_ids = [node.node_id for node in draft.nodes]
    duplicates = [node_id for node_id, count in Counter(node_ids).items() if count > 1]
    if duplicates:
        raise ValueError(f"duplicate node_id(s): {', '.join(sorted(duplicates))}")
    known = set(node_ids)
    for node in draft.nodes:
        if node.parent_id and node.parent_id not in known:
            raise ValueError(f"{node.node_id!r} references unknown parent_id {node.parent_id!r}")
        if node.level == 0 and node.parent_id is not None:
            raise ValueError(f"root node {node.node_id!r} must not have parent_id")
        if node.level > 0 and not node.parent_id:
            raise ValueError(f"child node {node.node_id!r} must have parent_id")


def write_json(path: str, payload: dict[str, Any]) -> None:
    out = Path(path)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def apply_draft(conn: psycopg.Connection, draft: TaxonomyDraft, *, status: str) -> int:
    validate_draft(draft)
    ordered = sorted(draft.nodes, key=lambda node: (node.level, node.parent_id or "", node.node_id))
    rows = [
        (
            draft.version,
            node.node_id,
            node.parent_id,
            node.label,
            node.definition,
            node.examples,
            node.level,
            status,
        )
        for node in ordered
    ]
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


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Induce a draft taxonomy for recall reasons.")
    parser.add_argument("--dsn", default=DEFAULT_DSN, help="Postgres DSN")
    parser.add_argument("--version", default="v1", help="taxonomy version to emit/apply")
    parser.add_argument("--model", default=None, help="chat model override for taxonomy drafting")
    parser.add_argument("--output-file", default=DEFAULT_OUTPUT, help="JSON output path")
    parser.add_argument("--limit", type=int, default=None, help="max distinct reason texts to read")
    parser.add_argument("--max-prefixes", type=int, default=50, help="max mined prefixes for the LLM")
    parser.add_argument("--min-prefix-count", type=int, default=5, help="minimum distinct texts per prefix")
    parser.add_argument("--max-clusters", type=int, default=24, help="max embedding clusters")
    parser.add_argument("--min-cluster-size", type=int, default=25, help="minimum distinct texts per cluster")
    parser.add_argument("--no-llm", action="store_true", help="emit a deterministic prefix draft; no API call")
    parser.add_argument("--apply", action="store_true", help="write draft nodes to taxonomy")
    parser.add_argument("--apply-status", choices=("draft", "active"), default="draft",
                        help="status to use when --apply writes taxonomy rows")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    with psycopg.connect(args.dsn) as conn:
        reasons = fetch_reason_texts(conn, args.limit)
        if not reasons:
            raise ValueError("no non-empty reason_for_recall texts found")
        attached = attach_vectors(reasons, load_reason_vectors(conn))
        prefixes = prefix_summaries(
            reasons,
            min_count=args.min_prefix_count,
            max_prefixes=args.max_prefixes,
        )
        clusters = cluster_reasons(
            reasons,
            max_clusters=args.max_clusters,
            min_cluster_size=args.min_cluster_size,
        )
        payload = induction_payload(reasons, prefixes, clusters)
        if args.no_llm:
            draft = deterministic_draft(args.version, prefixes)
            chat_config = None
        else:
            chat_config = llm.chat_config(model=args.model)
            draft = draft_with_llm(
                llm.create_chat_client(chat_config),
                config=chat_config,
                version=args.version,
                payload=payload,
            )
        validate_draft(draft)

        output = {
            "version": args.version,
            "model": None if args.no_llm or chat_config is None else chat_config.model,
            "provider": None if args.no_llm or chat_config is None else chat_config.provider,
            "source": SOURCE,
            "field": FIELD,
            "distinct_reason_count": len(reasons),
            "record_count": sum(reason.record_count for reason in reasons),
            "vectors_found": attached,
            "prefix_count": len(prefixes),
            "cluster_count": len(clusters),
            "dry_run": not args.apply,
            "draft": draft.model_dump(),
            "evidence": payload,
        }
        write_json(args.output_file, output)

        if args.apply:
            inserted = apply_draft(conn, draft, status=args.apply_status)
            print(f"applied {inserted} taxonomy node(s) to version={args.version} status={args.apply_status}")
        else:
            print(f"dry-run: wrote taxonomy draft to {args.output_file}; no DB writes")
        print(
            f"read {len(reasons)} distinct reason text(s), "
            f"{attached} with vectors, {len(prefixes)} prefix(es), {len(clusters)} cluster(s)"
        )


if __name__ == "__main__":
    main()
