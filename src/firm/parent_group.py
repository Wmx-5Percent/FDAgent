#!/usr/bin/env python3
"""Maintain auditable firm→parent_group edges for normalized exposure rollups.

This CLI is sidecar-only and dry-run by default. It applies reviewer-controlled
parent edges from a local JSON/JSONL seed file (for example under git-ignored
``data/real/``), and can create explicit ``unknown`` self-parent placeholders for
high-recall firms so missing parent identity stays visible without affecting
provenance-backed parent exposure totals.

Example seed entry::

    {
      "parent_group": "Example Parent",
      "firm_names": ["Example Subsidiary Inc."],
      "provenance_tier": "inferred_external_or_llm",
      "source": "manual",
      "source_name": "human-confirmed seed",
      "source_url": "https://www.wikidata.org/wiki/Q123",
      "source_id": "Q123",
      "as_of_date": "2026-07-15",
      "confidence": 0.95,
      "evidence": {"reviewer": "initial seed"}
    }
"""
from __future__ import annotations

import argparse
import json
import os
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any, Iterable

import psycopg
from dotenv import load_dotenv
from psycopg.types.json import Jsonb

try:  # package import
    from .resolve import normalize_name
except ImportError:  # script execution: python src/firm/parent_group.py
    from resolve import normalize_name  # type: ignore

load_dotenv()
DEFAULT_DSN = os.environ.get("DATABASE_URL", "postgresql://localhost:5432/fda")
PROVENANCE_TIERS = {"fda_fact", "inferred_external_or_llm", "unknown"}
SOURCES = {"fda", "external", "llm", "manual", "unknown"}


@dataclass(frozen=True)
class FirmMatch:
    firm_id: int
    canonical_name: str
    normalized_name: str


@dataclass(frozen=True)
class EdgeSeed:
    parent_group: str
    firm_names: list[str]
    provenance_tier: str = "inferred_external_or_llm"
    source: str = "manual"
    source_name: str | None = None
    source_url: str | None = None
    source_id: str | None = None
    as_of_date: date = field(default_factory=date.today)
    confidence: float = 0.9
    evidence: dict[str, Any] = field(default_factory=dict)

    @property
    def parent_normalized(self) -> str:
        return normalize_name(self.parent_group)


@dataclass
class ApplyStats:
    seed_edges_seen: int = 0
    seed_edges_written: int = 0
    unknown_edges_seen: int = 0
    unknown_edges_written: int = 0
    unresolved_firms: int = 0
    ambiguous_firms: int = 0


def _require_tables(conn: psycopg.Connection) -> None:
    expected = ("parent_group", "firm", "firm_alias", "firm_parent_group_edge")
    missing: list[str] = []
    with conn.cursor() as cur:
        for table in expected:
            cur.execute("SELECT to_regclass(%s)", (table,))
            if cur.fetchone()[0] is None:
                missing.append(table)
    if missing:
        raise RuntimeError(
            "Missing table(s): "
            + ", ".join(missing)
            + ". Run sql/008_firm_resolution.sql and sql/011_parent_group_rollup.sql first."
        )


def _coerce_date(value: Any) -> date:
    if value in (None, ""):
        return date.today()
    if isinstance(value, date):
        return value
    return date.fromisoformat(str(value))


def _coerce_seed(item: dict[str, Any], *, index: int) -> EdgeSeed:
    parent = str(item.get("parent_group") or item.get("parent") or "").strip()
    firm_names = item.get("firm_names", item.get("firms", []))
    if isinstance(firm_names, str):
        firm_names = [firm_names]
    firm_list = [str(name).strip() for name in firm_names if str(name).strip()]
    provenance_tier = str(item.get("provenance_tier", "inferred_external_or_llm")).strip()
    source = str(item.get("source", "manual")).strip()
    confidence = float(item.get("confidence", 0.9))
    if not parent or not firm_list:
        raise ValueError(f"seed #{index} needs parent_group and at least one firm name")
    if provenance_tier not in PROVENANCE_TIERS:
        raise ValueError(f"seed #{index} has invalid provenance_tier {provenance_tier!r}")
    if source not in SOURCES:
        raise ValueError(f"seed #{index} has invalid source {source!r}")
    if provenance_tier == "unknown" and source != "unknown":
        raise ValueError(f"seed #{index}: unknown provenance must use source='unknown'")
    if provenance_tier != "unknown" and source == "llm":
        raise ValueError(f"seed #{index}: LLM-only parent edges cannot be confirmed rollup edges")
    source_name = item.get("source_name")
    source_url = item.get("source_url")
    source_id = item.get("source_id")
    if provenance_tier != "unknown" and (not source_name or not (source_url or source_id)):
        raise ValueError(
            f"seed #{index}: confirmed parent edges need source_name and source_url or source_id"
        )
    if not 0 <= confidence <= 1:
        raise ValueError(f"seed #{index} confidence must be in [0,1]")
    evidence = item.get("evidence") or {}
    if not isinstance(evidence, dict):
        raise ValueError(f"seed #{index} evidence must be an object")
    return EdgeSeed(
        parent_group=parent,
        firm_names=firm_list,
        provenance_tier=provenance_tier,
        source=source,
        source_name=source_name,
        source_url=source_url,
        source_id=source_id,
        as_of_date=_coerce_date(item.get("as_of_date")),
        confidence=confidence,
        evidence=evidence,
    )


def load_seed_file(path: Path) -> list[EdgeSeed]:
    text = path.read_text(encoding="utf-8").strip()
    if not text:
        return []
    if path.suffix.lower() == ".jsonl":
        payload = [json.loads(line) for line in text.splitlines() if line.strip()]
    else:
        payload = json.loads(text)
        if isinstance(payload, dict):
            payload = payload.get("edges", [])
    if not isinstance(payload, list):
        raise ValueError("seed file must be a JSON list, JSONL lines, or an object with an 'edges' list")
    return [_coerce_seed(item, index=i) for i, item in enumerate(payload, 1)]


def _resolve_firm(conn: psycopg.Connection, name: str) -> list[FirmMatch]:
    normalized = normalize_name(name)
    if not normalized:
        return []
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT DISTINCT f.id, f.canonical_name, f.normalized_name
            FROM firm f
            LEFT JOIN firm_alias fa ON fa.firm_id = f.id
            WHERE f.normalized_name = %s
               OR fa.normalized_raw_firm = %s
               OR lower(f.canonical_name) = lower(%s)
               OR lower(fa.raw_firm) = lower(%s)
            ORDER BY f.canonical_name
            LIMIT 5
            """,
            (normalized, normalized, name, name),
        )
        return [FirmMatch(int(row[0]), row[1], row[2]) for row in cur.fetchall()]


def _upsert_parent_group(cur: psycopg.Cursor, seed: EdgeSeed) -> int:
    cur.execute(
        """
        INSERT INTO parent_group
            (canonical_name, normalized_name, source, confidence, evidence)
        VALUES (%s, %s, %s, %s, %s)
        ON CONFLICT (normalized_name) DO UPDATE
            SET canonical_name = EXCLUDED.canonical_name,
                source = CASE
                    WHEN parent_group.source = 'unknown' THEN EXCLUDED.source
                    ELSE parent_group.source
                END,
                confidence = GREATEST(parent_group.confidence, EXCLUDED.confidence),
                evidence = parent_group.evidence || jsonb_build_object('last_parent_seed', EXCLUDED.evidence),
                updated_at = now()
        RETURNING id
        """,
        (
            seed.parent_group,
            seed.parent_normalized,
            seed.source,
            seed.confidence,
            Jsonb({**seed.evidence, "source_name": seed.source_name, "source_id": seed.source_id}),
        ),
    )
    return int(cur.fetchone()[0])


def _write_edge(cur: psycopg.Cursor, *, firm: FirmMatch, parent_group_id: int, seed: EdgeSeed) -> int:
    cur.execute(
        """
        UPDATE firm_parent_group_edge
           SET active = false,
               review_status = 'superseded',
               updated_at = now(),
               evidence = evidence || jsonb_build_object('superseded_by', %s::jsonb)
         WHERE firm_id = %s
           AND active
           AND review_status = 'confirmed'
           AND NOT (
               parent_group_id = %s
               AND provenance_tier = %s
               AND source = %s
               AND COALESCE(source_id, '') = COALESCE(%s, '')
               AND as_of_date = %s
           )
        """,
        (Jsonb({"parent_group": seed.parent_group, "as_of_date": seed.as_of_date.isoformat()}),
         firm.firm_id, parent_group_id, seed.provenance_tier, seed.source, seed.source_id, seed.as_of_date),
    )
    cur.execute(
        """
        UPDATE firm_parent_group_edge
           SET provenance_tier = %s,
               source = %s,
               source_name = %s,
               source_url = %s,
               source_id = %s,
               as_of_date = %s,
               confidence = GREATEST(confidence, %s),
               evidence = evidence || jsonb_build_object('last_parent_seed', %s::jsonb),
               updated_at = now()
         WHERE firm_id = %s
           AND parent_group_id = %s
           AND active
           AND review_status = 'confirmed'
        RETURNING id
        """,
        (
            seed.provenance_tier,
            seed.source,
            seed.source_name,
            seed.source_url,
            seed.source_id,
            seed.as_of_date,
            seed.confidence,
            Jsonb(seed.evidence),
            firm.firm_id,
            parent_group_id,
        ),
    )
    row = cur.fetchone()
    if row is not None:
        edge_id = int(row[0])
        cur.execute(
            "UPDATE firm SET parent_group_id = %s, updated_at = now() WHERE id = %s",
            (parent_group_id, firm.firm_id),
        )
        return edge_id
    cur.execute(
        """
        INSERT INTO firm_parent_group_edge
            (
                firm_id,
                parent_group_id,
                provenance_tier,
                source,
                source_name,
                source_url,
                source_id,
                as_of_date,
                review_status,
                active,
                confidence,
                evidence
            )
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, 'confirmed', true, %s, %s)
        ON CONFLICT DO NOTHING
        RETURNING id
        """,
        (
            firm.firm_id,
            parent_group_id,
            seed.provenance_tier,
            seed.source,
            seed.source_name,
            seed.source_url,
            seed.source_id,
            seed.as_of_date,
            seed.confidence,
            Jsonb({
                **seed.evidence,
                "firm_name_input": firm.canonical_name,
                "parent_group_input": seed.parent_group,
            }),
        ),
    )
    row = cur.fetchone()
    if row is None:
        cur.execute(
            """
            SELECT id
            FROM firm_parent_group_edge
            WHERE firm_id = %s
              AND parent_group_id = %s
              AND provenance_tier = %s
              AND source = %s
              AND COALESCE(source_id, '') = COALESCE(%s, '')
              AND as_of_date = %s
            ORDER BY id DESC
            LIMIT 1
            """,
            (
                firm.firm_id,
                parent_group_id,
                seed.provenance_tier,
                seed.source,
                seed.source_id,
                seed.as_of_date,
            ),
        )
        edge_id = int(cur.fetchone()[0])
        cur.execute(
            """
            UPDATE firm_parent_group_edge
               SET active = true,
                   review_status = 'confirmed',
                   confidence = GREATEST(confidence, %s),
                   evidence = evidence || jsonb_build_object('last_parent_seed', %s::jsonb),
                   updated_at = now()
             WHERE id = %s
            """,
            (seed.confidence, Jsonb(seed.evidence), edge_id),
        )
    else:
        edge_id = int(row[0])
    cur.execute(
        "UPDATE firm SET parent_group_id = %s, updated_at = now() WHERE id = %s",
        (parent_group_id, firm.firm_id),
    )
    return edge_id


def _iter_seed_firms(seeds: Iterable[EdgeSeed]) -> Iterable[tuple[EdgeSeed, str]]:
    for seed in seeds:
        for name in seed.firm_names:
            yield seed, name


def apply_seeds(conn: psycopg.Connection, seeds: list[EdgeSeed], *, apply: bool) -> ApplyStats:
    stats = ApplyStats()
    for seed, firm_name in _iter_seed_firms(seeds):
        stats.seed_edges_seen += 1
        matches = _resolve_firm(conn, firm_name)
        if not matches:
            stats.unresolved_firms += 1
            print(f"UNRESOLVED seed firm: {firm_name!r} -> parent {seed.parent_group!r}")
            continue
        if len(matches) > 1:
            stats.ambiguous_firms += 1
            names = ", ".join(f"{m.firm_id}:{m.canonical_name}" for m in matches)
            print(f"AMBIGUOUS seed firm: {firm_name!r} matched {names}")
            continue
        firm = matches[0]
        print(
            f"{'APPLY' if apply else 'DRY'} seed edge: "
            f"firm_id={firm.firm_id} {firm.canonical_name!r} -> {seed.parent_group!r} "
            f"({seed.provenance_tier}, {seed.source}, as_of={seed.as_of_date})"
        )
        if apply:
            with conn.cursor() as cur:
                parent_id = _upsert_parent_group(cur, seed)
                _write_edge(cur, firm=firm, parent_group_id=parent_id, seed=seed)
            conn.commit()
            stats.seed_edges_written += 1
    return stats


def _top_unmapped_firms(conn: psycopg.Connection, limit: int) -> list[tuple[FirmMatch, int]]:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT f.id, f.canonical_name, f.normalized_name, COALESCE(sum(fa.record_count), 0)::int AS n
            FROM firm f
            JOIN firm_alias fa ON fa.firm_id = f.id
            LEFT JOIN firm_parent_group_edge edge
              ON edge.firm_id = f.id
             AND edge.active
             AND edge.review_status = 'confirmed'
            WHERE f.fda_present
              AND edge.id IS NULL
            GROUP BY f.id, f.canonical_name, f.normalized_name
            ORDER BY n DESC, f.canonical_name
            LIMIT %s
            """,
            (limit,),
        )
        return [(FirmMatch(int(row[0]), row[1], row[2]), int(row[3])) for row in cur.fetchall()]


def apply_unknown_self_parents(conn: psycopg.Connection, *, limit: int, apply: bool) -> ApplyStats:
    stats = ApplyStats()
    for firm, record_count in _top_unmapped_firms(conn, limit):
        stats.unknown_edges_seen += 1
        seed = EdgeSeed(
            parent_group=firm.canonical_name,
            firm_names=[firm.canonical_name],
            provenance_tier="unknown",
            source="unknown",
            source_name="unresolved parent identity",
            as_of_date=date.today(),
            confidence=0.0,
            evidence={
                "basis": (
                    "No confirmed parent edge is available. This self-parent placeholder "
                    "keeps the firm visible as unknown and is excluded from provenance-backed rollups."
                ),
                "record_count": record_count,
            },
        )
        print(
            f"{'APPLY' if apply else 'DRY'} unknown self-parent: "
            f"firm_id={firm.firm_id} {firm.canonical_name!r} ({record_count} recalls)"
        )
        if apply:
            with conn.cursor() as cur:
                parent_id = _upsert_parent_group(cur, seed)
                _write_edge(cur, firm=firm, parent_group_id=parent_id, seed=seed)
            conn.commit()
            stats.unknown_edges_written += 1
    return stats


def run(args: argparse.Namespace) -> int:
    seeds = load_seed_file(Path(args.seed_file)) if args.seed_file else []
    with psycopg.connect(args.db) as conn:
        _require_tables(conn)
        stats = ApplyStats()
        if seeds:
            seed_stats = apply_seeds(conn, seeds, apply=args.apply)
            stats.seed_edges_seen += seed_stats.seed_edges_seen
            stats.seed_edges_written += seed_stats.seed_edges_written
            stats.unresolved_firms += seed_stats.unresolved_firms
            stats.ambiguous_firms += seed_stats.ambiguous_firms
        if args.self_parent_top_n:
            unknown_stats = apply_unknown_self_parents(
                conn,
                limit=args.self_parent_top_n,
                apply=args.apply,
            )
            stats.unknown_edges_seen += unknown_stats.unknown_edges_seen
            stats.unknown_edges_written += unknown_stats.unknown_edges_written
        print("\nSummary:")
        print(f"  seed edges seen/written      : {stats.seed_edges_seen}/{stats.seed_edges_written}")
        print(f"  unknown edges seen/written   : {stats.unknown_edges_seen}/{stats.unknown_edges_written}")
        print(f"  unresolved seed firm inputs  : {stats.unresolved_firms}")
        print(f"  ambiguous seed firm inputs   : {stats.ambiguous_firms}")
        if not args.apply:
            print("  dry run only; re-run with --apply to write parent_group and edge rows")
    return 0


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Apply audited firm→parent_group edges for parent exposure rollups."
    )
    p.add_argument("--db", default=DEFAULT_DSN,
                   help="Postgres DSN (default: $DATABASE_URL or postgresql://localhost:5432/fda)")
    p.add_argument("--seed-file", default=None,
                   help="local JSON/JSONL file of reviewer-confirmed parent edges")
    p.add_argument("--self-parent-top-n", type=int, default=0,
                   help="create unknown self-parent placeholders for top N currently unmapped firms")
    p.add_argument("--apply", action="store_true",
                   help="write sidecar rows; default is dry-run only")
    return p.parse_args()


if __name__ == "__main__":
    raise SystemExit(run(parse_args()))
