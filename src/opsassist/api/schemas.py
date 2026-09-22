"""Public API contracts. These are the stable surface; internal types can change freely."""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from opsassist.gateway.gateway import AttemptRecord

MAX_MESSAGE_CHARS = 8000


class ApiModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


# ------------------------------------------------------------------ requests


class ChatRequest(ApiModel):
    message: str = Field(min_length=1, max_length=MAX_MESSAGE_CHARS)
    conversation_id: uuid.UUID | None = None
    model: str | None = Field(
        default=None,
        max_length=100,
        description="Route name (e.g. 'chat-default') or model id (e.g. 'ollama/llama3.2-3b').",
    )
    temperature: float = Field(default=0.2, ge=0.0, le=1.0)
    max_tokens: int = Field(default=1024, ge=1, le=4096)

    @field_validator("message")
    @classmethod
    def _not_blank(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("message must not be blank")
        return v


class EmbeddingRequest(ApiModel):
    input: list[str] = Field(min_length=1, max_length=64)
    model: str | None = Field(default=None, max_length=100)
    input_type: Literal["query", "passage"] = "query"

    @field_validator("input")
    @classmethod
    def _bounded(cls, v: list[str]) -> list[str]:
        if any(not s.strip() or len(s) > MAX_MESSAGE_CHARS for s in v):
            raise ValueError(f"each input must be non-blank and at most {MAX_MESSAGE_CHARS} chars")
        return v


class ConversationUpdate(ApiModel):
    """Switch the conversation's model without sending a message."""

    model: str = Field(min_length=1, max_length=100)


class DevTokenRequest(ApiModel):
    user_id: str = Field(pattern=r"^U[0-9]{3,}$")


# ------------------------------------------------------------------ responses


class UsageOut(BaseModel):
    prompt_tokens: int
    completion_tokens: int
    total_tokens: int
    estimated: bool
    cost_usd: Decimal


class AttemptOut(BaseModel):
    """What the client may see about each provider attempt: enough to understand a fallback,
    no provider error text (which can echo request content or internal detail)."""

    model: str
    provider: str
    attempt: int
    outcome: str
    error_type: str | None
    latency_ms: float

    @classmethod
    def of(cls, a: AttemptRecord) -> AttemptOut:
        return cls(
            model=a.model_id,
            provider=a.provider,
            attempt=a.attempt,
            outcome=a.outcome if a.outcome != "skipped" else f"skipped:{a.detail}",
            error_type=a.error_type,
            latency_ms=a.latency_ms,
        )


class ModelRef(BaseModel):
    id: str
    provider: str


class CitationOut(BaseModel):
    number: int
    doc_key: str
    version: int
    title: str
    locator: str
    ref: str
    label: str
    snippet: str


class RetrievalOut(BaseModel):
    candidates: int
    used: int
    below_threshold: int
    latency_ms: float
    embedding_model: str


class ChatResponse(BaseModel):
    conversation_id: uuid.UUID
    message_id: uuid.UUID
    content: str
    citations: list[CitationOut]
    grounded: bool
    abstained: bool
    # None when no model was called (nothing relevant was retrieved -> abstention).
    model: ModelRef | None
    route: str | None
    fallback_used: bool
    finish_reason: str | None
    usage: UsageOut
    latency_ms: float
    retrieval: RetrievalOut
    attempts: list[AttemptOut]
    request_id: str


class SearchRequest(ApiModel):
    query: str = Field(min_length=1, max_length=MAX_MESSAGE_CHARS)
    top_k: int = Field(default=4, ge=1, le=20)


class SearchHit(BaseModel):
    rank: int
    doc_key: str
    version: int
    title: str
    locator: str
    ref: str
    content: str
    similarity: float | None
    fts_rank: float | None
    score: float


class SearchResponse(BaseModel):
    hits: list[SearchHit]
    retrieval: RetrievalOut
    request_id: str


class EmbeddingItem(BaseModel):
    index: int
    embedding: list[float]


class EmbeddingResponse(BaseModel):
    model: ModelRef
    dimensions: int
    data: list[EmbeddingItem]
    usage: UsageOut
    request_id: str


class ModelInfo(BaseModel):
    id: str
    kind: Literal["chat", "embedding"]
    provider: str
    selectable: bool
    available: bool
    unavailable_reason: str | None
    circuit: str
    data_egress: bool
    context_window: int | None
    dimensions: int | None
    input_usd_per_mtok: Decimal
    output_usd_per_mtok: Decimal


class ModelsResponse(BaseModel):
    selectable: list[str]
    models: list[ModelInfo]
    routes: dict[str, list[str]]
    defaults: dict[str, str]


class MessageOut(BaseModel):
    id: uuid.UUID
    role: str
    content: str
    status: str
    model: str | None
    prompt_tokens: int | None
    completion_tokens: int | None
    citations: list[dict[str, object]] | None
    request_id: str | None
    created_at: datetime


class ConversationUsage(BaseModel):
    model_calls: int
    chat_calls: int
    embedding_calls: int  # query embeddings for retrieval
    prompt_tokens: int
    completion_tokens: int
    cost_usd: Decimal


class ConversationResponse(BaseModel):
    id: uuid.UUID
    title: str | None
    model: str | None
    created_at: datetime
    updated_at: datetime
    messages: list[MessageOut]
    usage: ConversationUsage


class DevTokenResponse(BaseModel):
    access_token: str
    token_type: Literal["bearer"] = "bearer"  # noqa: S105 - OAuth token type, not a secret
    expires_in: int
    user_id: str
    role: str


class ErrorBody(BaseModel):
    code: str
    message: str
    request_id: str


class ErrorResponse(BaseModel):
    error: ErrorBody
    attempts: list[AttemptOut] = []
