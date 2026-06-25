"""Natural-language front-end for the deterministic analytics engine.

Path 1 of the frequency-query design, online half: the LLM never writes SQL and never
states a number. It only emits a constrained Pydantic ``QuerySpec`` (intent + filters over
whitelisted columns/values); we validate that against the catalog and run it through
``analytics.py``, so every figure comes from SQL and is auditable.

Flow:  question -> (schema + column comments + allowed values injected) -> LLM -> QuerySpec
       -> validate against CATALOG -> RecallAnalytics call -> templated answer + evidence.

Run a demo (needs OPENAI_API_KEY in .env and the populated DB):
    .venv/bin/python src/nl_query.py                # canned questions
    .venv/bin/python src/nl_query.py "your question"
"""
from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from datetime import date, datetime
from enum import Enum
from typing import Any, Optional

from dotenv import load_dotenv
from openai import OpenAI
from psycopg import sql
from pydantic import BaseModel, Field

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))  # allow `import analytics` as a script
from analytics import CATALOG, GRAINS, OPS, Filter, Kind, RecallAnalytics  # noqa: E402

load_dotenv()
DEFAULT_DSN = os.environ.get("DATABASE_URL", "postgresql://localhost:5432/fda")
MODEL = os.environ.get("OPENAI_MODEL", "gpt-4o-mini")

# Categorical columns small enough to enumerate every value into the prompt (value index).
LOW_CARD = ["classification", "status", "product_type", "voluntary_mandated",
            "initial_firm_notification", "country", "state"]


# --------------------------------------------------------------------------- #
# Constrained query intent the LLM is allowed to produce
# --------------------------------------------------------------------------- #
class Intent(str, Enum):
    count_total = "count_total"   # a single number
    count_by = "count_by"         # distribution across a dimension
    trend = "trend"               # counts over time
    sample = "sample"             # a few example rows


class Op(str, Enum):
    eq = "eq"; ne = "ne"; in_ = "in"; gte = "gte"; lte = "lte"; between = "between"; ilike = "ilike"


class FilterSpec(BaseModel):
    column: str
    op: Op
    values: list[str]  # eq/ne/gte/lte/ilike -> [x]; in -> [...]; between -> [lo, hi]


class QuerySpec(BaseModel):
    intent: Intent
    filters: list[FilterSpec] = Field(default_factory=list)
    group_by: Optional[str] = None      # count_by: a dimension column
    grain: Optional[str] = None         # trend: year/quarter/month/week/day
    date_column: Optional[str] = None   # trend: a date column
    limit: int = 20


@dataclass
class Answer:
    question: str
    spec: QuerySpec
    summary: str
    result: Any


SYSTEM = """You convert a question about U.S. FDA drug recall enforcement reports into a QuerySpec.
Rules:
- Use ONLY columns and values from the SCHEMA below. Never invent a column or a value.
- Choose intent: count_total (one number), count_by (distribution across a dimension -> set group_by),
  trend (counts over time -> set grain and date_column), sample (a few example rows).
- Filters: use a column's listed allowed values exactly. Dates are ISO YYYY-MM-DD. Use op
  'ilike' for free-text contains on text columns, 'eq'/'in' for categories, 'between'/'gte'/'lte' for dates.
- Keep filters minimal and faithful to the question."""


# --------------------------------------------------------------------------- #
# schema injection
# --------------------------------------------------------------------------- #
def build_schema_context(a: RecallAnalytics) -> str:
    with a.conn.cursor() as cur:
        cur.execute(
            "SELECT attname, col_description('drug_enforcement'::regclass, attnum) "
            "FROM pg_attribute WHERE attrelid='drug_enforcement'::regclass "
            "AND attnum > 0 AND NOT attisdropped")
        comments = {name: (c or "") for name, c in cur.fetchall()}

    lines = ["Table drug_enforcement — U.S. FDA drug recall enforcement reports.",
             "Queryable columns (use ONLY these):"]
    for col, kind in CATALOG.items():
        c = comments.get(col, "")
        c = (c[:110] + "…") if len(c) > 110 else c
        lines.append(f"- {col} [{kind.value}] — {c}")

    lines.append("\nAllowed values for key categorical columns:")
    for col in LOW_CARD:
        with a.conn.cursor() as cur:
            cur.execute(sql.SQL(
                "SELECT DISTINCT {c} FROM drug_enforcement WHERE {c} IS NOT NULL ORDER BY 1"
            ).format(c=sql.Identifier(col)))
            vals = [str(r[0]) for r in cur.fetchall()][:60]
        lines.append(f"- {col}: {', '.join(vals)}")
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# LLM call + validation + execution
# --------------------------------------------------------------------------- #
def generate_spec(client: OpenAI, question: str, schema_ctx: str, model: str) -> QuerySpec:
    parse = getattr(client.chat.completions, "parse", None) or client.beta.chat.completions.parse
    completion = parse(
        model=model,
        temperature=0,
        messages=[{"role": "system", "content": f"{SYSTEM}\n\nSCHEMA:\n{schema_ctx}"},
                  {"role": "user", "content": question}],
        response_format=QuerySpec,
    )
    return completion.choices[0].message.parsed


def _parse_date(v: str) -> date:
    v = v.strip()
    for fmt in ("%Y-%m-%d", "%Y%m%d", "%Y/%m/%d"):
        try:
            return datetime.strptime(v, fmt).date()
        except ValueError:
            continue
    raise ValueError(f"unparseable date: {v!r}")


def _to_filters(spec: QuerySpec) -> list[Filter]:
    out: list[Filter] = []
    for f in spec.filters:
        if f.column not in CATALOG:
            raise ValueError(f"unknown column {f.column!r}")
        op = f.op.value
        if op not in OPS:
            raise ValueError(f"unknown op {op!r}")
        vals: list[Any] = list(f.values)
        if CATALOG[f.column] is Kind.DATE:
            vals = [_parse_date(v) for v in vals]
        if op == "in":
            value: Any = vals
        elif op == "between":
            if len(vals) != 2:
                raise ValueError("op 'between' needs exactly 2 values")
            value = (vals[0], vals[1])
        else:
            if not vals:
                raise ValueError(f"op {op!r} needs a value")
            value = vals[0]
        out.append(Filter(f.column, op, value))
    return out


def run_spec(a: RecallAnalytics, spec: QuerySpec) -> Any:
    filters = _to_filters(spec)
    if spec.intent is Intent.count_total:
        return a.count_total(filters)
    if spec.intent is Intent.count_by:
        if not spec.group_by or spec.group_by not in CATALOG:
            raise ValueError("count_by needs a valid group_by dimension")
        return a.count_by(spec.group_by, filters, limit=spec.limit or 20, with_evidence=True)
    if spec.intent is Intent.trend:
        dcol = spec.date_column or "recall_initiation_date"
        if CATALOG.get(dcol) is not Kind.DATE:
            raise ValueError(f"{dcol!r} is not a date column")
        grain = spec.grain if spec.grain in GRAINS else "year"
        return a.trend(filters, grain=grain, date_column=dcol)
    return a.sample(filters, n=spec.limit or 5)


def summarize(spec: QuerySpec, result: Any) -> str:
    if spec.intent is Intent.count_total:
        return f"Total matching recalls: {result:,}"
    if spec.intent is Intent.count_by:
        head = f"Recalls by {spec.group_by} (top {min(len(result), spec.limit or 20)}):"
        body = "\n".join(f"  {g.value}: {g.count:,}   e.g. {', '.join(g.evidence)}"
                         for g in result[:10])
        return f"{head}\n{body}"
    if spec.intent is Intent.trend:
        body = "\n".join(f"  {p}: {n:,}" for p, n in result)
        return f"Recalls over time (by {spec.grain or 'year'}):\n{body}"
    body = "\n".join(f"  [{r.get('recall_number')}] {r.get('recalling_firm')}: "
                     f"{(r.get('reason_for_recall') or '')[:60]}" for r in result)
    return f"{len(result)} example rows:\n{body}"


def ask(question: str, *, dsn: str = DEFAULT_DSN, model: str = MODEL) -> Answer:
    client = OpenAI()
    with RecallAnalytics(dsn) as a:
        schema_ctx = build_schema_context(a)
        spec = generate_spec(client, question, schema_ctx, model)
        try:
            result = run_spec(a, spec)
        except ValueError as exc:  # one repair attempt: feed the error back
            spec = generate_spec(
                client,
                f"{question}\n\n(Your previous QuerySpec was invalid: {exc}. Return a corrected one.)",
                schema_ctx, model)
            result = run_spec(a, spec)
        return Answer(question, spec, summarize(spec, result), result)


DEMO = [
    # "How many Class I drug recalls have there been?",
    # "Which firms had the most Class I recalls?",
    "What is the yearly trend of recalls in California?",
    "Show me a few sterility-related recalls.",
]


def _demo() -> None:
    for q in DEMO:
        print(f"Q: {q}")
        ans = ask(q)
        shown = ans.spec.model_dump(exclude_none=True, exclude_defaults=True)
        print(f"   intent={ans.spec.intent.value}  spec={shown}")
        print(ans.summary)
        print()


if __name__ == "__main__":
    if len(sys.argv) > 1:
        a = ask(" ".join(sys.argv[1:]))
        print(a.summary)
    else:
        _demo()
