"""OpenAI-compatible chat and embedding provider gateway for serving paths."""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from typing import Any, Mapping, Sequence, TypeVar

from dotenv import load_dotenv
from openai import (
    APIConnectionError,
    APIError,
    AuthenticationError,
    BadRequestError,
    OpenAI,
    OpenAIError,
    PermissionDeniedError,
    RateLimitError,
)
from pydantic import BaseModel, ValidationError

try:  # The SDK uses this internally for `.parse(...)`; reuse it without calling `.parse(...)`.
    from openai.lib._pydantic import to_strict_json_schema
except ImportError:  # pragma: no cover - defensive for older SDKs.
    to_strict_json_schema = None  # type: ignore[assignment]

load_dotenv()

DEFAULT_OPENAI_CHAT_MODEL = "gpt-4o-mini"
DEFAULT_OPENROUTER_CHAT_MODEL = "openai/gpt-4o-mini"
DEFAULT_OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"
DEFAULT_EMBED_MODEL = "text-embedding-3-small"
SUPPORTED_EMBED_MODELS = {DEFAULT_EMBED_MODEL: 1536}

T = TypeVar("T", bound=BaseModel)


class ProviderError(RuntimeError):
    """Base class for upstream provider failures with safe API metadata."""

    public_detail = "upstream model provider failed"
    status_code = 502
    retryable = False
    fallback_allowed = False

    def __init__(self, message: str | None = None, *, provider: str | None = None,
                 model: str | None = None, operation: str | None = None) -> None:
        super().__init__(message or self.public_detail)
        self.provider = provider
        self.model = model
        self.operation = operation


class ProviderConfigError(ProviderError):
    public_detail = "model provider is not configured"
    status_code = 503


class ProviderMissingKeyError(ProviderConfigError):
    public_detail = "model provider credentials are not configured"
    fallback_allowed = True


class ProviderUnsupportedConfigError(ProviderConfigError):
    public_detail = "model provider configuration is not supported"


class ProviderAuthError(ProviderError):
    public_detail = "model provider rejected the configured credentials"
    status_code = 502
    fallback_allowed = True


class ProviderQuotaError(ProviderError):
    public_detail = "model provider quota is exhausted"
    status_code = 503
    retryable = True
    fallback_allowed = True


class ProviderRateLimitError(ProviderError):
    public_detail = "model provider rate limit was reached"
    status_code = 503
    retryable = True
    fallback_allowed = True


class ProviderConnectionError(ProviderError):
    public_detail = "model provider is temporarily unreachable"
    status_code = 503
    retryable = True
    fallback_allowed = True


class ProviderCapabilityError(ProviderError):
    public_detail = "model provider does not support the requested capability"
    status_code = 502


class StructuredOutputError(ProviderError):
    public_detail = "model provider returned invalid structured output"
    status_code = 502


@dataclass(frozen=True)
class ChatConfig:
    provider: str
    model: str
    api_key: str | None
    base_url: str | None = None
    default_headers: Mapping[str, str] = field(default_factory=dict)

    @property
    def configured(self) -> bool:
        return bool(self.api_key)

    def status(self) -> dict[str, Any]:
        return {
            "llm_provider": self.provider,
            "llm_model": self.model,
            "llm_configured": self.configured,
        }


@dataclass(frozen=True)
class EmbeddingConfig:
    provider: str
    model: str
    api_key: str | None

    @property
    def configured(self) -> bool:
        return bool(self.api_key)

    @property
    def dimension(self) -> int | None:
        return SUPPORTED_EMBED_MODELS.get(self.model)

    def status(self) -> dict[str, Any]:
        return {
            "embed_provider": self.provider,
            "embed_model": self.model,
            "embed_configured": self.configured,
            "embed_dimension": self.dimension,
        }


def chat_config(*, model: str | None = None) -> ChatConfig:
    """Load chat provider config without exposing secrets."""
    provider = os.environ.get("LLM_PROVIDER", "openai").strip().lower() or "openai"
    if provider == "openai":
        chosen_model = model or os.environ.get("LLM_MODEL") or os.environ.get("OPENAI_MODEL") \
            or DEFAULT_OPENAI_CHAT_MODEL
        return ChatConfig(
            provider=provider,
            model=chosen_model,
            api_key=os.environ.get("OPENAI_API_KEY"),
        )
    if provider == "openrouter":
        chosen_model = model or os.environ.get("LLM_MODEL") or DEFAULT_OPENROUTER_CHAT_MODEL
        headers: dict[str, str] = {}
        if referer := os.environ.get("OPENROUTER_HTTP_REFERER"):
            headers["HTTP-Referer"] = referer
        if title := os.environ.get("OPENROUTER_APP_TITLE", "FDAgent"):
            headers["X-Title"] = title
        return ChatConfig(
            provider=provider,
            model=chosen_model,
            api_key=os.environ.get("OPENROUTER_API_KEY"),
            base_url=os.environ.get("OPENROUTER_BASE_URL", DEFAULT_OPENROUTER_BASE_URL),
            default_headers=headers,
        )
    raise ProviderUnsupportedConfigError(
        f"unsupported LLM_PROVIDER {provider!r}",
        provider=provider,
        model=model,
        operation="chat_config",
    )


def embedding_config(*, model: str | None = None) -> EmbeddingConfig:
    """Load embedding provider config separately from chat provider config."""
    provider = os.environ.get("EMBED_PROVIDER", "openai").strip().lower() or "openai"
    chosen_model = model or os.environ.get("EMBED_MODEL") or os.environ.get("OPENAI_EMBED_MODEL") \
        or DEFAULT_EMBED_MODEL
    return EmbeddingConfig(
        provider=provider,
        model=chosen_model,
        api_key=os.environ.get("OPENAI_API_KEY") if provider == "openai" else None,
    )


def create_chat_client(config: ChatConfig | None = None) -> OpenAI:
    config = config or chat_config()
    if not config.api_key:
        raise ProviderMissingKeyError(
            f"{config.provider} chat API key is not configured",
            provider=config.provider,
            model=config.model,
            operation="create_chat_client",
        )
    kwargs: dict[str, Any] = {"api_key": config.api_key}
    if config.base_url:
        kwargs["base_url"] = config.base_url
    if config.default_headers:
        kwargs["default_headers"] = dict(config.default_headers)
    return OpenAI(**kwargs)


def create_embedding_client(config: EmbeddingConfig | None = None) -> OpenAI:
    config = config or embedding_config()
    _validate_embedding_config(config)
    if not config.api_key:
        raise ProviderMissingKeyError(
            f"{config.provider} embedding API key is not configured",
            provider=config.provider,
            model=config.model,
            operation="create_embedding_client",
        )
    return OpenAI(api_key=config.api_key)


def _validate_embedding_config(config: EmbeddingConfig) -> None:
    if config.provider != "openai":
        raise ProviderUnsupportedConfigError(
            "only OpenAI embeddings are currently compatible with stored pgvector rows",
            provider=config.provider,
            model=config.model,
            operation="embedding_config",
        )
    if config.model not in SUPPORTED_EMBED_MODELS:
        raise ProviderUnsupportedConfigError(
            f"embedding model {config.model!r} is not known to be compatible with stored vectors",
            provider=config.provider,
            model=config.model,
            operation="embedding_config",
        )


def provider_status(chat: ChatConfig | None = None,
                    embed: EmbeddingConfig | None = None) -> dict[str, Any]:
    chat = chat or chat_config()
    embed = embed or embedding_config()
    return {**chat.status(), **embed.status()}


def structured_completion(
    client: OpenAI,
    config: ChatConfig,
    messages: Sequence[Mapping[str, Any]],
    response_model: type[T],
    *,
    temperature: float = 0,
    max_tokens: int | None = None,
) -> T:
    """Call an OpenAI-compatible chat endpoint and validate JSON locally with Pydantic."""
    kwargs: dict[str, Any] = {
        "model": config.model,
        "temperature": temperature,
        "messages": list(messages),
        "response_format": _response_format(response_model),
    }
    if max_tokens is not None:
        kwargs["max_tokens"] = max_tokens
    try:
        completion = client.chat.completions.create(**kwargs)
    except OpenAIError as exc:
        raise provider_error_from_openai(
            exc,
            provider=config.provider,
            model=config.model,
            operation=f"structured_completion:{response_model.__name__}",
        ) from exc
    content = _message_content(completion, provider=config.provider, model=config.model,
                               operation=f"structured_completion:{response_model.__name__}")
    try:
        return response_model.model_validate_json(content)
    except (ValidationError, ValueError, json.JSONDecodeError) as exc:
        raise StructuredOutputError(
            f"{config.provider} returned invalid {response_model.__name__} JSON",
            provider=config.provider,
            model=config.model,
            operation=f"structured_completion:{response_model.__name__}",
        ) from exc


def chat_completion_text(
    client: OpenAI,
    config: ChatConfig,
    messages: Sequence[Mapping[str, Any]],
    *,
    temperature: float = 0,
    max_tokens: int | None = None,
) -> str:
    kwargs: dict[str, Any] = {
        "model": config.model,
        "temperature": temperature,
        "messages": list(messages),
    }
    if max_tokens is not None:
        kwargs["max_tokens"] = max_tokens
    try:
        completion = client.chat.completions.create(**kwargs)
    except OpenAIError as exc:
        raise provider_error_from_openai(
            exc,
            provider=config.provider,
            model=config.model,
            operation="chat_completion",
        ) from exc
    return _message_content(completion, provider=config.provider, model=config.model,
                            operation="chat_completion")


def embed_text(client: OpenAI, config: EmbeddingConfig, text: str) -> list[float]:
    _validate_embedding_config(config)
    try:
        response = client.embeddings.create(model=config.model, input=[text])
    except OpenAIError as exc:
        raise provider_error_from_openai(
            exc,
            provider=config.provider,
            model=config.model,
            operation="embedding",
        ) from exc
    embedding = response.data[0].embedding
    if len(embedding) != SUPPORTED_EMBED_MODELS[config.model]:
        raise ProviderUnsupportedConfigError(
            "embedding response dimension does not match stored vectors",
            provider=config.provider,
            model=config.model,
            operation="embedding",
        )
    return list(embedding)


def can_fallback_to_fts(exc: BaseException) -> bool:
    return isinstance(exc, ProviderError) and exc.fallback_allowed


def provider_error_from_openai(exc: OpenAIError, *, provider: str, model: str,
                               operation: str) -> ProviderError:
    code = _provider_code(exc)
    if isinstance(exc, AuthenticationError | PermissionDeniedError):
        return ProviderAuthError(
            "provider authentication failed",
            provider=provider,
            model=model,
            operation=operation,
        )
    if _looks_like_quota(code, exc):
        return ProviderQuotaError(
            "provider quota exhausted",
            provider=provider,
            model=model,
            operation=operation,
        )
    if isinstance(exc, RateLimitError):
        return ProviderRateLimitError(
            "provider rate limit reached",
            provider=provider,
            model=model,
            operation=operation,
        )
    if isinstance(exc, APIConnectionError):
        return ProviderConnectionError(
            "provider connection failed",
            provider=provider,
            model=model,
            operation=operation,
        )
    if isinstance(exc, BadRequestError) and _looks_like_structured_output_error(code, exc):
        return ProviderCapabilityError(
            "provider/model rejected JSON Schema structured output",
            provider=provider,
            model=model,
            operation=operation,
        )
    if isinstance(exc, APIError):
        status_code = getattr(exc, "status_code", None)
        if status_code in {401, 403}:
            return ProviderAuthError(
                "provider authentication failed",
                provider=provider,
                model=model,
                operation=operation,
            )
        if status_code in {402, 429}:
            return ProviderQuotaError(
                "provider quota or rate limit reached",
                provider=provider,
                model=model,
                operation=operation,
            )
        if isinstance(status_code, int) and status_code >= 500:
            return ProviderConnectionError(
                "provider server error",
                provider=provider,
                model=model,
                operation=operation,
            )
    return ProviderError(
        "provider API call failed",
        provider=provider,
        model=model,
        operation=operation,
    )


def http_status(exc: ProviderError) -> int:
    return exc.status_code


def public_error_detail(exc: ProviderError) -> str:
    parts = [exc.public_detail]
    if exc.provider:
        parts.append(f"provider={exc.provider}")
    if exc.model:
        parts.append(f"model={exc.model}")
    if exc.operation:
        parts.append(f"operation={exc.operation}")
    if len(parts) == 1:
        return parts[0]
    return f"{parts[0]} ({', '.join(parts[1:])})"


def _response_format(response_model: type[BaseModel]) -> dict[str, Any]:
    schema = (
        to_strict_json_schema(response_model)
        if to_strict_json_schema is not None
        else response_model.model_json_schema()
    )
    return {
        "type": "json_schema",
        "json_schema": {
            "name": response_model.__name__,
            "strict": True,
            "schema": schema,
        },
    }


def _message_content(completion: Any, *, provider: str, model: str, operation: str) -> str:
    try:
        message = completion.choices[0].message
        content = message.content
    except (AttributeError, IndexError) as exc:
        raise StructuredOutputError(
            "provider response did not include a message",
            provider=provider,
            model=model,
            operation=operation,
        ) from exc
    if not content:
        refusal = getattr(message, "refusal", None)
        detail = "provider response was empty"
        if refusal:
            detail = "provider refused the request"
        raise StructuredOutputError(detail, provider=provider, model=model, operation=operation)
    return content


def _provider_code(exc: OpenAIError) -> str:
    code = getattr(exc, "code", None)
    if code:
        return str(code)
    body = getattr(exc, "body", None)
    if isinstance(body, Mapping):
        nested = body.get("error")
        if isinstance(nested, Mapping) and nested.get("code"):
            return str(nested["code"])
        if body.get("code"):
            return str(body["code"])
    return ""


def _looks_like_quota(code: str, exc: OpenAIError) -> bool:
    text = f"{code} {getattr(exc, 'message', '')} {type(exc).__name__}".casefold()
    return any(token in text for token in ("quota", "insufficient_quota", "credits"))


def _looks_like_structured_output_error(code: str, exc: OpenAIError) -> bool:
    text = f"{code} {getattr(exc, 'param', '')}".casefold()
    return any(token in text for token in ("response_format", "json_schema", "structured"))
