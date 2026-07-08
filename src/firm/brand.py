#!/usr/bin/env python3
"""Resolve brand/product names to firm or parent candidates with provenance tiers.

The resolver is conservative and sidecar-only:

    .venv/bin/python src/firm/brand.py "example brand"
    .venv/bin/python src/firm/brand.py "example brand" --infer-llm
    .venv/bin/python src/firm/brand.py "example brand" --apply

FDA-local evidence is reported as ``fda_fact``. Optional LLM output is reported as
``inferred_external_or_llm`` and is only written when it can link to an existing
sidecar firm/parent row. Unknowns and ambiguous candidates go to ``resolution_log``.
"""
from __future__ import annotations

import argparse
import os
from dataclasses import dataclass
from typing import Any

import psycopg
from dotenv import load_dotenv
from openai import OpenAI
from psycopg.types.json import Jsonb
from pydantic import BaseModel, Field

try:  # package import
    from .resolve import normalize_name
except ImportError:  # script execution: python src/firm/brand.py
    from resolve import normalize_name  # type: ignore

load_dotenv()
DEFAULT_DSN = os.environ.get("DATABASE_URL", "postgresql://localhost:5432/fda")
MODEL = os.environ.get("OPENAI_MODEL", "gpt-4o-mini")

FDA_FACT = "fda_fact"
INFERRED = "inferred_external_or_llm"
UNKNOWN = "unknown"


@dataclass
class BrandCandidate:
    brand_name: str
    normalized_brand_name: str
    provenance_tier: str
    source: str
    confidence: float
    firm_id: int | None = None
    firm_name: str | None = None
    parent_group_id: int | None = None
    parent_group_name: str | None = None
    evidence: dict[str, Any] | None = None

    @property
    def writable(self) -> bool:
        return (
            self.provenance_tier != UNKNOWN
            and (self.firm_id is not None or self.parent_group_id is not None)
        )


class BrandInference(BaseModel):
    known: bool = Field(description="False when the model is not highly confident.")
    firm_name: str | None = Field(default=None, description="Likely firm/legal owner if known.")
    parent_group_name: str | None = Field(default=None, description="Likely parent group if known.")
    confidence: float = Field(ge=0, le=1, description="Confidence in the inferred mapping.")
    reason: str = Field(description="Short reason; do not cite unavailable sources.")


def _require_sidecar_tables(conn: psycopg.Connection) -> None:
    expected = ("brand_alias", "firm", "firm_alias", "parent_group", "resolution_log")
    missing: list[str] = []
    with conn.cursor() as cur:
        for table in expected:
            cur.execute("SELECT to_regclass(%s)", (table,))
            if cur.fetchone()[0] is None:
                missing.append(table)
    if missing:
        raise RuntimeError(
            "Missing sidecar table(s): "
            + ", ".join(missing)
            + ". Run sql/008_firm_resolution.sql before executing brand resolution."
        )


def _existing_brand_aliases(
    conn: psycopg.Connection,
    *,
    brand_name: str,
    normalized_brand: str,
    limit: int,
) -> list[BrandCandidate]:
    q = """
        SELECT
            ba.provenance_tier,
            ba.source,
            ba.confidence,
            ba.evidence,
            f.id,
            f.canonical_name,
            pg.id,
            pg.canonical_name
        FROM brand_alias ba
        LEFT JOIN firm f ON f.id = ba.firm_id
        LEFT JOIN parent_group pg ON pg.id = ba.parent_group_id
        WHERE ba.normalized_brand_name = %s
        ORDER BY
            (ba.provenance_tier = 'fda_fact') DESC,
            ba.confidence DESC,
            ba.id
        LIMIT %s
    """
    with conn.cursor() as cur:
        cur.execute(q, (normalized_brand, limit))
        rows = cur.fetchall()
    return [
        BrandCandidate(
            brand_name=brand_name,
            normalized_brand_name=normalized_brand,
            provenance_tier=row[0],
            source=row[1],
            confidence=float(row[2]),
            evidence=row[3],
            firm_id=row[4],
            firm_name=row[5],
            parent_group_id=row[6],
            parent_group_name=row[7],
        )
        for row in rows
    ]


def _fda_product_candidates(
    conn: psycopg.Connection,
    *,
    brand_name: str,
    normalized_brand: str,
    limit: int,
) -> list[BrandCandidate]:
    q = """
        SELECT
            fa.firm_id,
            f.canonical_name AS firm_name,
            f.parent_group_id,
            pg.canonical_name AS parent_group_name,
            d.recalling_firm,
            count(*)::int AS record_count,
            (array_agg(d.recall_number ORDER BY d.report_date DESC NULLS LAST, d.recall_number))[1:5]
                AS recall_numbers
        FROM drug_enforcement d
        LEFT JOIN firm_alias fa
          ON fa.source_table = 'drug_enforcement'
         AND fa.source_field = 'recalling_firm'
         AND fa.raw_firm = d.recalling_firm
        LEFT JOIN firm f ON f.id = fa.firm_id
        LEFT JOIN parent_group pg ON pg.id = f.parent_group_id
        WHERE d.product_description ILIKE %s
        GROUP BY
            fa.firm_id,
            f.canonical_name,
            f.parent_group_id,
            pg.canonical_name,
            d.recalling_firm
        ORDER BY record_count DESC, d.recalling_firm
        LIMIT %s
    """
    with conn.cursor() as cur:
        cur.execute(q, (f"%{brand_name}%", limit))
        rows = cur.fetchall()

    candidates: list[BrandCandidate] = []
    for row in rows:
        firm_id, firm_name, parent_id, parent_name, raw_firm, record_count, recall_numbers = row
        confidence = 0.85 if firm_id is not None else 0.65
        candidates.append(
            BrandCandidate(
                brand_name=brand_name,
                normalized_brand_name=normalized_brand,
                provenance_tier=FDA_FACT,
                source="fda",
                confidence=confidence,
                firm_id=firm_id,
                firm_name=firm_name or raw_firm,
                parent_group_id=parent_id,
                parent_group_name=parent_name,
                evidence={
                    "basis": (
                        "FDA drug_enforcement.product_description contains the brand/product "
                        "string; recalling_firm is the FDA source field. This is evidence of "
                        "FDA-record co-occurrence, not a broad safety or ownership claim."
                    ),
                    "raw_recalling_firm": raw_firm,
                    "record_count": record_count,
                    "recall_numbers": recall_numbers or [],
                },
            )
        )
    return candidates


def _match_existing_firm_or_parent(
    conn: psycopg.Connection,
    *,
    name: str,
) -> tuple[int | None, str | None, int | None, str | None, float]:
    normalized = normalize_name(name)
    if not normalized:
        return None, None, None, None, 0.0
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT
                f.id,
                f.canonical_name,
                pg.id,
                pg.canonical_name,
                GREATEST(
                    similarity(f.normalized_name, %s),
                    CASE WHEN f.normalized_name = %s THEN 1 ELSE 0 END
                ) AS match_score
            FROM firm f
            LEFT JOIN parent_group pg ON pg.id = f.parent_group_id
            WHERE f.normalized_name = %s
               OR similarity(f.normalized_name, %s) >= 0.86
            ORDER BY match_score DESC, f.confidence DESC, f.canonical_name
            LIMIT 1
            """,
            (normalized, normalized, normalized, normalized),
        )
        row = cur.fetchone()
        if row is not None:
            return row[0], row[1], row[2], row[3], float(row[4])

        cur.execute(
            """
            SELECT
                pg.id,
                pg.canonical_name,
                GREATEST(
                    similarity(pg.normalized_name, %s),
                    CASE WHEN pg.normalized_name = %s THEN 1 ELSE 0 END
                ) AS match_score
            FROM parent_group pg
            WHERE pg.normalized_name = %s
               OR similarity(pg.normalized_name, %s) >= 0.86
            ORDER BY match_score DESC, pg.confidence DESC, pg.canonical_name
            LIMIT 1
            """,
            (normalized, normalized, normalized, normalized),
        )
        parent_row = cur.fetchone()
        if parent_row is not None:
            return None, None, parent_row[0], parent_row[1], float(parent_row[2])
    return None, None, None, None, 0.0


def _llm_inference(
    conn: psycopg.Connection,
    *,
    brand_name: str,
    normalized_brand: str,
    model: str,
) -> BrandCandidate:
    client = OpenAI()
    parse = getattr(client.chat.completions, "parse", None) or client.beta.chat.completions.parse
    completion = parse(
        model=model,
        temperature=0,
        messages=[
            {
                "role": "system",
                "content": (
                    "Infer a likely company for a drug brand/product name only if highly "
                    "confident from general public knowledge. This is not an FDA fact. "
                    "If unsure, return known=false. Do not invent citations."
                ),
            },
            {"role": "user", "content": f"Brand/product name: {brand_name}"},
        ],
        response_format=BrandInference,
    )
    inferred = completion.choices[0].message.parsed
    candidate_name = inferred.firm_name or inferred.parent_group_name or ""
    firm_id, firm_name, parent_id, parent_name, match_score = (
        _match_existing_firm_or_parent(conn, name=candidate_name) if inferred.known and candidate_name else
        (None, None, None, None, 0.0)
    )
    return BrandCandidate(
        brand_name=brand_name,
        normalized_brand_name=normalized_brand,
        provenance_tier=INFERRED if inferred.known else UNKNOWN,
        source="llm" if inferred.known else "unknown",
        confidence=min(inferred.confidence, match_score) if firm_id or parent_id else inferred.confidence,
        firm_id=firm_id,
        firm_name=firm_name or inferred.firm_name,
        parent_group_id=parent_id,
        parent_group_name=parent_name or inferred.parent_group_name,
        evidence={
            "basis": "optional structured LLM inference; not an FDA fact",
            "llm_known": inferred.known,
            "llm_reason": inferred.reason,
            "llm_firm_name": inferred.firm_name,
            "llm_parent_group_name": inferred.parent_group_name,
            "matched_existing_sidecar_score": match_score,
        },
    )


def collect_candidates(
    conn: psycopg.Connection,
    *,
    brand_name: str,
    limit: int,
    infer_llm: bool,
    model: str,
) -> list[BrandCandidate]:
    normalized_brand = normalize_name(brand_name)
    if not normalized_brand:
        return [
            BrandCandidate(
                brand_name=brand_name,
                normalized_brand_name="",
                provenance_tier=UNKNOWN,
                source="unknown",
                confidence=0.0,
                evidence={"reason": "empty brand/product name after token normalization"},
            )
        ]

    candidates = _existing_brand_aliases(
        conn,
        brand_name=brand_name,
        normalized_brand=normalized_brand,
        limit=limit,
    )
    candidates.extend(
        _fda_product_candidates(
            conn,
            brand_name=brand_name,
            normalized_brand=normalized_brand,
            limit=limit,
        )
    )
    if infer_llm:
        candidates.append(
            _llm_inference(conn, brand_name=brand_name, normalized_brand=normalized_brand, model=model)
        )
    if candidates:
        return candidates
    return [
        BrandCandidate(
            brand_name=brand_name,
            normalized_brand_name=normalized_brand,
            provenance_tier=UNKNOWN,
            source="unknown",
            confidence=0.0,
            evidence={"reason": "no FDA-local or sidecar match found"},
        )
    ]


def _upsert_brand_alias(cur: psycopg.Cursor, candidate: BrandCandidate) -> None:
    cur.execute(
        """
        UPDATE brand_alias
           SET brand_name = %s,
               confidence = GREATEST(brand_alias.confidence, %s),
               evidence = %s,
               updated_at = now()
         WHERE normalized_brand_name = %s
           AND provenance_tier = %s
           AND source = %s
           AND COALESCE(firm_id, 0::bigint) = COALESCE(%s::bigint, 0::bigint)
           AND COALESCE(parent_group_id, 0::bigint) = COALESCE(%s::bigint, 0::bigint)
        """,
        (
            candidate.brand_name,
            candidate.confidence,
            Jsonb(candidate.evidence or {}),
            candidate.normalized_brand_name,
            candidate.provenance_tier,
            candidate.source,
            candidate.firm_id,
            candidate.parent_group_id,
        ),
    )
    if cur.rowcount:
        return
    cur.execute(
        """
        INSERT INTO brand_alias
            (
                brand_name,
                normalized_brand_name,
                firm_id,
                parent_group_id,
                provenance_tier,
                source,
                confidence,
                evidence
            )
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
        """,
        (
            candidate.brand_name,
            candidate.normalized_brand_name,
            candidate.firm_id,
            candidate.parent_group_id,
            candidate.provenance_tier,
            candidate.source,
            candidate.confidence,
            Jsonb(candidate.evidence or {}),
        ),
    )


def _log_candidate(cur: psycopg.Cursor, candidate: BrandCandidate, *, status: str, reason: str) -> None:
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
                candidate_parent_group_ids,
                provenance_tier,
                source,
                evidence
            )
        VALUES ('brand', %s, %s, %s, %s, %s, %s, %s, 'src/firm/brand.py', %s)
        """,
        (
            candidate.brand_name,
            candidate.normalized_brand_name or None,
            status,
            reason,
            [candidate.firm_id] if candidate.firm_id is not None else [],
            [candidate.parent_group_id] if candidate.parent_group_id is not None else [],
            candidate.provenance_tier,
            Jsonb(candidate.evidence or {}),
        ),
    )


def apply_candidates(conn: psycopg.Connection, candidates: list[BrandCandidate]) -> tuple[int, int]:
    written = 0
    logged = 0
    with conn.cursor() as cur:
        for candidate in candidates:
            if candidate.writable:
                _upsert_brand_alias(cur, candidate)
                written += 1
            elif candidate.provenance_tier == UNKNOWN:
                _log_candidate(
                    cur,
                    candidate,
                    status="unknown",
                    reason="no conservative brand-to-firm/parent resolution",
                )
                logged += 1
            else:
                _log_candidate(
                    cur,
                    candidate,
                    status="needs_review",
                    reason="candidate lacks an existing sidecar firm/parent id",
                )
                logged += 1
    conn.commit()
    return written, logged


def print_candidates(candidates: list[BrandCandidate]) -> None:
    for idx, candidate in enumerate(candidates, 1):
        target = candidate.firm_name or candidate.parent_group_name or "(unresolved)"
        ids = []
        if candidate.firm_id is not None:
            ids.append(f"firm_id={candidate.firm_id}")
        if candidate.parent_group_id is not None:
            ids.append(f"parent_group_id={candidate.parent_group_id}")
        id_text = f" ({', '.join(ids)})" if ids else ""
        print(
            f"{idx}. [{candidate.provenance_tier}/{candidate.source}] "
            f"confidence={candidate.confidence:.3f} -> {target}{id_text}"
        )
        if candidate.evidence:
            basis = candidate.evidence.get("basis") or candidate.evidence.get("reason")
            if basis:
                print(f"   evidence: {basis}")


def run(args: argparse.Namespace) -> int:
    if args.infer_llm and not os.environ.get("OPENAI_API_KEY"):
        raise RuntimeError("--infer-llm requires OPENAI_API_KEY")

    with psycopg.connect(args.db) as conn:
        _require_sidecar_tables(conn)
        candidates = collect_candidates(
            conn,
            brand_name=args.name,
            limit=args.limit,
            infer_llm=args.infer_llm,
            model=args.model,
        )
        print(f"brand/product : {args.name}")
        print(f"normalized    : {normalize_name(args.name) or '(empty)'}")
        print(f"candidates    : {len(candidates)}\n")
        print_candidates(candidates)

        if not args.apply:
            print("\nDry run only. Re-run with --apply to write brand_alias/resolution_log rows.")
            return 0

        written, logged = apply_candidates(conn, candidates)
        print(f"\nApplied sidecar writes: {written} brand_alias row(s), {logged} resolution_log row(s).")
        return 0


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Resolve a brand/product name to firm or parent candidates with provenance tiers."
    )
    p.add_argument("name", help="brand or product name to resolve")
    p.add_argument("--db", default=DEFAULT_DSN,
                   help="Postgres DSN (default: $DATABASE_URL or postgresql://localhost:5432/fda)")
    p.add_argument("--limit", type=int, default=5,
                   help="maximum existing/FDA-local candidates to inspect")
    p.add_argument("--infer-llm", action="store_true",
                   help="optionally add an inferred_external_or_llm candidate")
    p.add_argument("--model", default=MODEL,
                   help=f"OpenAI model for --infer-llm (default: {MODEL})")
    p.add_argument("--apply", action="store_true",
                   help="write brand_alias rows when linked, otherwise log to resolution_log")
    return p.parse_args()


if __name__ == "__main__":
    raise SystemExit(run(parse_args()))
