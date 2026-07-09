#!/usr/bin/env python3
"""Resolve FDA recalling_firm strings into conservative sidecar firm aliases.

The resolver is offline, source-table aware, and dry-run by default:

    .venv/bin/python src/firm/resolve.py
    .venv/bin/python src/firm/resolve.py --mode full --limit 100
    .venv/bin/python src/firm/resolve.py --mode incremental --verification-policy web --apply
    .venv/bin/python src/firm/resolve.py --calibrate-golden evals/firm_resolution/golden_v1.json

Production flow runs after ingestion: ``fetch_openfda.py --since auto`` brings in
new rows, then this resolver discovers new/changed source firm strings, updates
only needed sidecar aliases, and records run/pair audit rows. It never rewrites
source FDA tables. Local string rules only recall candidate pairs; by default,
OpenRouter web search (DeepSeek V4 Pro unless overridden) verifies identity before
auto-merge. Ambiguous pairs go to review; unknown/skipped values go to
``resolution_log`` instead of being asserted as external identities.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import unicodedata
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Sequence

import psycopg
from dotenv import load_dotenv
from openai import OpenAI
from psycopg import sql
from psycopg.types.json import Jsonb
from pydantic import BaseModel, Field

try:
    from . import web_verify
except ImportError:  # script execution: python src/firm/resolve.py
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import web_verify  # type: ignore

load_dotenv()
DEFAULT_DSN = os.environ.get("DATABASE_URL", "postgresql://localhost:5432/fda")
MODEL = os.environ.get("OPENAI_MODEL", "gpt-4o-mini")
WEB_MODEL = web_verify.DEFAULT_WEB_MODEL

MODES = ("full", "incremental")
VERIFICATION_POLICIES = ("web", "deterministic", "llm")
DECISION_ACCEPTED = "accepted"
DECISION_NEEDS_REVIEW = "needs_review"
DECISION_REJECTED = "rejected"

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
class SourceConfig:
    table: str
    field: str


@dataclass(frozen=True)
class FirmName:
    idx: int
    raw_firm: str
    normalized: str
    tokens: tuple[str, ...]
    record_count: int
    selected: bool = True
    selection_reason: str = "full"
    existing_firm_id: int | None = None
    existing_alias_record_count: int | None = None

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
    decision: str = DECISION_NEEDS_REVIEW
    decision_reason: str = ""
    confidence: float = 0.0
    verified_by_llm: bool = False
    verification_method: str = "none"
    citations: list[dict[str, str]] = field(default_factory=list)

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
    def selected_members(self) -> list[FirmName]:
        return [member for member in self.members if member.selected]

    @property
    def total_records(self) -> int:
        return sum(m.record_count for m in self.members)

    @property
    def canonical(self) -> FirmName:
        existing = [m for m in self.members if m.existing_firm_id is not None]
        candidates = existing or self.members
        return sorted(candidates, key=lambda m: (-m.record_count, len(m.raw_firm), m.raw_firm.lower()))[0]

    @property
    def existing_firm_id(self) -> int | None:
        existing = [m for m in self.members if m.existing_firm_id is not None]
        if not existing:
            return None
        return self.canonical.existing_firm_id

    @property
    def confidence(self) -> float:
        if not self.edges:
            return 1.0
        return min(edge.confidence for edge in self.edges)


@dataclass(frozen=True)
class LoadedNames:
    names: list[FirmName]
    selected_count: int
    skipped_selected: list[str]

    @property
    def selected_idxs(self) -> set[int]:
        return {name.idx for name in self.names if name.selected}


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


def _require_tables(conn: psycopg.Connection, expected: Sequence[str], *, setup_hint: str) -> None:
    missing: list[str] = []
    with conn.cursor() as cur:
        for table in expected:
            cur.execute("SELECT to_regclass(%s)", (table,))
            if cur.fetchone()[0] is None:
                missing.append(table)
    if missing:
        raise RuntimeError(
            "Missing table(s): " + ", ".join(missing) + f". Run {setup_hint} before executing this command."
        )


def _require_sidecar_tables(conn: psycopg.Connection) -> None:
    _require_tables(
        conn,
        ("firm", "firm_alias", "resolution_log"),
        setup_hint="sql/008_firm_resolution.sql",
    )


def _require_audit_tables(conn: psycopg.Connection) -> None:
    _require_tables(
        conn,
        ("firm_resolution_run", "firm_match_pair"),
        setup_hint="sql/009_firm_resolution_runs.sql",
    )


def _validate_source_exists(conn: psycopg.Connection, source: SourceConfig) -> None:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT 1
            FROM information_schema.columns
            WHERE table_schema = current_schema()
              AND table_name = %s
              AND column_name = %s
            """,
            (source.table, source.field),
        )
        if cur.fetchone() is None:
            raise RuntimeError(f"Source column not found: {source.table}.{source.field}")


def _source_rows(conn: psycopg.Connection, source: SourceConfig) -> list[tuple[str, int]]:
    q = sql.SQL(
        """
        SELECT {field}::text AS raw_firm, count(*)::int AS record_count
        FROM {table}
        WHERE {field} IS NOT NULL AND btrim({field}::text) <> ''
        GROUP BY {field}
        ORDER BY record_count DESC, raw_firm
        """
    ).format(table=sql.Identifier(source.table), field=sql.Identifier(source.field))
    with conn.cursor() as cur:
        cur.execute(q)
        return [(row[0], int(row[1])) for row in cur.fetchall()]


def _existing_aliases(conn: psycopg.Connection, source: SourceConfig) -> dict[str, tuple[int, int]]:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT raw_firm, firm_id, record_count
            FROM firm_alias
            WHERE source_table = %s AND source_field = %s
            """,
            (source.table, source.field),
        )
        return {row[0]: (int(row[1]), int(row[2])) for row in cur.fetchall()}


def _retry_values(conn: psycopg.Connection) -> set[str]:
    """Raw single-value firm inputs previously logged as unknown/review-worthy."""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT DISTINCT input_value
            FROM resolution_log
            WHERE entity_type = 'firm'
              AND status IN ('unknown', 'needs_review')
              AND input_value NOT LIKE '% <-> %'
            """
        )
        return {row[0] for row in cur.fetchall()}


def load_firm_names(
    conn: psycopg.Connection,
    *,
    source: SourceConfig,
    mode: str,
    limit: int | None,
) -> LoadedNames:
    _validate_source_exists(conn, source)
    rows = _source_rows(conn, source)
    aliases = _existing_aliases(conn, source)
    retry_values = _retry_values(conn)

    selected_raw: list[str] = []
    selection_reason: dict[str, str] = {}
    for raw_firm, record_count in rows:
        existing = aliases.get(raw_firm)
        reason = ""
        if mode == "full":
            reason = "full"
        elif existing is None:
            reason = "new_raw_firm"
        elif existing[1] != record_count:
            reason = "record_count_changed"
        elif raw_firm in retry_values:
            reason = "retry_logged_value"

        if reason:
            selected_raw.append(raw_firm)
            selection_reason[raw_firm] = reason

    if limit is not None:
        selected_raw = selected_raw[:limit]
    selected_set = set(selected_raw)

    skipped_selected: list[str] = []
    names: list[FirmName] = []
    for raw_firm, record_count in rows:
        normalized = normalize_name(raw_firm)
        tokens = _tokens(normalized)
        selected = raw_firm in selected_set
        if not normalized or not tokens:
            if selected:
                skipped_selected.append(raw_firm)
            continue
        existing = aliases.get(raw_firm)
        names.append(
            FirmName(
                idx=len(names),
                raw_firm=raw_firm,
                normalized=normalized,
                tokens=tokens,
                record_count=record_count,
                selected=selected,
                selection_reason=selection_reason.get(raw_firm, "comparison_pool"),
                existing_firm_id=existing[0] if existing else None,
                existing_alias_record_count=existing[1] if existing else None,
            )
        )
    return LoadedNames(names=names, selected_count=len(selected_set), skipped_selected=skipped_selected)


def pg_trgm_pairs(
    conn: psycopg.Connection,
    names: Sequence[FirmName],
    *,
    selected_idxs: set[int],
    threshold: float,
) -> list[tuple[int, int, float, float, bool]]:
    if len(names) < 2 or not selected_idxs:
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
                record_count    integer NOT NULL,
                is_selected     boolean NOT NULL
            ) ON COMMIT DROP
            """
        )
        cur.executemany(
            """
            INSERT INTO firm_resolution_work
                (idx, raw_firm, normalized_firm, token_key, primary_token, record_count, is_selected)
            VALUES (%s, %s, %s, %s, %s, %s, %s)
            """,
            [
                (
                    n.idx,
                    n.raw_firm,
                    n.normalized,
                    n.token_key,
                    n.primary_token,
                    n.record_count,
                    n.idx in selected_idxs,
                )
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
            WHERE (a.is_selected OR b.is_selected)
              AND (
                   similarity(a.normalized_firm, b.normalized_firm) >= %s
                OR word_similarity(a.normalized_firm, b.normalized_firm) >= %s
                OR (
                    a.primary_token <> ''
                    AND b.primary_token <> ''
                    AND metaphone(a.primary_token, 8) = metaphone(b.primary_token, 8)
                    AND similarity(a.normalized_firm, b.normalized_firm) >= %s
                )
              )
            ORDER BY trigram_similarity DESC, word_similarity DESC, a.idx, b.idx
            """,
            (threshold, threshold, phonetic_floor),
        )
        return [(row[0], row[1], float(row[2]), float(row[3]), bool(row[4])) for row in cur.fetchall()]


def pair_signals(conn: psycopg.Connection, left: FirmName, right: FirmName) -> tuple[float, float, bool]:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT
                similarity(%s, %s),
                word_similarity(%s, %s),
                (
                    %s <> ''
                    AND %s <> ''
                    AND metaphone(%s, 8) = metaphone(%s, 8)
                )
            """,
            (
                left.normalized,
                right.normalized,
                left.normalized,
                right.normalized,
                left.primary_token,
                right.primary_token,
                left.primary_token,
                right.primary_token,
            ),
        )
        trigram, word, phonetic = cur.fetchone()
    return float(trigram), float(word), bool(phonetic)


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
    verification_policy: str,
    auto_merge_threshold: float,
    review_threshold: float,
    token_threshold: float,
    llm_confidence_threshold: float,
    model: str,
    web_model: str,
    web_engine: str,
    web_max_results: int,
    web_concurrency: int,
    web_max_tokens: int,
) -> tuple[list[CandidatePair], list[CandidatePair], list[CandidatePair]]:
    client = OpenAI() if verification_policy == "llm" else None
    accepted: list[CandidatePair] = []
    review: list[CandidatePair] = []
    rejected: list[CandidatePair] = []

    if verification_policy == "web":
        web_verified = _verify_web_pairs(
            pairs,
            web_model=web_model,
            web_engine=web_engine,
            web_max_results=web_max_results,
            web_concurrency=web_concurrency,
            web_max_tokens=web_max_tokens,
            confidence_threshold=llm_confidence_threshold,
        )
        for pair in web_verified:
            if pair.decision == DECISION_ACCEPTED:
                accepted.append(pair)
            elif pair.decision == DECISION_REJECTED:
                rejected.append(pair)
            else:
                review.append(pair)
        return accepted, review, rejected

    for pair in pairs:
        if verification_policy != "web" and pair.score < review_threshold:
            pair.decision = DECISION_REJECTED
            pair.decision_reason = "below review threshold"
            pair.confidence = pair.score
            rejected.append(pair)
            continue

        if client is not None:
            verdict = verify_pair(client, pair, model=model)
            pair.verified_by_llm = True
            pair.verification_method = "structured_llm"
            pair.decision_reason = f"llm: {verdict.reason}"
            pair.confidence = verdict.confidence
            if verdict.same_entity and verdict.confidence >= llm_confidence_threshold:
                pair.decision = DECISION_ACCEPTED
                accepted.append(pair)
            elif not verdict.same_entity and verdict.confidence >= llm_confidence_threshold:
                pair.decision = DECISION_REJECTED
                rejected.append(pair)
            else:
                pair.decision = DECISION_NEEDS_REVIEW
                review.append(pair)
            continue

        reason = deterministic_reason(
            pair,
            auto_merge_threshold=auto_merge_threshold,
            token_threshold=token_threshold,
        )
        if reason is not None:
            pair.decision = DECISION_ACCEPTED
            pair.decision_reason = reason
            pair.confidence = max(pair.score, auto_merge_threshold)
            pair.verification_method = "deterministic"
            accepted.append(pair)
        else:
            pair.decision = DECISION_NEEDS_REVIEW
            pair.decision_reason = "below auto-merge threshold; above review threshold"
            pair.confidence = pair.score
            pair.verification_method = "deterministic_review"
            review.append(pair)

    return accepted, review, rejected


def _verify_web_pairs(
    pairs: Sequence[CandidatePair],
    *,
    web_model: str,
    web_engine: str,
    web_max_results: int,
    web_concurrency: int,
    web_max_tokens: int,
    confidence_threshold: float,
) -> list[CandidatePair]:
    if not pairs:
        return []
    total = len(pairs)
    done = 0

    def verify(pair: CandidatePair) -> CandidatePair:
        try:
            verdict = web_verify.verify_pair(
                pair.left.raw_firm,
                pair.right.raw_firm,
                model=web_model,
                engine=web_engine,
                max_results=web_max_results,
                max_tokens=web_max_tokens,
            )
        except RuntimeError as exc:
            pair.decision = DECISION_NEEDS_REVIEW
            pair.decision_reason = f"openrouter_web_error: {exc}"
            pair.confidence = pair.score
            pair.verification_method = "openrouter_web_error"
            return pair
        pair.verified_by_llm = True
        pair.verification_method = "openrouter_web"
        pair.confidence = verdict.confidence
        pair.citations = [citation.model_dump() for citation in verdict.citations]
        citation_note = f"; citations={len(verdict.citations)}"
        pair.decision_reason = f"openrouter_web:{verdict.relationship}: {verdict.reason}{citation_note}"
        if verdict.same_entity and verdict.confidence >= confidence_threshold:
            pair.decision = DECISION_ACCEPTED
        elif not verdict.same_entity and verdict.confidence >= confidence_threshold:
            pair.decision = DECISION_REJECTED
        else:
            pair.decision = DECISION_NEEDS_REVIEW
        return pair

    print(f"  web verifying {total} candidate pair(s) with concurrency={web_concurrency}", flush=True)
    verified: list[CandidatePair] = []
    with ThreadPoolExecutor(max_workers=max(1, web_concurrency)) as executor:
        futures = [executor.submit(verify, pair) for pair in pairs]
        for future in as_completed(futures):
            verified.append(future.result())
            done += 1
            if done == 1 or done % 25 == 0 or done == total:
                print(f"  web verified {done}/{total}", flush=True)
    return verified


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
        "trigram_similarity": round(edge.trigram_similarity, 5),
        "word_similarity": round(edge.word_similarity, 5),
        "token_jaccard": round(edge.token_jaccard, 5),
        "phonetic_match": edge.phonetic_match,
        "decision": edge.decision,
        "decision_reason": edge.decision_reason,
        "confidence": round(edge.confidence, 5),
        "verified_by_llm": edge.verified_by_llm,
        "verification_method": edge.verification_method,
        "citations": edge.citations,
    }


def create_run(conn: psycopg.Connection, args: argparse.Namespace, *,
               source: SourceConfig, stats: dict[str, int]) -> int:
    _require_audit_tables(conn)
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO firm_resolution_run
                (
                    mode,
                    apply_writes,
                    source_table,
                    source_field,
                    limit_rows,
                    candidate_threshold,
                    auto_merge_threshold,
                    review_threshold,
                    token_threshold,
                    verify_llm,
                    llm_model,
                    llm_confidence_threshold,
                    source_value_count,
                    selected_value_count,
                    skipped_value_count,
                    candidate_pair_count,
                    accepted_pair_count,
                    review_pair_count,
                    rejected_pair_count,
                    stats
                )
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            RETURNING id
            """,
            (
                args.mode,
                args.apply,
                source.table,
                source.field,
                args.limit,
                args.threshold,
                args.auto_merge_threshold,
                args.review_threshold,
                args.token_threshold,
                args.verification_policy != "deterministic",
                (
                    args.web_model
                    if args.verification_policy == "web"
                    else args.model if args.verification_policy == "llm"
                    else None
                ),
                args.llm_confidence_threshold,
                stats["source_value_count"],
                stats["selected_value_count"],
                stats["skipped_value_count"],
                stats["candidate_pair_count"],
                stats["accepted_pair_count"],
                stats["review_pair_count"],
                stats["rejected_pair_count"],
                Jsonb(stats),
            ),
        )
        run_id = int(cur.fetchone()[0])
    conn.commit()
    return run_id


def complete_run(
    conn: psycopg.Connection,
    run_id: int,
    *,
    status: str,
    stats: dict[str, int],
    firm_rows_touched: int = 0,
    alias_rows_touched: int = 0,
    log_rows_written: int = 0,
    error_message: str | None = None,
) -> None:
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE firm_resolution_run
               SET status = %s,
                   completed_at = now(),
                   firm_rows_touched = %s,
                   alias_rows_touched = %s,
                   log_rows_written = %s,
                   stats = %s,
                   error_message = %s
             WHERE id = %s
            """,
            (
                status,
                firm_rows_touched,
                alias_rows_touched,
                log_rows_written,
                Jsonb(stats),
                error_message,
                run_id,
            ),
        )
    conn.commit()


def write_match_pairs(
    conn: psycopg.Connection,
    *,
    run_id: int,
    source: SourceConfig,
    pairs: Sequence[CandidatePair],
) -> int:
    with conn.cursor() as cur:
        cur.executemany(
            """
            INSERT INTO firm_match_pair
                (
                    run_id,
                    source_table,
                    source_field,
                    left_raw_firm,
                    right_raw_firm,
                    left_normalized,
                    right_normalized,
                    trigram_similarity,
                    word_similarity,
                    token_jaccard,
                    phonetic_match,
                    decision,
                    decision_reason,
                    confidence,
                    verified_by_llm,
                    evidence
                )
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT DO NOTHING
            """,
            [
                (
                    run_id,
                    source.table,
                    source.field,
                    pair.left.raw_firm,
                    pair.right.raw_firm,
                    pair.left.normalized,
                    pair.right.normalized,
                    round(pair.trigram_similarity, 5),
                    round(pair.word_similarity, 5),
                    round(pair.token_jaccard, 5),
                    pair.phonetic_match,
                    pair.decision,
                    pair.decision_reason,
                    round(pair.confidence, 5),
                    pair.verified_by_llm,
                    Jsonb(_edge_evidence(pair)),
                )
                for pair in pairs
            ],
        )
        return cur.rowcount


def _touch_firm(
    cur: psycopg.Cursor,
    *,
    cluster: Cluster,
    run_id: int | None,
    source: SourceConfig,
) -> int:
    canonical = cluster.canonical
    normalized_identity_key = _firm_identity_key(cluster)
    members = [
        {
            "raw_firm": member.raw_firm,
            "record_count": member.record_count,
            "selected": member.selected,
            "selection_reason": member.selection_reason,
        }
        for member in cluster.members
    ]
    evidence = {
        "resolver": "src/firm/resolve.py",
        "run_id": run_id,
        "source_table": source.table,
        "source_field": source.field,
        "member_count": len(cluster.members),
        "selected_member_count": len(cluster.selected_members),
        "total_records": cluster.total_records,
        "members": members,
        "accepted_edges": [_edge_evidence(edge) for edge in cluster.edges],
    }
    existing_firm_id = cluster.existing_firm_id
    if existing_firm_id is not None:
        cur.execute(
            """
            UPDATE firm
               SET fda_present = true,
                   source = 'fda',
                   confidence = GREATEST(firm.confidence, %s),
                   evidence = firm.evidence || %s::jsonb,
                   updated_at = now()
             WHERE id = %s
            RETURNING id
            """,
            (cluster.confidence, Jsonb({"last_resolution_run": evidence}), existing_firm_id),
        )
        return int(cur.fetchone()[0])

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
                evidence = firm.evidence || jsonb_build_object('last_resolution_run', EXCLUDED.evidence),
                updated_at = now()
        RETURNING id
        """,
        (canonical.raw_firm, normalized_identity_key, cluster.confidence, Jsonb(evidence)),
    )
    return int(cur.fetchone()[0])


def _firm_identity_key(cluster: Cluster) -> str:
    """Stable unique key for a canonical firm; avoids merging distinct entities with same normalized text."""
    canonical = cluster.canonical
    digest = hashlib.sha1(canonical.raw_firm.casefold().encode("utf-8")).hexdigest()[:12]
    return f"{canonical.normalized}::{digest}"


def write_clusters(
    conn: psycopg.Connection,
    *,
    clusters: Sequence[Cluster],
    review_pairs: Sequence[CandidatePair],
    skipped_raw: Sequence[str],
    source: SourceConfig,
    run_id: int | None,
) -> tuple[int, int, int]:
    _require_sidecar_tables(conn)
    raw_to_firm_id: dict[str, int] = {}
    firm_count = 0
    alias_count = 0
    log_count = 0

    with conn.cursor() as cur:
        for cluster in clusters:
            if not cluster.selected_members:
                continue
            firm_id = _touch_firm(cur, cluster=cluster, run_id=run_id, source=source)
            firm_count += 1

            for member in cluster.members:
                if not member.selected:
                    continue
                alias_evidence = {
                    "resolver": "src/firm/resolve.py",
                    "run_id": run_id,
                    "canonical_name": cluster.canonical.raw_firm,
                    "cluster_size": len(cluster.members),
                    "record_count": member.record_count,
                    "selection_reason": member.selection_reason,
                    "source_table": source.table,
                    "source_field": source.field,
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
                    VALUES (%s, %s, %s, 'recalling_firm', %s, %s, %s, 'fda', %s, %s)
                    ON CONFLICT (source_table, source_field, raw_firm) DO UPDATE
                        SET normalized_raw_firm = EXCLUDED.normalized_raw_firm,
                            firm_id = EXCLUDED.firm_id,
                            record_count = EXCLUDED.record_count,
                            source = 'fda',
                            confidence = GREATEST(firm_alias.confidence, EXCLUDED.confidence),
                            evidence = firm_alias.evidence || jsonb_build_object(
                                'last_resolution_run', EXCLUDED.evidence
                            ),
                            updated_at = now()
                    """,
                    (
                        member.raw_firm,
                        member.normalized,
                        firm_id,
                        source.table,
                        source.field,
                        member.record_count,
                        cluster.confidence,
                        Jsonb(alias_evidence),
                    ),
                )
                raw_to_firm_id[member.raw_firm] = firm_id
                alias_count += 1

        for pair in review_pairs:
            candidate_ids = sorted({
                id_
                for id_ in (
                    raw_to_firm_id.get(pair.left.raw_firm) or pair.left.existing_firm_id,
                    raw_to_firm_id.get(pair.right.raw_firm) or pair.right.existing_firm_id,
                )
                if id_ is not None
            })
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
                    Jsonb({
                        **_edge_evidence(pair),
                        "run_id": run_id,
                        "source_table": source.table,
                        "source_field": source.field,
                    }),
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
                (raw, Jsonb({"resolver": "src/firm/resolve.py", "run_id": run_id})),
            )
            log_count += 1

    return firm_count, alias_count, log_count


def print_summary(
    loaded: LoadedNames,
    pairs: Sequence[CandidatePair],
    accepted_pairs: Sequence[CandidatePair],
    review_pairs: Sequence[CandidatePair],
    rejected_pairs: Sequence[CandidatePair],
    clusters: Sequence[Cluster],
    *,
    source: SourceConfig,
    mode: str,
    top: int,
) -> None:
    print(f"source            : {source.table}.{source.field}")
    print(f"mode              : {mode}")
    print(f"firms in pool     : {len(loaded.names)}")
    print(f"firms selected    : {loaded.selected_count}")
    print(f"candidate pairs   : {len(pairs)}")
    print(f"accepted pairs    : {len(accepted_pairs)}")
    print(f"review pairs      : {len(review_pairs)}")
    print(f"rejected pairs    : {len(rejected_pairs)}")
    print(f"clusters          : {len(clusters)}")

    selected_clusters = [cluster for cluster in clusters if cluster.selected_members]
    merged = [cluster for cluster in selected_clusters if len(cluster.members) > 1]
    if not merged:
        print("\nNo multi-alias clusters were accepted for selected firms.")
        return

    print(f"\nTop {min(top, len(merged))} accepted selected multi-alias cluster(s):")
    for cluster in merged[:top]:
        names_text = "; ".join(member.raw_firm for member in cluster.members[:8])
        suffix = " ..." if len(cluster.members) > 8 else ""
        print(
            f"- {cluster.canonical.raw_firm} "
            f"({len(cluster.members)} aliases, {cluster.total_records} records, "
            f"confidence={cluster.confidence:.3f}): {names_text}{suffix}"
        )


def _base_stats(
    loaded: LoadedNames,
    pairs: Sequence[CandidatePair],
    accepted_pairs: Sequence[CandidatePair],
    review_pairs: Sequence[CandidatePair],
    rejected_pairs: Sequence[CandidatePair],
) -> dict[str, int]:
    return {
        "source_value_count": len(loaded.names),
        "selected_value_count": loaded.selected_count,
        "skipped_value_count": len(loaded.skipped_selected),
        "candidate_pair_count": len(pairs),
        "accepted_pair_count": len(accepted_pairs),
        "review_pair_count": len(review_pairs),
        "rejected_pair_count": len(rejected_pairs),
    }


def _golden_pairs(path: Path) -> list[dict[str, str]]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(payload, dict):
        pairs = payload.get("pairs", [])
    else:
        pairs = payload
    if not isinstance(pairs, list):
        raise ValueError("golden file must be a list or an object with a 'pairs' list")
    out: list[dict[str, str]] = []
    for idx, item in enumerate(pairs, 1):
        if not isinstance(item, dict):
            raise ValueError(f"golden pair #{idx} is not an object")
        left = str(item.get("left", "")).strip()
        right = str(item.get("right", "")).strip()
        label = str(item.get("label", "")).strip()
        if not left or not right or label not in {"same", "different", "uncertain"}:
            raise ValueError(f"golden pair #{idx} must include left/right and label same|different|uncertain")
        out.append({"left": left, "right": right, "label": label})
    return out


def run_calibration(conn: psycopg.Connection, args: argparse.Namespace) -> int:
    path = Path(args.calibrate_golden)
    pairs_payload = _golden_pairs(path)
    pairs: list[CandidatePair] = []
    for idx, item in enumerate(pairs_payload):
        left_normalized = normalize_name(item["left"])
        right_normalized = normalize_name(item["right"])
        left = FirmName(idx=idx * 2, raw_firm=item["left"], normalized=left_normalized,
                        tokens=_tokens(left_normalized), record_count=0)
        right = FirmName(idx=idx * 2 + 1, raw_firm=item["right"], normalized=right_normalized,
                         tokens=_tokens(right_normalized), record_count=0)
        trigram, word, phonetic = pair_signals(conn, left, right)
        pair = CandidatePair(
            left=left,
            right=right,
            trigram_similarity=trigram,
            word_similarity=word,
            phonetic_match=phonetic,
            token_jaccard=_jaccard(left.tokens, right.tokens),
        )
        pairs.append(pair)

    accepted, review, rejected = classify_pairs(
        pairs,
        verification_policy="deterministic",
        auto_merge_threshold=args.auto_merge_threshold,
        review_threshold=args.review_threshold,
        token_threshold=args.token_threshold,
        llm_confidence_threshold=args.llm_confidence_threshold,
        model=args.model,
        web_model=args.web_model,
        web_engine=args.web_engine,
        web_max_results=args.web_max_results,
        web_concurrency=args.web_concurrency,
        web_max_tokens=args.web_max_tokens,
    )
    by_key = {pair.key: pair for pair in pairs}
    tp = fp = fn = tn = uncertain = 0
    print(f"golden file       : {path}")
    print(f"golden pairs      : {len(pairs_payload)}")
    print(f"accepted/review/rejected: {len(accepted)}/{len(review)}/{len(rejected)}\n")
    for idx, item in enumerate(pairs_payload):
        pair = by_key[(idx * 2, idx * 2 + 1)]
        predicted_same = pair.decision == DECISION_ACCEPTED
        label = item["label"]
        if label == "uncertain":
            uncertain += 1
        elif label == "same" and predicted_same:
            tp += 1
        elif label == "same" and not predicted_same:
            fn += 1
        elif label == "different" and predicted_same:
            fp += 1
        elif label == "different" and not predicted_same:
            tn += 1
        print(
            f"- [{label:9}] {pair.decision:12} score={pair.score:.3f} "
            f"token={pair.token_jaccard:.3f} :: {pair.left.raw_firm} <-> {pair.right.raw_firm}"
        )
    precision = tp / (tp + fp) if tp + fp else 0.0
    recall = tp / (tp + fn) if tp + fn else 0.0
    print("\nCalibration summary (uncertain pairs excluded from metrics):")
    print(f"  TP={tp} FP={fp} FN={fn} TN={tn} uncertain={uncertain}")
    print(f"  precision={precision:.3f} recall={recall:.3f}")
    return 0


def run(args: argparse.Namespace) -> int:
    if args.verify_llm:
        args.verification_policy = "llm"
    if args.verification_policy == "llm" and not os.environ.get("OPENAI_API_KEY"):
        raise RuntimeError("--verify-llm requires OPENAI_API_KEY")
    if args.verification_policy == "web" and not os.environ.get("OPENROUTER_API_KEY"):
        raise RuntimeError("--verification-policy web requires OPENROUTER_API_KEY")

    source = SourceConfig(table=args.source_table, field=args.source_field)
    with psycopg.connect(args.db) as conn:
        _require_extensions(conn)
        if args.calibrate_golden:
            return run_calibration(conn, args)

        _require_sidecar_tables(conn)
        loaded = load_firm_names(conn, source=source, mode=args.mode, limit=args.limit)
        rows = pg_trgm_pairs(conn, loaded.names, selected_idxs=loaded.selected_idxs,
                             threshold=args.threshold)
        pairs = build_candidate_pairs(loaded.names, rows)
        accepted_pairs, review_pairs, rejected_pairs = classify_pairs(
            pairs,
            verification_policy=args.verification_policy,
            auto_merge_threshold=args.auto_merge_threshold,
            review_threshold=args.review_threshold,
            token_threshold=args.token_threshold,
            llm_confidence_threshold=args.llm_confidence_threshold,
            model=args.model,
            web_model=args.web_model,
            web_engine=args.web_engine,
            web_max_results=args.web_max_results,
            web_concurrency=args.web_concurrency,
            web_max_tokens=args.web_max_tokens,
        )
        clusters = build_clusters(loaded.names, accepted_pairs) if loaded.selected_idxs else []

        print_summary(
            loaded,
            pairs,
            accepted_pairs,
            review_pairs,
            rejected_pairs,
            clusters,
            source=source,
            mode=args.mode,
            top=args.show_clusters,
        )
        if loaded.skipped_selected:
            print(f"\nskipped after normalization: {len(loaded.skipped_selected)}")

        if not args.apply:
            print("\nDry run only. Re-run with --apply to write firm, firm_alias, audit, and resolution_log rows.")
            return 0

        stats = _base_stats(loaded, pairs, accepted_pairs, review_pairs, rejected_pairs)
        run_id: int | None = None
        try:
            run_id = create_run(conn, args, source=source, stats=stats)
            write_match_pairs(conn, run_id=run_id, source=source, pairs=pairs)
            firm_count, alias_count, log_count = write_clusters(
                conn,
                clusters=clusters,
                review_pairs=review_pairs,
                skipped_raw=loaded.skipped_selected,
                source=source,
                run_id=run_id,
            )
            final_stats = {
                **stats,
                "firm_rows_touched": firm_count,
                "alias_rows_touched": alias_count,
                "log_rows_written": log_count,
                "run_id": run_id,
            }
            complete_run(
                conn,
                run_id,
                status="succeeded",
                stats=final_stats,
                firm_rows_touched=firm_count,
                alias_rows_touched=alias_count,
                log_rows_written=log_count,
            )
        except Exception as exc:
            if run_id is not None:
                conn.rollback()
                complete_run(conn, run_id, status="failed", stats=stats, error_message=str(exc))
            raise

        print(
            "\nApplied sidecar writes: "
            f"run_id={run_id}, {firm_count} firm row(s), {alias_count} firm_alias row(s), "
            f"{log_count} resolution_log row(s), {len(pairs)} firm_match_pair row(s)."
        )
        return 0


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Incremental offline firm/entity resolver for FDA source firm fields."
    )
    p.add_argument("--db", default=DEFAULT_DSN,
                   help="Postgres DSN (default: $DATABASE_URL or postgresql://localhost:5432/fda)")
    p.add_argument("--mode", choices=MODES, default="incremental",
                   help="full processes every source value; incremental selects new/changed/retry values")
    p.add_argument("--source-table", default="drug_enforcement",
                   help="source table containing raw firm strings")
    p.add_argument("--source-field", default="recalling_firm",
                   help="source field containing raw firm strings")
    p.add_argument("--limit", type=int, default=None,
                   help="limit selected source values for a test run")
    p.add_argument("--threshold", type=float, default=0.86,
                   help="pg_trgm candidate threshold before conservative merge checks")
    p.add_argument("--auto-merge-threshold", type=float, default=0.86,
                   help="minimum trigram/word similarity for deterministic alias merges")
    p.add_argument("--review-threshold", type=float, default=0.72,
                   help="minimum score for deterministic/llm non-auto-merged pairs to enter review; ignored by web policy")
    p.add_argument("--token-threshold", type=float, default=0.80,
                   help="minimum token Jaccard overlap for deterministic alias merges")
    p.add_argument("--verification-policy", choices=VERIFICATION_POLICIES, default="web",
                   help="web uses OpenRouter web search for auto-merge decisions; deterministic is test-only")
    p.add_argument("--verify-llm", action="store_true",
                   help="back-compat alias for --verification-policy llm")
    p.add_argument("--llm-confidence-threshold", type=float, default=0.90,
                   help="minimum AI confidence required to accept/reject a pair")
    p.add_argument("--model", default=MODEL,
                   help=f"OpenAI model for --verification-policy llm (default: {MODEL})")
    p.add_argument("--web-model", default=WEB_MODEL,
                   help=f"OpenRouter web model for --verification-policy web (default: {WEB_MODEL})")
    p.add_argument("--web-engine", default="exa",
                   help="OpenRouter web plugin engine (default: exa)")
    p.add_argument("--web-max-results", type=int, default=5,
                   help="max OpenRouter web results per candidate pair")
    p.add_argument("--web-concurrency", type=int, default=4,
                   help="parallel OpenRouter web verification requests")
    p.add_argument("--web-max-tokens", type=int, default=800,
                   help="max output tokens for each OpenRouter web verification request")
    p.add_argument("--calibrate-golden", default=None,
                   help="evaluate thresholds against a firm-pair golden JSON file; no writes")
    p.add_argument("--show-clusters", type=int, default=10,
                   help="number of accepted multi-alias clusters to print")
    p.add_argument("--apply", action="store_true",
                   help="write sidecar firm/alias/audit/log rows; default is report-only")
    return p.parse_args()


if __name__ == "__main__":
    raise SystemExit(run(parse_args()))
