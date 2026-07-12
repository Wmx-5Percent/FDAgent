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
                "FROM {tbl}{where} GROUP BY {dim} ORDER BY n DESC LIMIT %s"
            ).format(dim=dim, tbl=tbl, where=where)
            params: list[Any] = [evidence_n, *wparams, limit]
        else:
            q = sql.SQL(
                "SELECT {dim} AS value, count(*) AS n "
                "FROM {tbl}{where} GROUP BY {dim} ORDER BY n DESC LIMIT %s"
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

        Joins drug_enforcement to recall_label and groups by node_id, counting DISTINCT records
        per category (a record may carry several labels). Base ``filters`` (e.g. classification)
        still apply to drug_enforcement; ``level`` optionally restricts to a taxonomy depth.
        Returns Group(value=node_id, count, evidence)."""
        tbl, lt = sql.Identifier(TABLE), sql.Identifier(LABEL_TABLE)
        base_conds, base_params = _conditions(filters)
        conds: list[sql.Composable] = [sql.SQL("rl.version = %s")]
        params_pre: list[Any] = [version]
        if level is not None:
            conds.append(sql.SQL("rl.level = %s"))
            params_pre.append(level)
        conds += base_conds
        where = sql.SQL(" WHERE ") + sql.SQL(" AND ").join(conds)
        join = sql.SQL("{tbl} JOIN {lt} rl ON rl.record_id = {tbl}.id").format(tbl=tbl, lt=lt)
        if with_evidence:
            q = sql.SQL(
                "SELECT rl.node_id AS value, count(DISTINCT {tbl}.id) AS n, "
                "(array_agg({tbl}.recall_number ORDER BY {tbl}.recall_initiation_date DESC NULLS LAST))[1:%s] AS evidence "
                "FROM {join}{where} GROUP BY rl.node_id ORDER BY n DESC LIMIT %s"
            ).format(tbl=tbl, join=join, where=where)
            params: list[Any] = [evidence_n, *params_pre, *base_params, limit]
        else:
            q = sql.SQL(
                "SELECT rl.node_id AS value, count(DISTINCT {tbl}.id) AS n "
                "FROM {join}{where} GROUP BY rl.node_id ORDER BY n DESC LIMIT %s"
            ).format(tbl=tbl, join=join, where=where)
            params = [*params_pre, *base_params, limit]
        return [
            Group(value=r[0], count=r[1],
                  evidence=list(r[2]) if with_evidence and r[2] else [])
            for r in self._rows(q, params)
        ]

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
