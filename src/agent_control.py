"""Guard /ask prompts before they enter the FDA recall query pipeline."""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

DOMAIN_TERMS = {
    "assay", "bacteria", "bacterial", "contamination", "drug", "fda",
    "firm", "glass", "impurity", "label", "ndma", "pill", "product",
    "products", "recalled", "recall", "recalls", "sterile", "sterility",
    "sterilization", "strong", "superpotent", "vial",
}
FUZZY_CONCEPT_TERMS = {
    "assay", "bacteria", "bacterial", "contamination", "glass",
    "impurity", "microbial", "ndma", "particle", "particles", "potency",
    "potent", "sterile", "sterility", "sterilization", "strong", "superpotent",
}
GENERIC_RECALL_TERMS = {
    "class", "drug", "drugs", "enforcement", "fda", "i", "ii", "iii", "recall",
    "recalls", "report", "reports",
}
META_PATTERNS = [
    r"\bwho\s+(are\s+you|you\s+are)\b",
    r"\bwhat\s+(are\s+you|can\s+you\s+do|do\s+you\s+do)\b",
    r"\bhow\s+can\s+you\s+help\b",
    r"\byour\s+(capabilities|purpose|scope)\b",
    r"(你是谁|你是什么|你可以做什么|你能做什么|你会做什么|你的能力|你能帮我什么|介绍一下你自己)",
]
OUT_OF_DOMAIN_PATTERNS = [
    r"\bjoke\b", r"\bweather\b", r"\bsports?\b", r"\bstock price\b",
    r"\bmovie\b", r"\brecipe\b", r"\bflight\b",
    r"\b(class\s+action|railroad)\b", r"\bshould\s+i\s+take\b",
    r"\b(product|products)\s+should\s+i\s+buy\b", r"\bwhat\s+product\s+should\b",
    r"(天气|笑话|体育|股票|电影|菜谱|航班)",
    r"(头疼|头痛).*(吃|服用|用|推荐|治疗).*(药|药品)",
]
STOPWORDS = {
    "a", "about", "all", "an", "and", "are", "for", "has", "have", "how",
    "in", "is", "many", "me", "of", "on", "or", "show", "that", "the",
    "there", "to", "too", "was", "were", "what", "which", "with",
}


@dataclass(frozen=True)
class AgentControlDecision:
    route: str
    reason: str
    message: str | None = None
    suggestions: list[str] = field(default_factory=list)

    @property
    def terminal(self) -> bool:
        return self.route != "in_domain"

    def as_dict(self) -> dict[str, Any]:
        return {
            "route": self.route,
            "reason": self.reason,
            "terminal": self.terminal,
            "suggestions": list(self.suggestions),
        }


@dataclass(frozen=True)
class AgentControlResult:
    kind: str
    route: str
    message: str
    reason: str
    suggestions: list[str] = field(default_factory=list)

    def as_data(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "route": self.route,
            "message": self.message,
            "reason": self.reason,
            "suggestions": list(self.suggestions),
        }


def classify(question: str) -> AgentControlDecision:
    text = _normalized(question)
    if not text:
        return clarification("empty_question")
    greeting_stripped = _strip_greeting(text)
    if greeting_stripped != text:
        if greeting_stripped:
            text = greeting_stripped
        else:
            return AgentControlDecision(
                route="chitchat_meta",
                reason="greeting",
                message=(
                    "Hi, I'm FDAgent. Ask me about FDA drug recalls, such as counts, "
                    "trends, firms, classifications, products, or recall reasons."
                ),
                suggestions=[
                    "How many Class I drug recalls have there been?",
                    "Show me sterility-related recalls.",
                ],
            )
    if _matches(text, META_PATTERNS):
        return AgentControlDecision(
            route="chitchat_meta",
            reason="meta_or_capability_question",
            message=(
                "I'm FDAgent, a local FDA drug-recall intelligence agent. I can answer "
                "evidence-backed questions over openFDA drug enforcement recalls, including "
                "counts, trends, firm distributions, semantic recall examples, and estimated "
                "semantic counts."
            ),
            suggestions=[
                "How many Class I drug recalls have there been?",
                "Show me sterility-related recalls.",
                "Which firms had the most Class I recalls?",
            ],
        )
    if _matches(text, OUT_OF_DOMAIN_PATTERNS):
        return AgentControlDecision(
            route="out_of_domain",
            reason="outside_fda_recall_scope",
            message=(
                "I only answer questions about FDA drug-recall enforcement data in this demo. "
                "Ask about recall counts, trends, firms, classifications, or recall reasons."
            ),
            suggestions=[
                "How many recalls involved sterility issues?",
                "What is the yearly trend of Class I recalls?",
            ],
        )
    if _is_bare_recall_request(text):
        return clarification("empty_sample")
    if _looks_in_domain(text):
        return AgentControlDecision(route="in_domain", reason="fda_recall_terms_detected")
    if len(_tokens(text)) <= 4:
        return clarification("too_short_or_underspecified")
    return AgentControlDecision(
        route="out_of_domain",
        reason="no_fda_recall_terms_detected",
        message=(
            "I could not connect that question to FDA drug-recall data. Please ask about "
            "recalls, classifications, firms, dates, products, or recall reasons."
        ),
        suggestions=[
            "Show me a few glass-particle recalls.",
            "Which firms had the most Class II recalls?",
        ],
    )


def clarification(reason: str = "ambiguous") -> AgentControlDecision:
    return AgentControlDecision(
        route="ambiguous",
        reason=reason,
        message=(
            "I need a more specific FDA recall question before querying the database. "
            "Please include a recall concept, firm, classification, date range, or product."
        ),
        suggestions=[
            "Show me a few sterility-related recalls.",
            "How many Class I recalls have there been?",
        ],
    )


def result_from_decision(decision: AgentControlDecision) -> AgentControlResult:
    kind = "clarification" if decision.route == "ambiguous" else "message"
    return AgentControlResult(
        kind=kind,
        route=decision.route,
        message=decision.message or "",
        reason=decision.reason,
        suggestions=list(decision.suggestions),
    )


def refine_semantic_query(question: str, semantic_query: str | None) -> tuple[str | None, list[str]]:
    if not semantic_query:
        return semantic_query, []
    haystack = f"{question} {semantic_query}".casefold()
    if re.search(r"\b(non[- ]?)?steril(?:e|ity|ization|isation)?\b", haystack) \
            or re.search(r"(无菌|非无菌|灭菌)", haystack):
        return "sterility", [
            "sterile",
            "non-sterile",
            "lack of assurance of sterility",
            "lack of sterility assurance",
        ]
    if re.search(r"\b(subpotent|sub[- ]?potent|low potency|low assay|below specification)\b", haystack) \
            or re.search(r"(药效不足|效价低|效价不足|含量不足|药力不足)", haystack):
        return "subpotent", [
            "low potency",
            "low assay",
            "below specification for assay",
            "potency below specification",
        ]
    if re.search(
        r"\b(superpotent|over[- ]?strength|too strong|high assay|high potency|"
        r"potency above|above specification for assay)\b",
        haystack,
    ) or re.search(r"(药效太强|药力太强|效价太高|含量过高|药效过强|太强)", haystack):
        return "superpotent", [
            "too strong",
            "over strength",
            "high assay",
            "potency above specification",
        ]
    if re.search(r"(细菌|微生物|感染|污染)", haystack):
        return "microbial contamination", [
            "bacterial contamination",
            "microbial contamination",
            "contamination",
        ]
    if re.search(r"(玻璃|颗粒|异物)", haystack):
        return "glass particles", [
            "glass particles",
            "particulate matter",
            "foreign matter",
        ]
    return semantic_query.strip(), []


def is_generic_recall_semantic_query(semantic_query: str | None) -> bool:
    if not semantic_query:
        return False
    tokens = set(_tokens(semantic_query))
    if tokens.intersection(FUZZY_CONCEPT_TERMS):
        return False
    return bool(tokens) and tokens.issubset(GENERIC_RECALL_TERMS)


def broad_fts_queries(query: str, aliases: list[str]) -> list[str]:
    out: list[str] = []
    for item in [query, *aliases]:
        normalized = " ".join(_tokens(item))
        if normalized and normalized not in out:
            out.append(normalized)
    terms = [tok for tok in _tokens(query) if tok not in STOPWORDS and len(tok) > 2]
    if len(terms) > 1:
        broad = " OR ".join(terms)
        if broad not in out:
            out.append(broad)
    return out


def _normalized(text: str) -> str:
    return " ".join(text.strip().casefold().split())


def _strip_greeting(text: str) -> str:
    if re.fullmatch(r"\s*(hi|hello|hey)[!.]?\s*", text):
        return ""
    if re.fullmatch(r"\s*(你好|您好|嗨)[!！。.]?\s*", text):
        return ""
    text = re.sub(r"^\s*(你好|您好|嗨)[,，!！\s]+", "", text, count=1).strip()
    return re.sub(r"^\s*(hi|hello|hey)[,!\s]+", "", text, count=1).strip()


def _tokens(text: str) -> list[str]:
    return re.findall(r"[a-z0-9]+(?:[-'][a-z0-9]+)?", text.casefold())


def _matches(text: str, patterns: list[str]) -> bool:
    return any(re.search(pattern, text) for pattern in patterns)


def _looks_in_domain(text: str) -> bool:
    tokens = set(_tokens(text))
    if tokens.intersection(DOMAIN_TERMS):
        return True
    if _has_chinese_recall_context(text):
        return True
    return bool(re.search(r"\bclass\s+(i|ii|iii|1|2|3)\b", text))


def _is_bare_recall_request(text: str) -> bool:
    if _has_chinese_specific_context(text):
        return False
    tokens = [tok for tok in _tokens(text) if tok not in STOPWORDS]
    if tokens in (["recalls"], ["recall"], ["show", "recalls"], ["show", "recall"]):
        return True
    return text in {"召回", "看召回", "查召回", "药品召回", "药物召回"}


def _has_chinese_recall_context(text: str) -> bool:
    return bool(re.search(
        r"(fda|openfda|召回|被召回|药品|药物|药效|效价|药力|无菌|非无菌|灭菌|"
        r"污染|细菌|微生物|感染|玻璃|颗粒|异物|公司|厂家|企业)",
        text,
    ))


def _has_chinese_specific_context(text: str) -> bool:
    return bool(re.search(
        r"(药品|药物|药效|效价|药力|无菌|非无菌|灭菌|污染|细菌|微生物|感染|"
        r"玻璃|颗粒|异物|公司|厂家|企业)",
        text,
    ))
