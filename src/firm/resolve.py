#!/usr/bin/env python3
"""Resolve FDA recalling_firm strings into conservative sidecar firm aliases.

The resolver is offline and dry-run by default:

    .venv/bin/python src/firm/resolve.py
    .venv/bin/python src/firm/resolve.py --limit 100
    .venv/bin/python src/firm/resolve.py --apply

It reads distinct ``drug_enforcement.recalling_firm`` values, normalizes tokens,
uses Postgres ``pg_trgm`` plus phonetic candidates, clusters accepted aliases with
union-find, and writes to ``firm`` / ``firm_alias`` only with ``--apply``. Ambiguous
candidate pairs are logged to ``resolution_log`` instead of being asserted as facts.
"""
from __future__ import annotations

import argparse
import os
import re
import unicodedata
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Iterable, Sequence

import psycopg
from dotenv import load_dotenv
from openai import OpenAI
from psycopg.types.json import Jsonb
from pydantic import BaseModel, Field

load_dotenv()
DEFAULT_DSN = os.environ.get("DATABASE_URL", "postgresql://localhost:5432/fda")
MODEL = os.environ.get("OPENAI_MODEL", "gpt-4o-mini")

LEGAL_SUFFIXES = {
    "ag",
    "bv",
    "co",
    "company",
    "corp",
    "corporation",
    "gmbh",
    "inc",
    "incorporated",
    "kg",
    "llc",
    "llp",
    "lp",
    "ltd",
    "limited",
    "nv",
    "plc",
    "pte",
    "sa",
    "sarl",
}
STOPWORDS = {"the"}
TOKEN_REWRITES = {
    "and": "&",
    "pharmaceutical": "pharma",
    "pharmaceuticals": "pharma",
    "laboratories": "labs",
    "laboratory": "labs",
    "mfg": "manufacturing",
    "usa": "us",
    "u": "us",
}


@dataclass(frozen=True)
class FirmName:
    idx: int
    raw_firm: str
    normalized: str
    tokens: tuple[str, ...]
    record_count: int

    @property
    def token_key(self) -> str:
        return " ".join(sorted(set(self.tokens)))

    @property
    def primary_token(self) -> str:
        return self.tokens[0] if self.tokens else ""


@dataclass
class CandidatePair:
    left: FirmName
    right: FirmName
    trigram_similarity: float
    word_similarity: float
    phonetic_match: bool
    token_jaccard: float
    decision: str = "needs_review"
    decision_reason: str = ""
    confidence: float = 0.0

    @property
    def key(self) -> tuple[int, int]:
        return (min(self.left.idx, self.right.idx), max(self.left.idx, self.right.idx))

    @property
    def score(self) -> float:
        return max(self.trigram_similarity, self.word_similarity, self.token_jaccard)


@dataclass
class Cluster:
    members: list[FirmName]
    edges: list[CandidatePair] = field(default_factory=list)

    @property
    def total_records(self) -> int:
        return sum(m.record_count for m in self.members)

    @property
    def canonical(self) -> FirmName:
        return sorted(self.members, key=lambda m: (-m.record_count, len(m.raw_firm), m.raw_firm.lower()))[0]

    @property
    def confidence(self) -> float:
        if not self.edges:
            return 1.0
        return min(edge.confidence for edge in self.edges)


class FirmPairVerification(BaseModel):
    same_entity: bool = Field(description="True only when the two names are the same firm/name variant.")
    confidence: float = Field(ge=0, le=1, description="Confidence in the same_entity decision.")
    reason: str = Field(description="Short reason based only on the names provided.")


class UnionFind:
    def __init__(self, items: Iterable[int]) -> None:
        items = list(items)
        self.parent = {item: item for item in items}
        self.rank = {item: 0 for item in items}

    def find(self, item: int) -> int:
        parent = self.parent[item]
        if parent != item:
            self.parent[item] = self.find(parent)
        return self.parent[item]

    def union(self, a: int, b: int) -> None:
        ra, rb = self.find(a), self.find(b)
        if ra == rb:
            return
        if self.rank[ra] < self.rank[rb]:
            ra, rb = rb, ra
        self.parent[rb] = ra
        if self.rank[ra] == self.rank[rb]:
            self.rank[ra] += 1


def normalize_name(value: str) -> str:
    """Normalize a firm/brand-like name without asserting identity."""
    text = unicodedata.normalize("NFKD", value)
    text = text.encode("ascii", "ignore").decode("ascii").lower()
    text = text.replace("&", " and ")
    text = re.sub(r"\bu\.?\s*s\.?\s*a\.?\b", " usa ", text)
    text = re.sub(r"[^a-z0-9]+", " ", text)
    tokens: list[str] = []
    for token in text.split():
        token = TOKEN_REWRITES.get(token, token)
        if token in LEGAL_SUFFIXES or token in STOPWORDS:
            continue
        tokens.append(token)
    return " ".join(tokens)


def _tokens(normalized: str) -> tuple[str, ...]:
    return tuple(token for token in normalized.split() if token)


def _jaccard(left: Sequence[str], right: Sequence[str]) -> float:
    a, b = set(left), set(right)
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def _require_extensions(conn: psycopg.Connection) -> None:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT extname FROM pg_extension WHERE extname IN ('pg_trgm', 'fuzzystrmatch')"
        )
        present = {row[0] for row in cur.fetchall()}
    missing = {"pg_trgm", "fuzzystrmatch"} - present
    if missing:
        raise RuntimeError(
            "Missing PostgreSQL extension(s): "
            + ", ".join(sorted(missing))
            + ". Run sql/008_firm_resolution.sql before executing the resolver."
        )


def _require_sidecar_tables(conn: psycopg.Connection) -> None:
    expected = {"firm", "firm_alias", "resolution_log"}
    with conn.cursor() as cur:
        cur.execute("SELECT to_regclass(%s), to_regclass(%s), to_regclass(%s)", tuple(expected))
        present = {name for name in cur.fetchone() if name is not None}
    missing = expected - present
    if missing:
        raise RuntimeError(
            "Missing sidecar table(s): "
            + ", ".join(sorted(missing))
            + ". Run sql/008_firm_resolution.sql before using --apply."
        )


def load_firm_names(conn: psycopg.Connection, *, limit: int | None) -> tuple[list[FirmName], list[str]]:
    q = """
        SELECT recalling_firm, count(*)::int AS record_count
        FROM drug_enforcement
        WHERE recalling_firm IS NOT NULL AND btrim(recalling_firm) <> ''
        GROUP BY recalling_firm
        ORDER BY record_count DESC, recalling_firm
    """
    params: list[object] = []
    if limit is not None:
        q += " LIMIT %s"
        params.append(limit)

    skipped: list[str] = []
    names: list[FirmName] = []
    with conn.cursor() as cur:
        cur.execute(q, params)
        for idx, (raw_firm, record_count) in enumerate(cur.fetchall()):
            normalized = normalize_name(raw_firm)
            tokens = _tokens(normalized)
            if not normalized or not tokens:
                skipped.append(raw_firm)
                continue
            names.append(FirmName(idx, raw_firm, normalized, tokens, record_count))
    return names, skipped


def pg_trgm_pairs(conn: psycopg.Connection, names: Sequence[FirmName], *,
                  threshold: float) -> list[tuple[int, int, float, float, bool]]:
    if len(names) < 2:
        return []

    with conn.cursor() as cur:
        cur.execute("DROP TABLE IF EXISTS firm_resolution_work")
        cur.execute(
            """
            CREATE TEMP TABLE firm_resolution_work (
                idx             integer PRIMARY KEY,
                raw_firm        text NOT NULL,
                normalized_firm text NOT NULL,
                token_key       text NOT NULL,
                primary_token   text NOT NULL,
                record_count    integer NOT NULL
            ) ON COMMIT DROP
            """
        )
        cur.executemany(
            """
            INSERT INTO firm_resolution_work
                (idx, raw_firm, normalized_firm, token_key, primary_token, record_count)
            VALUES (%s, %s, %s, %s, %s, %s)
            """,
            [
                (n.idx, n.raw_firm, n.normalized, n.token_key, n.primary_token, n.record_count)
                for n in names
            ],
        )
        phonetic_floor = max(0.45, threshold - 0.12)
        cur.execute(
            """
            SELECT
                a.idx,
                b.idx,
                similarity(a.normalized_firm, b.normalized_firm) AS trigram_similarity,
                word_similarity(a.normalized_firm, b.normalized_firm) AS word_similarity,
                (
                    a.primary_token <> ''
                    AND b.primary_token <> ''
                    AND metaphone(a.primary_token, 8) = metaphone(b.primary_token, 8)
                ) AS phonetic_match
            FROM firm_resolution_work a
            JOIN firm_resolution_work b ON a.idx < b.idx
            WHERE similarity(a.normalized_firm, b.normalized_firm) >= %s
               OR word_similarity(a.normalized_firm, b.normalized_firm) >= %s
               OR (
                    a.primary_token <> ''
                    AND b.primary_token <> ''
                    AND metaphone(a.primary_token, 8) = metaphone(b.primary_token, 8)
                    AND similarity(a.normalized_firm, b.normalized_firm) >= %s
               )
            ORDER BY trigram_similarity DESC, word_similarity DESC, a.idx, b.idx
            """,
            (threshold, threshold, phonetic_floor),
        )
        return [(row[0], row[1], float(row[2]), float(row[3]), bool(row[4])) for row in cur.fetchall()]


def build_candidate_pairs(
    names: Sequence[FirmName],
    rows: Sequence[tuple[int, int, float, float, bool]],
) -> list[CandidatePair]:
    by_idx = {name.idx: name for name in names}
    pairs: list[CandidatePair] = []
    for left_idx, right_idx, trigram, word, phonetic in rows:
        left, right = by_idx[left_idx], by_idx[right_idx]
        pairs.append(
            CandidatePair(
                left=left,
                right=right,
                trigram_similarity=trigram,
                word_similarity=word,
                phonetic_match=phonetic,
                token_jaccard=_jaccard(left.tokens, right.tokens),
            )
        )
    return pairs


def deterministic_reason(pair: CandidatePair, *, auto_merge_threshold: float,
                         token_threshold: float) -> str | None:
    exact_token_key = pair.left.token_key == pair.right.token_key
    if exact_token_key and pair.token_jaccard == 1.0:
        return "exact normalized token set"
    if pair.trigram_similarity >= auto_merge_threshold and pair.token_jaccard >= token_threshold:
        return "high trigram similarity with strong token overlap"
    if pair.word_similarity >= auto_merge_threshold and pair.token_jaccard >= token_threshold:
        return "high word similarity with strong token overlap"
    if pair.phonetic_match and pair.token_jaccard >= max(token_threshold, 0.9):
        return "phonetic primary-token match with strong token overlap"
    return None


def verify_pair(client: OpenAI, pair: CandidatePair, *, model: str) -> FirmPairVerification:
    parse = getattr(client.chat.completions, "parse", None) or client.beta.chat.completions.parse
    completion = parse(
        model=model,
        temperature=0,
        messages=[
            {
                "role": "system",
                "content": (
                    "You verify FDA recalling_firm name pairs. Return same_entity=true only "
                    "when the strings look like the same firm or direct spelling/legal-suffix "
                    "variant. Do not infer parent/subsidiary relationships, acquisitions, or "
                    "brand ownership from world knowledge. If uncertain, return false."
                ),
            },
            {
                "role": "user",
                "content": (
                    f"Firm A: {pair.left.raw_firm}\n"
                    f"Firm B: {pair.right.raw_firm}\n"
                    f"Normalized A: {pair.left.normalized}\n"
                    f"Normalized B: {pair.right.normalized}\n"
                    f"Trigram similarity: {pair.trigram_similarity:.3f}\n"
                    f"Token overlap: {pair.token_jaccard:.3f}"
                ),
            },
        ],
        response_format=FirmPairVerification,
    )
    return completion.choices[0].message.parsed


def classify_pairs(
    pairs: Sequence[CandidatePair],
    *,
    auto_merge_threshold: float,
    token_threshold: float,
    verify_llm: bool,
    llm_confidence_threshold: float,
    model: str,
) -> tuple[list[CandidatePair], list[CandidatePair]]:
    client = OpenAI() if verify_llm else None
    accepted: list[CandidatePair] = []
    review: list[CandidatePair] = []

    for pair in pairs:
        reason = deterministic_reason(
            pair,
            auto_merge_threshold=auto_merge_threshold,
            token_threshold=token_threshold,
        )
        if reason is not None:
            pair.decision = "accepted"
            pair.decision_reason = reason
            pair.confidence = max(pair.score, auto_merge_threshold)
            accepted.append(pair)
            continue

        if client is not None:
            verdict = verify_pair(client, pair, model=model)
            pair.decision_reason = f"llm: {verdict.reason}"
            pair.confidence = verdict.confidence
            if verdict.same_entity and verdict.confidence >= llm_confidence_threshold:
                pair.decision = "accepted"
                accepted.append(pair)
            else:
                pair.decision = "needs_review"
                review.append(pair)
            continue

        pair.decision_reason = "below deterministic merge threshold"
        pair.confidence = pair.score
        review.append(pair)

    return accepted, review


def build_clusters(names: Sequence[FirmName], accepted_pairs: Sequence[CandidatePair]) -> list[Cluster]:
    uf = UnionFind(name.idx for name in names)
    for pair in accepted_pairs:
        uf.union(pair.left.idx, pair.right.idx)

    members_by_root: dict[int, list[FirmName]] = defaultdict(list)
    for name in names:
        members_by_root[uf.find(name.idx)].append(name)

    edge_by_root: dict[int, list[CandidatePair]] = defaultdict(list)
    for pair in accepted_pairs:
        edge_by_root[uf.find(pair.left.idx)].append(pair)

    clusters = [
        Cluster(members=sorted(members, key=lambda m: (-m.record_count, m.raw_firm.lower())),
                edges=edge_by_root.get(root, []))
        for root, members in members_by_root.items()
    ]
    return sorted(clusters, key=lambda c: (-c.total_records, c.canonical.raw_firm.lower()))


def _edge_evidence(edge: CandidatePair) -> dict[str, object]:
    return {
        "left": edge.left.raw_firm,
        "right": edge.right.raw_firm,
        "trigram_similarity": round(edge.trigram_similarity, 4),
        "word_similarity": round(edge.word_similarity, 4),
        "token_jaccard": round(edge.token_jaccard, 4),
        "phonetic_match": edge.phonetic_match,
        "decision": edge.decision,
        "decision_reason": edge.decision_reason,
        "confidence": round(edge.confidence, 4),
    }


def write_clusters(
    conn: psycopg.Connection,
    clusters: Sequence[Cluster],
    review_pairs: Sequence[CandidatePair],
    skipped_raw: Sequence[str],
) -> tuple[int, int, int]:
    _require_sidecar_tables(conn)
    raw_to_firm_id: dict[str, int] = {}
    firm_count = 0
    alias_count = 0
    log_count = 0

    with conn.cursor() as cur:
        for cluster in clusters:
            canonical = cluster.canonical
            members = [
                {"raw_firm": member.raw_firm, "record_count": member.record_count}
                for member in cluster.members
            ]
            evidence = {
                "resolver": "src/firm/resolve.py",
                "source_table": "drug_enforcement",
                "source_field": "recalling_firm",
                "member_count": len(cluster.members),
                "total_records": cluster.total_records,
                "members": members,
                "accepted_edges": [_edge_evidence(edge) for edge in cluster.edges],
            }
            cur.execute(
                """
                INSERT INTO firm
                    (canonical_name, normalized_name, fda_present, source, confidence, evidence)
                VALUES (%s, %s, true, 'fda', %s, %s)
                ON CONFLICT (normalized_name) DO UPDATE
                    SET canonical_name = EXCLUDED.canonical_name,
                        fda_present = true,
                        source = 'fda',
                        confidence = GREATEST(firm.confidence, EXCLUDED.confidence),
                        evidence = EXCLUDED.evidence,
                        updated_at = now()
                RETURNING id
                """,
                (canonical.raw_firm, canonical.normalized, cluster.confidence, Jsonb(evidence)),
            )
            firm_id = cur.fetchone()[0]
            firm_count += 1

            for member in cluster.members:
                alias_evidence = {
                    "resolver": "src/firm/resolve.py",
                    "canonical_name": canonical.raw_firm,
                    "cluster_size": len(cluster.members),
                    "record_count": member.record_count,
                }
                cur.execute(
                    """
                    INSERT INTO firm_alias
                        (
                            raw_firm,
                            normalized_raw_firm,
                            firm_id,
                            alias_kind,
                            source_table,
                            source_field,
                            record_count,
                            source,
                            confidence,
                            evidence
                        )
                    VALUES (%s, %s, %s, 'recalling_firm', 'drug_enforcement',
                            'recalling_firm', %s, 'fda', %s, %s)
                    ON CONFLICT (source_table, source_field, raw_firm) DO UPDATE
                        SET normalized_raw_firm = EXCLUDED.normalized_raw_firm,
                            firm_id = EXCLUDED.firm_id,
                            record_count = EXCLUDED.record_count,
                            source = 'fda',
                            confidence = GREATEST(firm_alias.confidence, EXCLUDED.confidence),
                            evidence = EXCLUDED.evidence,
                            updated_at = now()
                    """,
                    (
                        member.raw_firm,
                        member.normalized,
                        firm_id,
                        member.record_count,
                        cluster.confidence,
                        Jsonb(alias_evidence),
                    ),
                )
                raw_to_firm_id[member.raw_firm] = firm_id
                alias_count += 1

        for pair in review_pairs:
            candidate_ids = sorted({
                raw_to_firm_id[pair.left.raw_firm],
                raw_to_firm_id[pair.right.raw_firm],
            } & set(raw_to_firm_id.values()))
            cur.execute(
                """
                INSERT INTO resolution_log
                    (
                        entity_type,
                        input_value,
                        normalized_input,
                        status,
                        reason,
                        candidate_firm_ids,
                        provenance_tier,
                        source,
                        evidence
                    )
                VALUES ('firm', %s, %s, 'needs_review', %s, %s,
                        'fda_fact', 'src/firm/resolve.py', %s)
                """,
                (
                    f"{pair.left.raw_firm} <-> {pair.right.raw_firm}",
                    f"{pair.left.normalized} <-> {pair.right.normalized}",
                    pair.decision_reason,
                    candidate_ids,
                    Jsonb(_edge_evidence(pair)),
                ),
            )
            log_count += 1

        for raw in skipped_raw:
            cur.execute(
                """
                INSERT INTO resolution_log
                    (entity_type, input_value, normalized_input, status, reason,
                     provenance_tier, source, evidence)
                VALUES ('firm', %s, NULL, 'skipped',
                        'empty value after token normalization', 'unknown',
                        'src/firm/resolve.py', %s)
                """,
                (raw, Jsonb({"resolver": "src/firm/resolve.py"})),
            )
            log_count += 1

    conn.commit()
    return firm_count, alias_count, log_count


def print_summary(
    names: Sequence[FirmName],
    pairs: Sequence[CandidatePair],
    accepted_pairs: Sequence[CandidatePair],
    review_pairs: Sequence[CandidatePair],
    clusters: Sequence[Cluster],
    *,
    top: int,
) -> None:
    print(f"firms loaded       : {len(names)}")
    print(f"candidate pairs    : {len(pairs)}")
    print(f"accepted pairs     : {len(accepted_pairs)}")
    print(f"needs review pairs : {len(review_pairs)}")
    print(f"clusters           : {len(clusters)}")

    merged = [cluster for cluster in clusters if len(cluster.members) > 1]
    if not merged:
        print("\nNo multi-alias clusters were accepted.")
        return

    print(f"\nTop {min(top, len(merged))} accepted multi-alias cluster(s):")
    for cluster in merged[:top]:
        names_text = "; ".join(member.raw_firm for member in cluster.members[:8])
        suffix = " ..." if len(cluster.members) > 8 else ""
        print(
            f"- {cluster.canonical.raw_firm} "
            f"({len(cluster.members)} aliases, {cluster.total_records} records, "
            f"confidence={cluster.confidence:.3f}): {names_text}{suffix}"
        )


def run(args: argparse.Namespace) -> int:
    if args.verify_llm and not os.environ.get("OPENAI_API_KEY"):
        raise RuntimeError("--verify-llm requires OPENAI_API_KEY")

    with psycopg.connect(args.db) as conn:
        _require_extensions(conn)
        names, skipped = load_firm_names(conn, limit=args.limit)
        rows = pg_trgm_pairs(conn, names, threshold=args.threshold)
        pairs = build_candidate_pairs(names, rows)
        accepted_pairs, review_pairs = classify_pairs(
            pairs,
            auto_merge_threshold=args.auto_merge_threshold,
            token_threshold=args.token_threshold,
            verify_llm=args.verify_llm,
            llm_confidence_threshold=args.llm_confidence_threshold,
            model=args.model,
        )
        clusters = build_clusters(names, accepted_pairs)

        print_summary(
            names,
            pairs,
            accepted_pairs,
            review_pairs,
            clusters,
            top=args.show_clusters,
        )
        if skipped:
            print(f"\nskipped after normalization: {len(skipped)}")

        if not args.apply:
            print("\nDry run only. Re-run with --apply to write firm, firm_alias, and resolution_log rows.")
            return 0

        firm_count, alias_count, log_count = write_clusters(conn, clusters, review_pairs, skipped)
        print(
            "\nApplied sidecar writes: "
            f"{firm_count} firm row(s), {alias_count} firm_alias row(s), "
            f"{log_count} resolution_log row(s)."
        )
        return 0


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Offline firm/entity resolver for drug_enforcement.recalling_firm."
    )
    p.add_argument("--db", default=DEFAULT_DSN,
                   help="Postgres DSN (default: $DATABASE_URL or postgresql://localhost:5432/fda)")
    p.add_argument("--limit", type=int, default=None,
                   help="Limit distinct recalling_firm values for a test run")
    p.add_argument("--threshold", type=float, default=0.62,
                   help="pg_trgm candidate threshold before conservative merge checks")
    p.add_argument("--auto-merge-threshold", type=float, default=0.86,
                   help="minimum trigram/word similarity for deterministic alias merges")
    p.add_argument("--token-threshold", type=float, default=0.80,
                   help="minimum token Jaccard overlap for deterministic alias merges")
    p.add_argument("--verify-llm", action="store_true",
                   help="use structured LLM verification for pairs below deterministic thresholds")
    p.add_argument("--llm-confidence-threshold", type=float, default=0.90,
                   help="minimum LLM confidence required to accept a pair")
    p.add_argument("--model", default=MODEL,
                   help=f"OpenAI model for --verify-llm (default: {MODEL})")
    p.add_argument("--show-clusters", type=int, default=10,
                   help="number of accepted multi-alias clusters to print")
    p.add_argument("--apply", action="store_true",
                   help="write sidecar firm/alias/log rows; default is report-only")
    return p.parse_args()


if __name__ == "__main__":
    raise SystemExit(run(parse_args()))
