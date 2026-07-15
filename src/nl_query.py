"""Natural-language front-end for the deterministic analytics engine.

Path 1 of the frequency-query design, online half: the LLM never writes SQL and never
states a number. It only emits a constrained Pydantic ``QuerySpec`` (intent + filters over
whitelisted columns/values); we validate that against the catalog and run it through
``analytics.py``, so every figure comes from SQL and is auditable.

Flow:  question -> (schema + column comments + allowed values injected) -> LLM -> QuerySpec
       -> validate against CATALOG -> RecallAnalytics call -> templated answer + evidence.

Run a demo (needs configured LLM credentials in .env and the populated DB):
    .venv/bin/python src/nl_query.py                # canned questions
    .venv/bin/python src/nl_query.py "your question"
"""
from __future__ import annotations

import os
import re
import sys
from collections import Counter
from dataclasses import dataclass, field
from datetime import date, datetime
from enum import Enum
from typing import Any, Optional

from dotenv import load_dotenv
from psycopg import sql
from pydantic import BaseModel, Field

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))  # allow `import analytics` as a script
import llm  # noqa: E402  (OpenAI-compatible provider gateway)
import agent_control  # noqa: E402  (/ask guard before the query pipeline)
import retrieval  # noqa: E402  (semantic search for concept queries)
import validation  # noqa: E402  (LLM validation for semantic counting)
from analytics import (  # noqa: E402
    CATALOG,
    GRAINS,
    OPS,
    PARENT_GROUP_EXPOSURE_SCOPE,
    Filter,
    Group,
    Kind,
    RawFirmExposureLeaderboard,
    RecallAnalytics,
)

load_dotenv()
DEFAULT_DSN = os.environ.get("DATABASE_URL", "postgresql://localhost:5432/fda")
MODEL = llm.chat_config().model

# Categorical columns small enough to enumerate every value into the prompt (value index).
LOW_CARD = ["classification", "status", "product_type", "voluntary_mandated",
            "initial_firm_notification", "country", "state"]

TAXONOMY_VERSION = "v1"  # recall-reason taxonomy version whose labels back exact category counts


# --------------------------------------------------------------------------- #
# Constrained query intent the LLM is allowed to produce
# --------------------------------------------------------------------------- #
class Intent(str, Enum):
    count_total = "count_total"   # a single number
    count_by = "count_by"         # distribution across a dimension
    count_by_taxonomy = "count_by_taxonomy"  # distribution across recall-reason taxonomy categories
    explain_taxonomy_node = "explain_taxonomy_node"  # plain-language definition of a taxonomy category
    raw_firm_exposure = "raw_firm_exposure"  # raw recalling_firm leaderboard + severity exposure score
    parent_group_exposure = "parent_group_exposure"  # normalized parent-group leaderboard from sidecar edges
    trend = "trend"               # counts over time
    sample = "sample"             # a few example rows


class Op(str, Enum):
    eq = "eq"; ne = "ne"; in_ = "in"; gte = "gte"; lte = "lte"; between = "between"; ilike = "ilike"


class ExposureMetric(str, Enum):
    severity_weighted = "severity_weighted"
    recall_count = "recall_count"


class FilterSpec(BaseModel):
    column: str
    op: Op
    values: list[str]  # eq/ne/gte/lte/ilike -> [x]; in -> [...]; between -> [lo, hi]


class QuerySpec(BaseModel):
    intent: Intent
    semantic_query: Optional[str] = None  # fuzzy concept -> semantic retrieval over recall text
    semantic_aliases: list[str] = Field(default_factory=list)  # FTS fallback expansions only
    semantic_k: Optional[int] = Field(default=None, ge=1, le=120)  # validation sample size for semantic counts
    filters: list[FilterSpec] = Field(default_factory=list)
    group_by: Optional[str] = None      # count_by: a dimension column
    taxonomy_node_id: Optional[str] = None  # exact recall-reason category filter (recall_label)
    exposure_metric: Optional[ExposureMetric] = None  # raw_firm_exposure ranking metric
    grain: Optional[str] = None         # trend: year/quarter/month/week/day
    date_column: Optional[str] = None   # trend: a date column
    limit: int = 20


@dataclass
class Answer:
    question: str
    spec: QuerySpec | None
    summary: str
    result: Any
    control: agent_control.AgentControlDecision | None = None
    metadata: dict[str, Any] = field(default_factory=dict)
    highlights: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class TaxonomyNodeInfo:
    version: str
    node_id: str
    parent_id: str | None
    label: str
    definition: str
    examples: list[str]
    level: int
    status: str
    parent_label: str | None = None
    parent_definition: str | None = None


@dataclass(frozen=True)
class TaxonomyExplanation:
    node: TaxonomyNodeInfo
    answer: str
    examples: list[dict[str, Any]] = field(default_factory=list)


@dataclass(frozen=True)
class ResultSection:
    id: str
    title: str
    data_kind: str
    dimension: str
    source: str
    spec: QuerySpec
    result: Any
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class MultiSectionResult:
    intent: str
    sections: list[ResultSection]


SYSTEM = """You convert a question about U.S. FDA drug recall enforcement reports into a QuerySpec.
Rules:
- Use ONLY columns and values from the SCHEMA below. Never invent a column or a value.
- Choose intent: count_total (one number), count_by (distribution across a dimension -> set group_by),
  count_by_taxonomy (distribution across recall-reason categories from the TAXONOMY list),
  explain_taxonomy_node (plain-language definition/explanation of one TAXONOMY category),
  raw_firm_exposure (rank raw FDA recalling_firm values with count + severity score),
  parent_group_exposure (rank normalized parent groups using confirmed sidecar firm→parent edges),
  trend (counts over time -> set grain and date_column), sample (a few example rows).
- Use explain_taxonomy_node when the user asks what a recall-reason category or FDA recall term MEANS,
  asks for a definition, or uses phrasing such as "what is", "what does X mean", "define X",
  "这是什么意思", "这到底是什么", "在药物领域是什么意思". Set taxonomy_node_id to the matching
  TAXONOMY node. Do NOT set group_by, filters, semantic_query, grain, or date_column for explanation
  questions. Explanation questions should not become count_by just because the category has exact labels.
- Explicit counting/chart wording overrides explanation: "how many", "count", "top N", "most common",
  "distribution", "breakdown", "trend", "by firm/classification", "多少", "几个", "排名", "分布"
  should use count_total/count_by/count_by_taxonomy/trend as appropriate, not explain_taxonomy_node.
- taxonomy_node_id: when the question is about a recall-REASON concept matching one of the TAXONOMY
  categories listed below, set taxonomy_node_id to that node_id for an EXACT count from labeled data
  (PREFERRED over semantic_query for recall-reason topics). Use with intent=count_total ("how many
  sterility recalls" -> taxonomy_node_id=sterility_assurance) or intent=count_by + group_by ("sterility
  recalls by firm"), or with intent=explain_taxonomy_node when the user asks what the category means.
  For "most common recall reasons / reason distribution", use intent=count_by_taxonomy (no node_id).
  When you set taxonomy_node_id or use count_by_taxonomy, do NOT set semantic_query.
- raw_firm_exposure: use when the user asks which firms/companies/厂商/公司 have the most FDA drug
  recalls, the highest recall exposure, or a severity-weighted raw company exposure leaderboard.
  This ranks raw FDA `recalling_firm` strings only. Set exposure_metric="recall_count" for recall-count
  wording ("most recalls", "top companies by recall count", "哪些公司召回最多"). Set
  exposure_metric="severity_weighted" for exposure/risk-exposure/severity-weighted wording
  ("recall exposure", "severity-weighted recalls", "风险暴露"). Do NOT use this intent for a
  class-specific distribution such as "Which firms had the most Class I recalls?", or for a
  concept-specific distribution such as "Which firms had the most sterility recalls?" -- use
  intent=count_by, group_by="recalling_firm", plus the classification/taxonomy/semantic concept
  filter instead. When raw_firm_exposure is appropriate and the user adds HARD constraints
  (state/country/status/date), preserve those constraints in filters; never drop them.
- parent_group_exposure: use only when the user explicitly asks for parent companies, parent groups,
  company groups, normalized company rollups, 母公司, 公司集团, or 集团 exposure/rankings. This is not
  the end-user Recall Profile/safety advisory. It uses only confirmed provenance-backed
  firm→parent_group sidecar edges, excludes unknown/unconfirmed/LLM-only edges from the ranked total,
  and keeps member firm breakdowns/evidence visible. Preserve HARD constraints in filters.
- semantic_query: when the question asks about a fuzzy CONCEPT/topic in free text (e.g.
  "sterility problems", "cancer-causing impurity", "pills that are too strong", "glass fragments",
  or the same idea in another language such as "药效太强" or "细菌感染"), put the CORE concept here
  as a short, canonical ENGLISH phrase, normalized to how it appears in U.S. recall reports and
  independent of the question's language. For example: "药效太强"/"pills too strong" -> "superpotent";
  "药效不足"/"low potency" -> "subpotent"; "无菌问题"/"non-sterile" -> "sterility"; "细菌感染" ->
  "microbial contamination"; "玻璃碎片" -> "glass particles"; "致癌杂质" -> "nitrosamine impurity".
  Keep semantic_query to the concept ITSELF; do not add generic words such as "problems"/"issues"
  unless the user did. Put keyword synonyms/variants (used only for a keyword-search fallback) in
  semantic_aliases, e.g. for superpotent: ["too strong", "over strength", "high assay",
  "potency above specification"]. Use intent=sample for show/find/example questions,
  intent=count_total for "how many" concept questions, and intent=count_by with group_by for
  concept distribution questions. This runs semantic retrieval over the recall text -- do NOT use
  an 'ilike' filter for a concept. Leave semantic_k unset unless the user asks to validate a
  specific sample size.
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


def load_taxonomy_nodes(a: RecallAnalytics, version: str = TAXONOMY_VERSION) -> list[tuple[str, str, str, int]]:
    """Active taxonomy categories that actually carry labels in recall_label (so an exact
    count is always meaningful). Returns (node_id, label, definition, level)."""
    with a.conn.cursor() as cur:
        cur.execute(
            "SELECT t.node_id, t.label, t.definition, t.level FROM taxonomy t "
            "WHERE t.version = %s AND t.status = 'active' AND EXISTS ("
            "  SELECT 1 FROM recall_label rl WHERE rl.version = t.version AND rl.node_id = t.node_id) "
            "ORDER BY t.level, t.node_id",
            [version],
        )
        return [(r[0], r[1], r[2] or "", r[3]) for r in cur.fetchall()]


def build_taxonomy_context(nodes: list[tuple[str, str, str, int]]) -> str:
    """Prompt block listing recall-reason categories the LLM may target for exact counts."""
    if not nodes:
        return ""
    lines = [
        "Recall-reason categories (set taxonomy_node_id to a node_id below for EXACT counts "
        "or explain_taxonomy_node definitions):"
    ]
    for node_id, label, definition, _level in nodes:
        d = (definition[:100] + "…") if len(definition) > 100 else definition
        lines.append(f"- {node_id} — {label}: {d}")
    return "\n".join(lines)


_EXPLAIN_EN_RE = re.compile(
    r"\b(what\s+(?:is|are|does)|what['’]?s|meaning\s+of|mean(?:s|ing)?|"
    r"explain|definition\s+of|define)\b",
    re.IGNORECASE,
)
_COUNT_OR_CHART_EN_RE = re.compile(
    r"\b(how many|count|counts|number of|top\s*\d*|most common|distribution|"
    r"breakdown|trend|over time|rank(?:ing)?|which firms?|which companies?|"
    r"by\s+(?:firm|company|state|classification|class|year|month|category|reason))\b",
    re.IGNORECASE,
)
_SAMPLE_REQUEST_EN_RE = re.compile(
    r"\b(show|find|list|sample|examples?|where|give me)\b",
    re.IGNORECASE,
)
_EXPLAIN_ZH_HINTS = ("是什么", "什么意思", "是什么意思", "这到底是什么", "解释", "定义", "概念", "意味着")
_COUNT_OR_CHART_ZH_HINTS = (
    "多少", "几个", "几次", "总数", "计数", "统计", "分布", "最多", "最常见",
    "排名", "排行", "趋势", "按公司", "按厂商", "按州", "按分类", "哪几家", "哪些公司",
)
_SAMPLE_REQUEST_ZH_HINTS = ("给我看", "看几个", "找", "查找", "列出", "示例", "例子", "样本")
_CLASS_SPECIFIC_RE = re.compile(r"\bclass\s*(?P<class>i{1,3}|1|2|3)\b", re.IGNORECASE)
_SIMPLE_CLASS_COUNT_RE = re.compile(
    r"\bhow\s+many\b.*\bclass\s*(?P<class>i{1,3}|1|2|3)\b.*\brecalls?\b",
    re.IGNORECASE,
)
_RAW_EXPOSURE_CONTEXT_RE = re.compile(
    r"\b(?:recall|recalls|recalling|exposure|severity[-\s]?weighted)\b",
    re.IGNORECASE,
)
_RAW_EXPOSURE_EN_PATTERNS = (
    re.compile(r"\bwhich\s+(?:raw\s+)?(?:recalling\s+)?(?:firms|companies)\b.*\bmost\b", re.I),
    re.compile(r"\btop\s*\d*\s+(?:raw\s+)?(?:recalling\s+)?(?:firms|companies)\b", re.I),
    re.compile(r"\brank(?:ing)?\s+(?:raw\s+)?(?:recalling\s+)?(?:firms|companies)\b", re.I),
    re.compile(r"\b(?:firms|companies)\b.*\bby\s+(?:recall\s+count|severity-weighted|exposure)\b", re.I),
    re.compile(r"\b(?:recall\s+exposure|risk\s+exposure)\b.*\b(?:firms|companies)\b", re.I),
)
_RAW_EXPOSURE_ZH_HINTS = (
    "哪些公司召回最多",
    "哪家公司召回最多",
    "哪个公司召回最多",
    "哪些厂商召回最多",
    "哪个厂商召回最多",
    "召回最多的公司",
    "召回最多的厂商",
    "公司召回排名",
    "厂商召回排名",
    "公司召回排行",
    "厂商召回排行",
    "哪个公司风险暴露最高",
    "哪家公司风险暴露最高",
    "哪个公司暴露最高",
    "哪家公司暴露最高",
)
_PARENT_GROUP_EXPOSURE_EN_PATTERNS = (
    re.compile(r"\b(?:parent\s+(?:companies|company|groups?|organi[sz]ations?)|company\s+groups?)\b", re.I),
    re.compile(r"\bnormalized\s+(?:company|firm|parent).*\b(?:exposure|recalls?|ranking|leaderboard)\b", re.I),
    re.compile(r"\b(?:exposure|recalls?|ranking|leaderboard)\b.*\b(?:parent\s+(?:companies|company|groups?)|company\s+groups?)\b", re.I),
)
_PARENT_GROUP_EXPOSURE_ZH_HINTS = (
    "母公司",
    "公司集团",
    "企业集团",
    "集团召回",
    "集团暴露",
    "集团排名",
    "归一化公司",
    "公司归并",
)
_RAW_EXPOSURE_WEIGHTED_HINTS = (
    "severity-weighted",
    "severity weighted",
    "weighted",
    "exposure",
    "risk exposure",
    "暴露",
    "风险暴露",
    "严重度",
    "加权",
)
_US_STATE_NAMES = (
    "alabama", "alaska", "arizona", "arkansas", "california", "colorado", "connecticut",
    "delaware", "florida", "georgia", "hawaii", "idaho", "illinois", "indiana", "iowa",
    "kansas", "kentucky", "louisiana", "maine", "maryland", "massachusetts", "michigan",
    "minnesota", "mississippi", "missouri", "montana", "nebraska", "nevada",
    "new hampshire", "new jersey", "new mexico", "new york", "north carolina",
    "north dakota", "ohio", "oklahoma", "oregon", "pennsylvania", "rhode island",
    "south carolina", "south dakota", "tennessee", "texas", "utah", "vermont",
    "virginia", "washington", "west virginia", "wisconsin", "wyoming",
)
_US_STATE_CODES = (
    "AL", "AK", "AZ", "AR", "CA", "CO", "CT", "DE", "FL", "GA", "HI", "ID", "IL", "IN",
    "IA", "KS", "KY", "LA", "ME", "MD", "MA", "MI", "MN", "MS", "MO", "MT", "NE", "NV",
    "NH", "NJ", "NM", "NY", "NC", "ND", "OH", "OK", "OR", "PA", "RI", "SC", "SD", "TN",
    "TX", "UT", "VT", "VA", "WA", "WV", "WI", "WY",
)
_US_STATE_NAME_TO_CODE = dict(zip(_US_STATE_NAMES, _US_STATE_CODES, strict=True))
_COUNTRY_ALIASES = {
    "canada": "Canada",
    "united states": "United States",
    "usa": "United States",
    "u.s.": "United States",
    "u.s": "United States",
    "us": "United States",
    "india": "India",
    "china": "China",
    "mexico": "Mexico",
    "jordan": "Jordan",
    "germany": "Germany",
    "spain": "Spain",
    "guyana": "Guyana",
    "turkey": "Turkey",
    "guatemala": "Guatemala",
    "japan": "Japan",
    "korea": "Korea (the Republic of)",
    "south korea": "Korea (the Republic of)",
    "united arab emirates": "United Arab Emirates",
    "uae": "United Arab Emirates",
    "switzerland": "Switzerland",
    "aruba": "Aruba",
    "australia": "Australia",
    "croatia": "Croatia",
    "ireland": "Ireland",
    "israel": "Israel",
    "taiwan": "Taiwan",
    "united kingdom": "United Kingdom",
    "uk": "United Kingdom",
    "dominican republic": "Dominican Republic (the)",
}
_COUNTRY_ALIAS_RE = re.compile(
    r"(?<![a-z0-9.])(?:the\s+)?("
    + "|".join(re.escape(name) for name in sorted(_COUNTRY_ALIASES, key=len, reverse=True))
    + r")(?![a-z0-9.])",
    re.IGNORECASE,
)
_STATE_NAME_RE = re.compile(
    r"\b(" + "|".join(re.escape(name) for name in _US_STATE_NAMES) + r")\b",
    re.IGNORECASE,
)
_STATE_CODE_RE = re.compile(r"\b(" + "|".join(_US_STATE_CODES) + r")\b")
_LOCATION_PHRASE_RE = re.compile(
    r"\b(?:in|from|country(?:\s+of)?|located\s+in)\s+(?P<phrase>[^?.!;:]+)",
    re.IGNORECASE,
)
_LOCATION_PHRASE_STOP_RE = re.compile(
    r"\b(?:have|has|had|were|are|is|there|with|by|for|about|rank|ranking|top|most|"
    r"highest|lowest|recalls?|drug|fda|companies?|firms?)\b",
    re.IGNORECASE,
)
_STATUS_ALIASES = {
    "completed": "Completed",
    "ongoing": "Ongoing",
    "terminated": "Terminated",
    "pending": "Pending",
}
_STATUS_FILTER_RE = re.compile(
    r"\b(" + "|".join(re.escape(name) for name in _STATUS_ALIASES) + r")\b",
    re.IGNORECASE,
)
_NEGATED_STATUS_FILTER_RE = re.compile(
    r"\b(?:not|except|excluding|without)\s+("
    + "|".join(re.escape(name) for name in _STATUS_ALIASES)
    + r")\b|\bnon[-\s]?("
    + "|".join(re.escape(name) for name in _STATUS_ALIASES)
    + r")\b",
    re.IGNORECASE,
)
_BARE_YEAR_RE = re.compile(r"(?<![\d/-])((?:19|20)\d{2})(?![\d/-])")
_UNSUPPORTED_TEMPORAL_FILTER_RE = re.compile(
    r"\b(?:since|after|before|between|during|last|past|recent|current|today|yesterday|"
    r"this\s+(?:year|quarter|month|week)|"
    r"(?:jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec)[a-z]*|"
    r"q[1-4]|quarter|month|week|day)\b|"
    r"\b\d{4}[-/]\d{1,2}(?:[-/]\d{1,2})?\b",
    re.IGNORECASE,
)
_UNHANDLED_SUBJECT_FILTER_RE = re.compile(
    r"\b(?:for|about|involving|related\s+to|made\s+by|manufactured\s+by)\s+"
    r"(?!the\s+most\b)(?!most\b)(?!fda\b)(?!drug\b)(?!recalls?\b)(?!class\b)(?!\d{4}\b)|"
    r"\b(?:containing|contains|including|includes)\s+\w+|"
    r"\brecalls?\s+of\s+(?!class\b)(?!fda\b)(?!drug\b)(?!\d{4}\b)|"
    r"\bwith\s+(?:product|brand|drug|medication)\b|"
    r"\b(?:product|brand)\s+(?:named|called|containing)\b",
    re.IGNORECASE,
)
_OUT_OF_SCOPE_PRODUCT_HINT_RE = re.compile(
    r"\b(?:cars?|vehicles?|automobiles?|toyota|honda|ford|tesla)\b",
    re.IGNORECASE,
)
_RAW_EXPOSURE_TOPIC_EXCLUSIONS = (
    "sterility",
    "sterile",
    "cgmp",
    "gmp",
    "contamination",
    "microbial",
    "glass",
    "nitrosamine",
    "subpotent",
    "superpotent",
    "labeling",
    "deviation",
    "recall reason",
    "recall reasons",
    "原因",
)
_SIMPLE_CLASS_COUNT_TOPIC_EXCLUSIONS = _RAW_EXPOSURE_TOPIC_EXCLUSIONS + (
    "product",
    "products",
    "brand",
    "brands",
    "firm",
    "firms",
    "company",
    "companies",
    "厂商",
    "公司",
    "产品",
    "品牌",
)
_HELPER_BOILERPLATE_TOKENS = {
    "all",
    "an",
    "and",
    "are",
    "by",
    "companies",
    "company",
    "count",
    "drug",
    "exposure",
    "fda",
    "five",
    "firm",
    "firms",
    "from",
    "group",
    "groups",
    "had",
    "has",
    "have",
    "been",
    "highest",
    "how",
    "in",
    "leaderboard",
    "many",
    "most",
    "normalized",
    "of",
    "or",
    "parent",
    "parents",
    "rank",
    "ranked",
    "ranking",
    "raw",
    "recall",
    "recalling",
    "recalls",
    "rollup",
    "rollups",
    "risk",
    "score",
    "scores",
    "severity",
    "state",
    "country",
    "located",
    "the",
    "there",
    "ten",
    "to",
    "top",
    "weighted",
    "were",
    "with",
    "which",
}
_TAXONOMY_EXPLANATION_ALIASES = {
    "cgmp_deviation": (
        "cgmp deviation",
        "cgmp deviations",
        "gmp deviation",
        "gmp deviations",
        "cgmp/gmp",
        "c gmp",
        "current good manufacturing practice",
        "good manufacturing practice",
    ),
}


def _ascii_phrase(text: str) -> str:
    return " ".join(re.findall(r"[a-z0-9]+", text.casefold()))


def _looks_like_explanation_question(question: str) -> bool:
    return bool(_EXPLAIN_EN_RE.search(question)) or any(hint in question for hint in _EXPLAIN_ZH_HINTS)


def _asks_for_count_or_chart(question: str) -> bool:
    return bool(_COUNT_OR_CHART_EN_RE.search(question)) or any(
        hint in question for hint in _COUNT_OR_CHART_ZH_HINTS
    )


def _asks_for_sample(question: str) -> bool:
    return bool(_SAMPLE_REQUEST_EN_RE.search(question)) or any(
        hint in question for hint in _SAMPLE_REQUEST_ZH_HINTS
    )


def _taxonomy_aliases(node_id: str, label: str) -> list[str]:
    aliases = [node_id.replace("_", " "), label]
    aliases.extend(_TAXONOMY_EXPLANATION_ALIASES.get(node_id, ()))
    for item in list(aliases):
        norm = _ascii_phrase(item)
        if norm and not norm.endswith("s"):
            aliases.append(f"{item}s")
    return aliases


def _maybe_taxonomy_explanation_spec(
    question: str,
    nodes: list[tuple[str, str, str, int]],
) -> QuerySpec | None:
    if not _looks_like_explanation_question(question) or _asks_for_count_or_chart(question):
        return None
    q = _ascii_phrase(question)
    best: tuple[int, str] | None = None
    for node_id, label, _definition, _level in nodes:
        for alias in _taxonomy_aliases(node_id, label):
            normalized = _ascii_phrase(alias)
            if len(normalized) >= 4 and normalized in q:
                score = len(normalized)
                if best is None or score > best[0]:
                    best = (score, node_id)
    if best is None:
        return None
    return QuerySpec(intent=Intent.explain_taxonomy_node, taxonomy_node_id=best[1])


def _extract_limit(question: str, default: int = 20) -> int:
    q = question.casefold()
    match = re.search(r"\btop\s*(\d{1,3})\b", q)
    if match:
        return max(1, min(int(match.group(1)), 100))
    match = re.search(r"前\s*(\d{1,3})", question)
    if match:
        return max(1, min(int(match.group(1)), 100))
    if "前十" in question or "top ten" in q:
        return 10
    if "前五" in question or "top five" in q:
        return 5
    return default


def _unique_preserve_order(values: list[str]) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for value in values:
        key = value.casefold()
        if key not in seen:
            seen.add(key)
            out.append(value)
    return out


def _multi_value_filter(column: str, values: list[str]) -> FilterSpec | None:
    values = _unique_preserve_order([value for value in values if value])
    if not values:
        return None
    op = Op.eq if len(values) == 1 else Op.in_
    return FilterSpec(column=column, op=op, values=values)


def _status_filter_spec_or_defer(question: str) -> tuple[FilterSpec | None, bool]:
    if _NEGATED_STATUS_FILTER_RE.search(question):
        return None, True
    values = [
        _STATUS_ALIASES[match.group(1).casefold()]
        for match in _STATUS_FILTER_RE.finditer(question)
    ]
    return _multi_value_filter("status", values), False


def _date_filter_spec_or_defer(question: str) -> tuple[FilterSpec | None, bool]:
    if _UNSUPPORTED_TEMPORAL_FILTER_RE.search(question):
        return None, True
    years = _unique_preserve_order(list(_BARE_YEAR_RE.findall(question)))
    if len(years) > 1:
        return None, True
    if not years:
        return None, False
    year = years[0]
    return FilterSpec(
        column="report_date",
        op=Op.between,
        values=[f"{year}-01-01", f"{year}-12-31"],
    ), False


def _state_codes_in_text(text: str) -> list[str]:
    values: list[str] = []
    for match in _STATE_NAME_RE.finditer(text):
        code = _US_STATE_NAME_TO_CODE.get(match.group(1).casefold())
        if code:
            values.append(code)
    for match in _STATE_CODE_RE.finditer(text):
        values.append(match.group(1).upper())
    return _unique_preserve_order(values)


def _country_values_in_text(text: str) -> list[str]:
    values: list[str] = []
    for match in _COUNTRY_ALIAS_RE.finditer(text):
        value = _COUNTRY_ALIASES.get(match.group(1).casefold())
        if value:
            values.append(value)
    return _unique_preserve_order(values)


def _trim_location_phrase(phrase: str) -> str:
    phrase = _LOCATION_PHRASE_STOP_RE.split(phrase, maxsplit=1)[0]
    return phrase.strip(" \t\r\n,")


def _location_remainder(phrase: str) -> str:
    remainder = _COUNTRY_ALIAS_RE.sub(" ", phrase)
    remainder = _STATE_NAME_RE.sub(" ", remainder)
    remainder = _STATE_CODE_RE.sub(" ", remainder)
    remainder = re.sub(r"\b(?:the|and|or)\b", " ", remainder, flags=re.IGNORECASE)
    remainder = re.sub(r"[,/&()+-]", " ", remainder)
    return " ".join(re.findall(r"[a-z]+", remainder.casefold()))


def _location_filter_specs_or_defer(question: str) -> tuple[list[FilterSpec], bool]:
    country_values: list[str] = []
    state_codes: list[str] = []
    saw_location_phrase = False
    for match in _LOCATION_PHRASE_RE.finditer(question):
        phrase = _trim_location_phrase(match.group("phrase"))
        if not phrase:
            continue
        # "in 2024" is a temporal filter handled by _date_filter_spec_or_defer, not a
        # location phrase. Do not treat numeric-only phrases as unknown locations.
        if not re.search(r"[a-z]", phrase, re.IGNORECASE):
            continue
        saw_location_phrase = True
        phrase_countries = _country_values_in_text(phrase)
        phrase_states = _state_codes_in_text(phrase)
        if _location_remainder(phrase):
            return [], True
        # Mixing state and country in one deterministic pre-route can mean either an AND
        # ("California, United States") or an OR ("California and Canada"). Defer rather
        # than risk changing the user's location semantics.
        if phrase_countries and phrase_states:
            return [], True
        country_values.extend(phrase_countries)
        state_codes.extend(phrase_states)

    # Bare location adjectives before the recall noun are hard filters too:
    # "Canada drug recalls", "California FDA drug recalls", "CA Class I recalls".
    # Preserve them exactly instead of letting unmatched-token cleanup hide them.
    country_values.extend(_country_values_in_text(question))
    state_codes.extend(_state_codes_in_text(question))
    country_values = _unique_preserve_order(country_values)
    state_codes = _unique_preserve_order(state_codes)

    # Mixed country+state can be an AND ("California, United States") or an OR/list
    # ("California and Canada"). Deterministic helpers cannot infer that safely; defer.
    if country_values and state_codes:
        return [], True

    filters: list[FilterSpec] = []
    country_filter = _multi_value_filter("country", country_values)
    if country_filter is not None:
        filters.append(country_filter)
    state_filter = _multi_value_filter("state", state_codes)
    if state_filter is not None:
        filters.append(state_filter)
    if saw_location_phrase and not filters:
        return [], True
    return filters, False


def _safe_hard_filter_specs_or_defer(question: str) -> tuple[list[FilterSpec], bool]:
    """Parse hard filters the deterministic fast paths can represent exactly.

    Any date/status/location wording outside this deliberately small grammar returns
    ``defer=True`` so the LLM QuerySpec path can preserve it instead of a helper issuing
    unfiltered or partially-filtered SQL.
    """
    filters: list[FilterSpec] = []
    status_filter, defer = _status_filter_spec_or_defer(question)
    if defer:
        return [], True
    if status_filter is not None:
        filters.append(status_filter)
    date_filter, defer = _date_filter_spec_or_defer(question)
    if defer:
        return [], True
    if date_filter is not None:
        filters.append(date_filter)
    location_filters, defer = _location_filter_specs_or_defer(question)
    if defer:
        return [], True
    filters.extend(location_filters)
    return filters, False


def _looks_like_unhandled_subject_filter(
    question: str,
    emitted_filters: list[FilterSpec] | None = None,
) -> bool:
    return bool(
        _UNHANDLED_SUBJECT_FILTER_RE.search(question)
        or _OUT_OF_SCOPE_PRODUCT_HINT_RE.search(question)
        or _unmatched_subject_tokens(question, emitted_filters)
    )


def _filter_values(emitted_filters: list[FilterSpec] | None, column: str) -> list[str]:
    values: list[str] = []
    for filter_spec in emitted_filters or []:
        if filter_spec.column == column:
            values.extend(filter_spec.values)
    return _unique_preserve_order(values)


def _remove_words(text: str, words: list[str]) -> str:
    for word in words:
        if word:
            text = re.sub(r"\b" + re.escape(word.casefold()) + r"\b", " ", text, flags=re.IGNORECASE)
    return text


def _unmatched_subject_tokens(
    question: str,
    emitted_filters: list[FilterSpec] | None = None,
) -> list[str]:
    """Return likely product/ingredient tokens left after known hard filters are removed.

    Deterministic helpers do not represent product/ingredient constraints. If a prompt leaves
    non-boilerplate tokens after removing the class/status/date/location filters that the helper
    can preserve, the safe behavior is to defer to the LLM QuerySpec path rather than answering a
    broader SQL question.
    """
    text = question.casefold()
    if _filter_values(emitted_filters, "classification"):
        text = _CLASS_SPECIFIC_RE.sub(" ", text)
    text = _remove_words(text, [value.casefold() for value in _filter_values(emitted_filters, "status")])
    for country in _filter_values(emitted_filters, "country"):
        aliases = [alias for alias, value in _COUNTRY_ALIASES.items() if value == country]
        text = _remove_words(text, aliases)
    for state_code in _filter_values(emitted_filters, "state"):
        state_names = [
            name for name, code in _US_STATE_NAME_TO_CODE.items()
            if code == state_code
        ]
        text = _remove_words(text, [state_code, *state_names])
    for filter_spec in emitted_filters or []:
        if filter_spec.column.endswith("_date") and filter_spec.values:
            years = [value[:4] for value in filter_spec.values if re.match(r"^(?:19|20)\d{2}", value)]
            text = _remove_words(text, years)
    tokens = re.findall(r"[a-z][a-z0-9]*", text)
    return [
        token
        for token in tokens
        if token not in _HELPER_BOILERPLATE_TOKENS
        and len(token) > 1
    ]


def _maybe_parent_group_exposure_spec(question: str) -> QuerySpec | None:
    """Deterministically catch explicit parent-group exposure wording.

    Generic "which companies" questions stay on the transparent raw FDA-name view unless
    the user asks for a parent company/group/normalized rollup.
    """
    has_parent_scope = any(pattern.search(question) for pattern in _PARENT_GROUP_EXPOSURE_EN_PATTERNS)
    has_parent_scope = has_parent_scope or any(hint in question for hint in _PARENT_GROUP_EXPOSURE_ZH_HINTS)
    if not has_parent_scope:
        return None
    if not (_RAW_EXPOSURE_CONTEXT_RE.search(question) or any(hint in question for hint in _RAW_EXPOSURE_ZH_HINTS)):
        return None
    q = question.casefold()
    metric = (
        ExposureMetric.severity_weighted
        if any(hint in q or hint in question for hint in _RAW_EXPOSURE_WEIGHTED_HINTS)
        else ExposureMetric.recall_count
    )
    filters: list[FilterSpec] = []
    class_label = _class_filter_label(question)
    if class_label:
        filters.append(FilterSpec(column="classification", op=Op.eq, values=[class_label]))
    hard_filters, defer = _safe_hard_filter_specs_or_defer(question)
    if defer:
        return None
    filters.extend(hard_filters)
    if _looks_like_unhandled_subject_filter(question, filters):
        return None
    if any(hint in q for hint in _RAW_EXPOSURE_TOPIC_EXCLUSIONS):
        return None
    return QuerySpec(
        intent=Intent.parent_group_exposure,
        exposure_metric=metric,
        filters=filters,
        limit=_extract_limit(question),
    )


def _maybe_raw_firm_exposure_spec(question: str) -> QuerySpec | None:
    """Deterministically catch the v0 raw-firm exposure wording before generic QuerySpec.

    This avoids relying on the model to discover the new intent while preserving the older
    filtered/class-specific ``count_by recalling_firm`` paths. The helper either preserves
    every hard filter it recognizes exactly (status, one bare report year, country/countries,
    state, classification) or returns ``None`` so the LLM-generated QuerySpec can handle the
    prompt instead of this pre-router fabricating an unfiltered/global leaderboard.
    """
    q = question.casefold()
    if not (_RAW_EXPOSURE_CONTEXT_RE.search(question) or any(hint in question for hint in _RAW_EXPOSURE_ZH_HINTS)):
        return None
    metric = (
        ExposureMetric.severity_weighted
        if any(hint in q or hint in question for hint in _RAW_EXPOSURE_WEIGHTED_HINTS)
        else ExposureMetric.recall_count
    )
    filters: list[FilterSpec] = []
    class_label = _class_filter_label(question)
    if class_label:
        # Preserve class-specific constraints only for explicit exposure/severity wording.
        # Plain "Which firms had the most Class I recalls?" stays on the legacy count_by path.
        if metric is not ExposureMetric.severity_weighted:
            return None
        filters.append(FilterSpec(column="classification", op=Op.eq, values=[class_label]))
    hard_filters, defer = _safe_hard_filter_specs_or_defer(question)
    if defer:
        return None
    filters.extend(hard_filters)
    if _looks_like_unhandled_subject_filter(question, filters):
        return None
    if any(hint in q for hint in _RAW_EXPOSURE_TOPIC_EXCLUSIONS):
        return None
    matched = any(pattern.search(question) for pattern in _RAW_EXPOSURE_EN_PATTERNS)
    matched = matched or any(hint in question for hint in _RAW_EXPOSURE_ZH_HINTS)
    if not matched:
        return None
    return QuerySpec(
        intent=Intent.raw_firm_exposure,
        exposure_metric=metric,
        filters=filters,
        limit=_extract_limit(question),
    )


def _class_filter_label(question: str) -> str | None:
    match = _CLASS_SPECIFIC_RE.search(question)
    if not match:
        return None
    class_token = match.group("class").casefold()
    labels = {
        "i": "Class I",
        "1": "Class I",
        "ii": "Class II",
        "2": "Class II",
        "iii": "Class III",
        "3": "Class III",
    }
    return labels.get(class_token)


def _maybe_simple_class_count_spec(question: str) -> QuerySpec | None:
    """Deterministic fast path for simple "How many Class X recalls" questions.

    The LLM occasionally adds unrelated fields such as date grains or sample sizes to this
    canonical count question. Keep the eval-stable, SQL-only interpretation explicit, but
    only after the domain guard has accepted the prompt and only when every hard constraint
    is exactly represented. Unknown subject/product/location/time qualifiers defer to the
    LLM QuerySpec path.
    """
    match = _SIMPLE_CLASS_COUNT_RE.search(question)
    if not match:
        return None
    q = question.casefold()
    if any(word in q for word in (" by ", " trend", " distribution", "breakdown", "most", "top", "rank")):
        return None
    if any(hint in q for hint in _SIMPLE_CLASS_COUNT_TOPIC_EXCLUSIONS):
        return None
    label = _class_filter_label(question)
    if label is None:
        return None
    filters = [FilterSpec(column="classification", op=Op.eq, values=[label])]
    hard_filters, defer = _safe_hard_filter_specs_or_defer(question)
    if defer:
        return None
    filters.extend(hard_filters)
    if _looks_like_unhandled_subject_filter(question, filters):
        return None
    return QuerySpec(
        intent=Intent.count_total,
        filters=filters,
    )


def _valid_taxonomy_ids(a: RecallAnalytics, version: str = TAXONOMY_VERSION) -> set[str]:
    """node_ids actually present in recall_label for a version (the only ids with exact counts)."""
    with a.conn.cursor() as cur:
        cur.execute("SELECT DISTINCT node_id FROM recall_label WHERE version = %s", [version])
        return {r[0] for r in cur.fetchall()}


def _load_taxonomy_node(
    a: RecallAnalytics,
    node_id: str,
    version: str = TAXONOMY_VERSION,
) -> TaxonomyNodeInfo:
    with a.conn.cursor() as cur:
        cur.execute(
            """
            SELECT
                t.version, t.node_id, t.parent_id, t.label, t.definition, t.examples,
                t.level, t.status, p.label AS parent_label, p.definition AS parent_definition
            FROM taxonomy t
            LEFT JOIN taxonomy p
                ON p.version = t.version AND p.node_id = t.parent_id
            WHERE t.version = %s AND t.node_id = %s
            """,
            [version, node_id],
        )
        row = cur.fetchone()
    if row is None:
        raise ValueError(f"unknown taxonomy_node_id {node_id!r}")
    return TaxonomyNodeInfo(
        version=row[0],
        node_id=row[1],
        parent_id=row[2],
        label=row[3],
        definition=row[4],
        examples=list(row[5] or []),
        level=int(row[6]),
        status=row[7],
        parent_label=row[8],
        parent_definition=row[9],
    )


def _taxonomy_recall_examples(
    a: RecallAnalytics,
    node_id: str,
    *,
    version: str = TAXONOMY_VERSION,
    limit: int = 3,
) -> list[dict[str, Any]]:
    with a.conn.cursor() as cur:
        cur.execute(
            """
            SELECT de.recall_number, de.classification, de.reason_for_recall
            FROM drug_enforcement de
            WHERE EXISTS (
                SELECT 1 FROM recall_label rl
                WHERE rl.record_id = de.id
                  AND rl.version = %s
                  AND rl.node_id = %s
            )
            ORDER BY de.recall_initiation_date DESC NULLS LAST, de.recall_number
            LIMIT %s
            """,
            [version, node_id, limit],
        )
        rows = cur.fetchall()
    return [
        {
            "recall_number": row[0],
            "classification": row[1],
            "reason_for_recall": row[2],
        }
        for row in rows
    ]


_EXPLAIN_TAXONOMY_SYSTEM = """You answer FDA drug-recall taxonomy explanation questions.
Rules:
- Answer in the same language as the user.
- Ground the category meaning in the provided TAXONOMY NODE. Use example recall reasons only as examples.
- You may expand standard regulatory abbreviations when relevant: cGMP means current Good Manufacturing
  Practice; GMP means Good Manufacturing Practice.
- Explain what the category usually signals in drug manufacturing/quality context.
- Do not produce counts, rankings, distributions, chart language, or safety verdicts.
- Keep the answer concise and user-friendly."""


def _clip(text: str | None, max_chars: int = 260) -> str:
    if not text:
        return ""
    text = " ".join(text.split())
    return f"{text[:max_chars - 1].rstrip()}…" if len(text) > max_chars else text


def _explain_taxonomy_node(
    a: RecallAnalytics,
    spec: QuerySpec,
    question: str,
    chat_client: Any,
    chat_config: llm.ChatConfig,
) -> TaxonomyExplanation:
    if not spec.taxonomy_node_id:
        raise ValueError("explain_taxonomy_node needs taxonomy_node_id")
    valid = _valid_taxonomy_ids(a)
    if spec.taxonomy_node_id not in valid:
        raise ValueError(
            f"unknown taxonomy_node_id {spec.taxonomy_node_id!r}; valid ids: {sorted(valid)}")
    node = _load_taxonomy_node(a, spec.taxonomy_node_id)
    examples = _taxonomy_recall_examples(a, node.node_id, limit=min(max(spec.limit or 3, 1), 5))
    example_lines = "\n".join(
        f"- {item['recall_number']} ({item['classification'] or 'unclassified'}): "
        f"{_clip(item['reason_for_recall'])}"
        for item in examples
    ) or "- (No local example recall reasons found.)"
    taxonomy_examples = ", ".join(node.examples) if node.examples else "(none listed)"
    parent = (
        f"{node.parent_id} — {node.parent_label}: {node.parent_definition}"
        if node.parent_id and node.parent_label else "(none)"
    )
    prompt = f"""User question:
{question}

TAXONOMY NODE:
- version: {node.version}
- node_id: {node.node_id}
- label: {node.label}
- definition: {node.definition}
- parent: {parent}
- taxonomy examples: {taxonomy_examples}

LOCAL EXAMPLE RECALL REASONS:
{example_lines}

Write the answer now."""
    answer = llm.chat_completion_text(
        chat_client,
        chat_config,
        [
            {"role": "system", "content": _EXPLAIN_TAXONOMY_SYSTEM},
            {"role": "user", "content": prompt},
        ],
        temperature=0,
        max_tokens=700,
    ).strip()
    if not answer:
        raise ValueError("empty taxonomy explanation")
    return TaxonomyExplanation(node=node, answer=answer, examples=examples)


# --------------------------------------------------------------------------- #
# LLM call + validation + execution
# --------------------------------------------------------------------------- #
def generate_spec(client: Any, config: llm.ChatConfig, question: str,
                  schema_ctx: str, taxonomy_ctx: str = "") -> QuerySpec:
    system = f"{SYSTEM}\n\nSCHEMA:\n{schema_ctx}"
    if taxonomy_ctx:
        system += f"\n\nTAXONOMY:\n{taxonomy_ctx}"
    spec = llm.structured_completion(
        client,
        config,
        [{"role": "system", "content": system},
         {"role": "user", "content": question}],
        QuerySpec,
        temperature=0,
    )
    return refine_spec(spec, question=question)


def refine_spec(spec: QuerySpec, question: str = "") -> QuerySpec:
    """Light, general hygiene on the model's QuerySpec -- no hardcoded concept rules.

    The LLM judges intent and emits the canonical semantic_query plus keyword aliases; here we
    only normalize whitespace and de-duplicate aliases.
    """
    spec.limit = max(1, min(spec.limit or 20, 100))
    if spec.intent not in {Intent.raw_firm_exposure, Intent.parent_group_exposure}:
        spec.exposure_metric = None
    # Exact taxonomy path (category filter or category distribution) is deterministic and wins
    # over semantic estimation -- never mix it with a semantic_query.
    if spec.taxonomy_node_id is not None:
        spec.taxonomy_node_id = spec.taxonomy_node_id.strip() or None
    if spec.intent is Intent.explain_taxonomy_node:
        spec.semantic_query = None
        spec.semantic_aliases = []
        spec.filters = []
        spec.group_by = None
        spec.grain = None
        spec.date_column = None
        return spec
    if spec.intent in {Intent.raw_firm_exposure, Intent.parent_group_exposure}:
        spec.semantic_query = None
        spec.semantic_aliases = []
        spec.group_by = None
        spec.taxonomy_node_id = None
        spec.grain = None
        spec.date_column = None
        spec.exposure_metric = spec.exposure_metric or ExposureMetric.severity_weighted
        return spec
    if spec.taxonomy_node_id or spec.intent is Intent.count_by_taxonomy:
        spec.semantic_query = None
        spec.semantic_aliases = []
        return spec
    if spec.semantic_query is not None:
        spec.semantic_query = spec.semantic_query.strip() or None
    if not spec.semantic_query:
        spec.semantic_aliases = []
        return spec
    if (
        spec.intent in {Intent.count_total, Intent.count_by}
        and question
        and _asks_for_sample(question)
        and not _asks_for_count_or_chart(question)
    ):
        spec.intent = Intent.sample
        spec.group_by = None
    concept = spec.semantic_query.casefold()
    seen: set[str] = set()
    aliases: list[str] = []
    for alias in spec.semantic_aliases:
        alias = alias.strip()
        key = alias.casefold()
        if alias and key != concept and key not in seen:
            seen.add(key)
            aliases.append(alias)
    spec.semantic_aliases = aliases
    return spec


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


_REASON_TOPN_HINTS = (
    "reason", "reasons", "recall reason", "recall reasons", "原因", "召回原因",
)
_PRODUCT_TOPN_HINTS = (
    "product", "products", "product_description", "产品", "药品", "旗下",
)
_TOPN_HINTS = (
    "top", "most", "common", "rank", "ranking", "table", "tables",
    "最多", "最高", "前十", "前 10", "top 10", "排名", "排行", "表", "分别",
)


def _has_firm_filter(spec: QuerySpec) -> bool:
    return any(
        f.column == "recalling_firm"
        and f.op in {Op.eq, Op.in_, Op.ilike}
        and bool(f.values)
        for f in spec.filters
    )


def _mentions_any(question: str, hints: tuple[str, ...]) -> bool:
    q = question.casefold()
    return any(hint.casefold() in q for hint in hints)


def _asks_for_firm_reason_product_topn(question: str, spec: QuerySpec) -> bool:
    if spec.semantic_query or not _has_firm_filter(spec):
        return False
    if not _mentions_any(question, _REASON_TOPN_HINTS):
        return False
    if not _mentions_any(question, _PRODUCT_TOPN_HINTS):
        return False
    return _mentions_any(question, _TOPN_HINTS) or spec.intent in {
        Intent.count_by,
        Intent.count_by_taxonomy,
    }


def _copy_filter_specs(spec: QuerySpec) -> list[FilterSpec]:
    return [f.model_copy(deep=True) for f in spec.filters]


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
    chat_client: Any,
    chat_config: llm.ChatConfig,
    embed_client: Any | None,
    embed_config: llm.EmbeddingConfig,
    embedding_error: BaseException | None,
    filters: list[Filter],
) -> validation.SemanticCountResult:
    if not spec.semantic_query:
        raise ValueError("semantic count needs semantic_query")
    if spec.intent is Intent.count_by and (not spec.group_by or spec.group_by not in CATALOG):
        raise ValueError("count_by needs a valid group_by dimension")
    validation_limit = validation.bounded_validation_limit(spec.semantic_k)
    retrieval_pool_limit = validation.bounded_retrieval_pool_limit(validation_limit)
    hits = retrieval.search(
        a.conn,
        embed_client,
        spec.semantic_query,
        k=retrieval_pool_limit,
        field="both",
        filters=filters,
        embed_config=embed_config,
        embedding_error=embedding_error,
        fts_queries=spec.semantic_aliases,
    )
    fallback_reason = getattr(hits, "embedding_fallback_reason", None) or (
        hits[0].embedding_fallback_reason
        if hits else (
            type(embedding_error).__name__
            if embedding_error is not None and llm.can_fallback_to_fts(embedding_error)
            else None
        )
    )
    retrieval_mode = (
        getattr(hits, "retrieval_mode", None)
        or (hits[0].retrieval_mode if hits else ("fts_only" if fallback_reason else "hybrid"))
    )
    eligible_hits = list(hits) if retrieval_mode == "fts_only" else [
        hit for hit in hits
        if hit.retrieval_score >= validation.MIN_RETRIEVAL_SCORE
    ]
    sample_hits = validation.select_validation_sample(eligible_hits, validation_limit)
    validations = validation.validate_hits(
        chat_client,
        chat_config,
        spec.semantic_query,
        sample_hits,
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
        retrieval_mode=retrieval_mode,
        embedding_fallback_reason=fallback_reason,
        group_by=spec.group_by if spec.intent is Intent.count_by else None,
        groups=groups,
    )


def _run_firm_reason_product_topn(a: RecallAnalytics, spec: QuerySpec) -> MultiSectionResult:
    filters = _to_filters(spec)
    limit = max(1, spec.limit or 10)
    filter_specs = _copy_filter_specs(spec)

    reason_spec = QuerySpec(
        intent=Intent.count_by_taxonomy,
        filters=filter_specs,
        limit=limit,
    )
    reason_groups = a.count_by_taxonomy(filters, limit=limit, with_evidence=True)
    reason_source = "taxonomy"
    reason_dimension = "recall_reason_category"
    if not reason_groups:
        reason_spec = QuerySpec(
            intent=Intent.count_by,
            filters=filter_specs,
            group_by="reason_for_recall",
            limit=limit,
        )
        reason_groups = a.count_by("reason_for_recall", filters, limit=limit, with_evidence=True)
        reason_source = "raw_reason_for_recall_fallback"
        reason_dimension = "reason_for_recall"

    product_spec = QuerySpec(
        intent=Intent.count_by,
        filters=filter_specs,
        group_by="product_description",
        limit=limit,
    )
    product_groups = a.count_by("product_description", filters, limit=limit, with_evidence=True)

    return MultiSectionResult(
        intent="multi_count_by",
        sections=[
            ResultSection(
                id="top_recall_reason_categories",
                title=f"Top recall reason categories (top {min(len(reason_groups), limit)})",
                data_kind="distribution",
                dimension=reason_dimension,
                source=reason_source,
                spec=reason_spec,
                result=reason_groups,
                metadata={
                    "taxonomy_version": TAXONOMY_VERSION if reason_source == "taxonomy" else None,
                    "fallback": reason_source != "taxonomy",
                },
            ),
            ResultSection(
                id="top_recalled_products",
                title=f"Top recalled products (top {min(len(product_groups), limit)})",
                data_kind="distribution",
                dimension="product_description",
                source="drug_enforcement.product_description",
                spec=product_spec,
                result=product_groups,
            ),
        ],
    )


def _parent_group_exposure_result(
    spec: QuerySpec,
    leaderboard: RawFirmExposureLeaderboard,
) -> MultiSectionResult:
    metric_label = (
        "severity-weighted exposure score"
        if leaderboard.metric == "severity_weighted" else "recall count"
    )
    primary_count = (
        (lambda item: item.exposure_score)
        if leaderboard.metric == "severity_weighted"
        else (lambda item: item.total_recalls)
    )
    groups = [
        Group(
            value=item.recalling_firm,
            count=primary_count(item),
            evidence=list(item.evidence),
            label=item.recalling_firm,
            metadata={
                "rank": item.rank,
                "parent_group_id": item.metadata.get("parent_group_id"),
                "parent_group_name": item.metadata.get("parent_group_name", item.recalling_firm),
                "primary_metric": leaderboard.metric,
                "primary_value": primary_count(item),
                "exposure_score": item.exposure_score,
                "total_recalls": item.total_recalls,
                "class_i_recalls": item.class_i_recalls,
                "class_ii_recalls": item.class_ii_recalls,
                "class_iii_recalls": item.class_iii_recalls,
                "unclassified_recalls": item.unclassified_recalls,
                "member_firm_count": item.metadata.get("member_firm_count", 0),
                "member_breakdown": item.metadata.get("member_breakdown", []),
                "top_reason_category": item.top_reason_category,
                "top_reason_node_id": item.top_reason_node_id,
                "top_reason_count": item.top_reason_count,
            },
        )
        for item in leaderboard.items
    ]
    return MultiSectionResult(
        intent=Intent.parent_group_exposure.value,
        sections=[
            ResultSection(
                id="parent_group_exposure",
                title=f"Parent-group exposure by {metric_label} (top {len(groups)})",
                data_kind="distribution",
                dimension="parent_group",
                source="parent_group_exposure_v1",
                spec=spec,
                result=groups,
                metadata={
                    "metric": leaderboard.metric,
                    "metric_label": metric_label,
                    "formula_version": leaderboard.formula_version,
                    "formula": leaderboard.formula,
                    "scope": leaderboard.scope,
                    "caveats": list(leaderboard.caveats),
                    **leaderboard.metadata,
                },
            )
        ],
    )


def _run_question_spec(
    a: RecallAnalytics,
    spec: QuerySpec,
    chat_client: Any,
    chat_config: llm.ChatConfig,
    embed_client: Any | None,
    embed_config: llm.EmbeddingConfig,
    embedding_error: BaseException | None,
    question: str,
) -> Any:
    if _asks_for_firm_reason_product_topn(question, spec):
        return _run_firm_reason_product_topn(a, spec)
    if spec.intent is Intent.parent_group_exposure:
        filters = _to_filters(spec)
        rank_by = (
            "recall_count"
            if spec.exposure_metric is ExposureMetric.recall_count
            else "severity_weighted_exposure"
        )
        return _parent_group_exposure_result(
            spec,
            a.parent_group_exposure_leaderboard(
                filters,
                limit=spec.limit or 20,
                rank_by=rank_by,
            ),
        )
    return run_spec(
        a,
        spec,
        chat_client,
        chat_config,
        embed_client,
        embed_config,
        embedding_error,
        question,
    )


def run_spec(
    a: RecallAnalytics,
    spec: QuerySpec,
    chat_client: Any,
    chat_config: llm.ChatConfig,
    embed_client: Any | None,
    embed_config: llm.EmbeddingConfig,
    embedding_error: BaseException | None = None,
    question: str = "",
) -> Any:
    if spec.intent is Intent.explain_taxonomy_node:
        return _explain_taxonomy_node(a, spec, question, chat_client, chat_config)
    filters = _to_filters(spec)
    if spec.intent is Intent.parent_group_exposure:
        rank_by = (
            "recall_count"
            if spec.exposure_metric is ExposureMetric.recall_count
            else "severity_weighted_exposure"
        )
        return a.parent_group_exposure_leaderboard(
            filters,
            limit=spec.limit or 20,
            rank_by=rank_by,
        )
    if spec.intent is Intent.raw_firm_exposure:
        rank_by = (
            "recall_count"
            if spec.exposure_metric is ExposureMetric.recall_count
            else "severity_weighted_exposure"
        )
        return a.raw_firm_exposure_leaderboard(
            filters,
            limit=spec.limit or 20,
            rank_by=rank_by,
        )
    if spec.intent is Intent.count_by_taxonomy:  # exact distribution across recall-reason categories
        return a.count_by_taxonomy(filters, limit=spec.limit or 20, with_evidence=True)
    if spec.taxonomy_node_id:  # exact counts filtered to one recall-reason category
        valid = _valid_taxonomy_ids(a)
        if spec.taxonomy_node_id not in valid:
            raise ValueError(
                f"unknown taxonomy_node_id {spec.taxonomy_node_id!r}; valid ids: {sorted(valid)}")
        if spec.intent is Intent.count_total:
            return a.count_total(filters, taxonomy_node_id=spec.taxonomy_node_id)
        if spec.intent is Intent.count_by:
            if not spec.group_by or spec.group_by not in CATALOG:
                raise ValueError("count_by needs a valid group_by dimension")
            return a.count_by(
                spec.group_by, filters, limit=spec.limit or 20,
                with_evidence=True, taxonomy_node_id=spec.taxonomy_node_id)
        raise ValueError(f"taxonomy_node_id does not support intent {spec.intent.value!r}")
    if spec.semantic_query and spec.intent is Intent.sample:  # preserve existing semantic retrieval
        return retrieval.search(
            a.conn,
            embed_client,
            spec.semantic_query,
            k=spec.limit or 10,
            field="both",
            filters=filters,
            embed_config=embed_config,
            embedding_error=embedding_error,
            fts_queries=spec.semantic_aliases,
        )
    if spec.semantic_query and spec.intent in {Intent.count_total, Intent.count_by}:
        return _run_semantic_count(
            a,
            spec,
            chat_client,
            chat_config,
            embed_client,
            embed_config,
            embedding_error,
            filters,
        )
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
    if spec.intent is Intent.sample and not filters:
        raise ValueError("sample needs filters or semantic_query")
    return a.sample(filters, n=spec.limit or 5)


def _clean_text(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def _short_text(value: Any, *, limit: int = 150) -> str:
    text = _clean_text(value)
    if len(text) <= limit:
        return text
    return f"{text[:limit - 1].rstrip()}…"


def _append_unique(out: list[str], bullet: str | None) -> None:
    if bullet and bullet not in out:
        out.append(bullet)


def _final_highlights(bullets: list[str], *, limit: int = 6) -> list[str]:
    return [b for b in bullets if b][:limit]


def _mode_from_hits(result: Any, *, fallback: str = "hybrid") -> str:
    return (
        getattr(result, "retrieval_mode", None)
        or (result[0].retrieval_mode if result else fallback)
    )


def _fallback_reason_from_hits(result: Any) -> str | None:
    return (
        getattr(result, "embedding_fallback_reason", None)
        or (result[0].embedding_fallback_reason if result else None)
    )


def _degraded_highlight(mode: str, reason: str | None, *, empty: bool) -> str | None:
    if mode != "fts_only":
        return None
    reason_note = f" ({reason})" if reason else ""
    if empty:
        return (
            "Semantic vector retrieval is unavailable"
            f"{reason_note}; keyword fallback returned no rows, so this is not evidence "
            "that FDA has zero matching recalls."
        )
    return (
        "Semantic vector retrieval is unavailable"
        f"{reason_note}; these highlights summarize only the degraded keyword-fallback rows."
    )


def _counter_sentence(
    label: str,
    values: list[Any],
    *,
    scope: str,
    require_repeat: bool = False,
) -> str | None:
    cleaned = [_clean_text(v) for v in values if _clean_text(v)]
    counts = Counter(cleaned)
    if not counts:
        return None
    data_total = sum(counts.values())
    top = counts.most_common(3)
    repeated = [(value, count) for value, count in top if count > 1]
    if require_repeat and not repeated:
        return None
    if len(counts) == 1:
        value, count = top[0]
        return f"All {count} {scope} with {label} data show {label} = {value}."
    shown = repeated if repeated else top
    prefix = "Repeated" if repeated else "Most common"
    parts = ", ".join(f"{value} ({count}/{data_total})" for value, count in shown)
    return f"{prefix} {label} values in the {scope}: {parts}."


def _date_range_sentence(values: list[Any], *, scope: str) -> str | None:
    dates = sorted({_clean_text(v)[:10] for v in values if _clean_text(v)})
    if not dates:
        return None
    if len(dates) == 1:
        return f"All {scope} with date metadata share date {dates[0]}."
    return f"{scope.capitalize()} with date metadata span {dates[0]} to {dates[-1]}."


def _snippet_sentence(hits: list[Any], *, scope: str) -> str | None:
    snippets = [_short_text(getattr(hit, "content", ""), limit=150) for hit in hits]
    snippets = [snippet for snippet in snippets if snippet]
    if not snippets:
        return None
    counts = Counter(snippets)
    repeated = [(snippet, count) for snippet, count in counts.most_common(2) if count > 1]
    if repeated:
        parts = "; ".join(f"\"{snippet}\" ({count} rows)" for snippet, count in repeated)
        return f"Repeated matched text in the {scope}: {parts}."
    return f"Top matched evidence text starts with: \"{snippets[0]}\"."


def _raw_evidence_note(kind: str) -> str:
    if kind == "retrieval":
        return "Review the raw retrieval rows below for exact recall numbers, scores, and detail links."
    if kind == "semantic_count":
        return "Review the validated evidence rows below for exact recall numbers, snippets, and confidence scores."
    if kind == "multi_section":
        return "Review the tables below for the raw counts, sample recall evidence, and drilldown links."
    return "Review the raw rows below for exact recall numbers and source details."


def _retrieval_highlights(spec: QuerySpec, result: Any) -> list[str]:
    hits = list(result)
    mode = _mode_from_hits(result)
    fallback_reason = _fallback_reason_from_hits(result)
    bullets: list[str] = []
    _append_unique(bullets, _degraded_highlight(mode, fallback_reason, empty=not hits))
    if not hits:
        _append_unique(
            bullets,
            f"No raw retrieval rows were returned for '{spec.semantic_query}'.",
        )
        _append_unique(bullets, _raw_evidence_note("retrieval"))
        return _final_highlights(bullets)

    mode_label = "keyword fallback" if mode == "fts_only" else "hybrid vector+keyword retrieval"
    _append_unique(
        bullets,
        (
            f"For '{spec.semantic_query}', {mode_label} returned {len(hits)} ranked FDA recall "
            "rows; the patterns below describe only this returned evidence set."
        ),
    )
    _append_unique(
        bullets,
        _counter_sentence(
            "classification",
            [getattr(hit, "classification", None) for hit in hits],
            scope="returned retrieval rows",
        ),
    )
    _append_unique(
        bullets,
        _counter_sentence(
            "recalling firm",
            [getattr(hit, "recalling_firm", None) for hit in hits],
            scope="returned retrieval rows",
            require_repeat=True,
        ),
    )
    _append_unique(
        bullets,
        _counter_sentence(
            "matched field",
            [getattr(hit, "field", None) for hit in hits],
            scope="returned retrieval rows",
        ),
    )
    _append_unique(bullets, _snippet_sentence(hits, scope="returned retrieval rows"))
    _append_unique(bullets, _raw_evidence_note("retrieval"))
    return _final_highlights(bullets)


def _semantic_count_highlights(result: validation.SemanticCountResult) -> list[str]:
    bullets: list[str] = []
    _append_unique(
        bullets,
        _degraded_highlight(
            result.retrieval_mode,
            result.embedding_fallback_reason,
            empty=result.candidate_count == 0,
        ),
    )
    if result.candidate_count == 0:
        _append_unique(
            bullets,
            f"No retrieval candidates were available for '{result.query}', so no validation sample was built.",
        )
        _append_unique(bullets, _raw_evidence_note("semantic_count"))
        return _final_highlights(bullets)

    estimate = (
        f"Estimated {result.estimated_count:,} matching recalls for '{result.query}' "
        f"from {result.candidate_count:,} retrieval candidates; "
        f"{result.verified_count}/{result.validated_count} validated evidence rows were accepted."
    )
    _append_unique(bullets, estimate)
    if result.group_by and result.groups:
        top_groups = ", ".join(
            f"{_clean_text(g.value) or '-'} (~{g.count:,})"
            for g in result.groups[:3]
        )
        _append_unique(
            bullets,
            f"Top estimated {result.group_by} groups in the returned candidate pool: {top_groups}.",
        )
    accepted_hits = [item.hit for item in result.validations if item.accepted]
    _append_unique(
        bullets,
        _counter_sentence(
            "classification",
            [getattr(hit, "classification", None) for hit in accepted_hits],
            scope="accepted validation rows",
        ),
    )
    _append_unique(
        bullets,
        _counter_sentence(
            "recalling firm",
            [getattr(hit, "recalling_firm", None) for hit in accepted_hits],
            scope="accepted validation rows",
            require_repeat=True,
        ),
    )
    _append_unique(bullets, _snippet_sentence(accepted_hits, scope="accepted validation rows"))
    _append_unique(bullets, _raw_evidence_note("semantic_count"))
    return _final_highlights(bullets)


def _multi_section_highlights(result: MultiSectionResult) -> list[str]:
    bullets: list[str] = []
    titles = ", ".join(section.title for section in result.sections[:3])
    suffix = "…" if len(result.sections) > 3 else ""
    _append_unique(
        bullets,
        f"Prepared {len(result.sections)} question-focused result section(s): {titles}{suffix}.",
    )
    for section in result.sections[:4]:
        rows = list(section.result)
        if not rows:
            _append_unique(bullets, f"{section.title}: no rows were returned.")
            continue
        top = rows[0]
        if result.intent == Intent.parent_group_exposure.value and section.id == "parent_group_exposure":
            metric = section.metadata.get("metric", "severity_weighted")
            metric_label = section.metadata.get(
                "metric_label",
                "severity-weighted exposure score" if metric == "severity_weighted" else "recall count",
            )
            total_recalls = int(top.metadata.get("total_recalls") or top.count)
            _append_unique(
                bullets,
                (
                    f"{section.title}: top returned {section.dimension} is "
                    f"{_short_text(top.value, limit=120) or '-'} "
                    f"({metric_label}: {int(top.count):,}; total recalls: {total_recalls:,})."
                ),
            )
            continue
        _append_unique(
            bullets,
            (
                f"{section.title}: top returned {section.dimension} is "
                f"{_short_text(top.value, limit=120) or '-'} ({int(top.count):,} recalls)."
            ),
        )
    _append_unique(bullets, _raw_evidence_note("multi_section"))
    return _final_highlights(bullets)


def _sample_row_highlights(spec: QuerySpec, result: Any) -> list[str]:
    rows = [row for row in result if isinstance(row, dict)]
    if not rows:
        return []
    bullets: list[str] = [
        f"Returned {len(rows)} example row(s) for the requested filters; highlights describe only these sample rows.",
    ]
    _append_unique(
        bullets,
        _counter_sentence(
            "classification",
            [row.get("classification") for row in rows],
            scope="sample rows",
        ),
    )
    _append_unique(
        bullets,
        _counter_sentence(
            "recalling firm",
            [row.get("recalling_firm") for row in rows],
            scope="sample rows",
            require_repeat=True,
        ),
    )
    _append_unique(
        bullets,
        _date_range_sentence(
            [row.get("recall_initiation_date") or row.get("report_date") for row in rows],
            scope="sample rows",
        ),
    )
    _append_unique(bullets, _raw_evidence_note("rows"))
    return _final_highlights(bullets)


def build_highlights(question: str, spec: QuerySpec | None, result: Any) -> list[str]:
    """Build concise, evidence-grounded highlights before raw retrieval/result rows.

    This intentionally summarizes only data already returned by the query/retrieval layer.
    It does not ask the model to infer new facts, so degraded or empty retrieval states stay
    conservative and raw rows remain the auditable evidence below the highlights.
    """
    if spec is None:
        return []
    if isinstance(result, MultiSectionResult):
        return _multi_section_highlights(result)
    if isinstance(result, validation.SemanticCountResult):
        return _semantic_count_highlights(result)
    if spec.semantic_query:
        return _retrieval_highlights(spec, result)
    if spec.intent is Intent.sample and isinstance(result, list):
        return _sample_row_highlights(spec, result)
    return []


def summarize(spec: QuerySpec, result: Any) -> str:
    if isinstance(result, MultiSectionResult):
        if result.intent == Intent.parent_group_exposure.value and result.sections:
            section = result.sections[0]
            metadata = section.metadata
            metric = metadata.get("metric", "severity_weighted")
            metric_label = metadata.get(
                "metric_label",
                "severity-weighted exposure score" if metric == "severity_weighted" else "recall count",
            )
            lines = [
                f"Provenance-backed parent-group exposure leaderboard (top {len(section.result)}, "
                f"ranked by {metric_label}).",
                f"Formula ({metadata.get('formula_version')}): {metadata.get('formula')}.",
                *list(metadata.get("caveats") or []),
            ]
            if not section.result:
                lines.append("No confirmed provenance-backed parent edges are available for this query yet.")
                return "\n".join(lines)
            excluded = metadata.get("excluded_recall_count")
            if excluded:
                lines.append(
                    f"Excluded {excluded:,} recalls with unmapped, unknown, unconfirmed, or LLM-only parent edges."
                )
            for item in section.result[:10]:
                item_meta = item.metadata
                members = item_meta.get("member_breakdown") or []
                member_names = [
                    str(member.get("firm_name"))
                    for member in members[:3]
                    if isinstance(member, dict) and member.get("firm_name")
                ]
                member_text = f"; members: {', '.join(member_names)}" if member_names else ""
                if len(members) > 3:
                    member_text += f" +{len(members) - 3} more"
                reason = (
                    f"; top reason category: {item_meta.get('top_reason_category')}"
                    f" ({item_meta.get('top_reason_count'):,})"
                    if item_meta.get("top_reason_category") and item_meta.get("top_reason_count") is not None else ""
                )
                evidence = f"; e.g. {', '.join(item.evidence)}" if item.evidence else ""
                class_breakdown = (
                    f"Class I/II/III "
                    f"{int(item_meta.get('class_i_recalls') or 0):,}/"
                    f"{int(item_meta.get('class_ii_recalls') or 0):,}/"
                    f"{int(item_meta.get('class_iii_recalls') or 0):,}"
                )
                exposure_score = int(item_meta.get("exposure_score") or 0)
                total_recalls = int(item_meta.get("total_recalls") or item.count)
                if metric == "severity_weighted":
                    primary = f"score {item.count:,}; total {total_recalls:,}; {class_breakdown}"
                else:
                    primary = f"total {total_recalls:,}; severity score {exposure_score:,}; {class_breakdown}"
                rank = item_meta.get("rank")
                rank_text = f"{rank}. " if rank else ""
                lines.append(f"  {rank_text}{item.value}: {primary}{member_text}{reason}{evidence}")
            return "\n".join(lines)
        titles = ", ".join(section.title for section in result.sections)
        return f"Produced separate tables for the requested answer: {titles}."
    if isinstance(result, TaxonomyExplanation):
        return result.answer
    if isinstance(result, RawFirmExposureLeaderboard):
        metric_label = (
            "severity-weighted exposure score"
            if result.metric == "severity_weighted" else "raw recall count"
        )
        if result.scope == PARENT_GROUP_EXPOSURE_SCOPE:
            lines = [
                f"Provenance-backed parent-group exposure leaderboard (top {len(result.items)}, "
                f"ranked by {metric_label}).",
                f"Formula ({result.formula_version}): {result.formula}.",
                *result.caveats,
            ]
            if not result.items:
                lines.append("No confirmed provenance-backed parent edges are available for this query yet.")
                return "\n".join(lines)
            excluded = result.metadata.get("excluded_recall_count")
            if excluded:
                lines.append(
                    f"Excluded {excluded:,} recalls with unmapped, unknown, unconfirmed, or LLM-only parent edges."
                )
            for item in result.items[:10]:
                members = item.metadata.get("member_breakdown") or []
                member_names = [
                    str(member.get("firm_name"))
                    for member in members[:3]
                    if isinstance(member, dict) and member.get("firm_name")
                ]
                member_text = f"; members: {', '.join(member_names)}" if member_names else ""
                if len(members) > 3:
                    member_text += f" +{len(members) - 3} more"
                reason = (
                    f"; top reason category: {item.top_reason_category}"
                    f" ({item.top_reason_count:,})"
                    if item.top_reason_category and item.top_reason_count is not None else ""
                )
                evidence = f"; e.g. {', '.join(item.evidence)}" if item.evidence else ""
                class_breakdown = (
                    f"Class I/II/III "
                    f"{item.class_i_recalls:,}/{item.class_ii_recalls:,}/{item.class_iii_recalls:,}"
                )
                if result.metric == "severity_weighted":
                    primary = (
                        f"score {item.exposure_score:,}; total {item.total_recalls:,}; "
                        f"{class_breakdown}"
                    )
                else:
                    primary = (
                        f"total {item.total_recalls:,}; severity score {item.exposure_score:,}; "
                        f"{class_breakdown}"
                    )
                lines.append(f"  {item.rank}. {item.recalling_firm}: {primary}{member_text}{reason}{evidence}")
            return "\n".join(lines)
        lines = [
            f"Raw FDA `recalling_firm` exposure leaderboard (top {len(result.items)}, "
            f"ranked by {metric_label}).",
            f"Formula ({result.formula_version}): {result.formula}.",
            *result.caveats,
        ]
        for item in result.items[:10]:
            reason = (
                f"; top reason category: {item.top_reason_category}"
                f" ({item.top_reason_count:,})"
                if item.top_reason_category and item.top_reason_count is not None else ""
            )
            evidence = f"; e.g. {', '.join(item.evidence)}" if item.evidence else ""
            class_breakdown = (
                f"Class I/II/III "
                f"{item.class_i_recalls:,}/{item.class_ii_recalls:,}/{item.class_iii_recalls:,}"
            )
            if result.metric == "severity_weighted":
                primary = (
                    f"score {item.exposure_score:,}; total {item.total_recalls:,}; "
                    f"{class_breakdown}"
                )
            else:
                primary = (
                    f"total {item.total_recalls:,}; severity score {item.exposure_score:,}; "
                    f"{class_breakdown}"
                )
            lines.append(f"  {item.rank}. {item.recalling_firm}: {primary}{reason}{evidence}")
        return "\n".join(lines)
    if isinstance(result, validation.SemanticCountResult):
        if result.retrieval_mode == "fts_only" and result.candidate_count == 0:
            reason = (
                f" ({result.embedding_fallback_reason})"
                if result.embedding_fallback_reason else ""
            )
            return (
                f"Semantic vector retrieval is unavailable{reason}; keyword fallback found no "
                f"candidates for '{result.query}'. This degraded FTS-only result is not evidence "
                "that FDA has zero matching recalls."
            )
        mode_note = (
            " using degraded FTS-only fallback"
            if result.retrieval_mode == "fts_only" else ""
        )
        if result.group_by:
            head = (f"Estimated {result.estimated_count:,} recalls matching '{result.query}'"
                    f"{mode_note} "
                    f"across {result.candidate_count} retrieval candidates "
                    f"(verified {result.verified_count}/{result.validated_count} validated, "
                    f"avg confidence {result.confidence['accepted_avg']:.2f}), grouped by {result.group_by}:")
            body = "\n".join(
                f"  {g.value}: ~{g.count:,}"
                for g in result.groups[:10]
            )
            return f"{head}\n{body}"
        return (
            f"Estimated {result.estimated_count:,} recalls matching '{result.query}'{mode_note} "
            f"across {result.candidate_count} retrieval candidates "
            f"(verified {result.verified_count}/{result.validated_count} validated, "
            f"avg confidence {result.confidence['accepted_avg']:.2f}; "
            f"confidence band {result.confidence_interval['lower']}-"
            f"{result.confidence_interval['upper']})."
        )
    if spec.semantic_query:
        mode_note = (
            " using degraded FTS-only fallback"
            if getattr(result, "retrieval_mode", None) == "fts_only"
            or (result and result[0].retrieval_mode == "fts_only") else ""
        )
        return (
            f"Found {len(result)} ranked FDA recall match(es) for '{spec.semantic_query}'"
            f"{mode_note}. See Highlights first, then raw evidence rows below."
        )
    if spec.intent is Intent.count_by_taxonomy:
        head = f"Recalls by recall-reason category (top {min(len(result), spec.limit or 20)}):"
        body = "\n".join(f"  {g.value}: {g.count:,}" for g in result[:10])
        return f"{head}\n{body}"
    if spec.intent is Intent.count_total:
        if spec.taxonomy_node_id:
            return f"Total '{spec.taxonomy_node_id}' recalls: {result:,}"
        return f"Total matching recalls: {result:,}"
    if spec.intent is Intent.count_by:
        cat = f" (category '{spec.taxonomy_node_id}')" if spec.taxonomy_node_id else ""
        head = f"Recalls by {spec.group_by}{cat} (top {min(len(result), spec.limit or 20)}):"
        body = "\n".join(f"  {g.value}: {g.count:,}" for g in result[:10])
        return f"{head}\n{body}"
    if spec.intent is Intent.trend:
        body = "\n".join(f"  {p}: {n:,}" for p, n in result)
        return f"Recalls over time (by {spec.grain or 'year'}):\n{body}"
    return f"Returned {len(result)} example rows. See Highlights first, then raw rows below."


class NLEngine:
    """Reusable NL->SQL engine for long-running services (e.g. the FastAPI app).

    Warms the expensive, request-invariant pieces ONCE — provider clients and the schema context
    (which runs several DISTINCT queries) — then answers each question with a fresh, short-lived
    read-only DB connection (safe under concurrency; cheap on localhost).
    """

    def __init__(self, *, dsn: str = DEFAULT_DSN, model: str | None = None) -> None:
        self.dsn = dsn
        self.chat_config = llm.chat_config(model=model)
        self.model = self.chat_config.model
        self.chat_client: Any | None = None
        self.chat_error: llm.ProviderError | None = None
        try:
            self.chat_client = llm.create_chat_client(self.chat_config)
        except llm.ProviderError as exc:
            self.chat_error = exc
        self.client = self.chat_client  # backwards-compatible alias
        self.title_config = llm.title_config()
        self.title_client: Any | None = None
        self.title_error: llm.ProviderError | None = None
        try:
            self.title_client = llm.create_chat_client(self.title_config)
        except llm.ProviderError as exc:
            self.title_error = exc
        self.embed_config = llm.embedding_config()
        self.embed_client: Any | None = None
        self.embedding_error: llm.ProviderError | None = None
        try:
            self.embed_client = llm.create_embedding_client(self.embed_config)
        except llm.ProviderError as exc:
            self.embedding_error = exc
        with RecallAnalytics(dsn) as a:
            self.schema_ctx = build_schema_context(a)  # cached for the engine's lifetime
            self.taxonomy_nodes = load_taxonomy_nodes(a)
            self.taxonomy_ctx = build_taxonomy_context(self.taxonomy_nodes)

    def provider_status(self) -> dict[str, Any]:
        status = llm.provider_status(self.chat_config, self.embed_config, self.title_config)
        status["llm_available"] = self.chat_error is None
        if self.chat_error is not None:
            status["llm_error_type"] = type(self.chat_error).__name__
        status["title_llm_available"] = self.title_error is None
        if self.title_error is not None:
            status["title_llm_error_type"] = type(self.title_error).__name__
        status["embed_available"] = self.embedding_error is None
        if self.embedding_error is not None:
            status["embed_error_type"] = type(self.embedding_error).__name__
        return status

    def ask(self, question: str) -> Answer:
        if self.chat_client is None:
            raise self.chat_error or llm.ProviderMissingKeyError(
                "chat client is not configured",
                provider=self.chat_config.provider,
                model=self.chat_config.model,
                operation="chat",
            )
        control = agent_control.classify_llm(self.chat_client, self.chat_config, question)
        if control.terminal:
            result = agent_control.result_from_decision(control)
            return Answer(
                question,
                None,
                result.message,
                result,
                control=control,
                metadata={"control_route": control.route, "control_reason": control.reason},
            )
        explanation_spec = _maybe_taxonomy_explanation_spec(question, self.taxonomy_nodes)
        simple_class_count_spec = _maybe_simple_class_count_spec(question)
        exposure_spec = _maybe_parent_group_exposure_spec(question) or _maybe_raw_firm_exposure_spec(question)
        spec = explanation_spec or simple_class_count_spec or exposure_spec or generate_spec(
            self.chat_client, self.chat_config, question, self.schema_ctx, self.taxonomy_ctx)
        with RecallAnalytics(self.dsn) as a:
            try:
                result = _run_question_spec(
                    a,
                    spec,
                    self.chat_client,
                    self.chat_config,
                    self.embed_client,
                    self.embed_config,
                    self.embedding_error,
                    question,
                )
            except ValueError as exc:  # one repair attempt: feed the error back
                if "sample needs filters or semantic_query" in str(exc):
                    control = agent_control.clarification("empty_sample")
                    agent_result = agent_control.result_from_decision(control)
                    return Answer(
                        question,
                        None,
                        agent_result.message,
                        agent_result,
                        control=control,
                        metadata={"control_route": control.route, "control_reason": control.reason},
                    )
                spec = generate_spec(
                    self.chat_client,
                    self.chat_config,
                    f"{question}\n\n(Your previous QuerySpec was invalid: {exc}. Return a corrected one.)",
                    self.schema_ctx,
                    self.taxonomy_ctx,
                )
                result = _run_question_spec(
                    a,
                    spec,
                    self.chat_client,
                    self.chat_config,
                    self.embed_client,
                    self.embed_config,
                    self.embedding_error,
                    question,
                )
        metadata: dict[str, Any] = {}
        if isinstance(result, MultiSectionResult):
            metadata["intent"] = result.intent
            metadata["sections"] = [
                {
                    "id": section.id,
                    "data_kind": section.data_kind,
                    "dimension": section.dimension,
                    "source": section.source,
                    "result_count": len(section.result) if isinstance(section.result, list) else None,
                }
                for section in result.sections
            ]
            metadata["sub_specs"] = [
                section.spec.model_dump(mode="json", exclude_none=True)
                for section in result.sections
            ]
        if isinstance(result, RawFirmExposureLeaderboard):
            metadata.update({
                "formula_version": result.formula_version,
                "formula": result.formula,
                "scope": result.scope,
                "exposure_metric": result.metric,
                "result_count": len(result.items),
            })
            metadata.update(result.metadata)
        if (
            spec.semantic_query
            and spec.intent is Intent.sample
            and getattr(result, "retrieval_mode", None) == "fts_only"
            and getattr(result, "embedding_fallback_reason", None)
        ):
            metadata.update({
                "retrieval_mode": "fts_only",
                "embedding_fallback_reason": getattr(result, "embedding_fallback_reason", None),
                "degraded": True,
            })
            if not result:
                summary = (
                    f"No keyword fallback matches for '{spec.semantic_query}'. Semantic vector retrieval "
                    "is currently unavailable, so this empty result is not a full semantic conclusion."
                )
            else:
                summary = summarize(spec, result)
        else:
            summary = summarize(spec, result)
        highlights = build_highlights(question, spec, result)
        return Answer(
            question,
            spec,
            summary,
            result,
            control=control,
            metadata=metadata,
            highlights=highlights,
        )


def ask(question: str, *, dsn: str = DEFAULT_DSN, model: str | None = None) -> Answer:
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
