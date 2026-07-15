"""Deterministic frequency/aggregation query engine over drug_enforcement.

Path 1 ("counting != retrieval") from the frequency-query design: safe, parameterized
SQL aggregations over the parsed structured columns. There is NO LLM here — the future
natural-language layer only translates a question into calls to these functions, so every
number comes from SQL (auditable), never hallucinated by a model.

Safety model:
    * Column names are whitelisted via CATALOG and emitted with psycopg.sql.Identifier
      (no user string ever becomes an identifier).
    * All values are bound as query parameters (no string interpolation -> no injection).
    * The connection is opened read-only; every statement is a SELECT with a LIMIT.

Run a no-dependency demo (needs only the populated DB):
    .venv/bin/python src/analytics.py
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Iterable, Sequence

import psycopg
from psycopg import sql

DEFAULT_DSN = os.environ.get("DATABASE_URL", "postgresql://localhost:5432/fda")
TABLE = "drug_enforcement"
LABEL_TABLE = "recall_label"          # taxonomy sidecar: one row per (record, version, node_id, labeler)
DEFAULT_TAXONOMY_VERSION = "v1"
RAW_FIRM_EXPOSURE_FORMULA_VERSION = "raw_severity_v0"
RAW_FIRM_EXPOSURE_FORMULA = "3 * Class I recalls + 2 * Class II recalls + 1 * Class III recalls"
RAW_FIRM_EXPOSURE_SCOPE = "raw_fda_recalling_firm"
RAW_FIRM_EXPOSURE_CAVEATS = [
    "This leaderboard uses raw FDA `recalling_firm` strings exactly as they appear in openFDA.",
    "Legal entities, subsidiaries, and parent groups are not normalized or merged in this v0 view.",
    "The score is a recall-data exposure signal, not a legal, medical, or safety verdict.",
]
PARENT_GROUP_EXPOSURE_FORMULA_VERSION = "parent_group_severity_v1"
PARENT_GROUP_EXPOSURE_SCOPE = "parent_group_provenance_backed"
PARENT_GROUP_EXPOSURE_CAVEATS = [
    "This leaderboard uses only confirmed firm→parent_group edges with provenance metadata.",
    "Unmapped, unknown, unconfirmed, and LLM-only parent edges are excluded from the ranked total and reported separately.",
    "Parent-group aggregation is inferred from the sidecar; FDA facts remain the underlying recalling_firm records.",
    "The score is a recall-data exposure signal, not a legal, medical, or safety verdict.",
]


class Kind(str, Enum):
    DIMENSION = "dimension"  # categorical: filter + group by
    DATE = "date"            # date: range filter + time trend
    TEXT = "text"            # free text: ILIKE filter only
    ID = "id"                # identifier: equality filter / evidence


# Whitelist of queryable columns and how each may be used. Anything not here is rejected.
CATALOG: dict[str, Kind] = {
    "classification": Kind.DIMENSION,
    "status": Kind.DIMENSION,
    "product_type": Kind.DIMENSION,
    "voluntary_mandated": Kind.DIMENSION,
    "initial_firm_notification": Kind.DIMENSION,
    "recalling_firm": Kind.DIMENSION,
    "state": Kind.DIMENSION,
    "country": Kind.DIMENSION,
    "city": Kind.DIMENSION,
    "distribution_pattern": Kind.DIMENSION,
    "recall_initiation_date": Kind.DATE,
    "center_classification_date": Kind.DATE,
    "termination_date": Kind.DATE,
    "report_date": Kind.DATE,
    "product_description": Kind.TEXT,
    "reason_for_recall": Kind.TEXT,
    "code_info": Kind.TEXT,
    "product_quantity": Kind.TEXT,
    "recall_number": Kind.ID,
    "event_id": Kind.ID,
}

OPS = {"eq", "ne", "in", "gte", "lte", "between", "ilike"}
GRAINS = {"year", "quarter", "month", "week", "day"}


@dataclass
class Filter:
    """A single WHERE condition on a whitelisted column."""
    column: str
    op: str
    value: Any

    def __post_init__(self) -> None:
        if self.column not in CATALOG:
            raise ValueError(f"unknown column: {self.column!r}")
        if self.op not in OPS:
            raise ValueError(f"unknown op: {self.op!r} (allowed: {sorted(OPS)})")


@dataclass
class Group:
    """One row of a GROUP BY result."""
    value: Any
    count: int
    evidence: list[str] = field(default_factory=list)
    label: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class FirmExposure:
    """One raw FDA recalling_firm row in the exposure leaderboard."""
    rank: int
    recalling_firm: str
    exposure_score: int
    total_recalls: int
    class_i_recalls: int
    class_ii_recalls: int
    class_iii_recalls: int
    unclassified_recalls: int
    top_reason_category: str | None = None
    top_reason_node_id: str | None = None
    top_reason_count: int | None = None
    evidence: list[str] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class RawFirmExposureLeaderboard:
    """Ranked v0 exposure view over raw FDA recalling_firm strings."""
    metric: str
    items: list[FirmExposure]
    formula_version: str = RAW_FIRM_EXPOSURE_FORMULA_VERSION
    formula: str = RAW_FIRM_EXPOSURE_FORMULA
    scope: str = RAW_FIRM_EXPOSURE_SCOPE
    caveats: list[str] = field(default_factory=lambda: list(RAW_FIRM_EXPOSURE_CAVEATS))
    metadata: dict[str, Any] = field(default_factory=dict)


def _conditions(filters: Sequence[Filter]) -> tuple[list[sql.Composable], list[Any]]:
    """WHERE conditions (unprefixed) + bound params for a set of whitelisted filters."""
    conds: list[sql.Composable] = []
    params: list[Any] = []
    for f in filters:
        col = sql.Identifier(f.column)
        if f.op == "eq":
            conds.append(sql.SQL("{} = %s").format(col)); params.append(f.value)
        elif f.op == "ne":
            conds.append(sql.SQL("{} <> %s").format(col)); params.append(f.value)
        elif f.op == "in":
            conds.append(sql.SQL("{} = ANY(%s)").format(col)); params.append(list(f.value))
        elif f.op == "gte":
            conds.append(sql.SQL("{} >= %s").format(col)); params.append(f.value)
        elif f.op == "lte":
            conds.append(sql.SQL("{} <= %s").format(col)); params.append(f.value)
        elif f.op == "between":
            lo, hi = f.value
            conds.append(sql.SQL("{} BETWEEN %s AND %s").format(col)); params += [lo, hi]
        elif f.op == "ilike":
            conds.append(sql.SQL("{} ILIKE %s").format(col)); params.append(f"%{f.value}%")
    return conds, params


def _build_where(filters: Sequence[Filter]) -> tuple[sql.Composable, list[Any]]:
    conds, params = _conditions(filters)
    if not conds:
        return sql.SQL(""), params
    return sql.SQL(" WHERE ") + sql.SQL(" AND ").join(conds), params


def _taxonomy_condition(node_id: str, version: str) -> tuple[sql.Composable, list[Any]]:
    """WHERE fragment restricting drug_enforcement to rows carrying a given taxonomy label
    (version, node_id) in the recall_label sidecar. EXISTS avoids fanning out the base table
    across the one-to-many label join."""
    cond = sql.SQL(
        "EXISTS (SELECT 1 FROM {lt} rl WHERE rl.record_id = {tbl}.id "
        "AND rl.version = %s AND rl.node_id = %s)"
    ).format(lt=sql.Identifier(LABEL_TABLE), tbl=sql.Identifier(TABLE))
    return cond, [version, node_id]


def _build_where_ex(
    filters: Sequence[Filter],
    *,
    taxonomy_node_id: str | None = None,
    taxonomy_version: str = DEFAULT_TAXONOMY_VERSION,
) -> tuple[sql.Composable, list[Any]]:
    """``_build_where`` plus an optional taxonomy-label membership constraint."""
    conds, params = _conditions(filters)
    if taxonomy_node_id:
        cond, tparams = _taxonomy_condition(taxonomy_node_id, taxonomy_version)
        conds.append(cond)
        params += tparams
    if not conds:
        return sql.SQL(""), params
    return sql.SQL(" WHERE ") + sql.SQL(" AND ").join(conds), params


class RecallAnalytics:
    """Read-only aggregation queries over the drug_enforcement table."""

    def __init__(self, dsn: str = DEFAULT_DSN) -> None:
        self.conn = psycopg.connect(dsn, autocommit=True)
        self.conn.read_only = True  # defense-in-depth: block any accidental write

    def close(self) -> None:
        self.conn.close()

    def __enter__(self) -> "RecallAnalytics":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # -- low-level helpers --------------------------------------------------
    def _rows(self, query: sql.Composable, params: Sequence[Any]) -> list[tuple]:
        with self.conn.cursor() as cur:
            cur.execute(query, params)
            return cur.fetchall()

    def _scalar(self, query: sql.Composable, params: Sequence[Any]) -> Any:
        with self.conn.cursor() as cur:
            cur.execute(query, params)
            row = cur.fetchone()
            return row[0] if row else None

    def _regclass_exists(self, name: str) -> bool:
        return self._scalar(sql.SQL("SELECT to_regclass(%s)"), [name]) is not None

    # -- public API ---------------------------------------------------------
    def count_total(
        self,
        filters: Sequence[Filter] = (),
        *,
        taxonomy_node_id: str | None = None,
        taxonomy_version: str = DEFAULT_TAXONOMY_VERSION,
    ) -> int:
        where, params = _build_where_ex(
            filters, taxonomy_node_id=taxonomy_node_id, taxonomy_version=taxonomy_version)
        q = sql.SQL("SELECT count(*) FROM {tbl}{where}").format(
            tbl=sql.Identifier(TABLE), where=where)
        return int(self._scalar(q, params) or 0)

    def count_by(
        self,
        dimension: str,
        filters: Sequence[Filter] = (),
        *,
        limit: int = 50,
        with_evidence: bool = False,
        evidence_n: int = 3,
        taxonomy_node_id: str | None = None,
        taxonomy_version: str = DEFAULT_TAXONOMY_VERSION,
    ) -> list[Group]:
        """GROUP BY a whitelisted column; counts descending. Optionally attach a
        few example recall_numbers per group as evidence. An optional ``taxonomy_node_id``
        restricts to records carrying that recall_label taxonomy label (exact category counts)."""
        if dimension not in CATALOG:
            raise ValueError(f"unknown dimension: {dimension!r}")
        where, wparams = _build_where_ex(
            filters, taxonomy_node_id=taxonomy_node_id, taxonomy_version=taxonomy_version)
        dim, tbl = sql.Identifier(dimension), sql.Identifier(TABLE)
        if with_evidence:
            q = sql.SQL(
                "SELECT {dim} AS value, count(*) AS n, "
                "(array_agg(recall_number ORDER BY recall_initiation_date DESC NULLS LAST))[1:%s] AS evidence "
                "FROM {tbl}{where} GROUP BY {dim} ORDER BY n DESC, {dim} ASC NULLS LAST LIMIT %s"
            ).format(dim=dim, tbl=tbl, where=where)
            params: list[Any] = [evidence_n, *wparams, limit]
        else:
            q = sql.SQL(
                "SELECT {dim} AS value, count(*) AS n "
                "FROM {tbl}{where} GROUP BY {dim} ORDER BY n DESC, {dim} ASC NULLS LAST LIMIT %s"
            ).format(dim=dim, tbl=tbl, where=where)
            params = [*wparams, limit]
        return [
            Group(value=r[0], count=r[1],
                  evidence=list(r[2]) if with_evidence and r[2] else [])
            for r in self._rows(q, params)
        ]

    def count_by_taxonomy(
        self,
        filters: Sequence[Filter] = (),
        *,
        version: str = DEFAULT_TAXONOMY_VERSION,
        level: int | None = None,
        limit: int = 50,
        with_evidence: bool = False,
        evidence_n: int = 3,
    ) -> list[Group]:
        """Distribution of recalls across taxonomy categories (the recall_label sidecar).

        Joins drug_enforcement to recall_label/taxonomy and groups by node_id, counting
        DISTINCT records per category (a record may carry several labels). Base ``filters``
        (e.g. classification) still apply to drug_enforcement; ``level`` optionally restricts
        to a taxonomy depth. Returns user-facing labels in ``Group.value`` plus node ids in
        ``Group.metadata`` for auditability."""
        tbl, lt, taxonomy = sql.Identifier(TABLE), sql.Identifier(LABEL_TABLE), sql.Identifier("taxonomy")
        base_conds, base_params = _conditions(filters)
        conds: list[sql.Composable] = [sql.SQL("rl.version = %s")]
        params_pre: list[Any] = [version]
        if level is not None:
            conds.append(sql.SQL("t.level = %s"))
            params_pre.append(level)
        conds += base_conds
        where = sql.SQL(" WHERE ") + sql.SQL(" AND ").join(conds)
        join = sql.SQL(
            "{tbl} JOIN {lt} rl ON rl.record_id = {tbl}.id "
            "JOIN {taxonomy} t ON t.version = rl.version AND t.node_id = rl.node_id"
        ).format(tbl=tbl, lt=lt, taxonomy=taxonomy)
        if with_evidence:
            q = sql.SQL(
                "SELECT t.label AS value, rl.node_id, count(DISTINCT {tbl}.id) AS n, "
                "(array_agg({tbl}.recall_number ORDER BY {tbl}.recall_initiation_date DESC NULLS LAST))[1:%s] AS evidence "
                "FROM {join}{where} GROUP BY rl.node_id, t.label ORDER BY n DESC, t.label ASC LIMIT %s"
            ).format(tbl=tbl, join=join, where=where)
            params: list[Any] = [evidence_n, *params_pre, *base_params, limit]
        else:
            q = sql.SQL(
                "SELECT t.label AS value, rl.node_id, count(DISTINCT {tbl}.id) AS n "
                "FROM {join}{where} GROUP BY rl.node_id, t.label ORDER BY n DESC, t.label ASC LIMIT %s"
            ).format(tbl=tbl, join=join, where=where)
            params = [*params_pre, *base_params, limit]
        return [
            Group(
                value=r[0],
                count=r[2],
                evidence=list(r[3]) if with_evidence and r[3] else [],
                label=r[0],
                metadata={
                    "node_id": r[1],
                    "taxonomy_version": version,
                    "source": "taxonomy",
                },
            )
            for r in self._rows(q, params)
        ]

    def raw_firm_exposure_leaderboard(
        self,
        filters: Sequence[Filter] = (),
        *,
        limit: int = 20,
        rank_by: str = "severity_weighted_exposure",
        evidence_n: int = 3,
        taxonomy_version: str = DEFAULT_TAXONOMY_VERSION,
    ) -> RawFirmExposureLeaderboard:
        """Rank raw FDA ``recalling_firm`` strings by recall exposure.

        This is intentionally a v0 FDA-name-level view: it does not consume the firm
        resolution sidecar, parent groups, brand aliases, or any safety verdict model.
        The severity score is fully deterministic SQL:
        ``3 * Class I + 2 * Class II + 1 * Class III``.
        """
        if rank_by not in {"severity_weighted_exposure", "recall_count"}:
            raise ValueError("rank_by must be 'severity_weighted_exposure' or 'recall_count'")
        limit = max(1, min(int(limit), 100))
        evidence_n = max(1, min(int(evidence_n), 10))

        conds, params = _conditions(filters)
        conds.extend([
            sql.SQL("recalling_firm IS NOT NULL"),
            sql.SQL("btrim(recalling_firm) <> ''"),
        ])
        where = sql.SQL(" WHERE ") + sql.SQL(" AND ").join(conds)
        order_by = (
            sql.SQL("severity_weighted_exposure DESC, total_recalls DESC, recalling_firm ASC")
            if rank_by == "severity_weighted_exposure"
            else sql.SQL("total_recalls DESC, severity_weighted_exposure DESC, recalling_firm ASC")
        )
        tbl, label_tbl, taxonomy_tbl = (
            sql.Identifier(TABLE),
            sql.Identifier(LABEL_TABLE),
            sql.Identifier("taxonomy"),
        )
        q = sql.SQL(
            """
            WITH base AS (
                SELECT id, recall_number, recalling_firm, classification, recall_initiation_date
                FROM {tbl}
                {where}
            ),
            firm_counts AS (
                SELECT
                    recalling_firm,
                    count(*) AS total_recalls,
                    count(*) FILTER (WHERE classification = 'Class I') AS class_i_recalls,
                    count(*) FILTER (WHERE classification = 'Class II') AS class_ii_recalls,
                    count(*) FILTER (WHERE classification = 'Class III') AS class_iii_recalls,
                    count(*) FILTER (
                        WHERE classification IS NULL
                           OR classification NOT IN ('Class I', 'Class II', 'Class III')
                    ) AS unclassified_recalls,
                    (
                        3 * count(*) FILTER (WHERE classification = 'Class I')
                        + 2 * count(*) FILTER (WHERE classification = 'Class II')
                        + count(*) FILTER (WHERE classification = 'Class III')
                    ) AS severity_weighted_exposure,
                    (
                        array_agg(
                            recall_number
                            ORDER BY
                                CASE classification
                                    WHEN 'Class I' THEN 3
                                    WHEN 'Class II' THEN 2
                                    WHEN 'Class III' THEN 1
                                    ELSE 0
                                END DESC,
                                recall_initiation_date DESC NULLS LAST,
                                recall_number
                        )
                    )[1:%s] AS evidence
                FROM base
                GROUP BY recalling_firm
            ),
            reason_counts AS (
                SELECT
                    b.recalling_firm,
                    t.label,
                    rl.node_id,
                    count(DISTINCT b.id) AS category_count,
                    row_number() OVER (
                        PARTITION BY b.recalling_firm
                        ORDER BY count(DISTINCT b.id) DESC, t.label ASC
                    ) AS rn
                FROM base b
                JOIN {label_tbl} rl ON rl.record_id = b.id AND rl.version = %s
                JOIN {taxonomy_tbl} t ON t.version = rl.version AND t.node_id = rl.node_id
                GROUP BY b.recalling_firm, t.label, rl.node_id
            )
            SELECT
                fc.recalling_firm,
                fc.severity_weighted_exposure,
                fc.total_recalls,
                fc.class_i_recalls,
                fc.class_ii_recalls,
                fc.class_iii_recalls,
                fc.unclassified_recalls,
                rc.label AS top_reason_category,
                rc.node_id AS top_reason_node_id,
                rc.category_count AS top_reason_count,
                fc.evidence
            FROM firm_counts fc
            LEFT JOIN reason_counts rc
                ON rc.recalling_firm = fc.recalling_firm AND rc.rn = 1
            ORDER BY {order_by}
            LIMIT %s
            """
        ).format(
            tbl=tbl,
            where=where,
            label_tbl=label_tbl,
            taxonomy_tbl=taxonomy_tbl,
            order_by=order_by,
        )
        rows = self._rows(q, [*params, evidence_n, taxonomy_version, limit])
        items = [
            FirmExposure(
                rank=i,
                recalling_firm=str(row[0]),
                exposure_score=int(row[1] or 0),
                total_recalls=int(row[2] or 0),
                class_i_recalls=int(row[3] or 0),
                class_ii_recalls=int(row[4] or 0),
                class_iii_recalls=int(row[5] or 0),
                unclassified_recalls=int(row[6] or 0),
                top_reason_category=row[7],
                top_reason_node_id=row[8],
                top_reason_count=int(row[9]) if row[9] is not None else None,
                evidence=list(row[10]) if row[10] else [],
            )
            for i, row in enumerate(rows, start=1)
        ]
        return RawFirmExposureLeaderboard(
            metric="severity_weighted" if rank_by == "severity_weighted_exposure" else "recall_count",
            items=items,
        )

    def parent_group_exposure_leaderboard(
        self,
        filters: Sequence[Filter] = (),
        *,
        limit: int = 20,
        rank_by: str = "severity_weighted_exposure",
        evidence_n: int = 3,
        taxonomy_version: str = DEFAULT_TAXONOMY_VERSION,
    ) -> RawFirmExposureLeaderboard:
        """Rank provenance-backed parent groups by recall exposure.

        This consumes the firm-resolution sidecar only when ``sql/011`` has created
        confirmed firm→parent edges. Unknown, unconfirmed, and LLM-only edges are
        kept out of the parent total and summarized in metadata so inferred rollups
        never silently mix with raw FDA facts.
        """
        if rank_by not in {"severity_weighted_exposure", "recall_count"}:
            raise ValueError("rank_by must be 'severity_weighted_exposure' or 'recall_count'")
        limit = max(1, min(int(limit), 100))
        evidence_n = max(1, min(int(evidence_n), 10))
        metric = "severity_weighted" if rank_by == "severity_weighted_exposure" else "recall_count"

        required = ("firm_alias", "firm", "parent_group", "firm_parent_group_edge")
        missing = [name for name in required if not self._regclass_exists(name)]
        if missing:
            return RawFirmExposureLeaderboard(
                metric=metric,
                items=[],
                formula_version=PARENT_GROUP_EXPOSURE_FORMULA_VERSION,
                formula=RAW_FIRM_EXPOSURE_FORMULA,
                scope=PARENT_GROUP_EXPOSURE_SCOPE,
                caveats=[
                    *PARENT_GROUP_EXPOSURE_CAVEATS,
                    "Parent-group rollup prerequisites are not installed; run sql/011_parent_group_rollup.sql.",
                ],
                metadata={
                    "available": False,
                    "missing_tables": missing,
                    "backed_edge_count": 0,
                    "mapped_recall_count": 0,
                    "excluded_recall_count": 0,
                },
            )

        backed_edge_count = int(self._scalar(
            sql.SQL(
                """
                SELECT count(*)
                FROM firm_parent_group_edge
                WHERE active
                  AND review_status = 'confirmed'
                  AND provenance_tier <> 'unknown'
                  AND source <> 'llm'
                """
            ),
            [],
        ) or 0)
        if backed_edge_count == 0:
            return RawFirmExposureLeaderboard(
                metric=metric,
                items=[],
                formula_version=PARENT_GROUP_EXPOSURE_FORMULA_VERSION,
                formula=RAW_FIRM_EXPOSURE_FORMULA,
                scope=PARENT_GROUP_EXPOSURE_SCOPE,
                caveats=[
                    *PARENT_GROUP_EXPOSURE_CAVEATS,
                    "No confirmed provenance-backed parent edges are present yet; raw-firm exposure remains the fallback view.",
                ],
                metadata={
                    "available": False,
                    "backed_edge_count": 0,
                    "mapped_recall_count": 0,
                    "excluded_recall_count": None,
                },
            )

        conds, params = _conditions(filters)
        conds.extend([
            sql.SQL("recalling_firm IS NOT NULL"),
            sql.SQL("btrim(recalling_firm) <> ''"),
        ])
        where = sql.SQL(" WHERE ") + sql.SQL(" AND ").join(conds)
        order_by = (
            sql.SQL("exposure_score DESC, total_recalls DESC, parent_group_name ASC")
            if rank_by == "severity_weighted_exposure"
            else sql.SQL("total_recalls DESC, exposure_score DESC, parent_group_name ASC")
        )
        tbl, label_tbl, taxonomy_tbl = (
            sql.Identifier(TABLE),
            sql.Identifier(LABEL_TABLE),
            sql.Identifier("taxonomy"),
        )
        q = sql.SQL(
            """
            WITH base AS (
                SELECT id, recall_number, recalling_firm, classification, recall_initiation_date
                FROM {tbl}
                {where}
            ),
            mapped AS (
                SELECT
                    b.id,
                    b.recall_number,
                    b.recalling_firm,
                    b.classification,
                    b.recall_initiation_date,
                    f.id AS firm_id,
                    f.canonical_name AS firm_name,
                    pg.id AS parent_group_id,
                    pg.canonical_name AS parent_group_name,
                    edge.provenance_tier,
                    edge.source,
                    edge.source_name,
                    edge.source_url,
                    edge.source_id,
                    edge.as_of_date,
                    edge.evidence AS edge_evidence
                FROM base b
                JOIN firm_alias fa
                  ON fa.source_table = 'drug_enforcement'
                 AND fa.source_field = 'recalling_firm'
                 AND fa.raw_firm = b.recalling_firm
                JOIN firm f ON f.id = fa.firm_id
                JOIN firm_parent_group_edge edge
                  ON edge.firm_id = f.id
                 AND edge.active
                 AND edge.review_status = 'confirmed'
                 AND edge.provenance_tier <> 'unknown'
                 AND edge.source <> 'llm'
                JOIN parent_group pg ON pg.id = edge.parent_group_id
            ),
            parent_counts AS (
                SELECT
                    parent_group_id,
                    parent_group_name,
                    count(*) AS total_recalls,
                    count(*) FILTER (WHERE classification = 'Class I') AS class_i_recalls,
                    count(*) FILTER (WHERE classification = 'Class II') AS class_ii_recalls,
                    count(*) FILTER (WHERE classification = 'Class III') AS class_iii_recalls,
                    count(*) FILTER (
                        WHERE classification IS NULL
                           OR classification NOT IN ('Class I', 'Class II', 'Class III')
                    ) AS unclassified_recalls,
                    (
                        3 * count(*) FILTER (WHERE classification = 'Class I')
                        + 2 * count(*) FILTER (WHERE classification = 'Class II')
                        + count(*) FILTER (WHERE classification = 'Class III')
                    ) AS exposure_score,
                    count(DISTINCT firm_id) AS member_firm_count,
                    (
                        array_agg(
                            recall_number
                            ORDER BY
                                CASE classification
                                    WHEN 'Class I' THEN 3
                                    WHEN 'Class II' THEN 2
                                    WHEN 'Class III' THEN 1
                                    ELSE 0
                                END DESC,
                                recall_initiation_date DESC NULLS LAST,
                                recall_number
                        )
                    )[1:%s] AS evidence
                FROM mapped
                GROUP BY parent_group_id, parent_group_name
            ),
            member_counts AS (
                SELECT
                    parent_group_id,
                    firm_id,
                    firm_name,
                    count(*) AS total_recalls,
                    count(*) FILTER (WHERE classification = 'Class I') AS class_i_recalls,
                    count(*) FILTER (WHERE classification = 'Class II') AS class_ii_recalls,
                    count(*) FILTER (WHERE classification = 'Class III') AS class_iii_recalls,
                    count(*) FILTER (
                        WHERE classification IS NULL
                           OR classification NOT IN ('Class I', 'Class II', 'Class III')
                    ) AS unclassified_recalls,
                    (
                        3 * count(*) FILTER (WHERE classification = 'Class I')
                        + 2 * count(*) FILTER (WHERE classification = 'Class II')
                        + count(*) FILTER (WHERE classification = 'Class III')
                    ) AS exposure_score,
                    min(provenance_tier) AS provenance_tier,
                    min(source) AS source,
                    min(source_name) AS source_name,
                    min(source_url) AS source_url,
                    min(source_id) AS source_id,
                    min(as_of_date) AS as_of_date,
                    jsonb_agg(DISTINCT edge_evidence) FILTER (WHERE edge_evidence <> '{{}}'::jsonb) AS edge_evidence,
                    (
                        array_agg(
                            recall_number
                            ORDER BY
                                CASE classification
                                    WHEN 'Class I' THEN 3
                                    WHEN 'Class II' THEN 2
                                    WHEN 'Class III' THEN 1
                                    ELSE 0
                                END DESC,
                                recall_initiation_date DESC NULLS LAST,
                                recall_number
                        )
                    )[1:%s] AS evidence
                FROM mapped
                GROUP BY parent_group_id, firm_id, firm_name
            ),
            members AS (
                SELECT
                    parent_group_id,
                    jsonb_agg(
                        jsonb_build_object(
                            'firm_id', firm_id,
                            'firm_name', firm_name,
                            'total_recalls', total_recalls,
                            'exposure_score', exposure_score,
                            'class_i_recalls', class_i_recalls,
                            'class_ii_recalls', class_ii_recalls,
                            'class_iii_recalls', class_iii_recalls,
                            'unclassified_recalls', unclassified_recalls,
                            'edge_provenance_tier', provenance_tier,
                            'edge_source', source,
                            'edge_source_name', source_name,
                            'edge_source_url', source_url,
                            'edge_source_id', source_id,
                            'edge_as_of_date', as_of_date,
                            'edge_evidence', COALESCE(edge_evidence, '[]'::jsonb),
                            'evidence', evidence
                        )
                        ORDER BY exposure_score DESC, total_recalls DESC, firm_name ASC
                    ) AS member_breakdown
                FROM member_counts
                GROUP BY parent_group_id
            ),
            reason_counts AS (
                SELECT
                    m.parent_group_id,
                    t.label,
                    rl.node_id,
                    count(DISTINCT m.id) AS category_count,
                    row_number() OVER (
                        PARTITION BY m.parent_group_id
                        ORDER BY count(DISTINCT m.id) DESC, t.label ASC
                    ) AS rn
                FROM mapped m
                JOIN {label_tbl} rl ON rl.record_id = m.id AND rl.version = %s
                JOIN {taxonomy_tbl} t ON t.version = rl.version AND t.node_id = rl.node_id
                GROUP BY m.parent_group_id, t.label, rl.node_id
            )
            SELECT
                pc.parent_group_id,
                pc.parent_group_name,
                pc.exposure_score,
                pc.total_recalls,
                pc.class_i_recalls,
                pc.class_ii_recalls,
                pc.class_iii_recalls,
                pc.unclassified_recalls,
                pc.member_firm_count,
                rc.label AS top_reason_category,
                rc.node_id AS top_reason_node_id,
                rc.category_count AS top_reason_count,
                pc.evidence,
                COALESCE(m.member_breakdown, '[]'::jsonb) AS member_breakdown
            FROM parent_counts pc
            LEFT JOIN reason_counts rc
                ON rc.parent_group_id = pc.parent_group_id AND rc.rn = 1
            LEFT JOIN members m ON m.parent_group_id = pc.parent_group_id
            ORDER BY {order_by}
            LIMIT %s
            """
        ).format(
            tbl=tbl,
            where=where,
            label_tbl=label_tbl,
            taxonomy_tbl=taxonomy_tbl,
            order_by=order_by,
        )
        rows = self._rows(q, [*params, evidence_n, evidence_n, taxonomy_version, limit])

        coverage_q = sql.SQL(
            """
            WITH base AS (
                SELECT id, recalling_firm
                FROM {tbl}
                {where}
            ),
            classified AS (
                SELECT
                    b.id,
                    b.recalling_firm,
                    CASE
                        WHEN fa.id IS NULL THEN 'unaliased'
                        WHEN EXISTS (
                            SELECT 1 FROM firm_parent_group_edge edge
                            WHERE edge.firm_id = fa.firm_id
                              AND edge.active
                              AND edge.review_status = 'confirmed'
                              AND edge.provenance_tier <> 'unknown'
                              AND edge.source <> 'llm'
                        ) THEN 'mapped'
                        WHEN EXISTS (
                            SELECT 1 FROM firm_parent_group_edge edge
                            WHERE edge.firm_id = fa.firm_id
                              AND edge.active
                              AND edge.review_status <> 'confirmed'
                        ) THEN 'unconfirmed_parent_edge'
                        WHEN EXISTS (
                            SELECT 1 FROM firm_parent_group_edge edge
                            WHERE edge.firm_id = fa.firm_id
                              AND edge.active
                              AND edge.review_status = 'confirmed'
                              AND edge.provenance_tier = 'unknown'
                        ) THEN 'unknown_parent_edge'
                        WHEN EXISTS (
                            SELECT 1 FROM firm_parent_group_edge edge
                            WHERE edge.firm_id = fa.firm_id
                              AND edge.active
                              AND edge.review_status = 'confirmed'
                              AND edge.source = 'llm'
                        ) THEN 'llm_only_parent_edge'
                        WHEN EXISTS (
                            SELECT 1 FROM firm_parent_group_edge edge
                            WHERE edge.firm_id = fa.firm_id
                              AND edge.active
                        ) THEN 'unconfirmed_parent_edge'
                        ELSE 'no_parent_edge'
                    END AS mapping_status
                FROM base b
                LEFT JOIN firm_alias fa
                  ON fa.source_table = 'drug_enforcement'
                 AND fa.source_field = 'recalling_firm'
                 AND fa.raw_firm = b.recalling_firm
            )
            SELECT
                COALESCE(sum(n) FILTER (WHERE mapping_status = 'mapped'), 0)::int AS mapped_recalls,
                COALESCE(sum(n) FILTER (WHERE mapping_status <> 'mapped'), 0)::int AS excluded_recalls,
                (
                    SELECT count(DISTINCT recalling_firm)::int
                    FROM classified
                    WHERE mapping_status <> 'mapped'
                ) AS excluded_raw_firms,
                COALESCE(jsonb_object_agg(mapping_status, n ORDER BY mapping_status), '{{}}'::jsonb) AS status_counts
            FROM (
                SELECT mapping_status, count(*)::int AS n
                FROM classified
                GROUP BY mapping_status
            ) grouped
            """
        ).format(tbl=tbl, where=where)
        coverage = self._rows(coverage_q, params)
        mapped_recalls, excluded_recalls, excluded_raw_firms, status_counts = (
            coverage[0] if coverage else (0, 0, 0, {})
        )

        items = [
            FirmExposure(
                rank=i,
                recalling_firm=str(row[1]),
                exposure_score=int(row[2] or 0),
                total_recalls=int(row[3] or 0),
                class_i_recalls=int(row[4] or 0),
                class_ii_recalls=int(row[5] or 0),
                class_iii_recalls=int(row[6] or 0),
                unclassified_recalls=int(row[7] or 0),
                top_reason_category=row[9],
                top_reason_node_id=row[10],
                top_reason_count=int(row[11]) if row[11] is not None else None,
                evidence=list(row[12]) if row[12] else [],
                metadata={
                    "parent_group_id": int(row[0]),
                    "parent_group_name": row[1],
                    "member_firm_count": int(row[8] or 0),
                    "member_breakdown": row[13] or [],
                },
            )
            for i, row in enumerate(rows, start=1)
        ]
        return RawFirmExposureLeaderboard(
            metric=metric,
            items=items,
            formula_version=PARENT_GROUP_EXPOSURE_FORMULA_VERSION,
            formula=RAW_FIRM_EXPOSURE_FORMULA,
            scope=PARENT_GROUP_EXPOSURE_SCOPE,
            caveats=list(PARENT_GROUP_EXPOSURE_CAVEATS),
            metadata={
                "available": bool(items),
                "backed_edge_count": backed_edge_count,
                "mapped_recall_count": int(mapped_recalls or 0),
                "excluded_recall_count": int(excluded_recalls or 0),
                "excluded_raw_firm_count": int(excluded_raw_firms or 0),
                "mapping_status_counts": status_counts or {},
            },
        )

    def trend(
        self,
        filters: Sequence[Filter] = (),
        *,
        grain: str = "year",
        date_column: str = "recall_initiation_date",
    ) -> list[tuple[Any, int]]:
        """Time series: count per date bucket (year/quarter/month/week/day)."""
        if grain not in GRAINS:
            raise ValueError(f"unknown grain: {grain!r} (allowed: {sorted(GRAINS)})")
        if CATALOG.get(date_column) is not Kind.DATE:
            raise ValueError(f"{date_column!r} is not a date column")
        where, wparams = _build_where(filters)
        dcol, tbl = sql.Identifier(date_column), sql.Identifier(TABLE)
        notnull = sql.SQL("{} IS NOT NULL").format(dcol)
        full_where = (sql.SQL(" WHERE ") + notnull) if where == sql.SQL("") \
            else where + sql.SQL(" AND ") + notnull
        q = sql.SQL(
            "SELECT date_trunc(%s, {dcol})::date AS period, count(*) AS n "
            "FROM {tbl}{where} GROUP BY 1 ORDER BY 1"
        ).format(dcol=dcol, tbl=tbl, where=full_where)
        return [(r[0], r[1]) for r in self._rows(q, [grain, *wparams])]

    def sample(
        self,
        filters: Sequence[Filter] = (),
        *,
        columns: Iterable[str] = ("recall_number", "classification", "recalling_firm", "reason_for_recall"),
        n: int = 5,
    ) -> list[dict[str, Any]]:
        """Return a few raw rows as evidence (selected whitelisted columns)."""
        cols = list(columns)
        for c in cols:
            if c not in CATALOG:
                raise ValueError(f"unknown column: {c!r}")
        where, wparams = _build_where(filters)
        q = sql.SQL("SELECT {cols} FROM {tbl}{where} LIMIT %s").format(
            cols=sql.SQL(", ").join(sql.Identifier(c) for c in cols),
            tbl=sql.Identifier(TABLE), where=where)
        return [dict(zip(cols, r)) for r in self._rows(q, [*wparams, n])]


def _demo() -> None:
    with RecallAnalytics() as a:
        print(f"Total drug recalls: {a.count_total():,}\n")

        print("By classification:")
        for g in a.count_by("classification"):
            print(f"  {g.value:<20} {g.count:>6,}")

        print("\nTop 5 recalling firms for Class I recalls (with evidence):")
        for g in a.count_by(
            "recalling_firm",
            [Filter("classification", "eq", "Class I")],
            limit=5, with_evidence=True,
        ):
            print(f"  {g.count:>4}  {g.value}")
            print(f"        e.g. {', '.join(g.evidence)}")

        print("\nYearly trend of Class I recalls:")
        for period, n in a.trend([Filter("classification", "eq", "Class I")], grain="year"):
            print(f"  {period:%Y}: {'#' * (n // 5)} {n}")

        print("\nSterility-related recalls (reason ILIKE '%steril%'), sample:")
        for row in a.sample([Filter("reason_for_recall", "ilike", "steril")], n=3):
            print(f"  [{row['recall_number']}] {row['recalling_firm']}: "
                  f"{(row['reason_for_recall'] or '')[:70]}...")


if __name__ == "__main__":
    _demo()
