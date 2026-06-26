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
    def count_total(self, filters: Sequence[Filter] = ()) -> int:
        where, params = _build_where(filters)
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
    ) -> list[Group]:
        """GROUP BY a whitelisted column; counts descending. Optionally attach a
        few example recall_numbers per group as evidence."""
        if dimension not in CATALOG:
            raise ValueError(f"unknown dimension: {dimension!r}")
        where, wparams = _build_where(filters)
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
