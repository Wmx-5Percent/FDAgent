"""OpenRouter web-search verification for firm-name candidate pairs."""
from __future__ import annotations

import json
import os
from typing import Any

from dotenv import load_dotenv
from openai import OpenAI, OpenAIError
from pydantic import BaseModel, Field, ValidationError

load_dotenv()

DEFAULT_WEB_MODEL = os.environ.get(
    "OPENROUTER_WEB_MODEL",
    "deepseek/deepseek-v4-pro",
)
DEFAULT_OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"


class WebCitation(BaseModel):
    title: str = Field(description="Short page/source title.")
    url: str = Field(description="Public URL used as evidence.")
    quote: str = Field(description="Short supporting quote or snippet from the source.")


class WebFirmPairVerification(BaseModel):
    same_entity: bool = Field(description="True only when public web evidence supports same legal entity/name variant.")
    confidence: float = Field(ge=0, le=1, description="Confidence based on public web evidence.")
    canonical_name: str | None = Field(default=None, description="Best public canonical/legal name if verified.")
    relationship: str = Field(description="same_name_variant, subsidiary_parent, related_but_distinct, different, or unknown.")
    reason: str = Field(description="Short explanation grounded in the citations.")
    citations: list[WebCitation] = Field(default_factory=list, description="Public sources used as evidence.")


SYSTEM = """You verify whether two FDA recalling_firm strings refer to the same company identity.

Use web search. Do not rely only on string similarity. Only return same_entity=true when public
sources support that the two strings are the same legal entity or a direct name/spelling/suffix
variant. Parent/subsidiary, distributor/manufacturer, acquisition history, or brand ownership is
NOT enough for same_entity=true unless the source explicitly says the names are the same entity.

Return JSON only with:
{
  "same_entity": boolean,
  "confidence": number 0..1,
  "canonical_name": string or null,
  "relationship": "same_name_variant" | "subsidiary_parent" | "related_but_distinct" | "different" | "unknown",
  "reason": string,
  "citations": [{"title": string, "url": string, "quote": string}]
}

If public evidence is weak or conflicting, return same_entity=false with relationship="unknown".
"""


def verify_pair(
    left: str,
    right: str,
    *,
    model: str = DEFAULT_WEB_MODEL,
    engine: str = "exa",
    max_results: int = 5,
    max_tokens: int = 800,
) -> WebFirmPairVerification:
    """Verify a firm pair using OpenRouter web search and locally validated JSON."""
    api_key = os.environ.get("OPENROUTER_API_KEY")
    if not api_key:
        raise RuntimeError("OPENROUTER_API_KEY is required for OpenRouter web verification")
    client = OpenAI(
        api_key=api_key,
        base_url=os.environ.get("OPENROUTER_BASE_URL", DEFAULT_OPENROUTER_BASE_URL),
        default_headers=_openrouter_headers(),
    )
    prompt = (
        f"Firm A: {left}\n"
        f"Firm B: {right}\n\n"
        "Search the web and decide whether these are the same legal company/name variant. "
        "Cite public evidence. Be conservative."
    )
    try:
        completion = client.chat.completions.create(
            model=model,
            temperature=0,
            messages=[
                {"role": "system", "content": SYSTEM},
                {"role": "user", "content": prompt},
            ],
            response_format=_response_format(WebFirmPairVerification),
            max_tokens=max_tokens,
            extra_body={"plugins": [{"id": "web", "engine": engine, "max_results": max_results}]},
        )
    except TypeError:
        completion = client.chat.completions.create(
            model=_online_model(model),
            temperature=0,
            messages=[
                {"role": "system", "content": SYSTEM},
                {"role": "user", "content": prompt},
            ],
            response_format=_response_format(WebFirmPairVerification),
            max_tokens=max_tokens,
        )
    except OpenAIError as exc:
        raise RuntimeError(f"OpenRouter web verification failed: {_openrouter_error_detail(exc)}") from exc

    text = _message_text(completion)
    try:
        verdict = WebFirmPairVerification.model_validate_json(_json_blob(text))
    except (ValidationError, ValueError, json.JSONDecodeError) as exc:
        raise RuntimeError("web verification returned invalid JSON") from exc
    if not verdict.citations:
        verdict = verdict.model_copy(update={"citations": _annotation_citations(completion)})
    return verdict


def _openrouter_headers() -> dict[str, str]:
    headers: dict[str, str] = {}
    if referer := os.environ.get("OPENROUTER_HTTP_REFERER"):
        headers["HTTP-Referer"] = referer
    if title := os.environ.get("OPENROUTER_APP_TITLE", "FDAgent"):
        headers["X-Title"] = title
    return headers


def _response_format(response_model: type[BaseModel]) -> dict[str, Any]:
    return {
        "type": "json_schema",
        "json_schema": {
            "name": response_model.__name__,
            "strict": True,
            "schema": response_model.model_json_schema(),
        },
    }


def _online_model(model: str) -> str:
    return model if ":online" in model else f"{model}:online"


def _message_text(completion: Any) -> str:
    try:
        content = completion.choices[0].message.content
    except (AttributeError, IndexError) as exc:
        raise RuntimeError("OpenRouter response did not include a message") from exc
    if not content:
        raise RuntimeError("OpenRouter web verification response was empty")
    return str(content)


def _openrouter_error_detail(exc: OpenAIError) -> str:
    status = getattr(exc, "status_code", None)
    body = getattr(exc, "body", None)
    if isinstance(body, dict):
        message = body.get("message")
        code = body.get("code")
        if message or code:
            return f"{type(exc).__name__}(status={status}, code={code}, message={message})"
    message = getattr(exc, "message", None)
    if message:
        return f"{type(exc).__name__}(status={status}, message={str(message)[:300]})"
    return f"{type(exc).__name__}(status={status})"


def _annotation_citations(completion: Any) -> list[WebCitation]:
    try:
        annotations = completion.choices[0].message.annotations or []
    except (AttributeError, IndexError):
        return []
    citations: list[WebCitation] = []
    for annotation in annotations:
        payload = getattr(annotation, "url_citation", None)
        if payload is None and isinstance(annotation, dict):
            payload = annotation.get("url_citation")
        if not payload:
            continue
        if isinstance(payload, dict):
            title = str(payload.get("title") or payload.get("url") or "web source")
            url = str(payload.get("url") or "")
            quote = str(payload.get("content") or "")
        else:
            title = str(getattr(payload, "title", "") or getattr(payload, "url", "") or "web source")
            url = str(getattr(payload, "url", "") or "")
            quote = str(getattr(payload, "content", "") or "")
        if url:
            citations.append(WebCitation(title=title, url=url, quote=quote[:1000]))
    return citations


def _json_blob(text: str) -> str:
    stripped = text.strip()
    if stripped.startswith("{") and stripped.endswith("}"):
        return stripped
    start = stripped.find("{")
    end = stripped.rfind("}")
    if start == -1 or end == -1 or end <= start:
        raise ValueError("no JSON object found")
    return stripped[start:end + 1]
