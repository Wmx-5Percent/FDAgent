"""Validate semantic retrieval candidates and build semantic-count results."""
from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from math import sqrt
from statistics import mean
from typing import Any, Sequence

from pydantic import BaseModel, Field

import llm
from retrieval import Hit

DEFAULT_VALIDATION_LIMIT = int(os.environ.get("SEMANTIC_COUNT_K", "40"))
DEFAULT_RETRIEVAL_POOL_LIMIT = int(os.environ.get("SEMANTIC_RETRIEVAL_POOL_K", "400"))
MAX_VALIDATION_LIMIT = int(os.environ.get("SEMANTIC_COUNT_MAX_K", "120"))
MAX_RETRIEVAL_POOL_LIMIT = int(os.environ.get("SEMANTIC_RETRIEVAL_POOL_MAX_K", "2000"))
MIN_RETRIEVAL_SCORE = float(os.environ.get("SEMANTIC_MIN_RETRIEVAL_SCORE", "0.15"))
MIN_VALIDATION_CONFIDENCE = float(os.environ.get("SEMANTIC_MIN_VALIDATION_CONFIDENCE", "0.70"))

_WS = re.compile(r"\s+")


class CandidateValidation(BaseModel):
    recall_number: str
    field: str
    is_match: bool
    confidence: float = Field(ge=0.0, le=1.0)
    supporting_snippet: str = ""
    rationale: str


class ValidationBatch(BaseModel):
    items: list[CandidateValidation]


class SemanticValidationError(RuntimeError):
    pass


@dataclass(frozen=True)
class ValidatedHit:
    hit: Hit
    validation: CandidateValidation
    accepted: bool
    rejection_reason: str | None = None

    @property
    def recall_number(self) -> str:
        return self.hit.recall_number


@dataclass(frozen=True)
class SemanticCountGroup:
    value: Any
    count: int
    evidence: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class SemanticCountResult:
    query: str
    intent: str
    retrieval_mode: str
    estimated_count: int
    candidate_count: int
    validated_count: int
    verified_count: int
    retrieval_pool_count: int
    evidence: list[str]
    confidence: dict[str, Any]
    confidence_interval: dict[str, int]
    thresholds: dict[str, Any]
    validations: list[ValidatedHit]
    embedding_fallback_reason: str | None = None
    group_by: str | None = None
    groups: list[SemanticCountGroup] = field(default_factory=list)


def threshold_policy(*, retrieval_pool_limit: int, validation_limit: int,
                     retrieval_mode: str) -> dict[str, Any]:
    score_name = "fts_rank" if retrieval_mode == "fts_only" else "similarity"
    return {
        "retrieval_mode": retrieval_mode,
        "retrieval_pool_limit": retrieval_pool_limit,
        "validation_limit": validation_limit,
        "retrieval_score": score_name,
        "min_retrieval_score": 0.0 if retrieval_mode == "fts_only" else MIN_RETRIEVAL_SCORE,
        "min_validation_confidence": MIN_VALIDATION_CONFIDENCE,
        "validation_sampling": "deterministic_rank_stratified",
        "snippet_grounding": "accepted matches require supporting_snippet grounded in candidate text",
    }


def bounded_candidate_limit(value: int | None) -> int:
    return bounded_validation_limit(value)


def bounded_validation_limit(value: int | None) -> int:
    if value is None:
        return DEFAULT_VALIDATION_LIMIT
    return min(max(1, int(value)), MAX_VALIDATION_LIMIT)


def bounded_retrieval_pool_limit(validation_limit: int) -> int:
    default_pool = max(DEFAULT_RETRIEVAL_POOL_LIMIT, validation_limit)
    return min(default_pool, MAX_RETRIEVAL_POOL_LIMIT)


def select_validation_sample(hits: Sequence[Hit], limit: int) -> list[Hit]:
    pool = list(hits)
    if len(pool) <= limit:
        return pool
    if limit <= 1:
        return [pool[0]]
    last = len(pool) - 1
    indexes = [round(i * last / (limit - 1)) for i in range(limit)]
    return [pool[i] for i in indexes]


def _norm(text: str | None) -> str:
    return _WS.sub(" ", text or "").strip().casefold()


def snippet_is_grounded(snippet: str, content: str) -> bool:
    normalized = _norm(snippet)
    return bool(normalized) and normalized in _norm(content)


def _validation_prompt(query: str, hits: Sequence[Hit]) -> str:
    candidates: list[str] = []
    for idx, hit in enumerate(hits, 1):
        candidates.append(
            "\n".join([
                f"Candidate {idx}",
                f"recall_number: {hit.recall_number}",
                f"field: {hit.field}",
                f"retrieval_score: {hit.retrieval_score:.3f}",
                "text:",
                hit.content or "",
            ])
        )
    return (
        "Semantic query:\n"
        f"{query}\n\n"
        "For each candidate, decide whether the candidate text actually describes the semantic query. "
        "Return one item per candidate. If is_match is true, supporting_snippet must be an exact span "
        "copied from that candidate's text. If no exact supporting span exists, set is_match=false.\n\n"
        + "\n\n---\n\n".join(candidates)
    )


def validate_hits(
    client: Any,
    config: llm.ChatConfig,
    query: str,
    hits: Sequence[Hit],
) -> list[ValidatedHit]:
    if not hits:
        return []
    parsed = llm.structured_completion(
        client,
        config,
        temperature=0,
        messages=[
            {
                "role": "system",
                "content": (
                    "You are validating FDA recall retrieval candidates. "
                    "Use only the provided candidate text. Do not infer from outside knowledge. "
                    "Return structured yes/no judgments with calibrated confidence from 0 to 1."
                ),
            },
            {"role": "user", "content": _validation_prompt(query, hits)},
        ],
        response_model=ValidationBatch,
    )
    expected = {(hit.recall_number, hit.field) for hit in hits}
    by_key: dict[tuple[str, str], CandidateValidation] = {}
    for item in parsed.items:
        key = (item.recall_number, item.field)
        if key not in expected:
            raise SemanticValidationError(f"validation returned unexpected candidate {key!r}")
        if key in by_key:
            raise SemanticValidationError(f"validation returned duplicate candidate {key!r}")
        by_key[key] = item
    missing = expected.difference(by_key)
    if missing:
        raise SemanticValidationError(f"validation omitted candidates: {sorted(missing)!r}")

    out: list[ValidatedHit] = []
    for hit in hits:
        validation = by_key[(hit.recall_number, hit.field)]
        rejection_reason = _rejection_reason(hit, validation)
        out.append(ValidatedHit(
            hit=hit,
            validation=validation,
            accepted=rejection_reason is None,
            rejection_reason=rejection_reason,
        ))
    return out


def _rejection_reason(hit: Hit, validation: CandidateValidation) -> str | None:
    if not validation.is_match:
        return "llm_rejected"
    if hit.retrieval_score < MIN_RETRIEVAL_SCORE:
        return "below_retrieval_threshold"
    if validation.confidence < MIN_VALIDATION_CONFIDENCE:
        return "below_validation_confidence"
    if not snippet_is_grounded(validation.supporting_snippet, hit.content):
        return "ungrounded_supporting_snippet"
    return None


def build_count_result(
    *,
    query: str,
    intent: str,
    candidate_count: int,
    retrieval_pool_count: int,
    retrieval_pool_limit: int,
    validation_limit: int,
    validations: Sequence[ValidatedHit],
    retrieval_mode: str = "hybrid",
    embedding_fallback_reason: str | None = None,
    group_by: str | None = None,
    groups: Sequence[SemanticCountGroup] = (),
) -> SemanticCountResult:
    accepted = [item for item in validations if item.accepted]
    validated_count = len(validations)
    confidences = [item.validation.confidence for item in accepted]
    acceptance_rate = (len(accepted) / validated_count) if validated_count else 0.0
    estimated_count = round(acceptance_rate * candidate_count)
    lower, upper = _wilson_count_interval(len(accepted), validated_count, candidate_count)
    confidence: dict[str, Any] = {
        "accepted_avg": round(mean(confidences), 3) if confidences else 0.0,
        "accepted_min": round(min(confidences), 3) if confidences else 0.0,
        "accepted_max": round(max(confidences), 3) if confidences else 0.0,
        "acceptance_rate": round(acceptance_rate, 3),
        "rejected_count": validated_count - len(accepted),
    }
    return SemanticCountResult(
        query=query,
        intent=intent,
        retrieval_mode=retrieval_mode,
        estimated_count=estimated_count,
        candidate_count=candidate_count,
        validated_count=validated_count,
        verified_count=len(accepted),
        retrieval_pool_count=retrieval_pool_count,
        evidence=[item.recall_number for item in accepted],
        confidence=confidence,
        confidence_interval={"lower": lower, "upper": upper},
        thresholds=threshold_policy(
            retrieval_pool_limit=retrieval_pool_limit,
            validation_limit=validation_limit,
            retrieval_mode=retrieval_mode,
        ),
        validations=list(validations),
        embedding_fallback_reason=embedding_fallback_reason,
        group_by=group_by,
        groups=list(groups),
    )


def _wilson_count_interval(successes: int, sample_size: int, population_size: int) -> tuple[int, int]:
    if sample_size <= 0 or population_size <= 0:
        return 0, 0
    z = 1.96
    p = successes / sample_size
    denom = 1 + z**2 / sample_size
    center = (p + z**2 / (2 * sample_size)) / denom
    margin = z * sqrt((p * (1 - p) + z**2 / (4 * sample_size)) / sample_size) / denom
    lower = max(0, round((center - margin) * population_size))
    upper = min(population_size, round((center + margin) * population_size))
    return lower, max(lower, upper)
