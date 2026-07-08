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
import retrieval  # noqa: E402  (semantic search for concept queries)
import validation  # noqa: E402  (LLM validation for semantic counting)
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
    semantic_query: Optional[str] = None  # fuzzy concept -> semantic retrieval over recall text
    semantic_k: Optional[int] = Field(default=None, ge=1, le=120)  # validation sample size for semantic counts
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
- semantic_query: when the question asks about a fuzzy CONCEPT/topic in free text (e.g.
  "sterility problems", "cancer-causing impurity", "pills that are too strong", "glass fragments"),
  put that concept here as a short natural-language phrase. Use intent=sample for show/find/example
  questions, intent=count_total for "how many" concept questions, and intent=count_by with group_by
  for concept distribution questions. This runs semantic retrieval over the recall text -- do NOT
  use an 'ilike' filter for a concept (ilike only matches literal words and misses synonyms like
  microbial / superpotent). Leave semantic_k unset unless the user asks to validate a specific
  sample size.
- filters: only for HARD constraints -- categories (classification/state/...) via 'eq'/'in' with a
  column's listed allowed values, and dates (ISO YYYY-MM-DD) via 'between'/'gte'/'lte'. You may combine
  semantic_query (the concept) with filters (the hard constraints), e.g. "Class I sterility recalls".
- Keep it minimal and faithful to the question."""


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


def _semantic_groups(
    a: RecallAnalytics,
    group_by: str,
    candidates: list[retrieval.Hit],
    accepted: list[validation.ValidatedHit],
    estimated_total: int,
) -> list[validation.SemanticCountGroup]:
    if group_by not in CATALOG:
        raise ValueError("count_by needs a valid group_by dimension")
    if not candidates:
        return []
    recall_numbers = [item.recall_number for item in candidates]
    q = sql.SQL(
        "SELECT recall_number, {dim} FROM drug_enforcement WHERE recall_number = ANY(%s)"
    ).format(dim=sql.Identifier(group_by))
    with a.conn.cursor() as cur:
        cur.execute(q, [recall_numbers])
        values = {r[0]: r[1] for r in cur.fetchall()}

    pool_counts: dict[Any, int] = {}
    evidence: dict[Any, list[str]] = {}
    for hit in candidates:
        value = values.get(hit.recall_number)
        pool_counts[value] = pool_counts.get(value, 0) + 1
    for item in accepted:
        value = values.get(item.recall_number)
        evidence.setdefault(value, [])
        if len(evidence[value]) < 3:
            evidence[value].append(item.recall_number)

    allocated_counts = _allocate_group_counts(pool_counts, estimated_total)
    return [
        validation.SemanticCountGroup(
            value=value,
            count=allocated_counts.get(value, 0),
            evidence=evidence.get(value, []),
        )
        for value, pool_count in sorted(pool_counts.items(), key=lambda kv: (-kv[1], str(kv[0])))
    ]


def _allocate_group_counts(pool_counts: dict[Any, int], estimated_total: int) -> dict[Any, int]:
    if estimated_total <= 0 or not pool_counts:
        return {value: 0 for value in pool_counts}
    pool_total = sum(pool_counts.values())
    raw = {
        value: (count / pool_total) * estimated_total
        for value, count in pool_counts.items()
    }
    out = {value: int(value_estimate) for value, value_estimate in raw.items()}
    remaining = estimated_total - sum(out.values())
    remainders = sorted(
        raw,
        key=lambda value: (-(raw[value] - out[value]), str(value)),
    )
    for value in remainders[:remaining]:
        out[value] += 1
    return out


def _run_semantic_count(
    a: RecallAnalytics,
    spec: QuerySpec,
    client: OpenAI,
    filters: list[Filter],
    *,
    model: str,
) -> validation.SemanticCountResult:
    if not spec.semantic_query:
        raise ValueError("semantic count needs semantic_query")
    if spec.intent is Intent.count_by and (not spec.group_by or spec.group_by not in CATALOG):
        raise ValueError("count_by needs a valid group_by dimension")
    validation_limit = validation.bounded_validation_limit(spec.semantic_k)
    retrieval_pool_limit = validation.bounded_retrieval_pool_limit(validation_limit)
    hits = retrieval.search(a.conn, client, spec.semantic_query,
                            k=retrieval_pool_limit, field="both", filters=filters)
    eligible_hits = [
        hit for hit in hits
        if hit.retrieval_score >= validation.MIN_RETRIEVAL_SCORE
    ]
    sample_hits = validation.select_validation_sample(eligible_hits, validation_limit)
    validations = validation.validate_hits(
        client,
        spec.semantic_query,
        sample_hits,
        model=model,
    )
    accepted = [item for item in validations if item.accepted]
    acceptance_rate = (len(accepted) / len(validations)) if validations else 0.0
    estimated_total = round(acceptance_rate * len(eligible_hits))
    groups = (
        _semantic_groups(a, spec.group_by, eligible_hits, accepted, estimated_total)
        if spec.intent is Intent.count_by else []
    )
    return validation.build_count_result(
        query=spec.semantic_query,
        intent=spec.intent.value,
        candidate_count=len(eligible_hits),
        retrieval_pool_count=len(hits),
        retrieval_pool_limit=retrieval_pool_limit,
        validation_limit=validation_limit,
        validations=validations,
        group_by=spec.group_by if spec.intent is Intent.count_by else None,
        groups=groups,
    )


def run_spec(a: RecallAnalytics, spec: QuerySpec, client: OpenAI, *, model: str = MODEL) -> Any:
    filters = _to_filters(spec)
    if spec.semantic_query and spec.intent is Intent.sample:  # preserve existing semantic retrieval
        return retrieval.search(a.conn, client, spec.semantic_query,
                                k=spec.limit or 10, field="both", filters=filters)
    if spec.semantic_query and spec.intent in {Intent.count_total, Intent.count_by}:
        return _run_semantic_count(a, spec, client, filters, model=model)
    if spec.semantic_query:
        raise ValueError(f"semantic_query does not support intent {spec.intent.value!r}")
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
    if isinstance(result, validation.SemanticCountResult):
        if result.group_by:
            head = (f"Estimated {result.estimated_count:,} recalls matching '{result.query}' "
                    f"across {result.candidate_count} retrieval candidates "
                    f"(verified {result.verified_count}/{result.validated_count} validated, "
                    f"avg confidence {result.confidence['accepted_avg']:.2f}), grouped by {result.group_by}:")
            body = "\n".join(
                f"  {g.value}: ~{g.count:,}   e.g. {', '.join(g.evidence)}"
                for g in result.groups[:10]
            )
            return f"{head}\n{body}"
        return (
            f"Estimated {result.estimated_count:,} recalls matching '{result.query}' "
            f"across {result.candidate_count} retrieval candidates "
            f"(verified {result.verified_count}/{result.validated_count} validated, "
            f"avg confidence {result.confidence['accepted_avg']:.2f}; "
            f"confidence band {result.confidence_interval['lower']}-"
            f"{result.confidence_interval['upper']})."
        )
    if spec.semantic_query:
        head = f"Top {len(result)} recalls matching '{spec.semantic_query}':"
        body = "\n".join(
            f"  [{h.recall_number}] sim={h.similarity:.2f}  {h.recalling_firm or '-'}: "
            f"{(h.content or '')[:70]}" for h in result)
        return f"{head}\n{body}"
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


class NLEngine:
    """Reusable NL->SQL engine for long-running services (e.g. the FastAPI app).

    Warms the expensive, request-invariant pieces ONCE — the OpenAI client and the schema
    context (which runs several DISTINCT queries) — then answers each question with a fresh,
    short-lived read-only DB connection (safe under concurrency; cheap on localhost).
    """

    def __init__(self, *, dsn: str = DEFAULT_DSN, model: str = MODEL) -> None:
        self.dsn = dsn
        self.model = model
        self.client = OpenAI()
        with RecallAnalytics(dsn) as a:
            self.schema_ctx = build_schema_context(a)  # cached for the engine's lifetime

    def ask(self, question: str) -> Answer:
        spec = generate_spec(self.client, question, self.schema_ctx, self.model)
        with RecallAnalytics(self.dsn) as a:
            try:
                result = run_spec(a, spec, self.client, model=self.model)
            except ValueError as exc:  # one repair attempt: feed the error back
                spec = generate_spec(
                    self.client,
                    f"{question}\n\n(Your previous QuerySpec was invalid: {exc}. Return a corrected one.)",
                    self.schema_ctx, self.model)
                result = run_spec(a, spec, self.client, model=self.model)
        return Answer(question, spec, summarize(spec, result), result)


def ask(question: str, *, dsn: str = DEFAULT_DSN, model: str = MODEL) -> Answer:
    """One-shot convenience used by the CLI: builds a throwaway engine and answers once.
    Long-running callers should hold a single :class:`NLEngine` and reuse ``.ask``."""
    return NLEngine(dsn=dsn, model=model).ask(question)


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
