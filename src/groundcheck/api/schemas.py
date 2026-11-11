"""Pydantic v2 schemas for GroundCheck REST API."""

from __future__ import annotations

from typing import Any, Literal
from pydantic import BaseModel, Field


class FactModel(BaseModel):
    id: str = Field(..., description="Unique fact identifier, e.g. velocity_72h")
    label: str = Field(..., description="Human-readable label")
    value: float = Field(..., description="Exact numeric ground truth")
    unit: Literal["views", "ratio", "pct", "count"] = Field(..., description="Metric unit")
    query: str = Field(default="provided", description="Provenance or calculation query")
    cue: str | None = Field(default=None, description="Optional custom regex cue")


class SegmentModel(BaseModel):
    text: str
    status: Literal["plain", "ok", "unsupported", "misattributed", "style"]
    detail: str


class VerifyRequest(BaseModel):
    text: str = Field(..., min_length=1, max_length=10000, description="Analytics summary to verify")
    facts: list[FactModel] = Field(..., min_length=1, description="Ground truth facts")
    ignore: list[str] = Field(default_factory=list, description="Entities with digits to ignore (e.g. Apollo 13)")
    enable_semantic_aiml: bool = Field(default=True, description="Enable hybrid semantic n-gram matching")


class VerifyResponse(BaseModel):
    ok: bool = Field(..., description="True if no unsupported numbers, misattributions, or style flags")
    n_numbers: int = Field(..., description="Total numeric claims detected")
    claim_accuracy: float = Field(..., description="Ratio of numbers with correct metric and window")
    groundedness: float = Field(..., description="Ratio of numbers supported by ground truth")
    problems: list[str] = Field(default_factory=list, description="Detailed failure rationales")
    segments: list[SegmentModel] = Field(default_factory=list, description="Annotated text segment spans")
    latency_ms: float = Field(..., description="Processing time in milliseconds")


class BatchItemRequest(BaseModel):
    item_id: str
    text: str
    facts: list[FactModel]
    ignore: list[str] = Field(default_factory=list)


class BatchVerifyRequest(BaseModel):
    items: list[BatchItemRequest] = Field(..., min_length=1, max_length=100)
    enable_semantic_aiml: bool = True


class BatchItemResponse(BaseModel):
    item_id: str
    ok: bool
    n_numbers: int
    claim_accuracy: float
    problems: list[str]
    segments: list[SegmentModel]


class BatchVerifyResponse(BaseModel):
    total_processed: int
    passed: int
    failed: int
    pass_rate: float
    items: list[BatchItemResponse]
    latency_ms: float


class TitleModel(BaseModel):
    id: int
    name: str
    genre: str | None


class HealthResponse(BaseModel):
    status: Literal["healthy", "degraded", "unhealthy"]
    version: str
    database: str
    uptime_seconds: float
    timestamp: str


class ReadinessResponse(BaseModel):
    ready: bool
    checks: dict[str, bool]
