"""Guard /ask prompts before they enter the FDA recall query pipeline."""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Literal

from pydantic import BaseModel, Field

import llm

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


# --------------------------------------------------------------------------- #
# LLM intent gate (primary): language-agnostic routing instead of brittle rules
# --------------------------------------------------------------------------- #
_INTENT_ROUTE = Literal["in_domain", "chitchat_meta", "out_of_domain", "ambiguous"]
LLM_CLASSIFIER_MAX_TOKENS = 500

_DEFAULT_SUGGESTIONS = (
    "How many Class I drug recalls have there been?",
    "Show me a few sterility-related recalls.",
    "Which firms had the most Class I recalls?",
)

_LLM_CLASSIFIER_SYSTEM = """You are the intent router for FDAgent, a local assistant that
answers questions about U.S. FDA drug RECALL enforcement data (the openFDA drug/enforcement
dataset of ~17k drug-recall reports). Read the user's message -- it may be written in ANY
language -- and classify it into exactly ONE route.

Routes:
- "in_domain": a genuine question that can plausibly be answered from FDA drug-recall data.
  This INCLUDES any language and fuzzy free-text concepts: recall counts, trends over time,
  distributions by firm / classification (Class I/II/III) / state / product type, specific
  recall reasons, and concepts such as sterility or non-sterile conditions, contamination,
  bacterial/microbial issues, particulates or glass particles, impurities (e.g. NDMA /
  nitrosamine), mislabeling, subpotent (too weak) and superpotent (too strong / potency above
  specification). This also INCLUDES hard filters over FDA fields such as country/location
  even when the country is outside the U.S.; e.g. "How many Class I drug recalls in Canada?"
  means FDA drug-enforcement rows whose `country` field is Canada, not Canadian regulator data.
  This also INCLUDES plain-language explanation questions about FDA recall
  taxonomy categories or recall terms, such as "what does cGMP deviation mean in drug recalls?"
  or "CGMP Deviations Reason for recall 这到底是什么". When the message plausibly refers to drug
  recalls, PREFER "in_domain".
  Examples that ARE in_domain: "How many Class I recalls?", "sterility recalls by firm",
  "How many Class I drug recalls in Canada?", "有多少药品因为药效太强被召回",
  "有多少个关于细菌感染的 recall", "召回最多的公司是哪几家".
- "chitchat_meta": greetings, or questions about the assistant itself -- its identity,
  capabilities, purpose, or scope. Examples: "who are you", "what can you do", "hi",
  "你是谁", "你可以做什么".
- "out_of_domain": clearly unrelated to FDA drug recalls. Examples: weather, sports, jokes,
  stock prices, recipes, flights, personal medical advice ("what should I take for a
  headache"), shopping advice ("what product should I buy").
- "ambiguous": about drug recalls in general but too vague to run a query (no concept, firm,
  filter, dimension, or timeframe). Examples: "recalls", "show me recalls", "tell me about
  drugs".

Reply rules:
- For "chitchat_meta", "out_of_domain", and "ambiguous": write a short, friendly `message` in
  the SAME LANGUAGE as the user, plus 2-3 concrete `suggestions` of in_domain questions (also
  in the user's language). For "chitchat_meta" briefly say what FDAgent can do; for
  "out_of_domain" say you only cover FDA drug-recall data; for "ambiguous" ask for a specific
  recall concept, firm, classification, date range, or product.
- For "in_domain": set message to "" and suggestions to [] -- the query pipeline handles it.
- Set `reason` to a short machine-readable tag (e.g. "capability_question", "weather_request",
  "superpotent_recall_count", "too_vague").

Return only the structured fields."""


class LLMIntentDecision(BaseModel):
    """Structured routing decision emitted by the LLM intent classifier."""

    route: _INTENT_ROUTE
    reason: str = ""
    message: str = ""
    suggestions: list[str] = Field(default_factory=list)


def classify_llm(client: Any, config: Any, question: str) -> AgentControlDecision:
    """Primary /ask gate: classify intent with the LLM (language-agnostic).

    Returns an :class:`AgentControlDecision`. Terminal routes (chitchat_meta / out_of_domain /
    ambiguous) carry a user-facing ``message`` + ``suggestions`` in the user's language; the
    ``in_domain`` route hands off to the QuerySpec pipeline. Provider errors propagate to the
    caller (same contract as ``generate_spec``); callers may fall back to :func:`classify`.
    """
    text = _normalized(question)
    if not text:
        return clarification("empty_question")
    decision = llm.structured_completion(
        client,
        config,
        [
            {"role": "system", "content": _LLM_CLASSIFIER_SYSTEM},
            {"role": "user", "content": question},
        ],
        LLMIntentDecision,
        temperature=0,
        max_tokens=LLM_CLASSIFIER_MAX_TOKENS,
    )
    route = decision.route
    reason = decision.reason.strip() or f"llm_{route}"
    if route == "in_domain":
        return AgentControlDecision(route="in_domain", reason=reason)
    suggestions = [s.strip() for s in decision.suggestions if s and s.strip()][:3]
    return AgentControlDecision(
        route=route,
        reason=reason,
        message=decision.message.strip() or _default_control_message(route),
        suggestions=suggestions or list(_DEFAULT_SUGGESTIONS),
    )


def _default_control_message(route: str) -> str:
    if route == "chitchat_meta":
        return (
            "I'm FDAgent, a local FDA drug-recall intelligence agent. I answer evidence-backed "
            "questions over openFDA drug enforcement recalls, including counts, trends, firm and "
            "classification distributions, semantic recall examples, and estimated semantic counts."
        )
    if route == "out_of_domain":
        return (
            "I only answer questions about FDA drug-recall enforcement data. Ask about recall "
            "counts, trends, firms, classifications, products, or recall reasons."
        )
    return (
        "I need a more specific FDA recall question before querying the database. Please include "
        "a recall concept, firm, classification, date range, or product."
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


def _tokens(text: str) -> list[str]:
    return re.findall(r"[a-z0-9]+(?:[-'][a-z0-9]+)?", text.casefold())
