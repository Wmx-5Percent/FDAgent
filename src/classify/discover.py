"""Discover candidate taxonomy categories from residual recall reasons.

Offline Phase 4, P3: inspect records with no labels, low-confidence labels, or
an explicit other label; cluster their reason_for_recall text; compute size,
growth, and coherence signals; and ask the LLM to name candidate new categories.
By default this writes only a report. taxonomy_candidate writes happen only with
--apply.

Run:
    .venv/bin/python src/classify/discover.py --version v1
    .venv/bin/python src/classify/discover.py --version v1 --no-llm
    .venv/bin/python src/classify/discover.py --version v1 --apply
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
from collections import Counter, defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Sequence

import psycopg
from dotenv import load_dotenv
from openai import OpenAI
from psycopg import errors
from psycopg.types.json import Jsonb
from pydantic import BaseModel, Field, field_validator
from tenacity import retry, stop_after_attempt, wait_exponential

load_dotenv()

DEFAULT_DSN = os.environ.get("DATABASE_URL", "postgresql://localhost:5432/fda")
DEFAULT_MODEL = os.environ.get("OPENAI_MODEL", "gpt-4o-mini")
SOURCE = "drug_enforcement"
FIELD = "reason_for_recall"


@dataclass(frozen=True)
class TaxonomyNode:
    node_id: str
    parent_id: str | None
    label: str
    definition: str
    examples: list[str]
    level: int
    status: str


@dataclass
class ResidualReason:
    text: str
    text_hash: str
    record_count: int
    growth_count: int
    first_report_date: str | None
    last_report_date: str | None
    vector: list[float] | None = None


@dataclass
class ResidualCluster:
    cluster_key: str
    text_count: int
    size: int
    growth_count: int
    coherence: float | None
    top_prefixes: list[dict[str, Any]]
    examples: list[str]
    member_hashes: list[str]


class CandidateCategory(BaseModel):
    cluster_key: str
    candidate_node_id: str
    parent_id: str | None = None
    proposed_label: str
    definition: str
    examples: list[str] = Field(default_factory=list)
    confidence: float = Field(ge=0, le=1)

    @field_validator("candidate_node_id")
    @classmethod
    def node_id_is_slug(cls, value: str) -> str:
        if not re.fullmatch(r"[a-z][a-z0-9_]{1,80}", value):
            raise ValueError("candidate_node_id must be lowercase snake_case and start with a letter")
        return value


class CandidateDraft(BaseModel):
    candidates: list[CandidateCategory] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)


def normalize_text(text: str) -> str:
    return re.sub(r"\s+", " ", text.strip()).lower()


def text_hash(text: str) -> str:
    return hashlib.sha256(normalize_text(text).encode("utf-8")).hexdigest()


def safe_name(value: str) -> str:
    name = re.sub(r"[^a-zA-Z0-9_.-]+", "_", value).strip("_")
    return name or "default"


def slugify(value: str, *, fallback: str = "candidate") -> str:
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


def output_file(version: str) -> str:
    return f"data/processed/taxonomy_candidates_{safe_name(version)}.json"


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


def fetch_residual_reasons(
    conn: psycopg.Connection,
    *,
    version: str,
    labeler: str,
    confidence_threshold: float,
    other_node_id: str,
    recent_days: int,
    limit: int | None,
) -> list[ResidualReason]:
    query = """
        SELECT d.reason_for_recall,
               count(*)::integer AS record_count,
               count(*) FILTER (
                   WHERE d.report_date >= (current_date - (%s * interval '1 day'))
               )::integer AS growth_count,
               min(d.report_date)::text AS first_report_date,
               max(d.report_date)::text AS last_report_date
        FROM drug_enforcement d
        WHERE d.reason_for_recall IS NOT NULL
          AND length(btrim(d.reason_for_recall)) > 0
          AND (
              NOT EXISTS (
                  SELECT 1
                  FROM recall_label rl
                  WHERE rl.record_id = d.id
                    AND rl.version = %s
                    AND rl.labeler = %s
              )
              OR EXISTS (
                  SELECT 1
                  FROM recall_label rl
                  LEFT JOIN taxonomy t
                    ON t.version = rl.version AND t.node_id = rl.node_id
                  WHERE rl.record_id = d.id
                    AND rl.version = %s
                    AND rl.labeler = %s
                    AND (
                        rl.confidence < %s
                        OR rl.node_id = %s
                        OR lower(t.label) = 'other'
                    )
              )
          )
        GROUP BY d.reason_for_recall
        ORDER BY record_count DESC, d.reason_for_recall
    """
    params: list[Any] = [
        recent_days,
        version,
        labeler,
        version,
        labeler,
        confidence_threshold,
        other_node_id,
    ]
    if limit is not None:
        query += " LIMIT %s"
        params.append(limit)
    with conn.cursor() as cur:
        cur.execute(query, params)
        rows = cur.fetchall()
    return [
        ResidualReason(
            text=row[0],
            text_hash=text_hash(row[0]),
            record_count=int(row[1]),
            growth_count=int(row[2]),
            first_report_date=row[3],
            last_report_date=row[4],
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
        print("warning: embeddings table/columns are unavailable; using prefix residual clusters only")
        return {}
    return {content: vec for content, raw in rows if (vec := parse_vector(raw))}


def attach_vectors(reasons: list[ResidualReason], vectors: dict[str, list[float]]) -> int:
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
    reasons: Sequence[ResidualReason],
    *,
    min_count: int,
    max_prefixes: int,
) -> list[dict[str, Any]]:
    counts: Counter[str] = Counter()
    records: Counter[str] = Counter()
    examples: dict[str, list[str]] = defaultdict(list)
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


def cluster_key(member_hashes: Sequence[str], prefix: str | None = None) -> str:
    seed = "|".join(sorted(member_hashes)[:50])
    if prefix:
        seed = f"{prefix}|{seed}"
    return hashlib.sha256(seed.encode("utf-8")).hexdigest()[:16]


def vector_clusters(
    reasons: Sequence[ResidualReason],
    *,
    max_clusters: int,
    min_cluster_size: int,
) -> list[ResidualCluster]:
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
        random_state=17,
        n_init=10,
        batch_size=min(512, max(32, len(vector_reasons))),
    )
    labels = model.fit_predict(matrix)
    centers = model.cluster_centers_
    center_norms = np.linalg.norm(centers, axis=1, keepdims=True)
    center_norms[center_norms == 0] = 1
    centers = centers / center_norms

    grouped: dict[int, list[tuple[int, ResidualReason]]] = defaultdict(list)
    for idx, label in enumerate(labels):
        grouped[int(label)].append((idx, vector_reasons[idx]))

    clusters: list[ResidualCluster] = []
    for label, members in grouped.items():
        if len(members) < min_cluster_size:
            continue
        member_reasons = [reason for _, reason in members]
        sims = [float(matrix[idx].dot(centers[label])) for idx, _ in members]
        coherence = round(max(0.0, min(1.0, sum(sims) / len(sims))), 4)
        member_reasons.sort(key=lambda item: (-item.record_count, item.text))
        hashes = [reason.text_hash for reason in member_reasons]
        clusters.append(
            ResidualCluster(
                cluster_key=cluster_key(hashes),
                text_count=len(member_reasons),
                size=sum(reason.record_count for reason in member_reasons),
                growth_count=sum(reason.growth_count for reason in member_reasons),
                coherence=coherence,
                top_prefixes=prefix_summaries(member_reasons, min_count=2, max_prefixes=8),
                examples=[reason.text for reason in member_reasons[:8]],
                member_hashes=sorted(hashes)[:20],
            )
        )
    clusters.sort(key=lambda item: (-item.size, -(item.coherence or 0), item.cluster_key))
    return clusters


def prefix_clusters(
    reasons: Sequence[ResidualReason],
    *,
    min_cluster_size: int,
) -> list[ResidualCluster]:
    grouped: dict[str, list[ResidualReason]] = defaultdict(list)
    for reason in reasons:
        prefix = extract_prefix(reason.text)
        if prefix:
            grouped[prefix].append(reason)
    clusters: list[ResidualCluster] = []
    for prefix, members in grouped.items():
        if len(members) < min_cluster_size:
            continue
        members.sort(key=lambda item: (-item.record_count, item.text))
        hashes = [reason.text_hash for reason in members]
        clusters.append(
            ResidualCluster(
                cluster_key=cluster_key(hashes, prefix=prefix),
                text_count=len(members),
                size=sum(reason.record_count for reason in members),
                growth_count=sum(reason.growth_count for reason in members),
                coherence=1.0,
                top_prefixes=prefix_summaries(members, min_count=2, max_prefixes=8),
                examples=[reason.text for reason in members[:8]],
                member_hashes=sorted(hashes)[:20],
            )
        )
    clusters.sort(key=lambda item: (-item.size, item.cluster_key))
    return clusters


def discover_clusters(
    reasons: Sequence[ResidualReason],
    *,
    max_clusters: int,
    min_cluster_size: int,
) -> list[ResidualCluster]:
    clusters = vector_clusters(reasons, max_clusters=max_clusters, min_cluster_size=min_cluster_size)
    if clusters:
        return clusters
    return prefix_clusters(reasons, min_cluster_size=min_cluster_size)


def taxonomy_context(nodes: Sequence[TaxonomyNode]) -> str:
    lines: list[str] = []
    for node in nodes:
        parent = node.parent_id or "-"
        lines.append(
            f"- {node.node_id} | label={node.label} | level={node.level} | "
            f"parent={parent} | definition={node.definition}"
        )
    return "\n".join(lines)


def clusters_payload(clusters: Sequence[ResidualCluster]) -> list[dict[str, Any]]:
    return [
        {
            "cluster_key": cluster.cluster_key,
            "text_count": cluster.text_count,
            "size": cluster.size,
            "growth_count": cluster.growth_count,
            "coherence": cluster.coherence,
            "top_prefixes": cluster.top_prefixes,
            "examples": cluster.examples,
            "member_hashes": cluster.member_hashes,
        }
        for cluster in clusters
    ]


SYSTEM = """You review residual FDA drug recall reasons that were unlabeled,
low-confidence, or assigned to other. Propose only genuinely new taxonomy
categories that are not already covered by the existing taxonomy.

Rules:
- candidate_node_id must be lowercase snake_case.
- cluster_key must exactly match one input residual cluster.
- parent_id must be null or one existing taxonomy node_id.
- Leave candidates empty when residual clusters are noise or duplicates.
- Definitions must be operational enough for a future closed-set labeler.
"""


@retry(stop=stop_after_attempt(4), wait=wait_exponential(multiplier=1, min=2, max=20))
def candidates_with_llm(
    client: OpenAI,
    *,
    model: str,
    taxonomy_nodes: Sequence[TaxonomyNode],
    clusters: Sequence[ResidualCluster],
) -> CandidateDraft:
    parse = getattr(client.chat.completions, "parse", None) or client.beta.chat.completions.parse
    completion = parse(
        model=model,
        temperature=0,
        messages=[
            {"role": "system", "content": SYSTEM},
            {
                "role": "user",
                "content": (
                    "Existing taxonomy:\n"
                    f"{taxonomy_context(taxonomy_nodes)}\n\n"
                    "Residual clusters JSON:\n"
                    f"{json.dumps(clusters_payload(clusters), ensure_ascii=False)}"
                ),
            },
        ],
        response_format=CandidateDraft,
    )
    return completion.choices[0].message.parsed


def deterministic_candidates(clusters: Sequence[ResidualCluster]) -> CandidateDraft:
    candidates: list[CandidateCategory] = []
    for cluster in clusters[:20]:
        label_seed = cluster.top_prefixes[0]["prefix"] if cluster.top_prefixes else cluster.examples[0][:60]
        label = label_seed.title()
        candidates.append(
            CandidateCategory(
                cluster_key=cluster.cluster_key,
                candidate_node_id=slugify(label_seed),
                parent_id=None,
                proposed_label=label,
                definition=f"Residual recall reasons related to {label}.",
                examples=cluster.examples[:5],
                confidence=cluster.coherence if cluster.coherence is not None else 0.5,
            )
        )
    return CandidateDraft(
        candidates=candidates,
        notes=["Deterministic residual candidates because --no-llm was used."],
    )


def validate_candidates(
    draft: CandidateDraft,
    *,
    clusters: Sequence[ResidualCluster],
    taxonomy_nodes: Sequence[TaxonomyNode],
) -> None:
    known_clusters = {cluster.cluster_key for cluster in clusters}
    known_nodes = {node.node_id for node in taxonomy_nodes}
    for candidate in draft.candidates:
        if candidate.cluster_key not in known_clusters:
            raise ValueError(f"candidate references unknown cluster_key {candidate.cluster_key!r}")
        if candidate.parent_id is not None and candidate.parent_id not in known_nodes:
            raise ValueError(f"candidate references unknown parent_id {candidate.parent_id!r}")


def apply_candidates(
    conn: psycopg.Connection,
    *,
    version: str,
    draft: CandidateDraft,
    clusters_by_key: dict[str, ResidualCluster],
) -> int:
    rows: list[tuple[Any, ...]] = []
    for candidate in draft.candidates:
        cluster = clusters_by_key[candidate.cluster_key]
        evidence = {
            "member_hashes": cluster.member_hashes,
            "top_prefixes": cluster.top_prefixes,
            "examples": cluster.examples,
            "text_count": cluster.text_count,
        }
        rows.append(
            (
                version,
                candidate.cluster_key,
                candidate.candidate_node_id,
                candidate.parent_id,
                candidate.proposed_label,
                candidate.definition,
                candidate.examples[:8],
                cluster.size,
                cluster.growth_count,
                cluster.coherence,
                round(float(candidate.confidence), 4),
                Jsonb(evidence),
            )
        )
    if not rows:
        return 0
    with conn.cursor() as cur:
        cur.executemany(
            """
            INSERT INTO taxonomy_candidate
                (taxonomy_version, cluster_key, candidate_node_id, parent_id,
                 proposed_label, definition, examples, size, growth_count,
                 coherence, confidence, evidence)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (taxonomy_version, cluster_key) DO UPDATE
                SET candidate_node_id = EXCLUDED.candidate_node_id,
                    parent_id = EXCLUDED.parent_id,
                    proposed_label = EXCLUDED.proposed_label,
                    definition = EXCLUDED.definition,
                    examples = EXCLUDED.examples,
                    size = EXCLUDED.size,
                    growth_count = EXCLUDED.growth_count,
                    coherence = EXCLUDED.coherence,
                    confidence = EXCLUDED.confidence,
                    evidence = EXCLUDED.evidence,
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
    parser = argparse.ArgumentParser(description="Discover candidate taxonomy categories from residuals.")
    parser.add_argument("--dsn", default=DEFAULT_DSN, help="Postgres DSN")
    parser.add_argument("--version", default="v1", help="taxonomy version to inspect")
    parser.add_argument("--taxonomy-status", choices=("active", "draft", "deprecated", "any"),
                        default="active", help="taxonomy node status filter")
    parser.add_argument("--model", default=DEFAULT_MODEL, help="OpenAI model for candidate naming")
    parser.add_argument("--labeler", default=None, help="labeler id used in recall_label")
    parser.add_argument("--output-file", default=None, help="JSON report path")
    parser.add_argument("--confidence-threshold", type=float, default=0.70,
                        help="labels below this confidence are residuals")
    parser.add_argument("--other-node-id", default="other", help="node_id treated as explicit other")
    parser.add_argument("--recent-days", type=int, default=180, help="window for growth_count signal")
    parser.add_argument("--limit", type=int, default=None, help="max distinct residual reasons to inspect")
    parser.add_argument("--max-clusters", type=int, default=24, help="max residual clusters")
    parser.add_argument("--min-cluster-size", type=int, default=10, help="minimum distinct texts per cluster")
    parser.add_argument("--no-llm", action="store_true", help="emit deterministic candidates; no API call")
    parser.add_argument("--apply", action="store_true", help="upsert taxonomy_candidate rows")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    if not 0 <= args.confidence_threshold <= 1:
        raise ValueError("--confidence-threshold must be between 0 and 1")
    labeler = args.labeler or f"llm:{args.model}"
    out = args.output_file or output_file(args.version)

    with psycopg.connect(args.dsn) as conn:
        taxonomy_nodes = load_taxonomy(conn, args.version, args.taxonomy_status)
        residuals = fetch_residual_reasons(
            conn,
            version=args.version,
            labeler=labeler,
            confidence_threshold=args.confidence_threshold,
            other_node_id=args.other_node_id,
            recent_days=args.recent_days,
            limit=args.limit,
        )
        attached = attach_vectors(residuals, load_reason_vectors(conn)) if residuals else 0
        clusters = discover_clusters(
            residuals,
            max_clusters=args.max_clusters,
            min_cluster_size=args.min_cluster_size,
        ) if residuals else []

        if args.no_llm or not clusters:
            draft = deterministic_candidates(clusters)
        else:
            draft = candidates_with_llm(
                OpenAI(),
                model=args.model,
                taxonomy_nodes=taxonomy_nodes,
                clusters=clusters,
            )
        validate_candidates(draft, clusters=clusters, taxonomy_nodes=taxonomy_nodes)
        clusters_by_key = {cluster.cluster_key: cluster for cluster in clusters}
        applied = apply_candidates(
            conn,
            version=args.version,
            draft=draft,
            clusters_by_key=clusters_by_key,
        ) if args.apply else 0

        report = {
            "version": args.version,
            "taxonomy_status": args.taxonomy_status,
            "model": None if args.no_llm else args.model,
            "labeler": labeler,
            "confidence_threshold": args.confidence_threshold,
            "other_node_id": args.other_node_id,
            "recent_days": args.recent_days,
            "dry_run": not args.apply,
            "residual_distinct_reason_count": len(residuals),
            "residual_record_count": sum(reason.record_count for reason in residuals),
            "vectors_found": attached,
            "cluster_count": len(clusters),
            "applied_candidates": applied,
            "clusters": clusters_payload(clusters),
            "candidate_draft": draft.model_dump(),
        }
        write_json(out, report)

    if args.apply:
        print(f"applied {applied} taxonomy_candidate row(s); report={out}")
    else:
        print(f"dry-run: wrote candidate report to {out}; no DB writes")
    print(
        f"residuals={len(residuals)} distinct text(s), "
        f"vectors={attached}, clusters={len(clusters)}, candidates={len(draft.candidates)}"
    )


if __name__ == "__main__":
    main()
