"""Pydantic request/response models for the FastAPI control plane."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field


# ── Request Schemas ──────────────────────────────────────────────


class CreateTaskRequest(BaseModel):
    """Request body for POST /api/v1/tasks."""

    goal: str = Field(..., min_length=1, max_length=4096, description="Research goal / question")
    priority: int = Field(default=0, ge=0, le=10, description="Higher = more urgent")
    search_depth: str = Field(default="deep", pattern="^(shallow|normal|deep)$")
    output_format: str = Field(default="report", pattern="^(report|summary|raw)$")
    budget_tokens: int = Field(default=200_000, gt=0)
    budget_seconds: int = Field(default=1800, gt=0)
    budget_cost: float = Field(default=5.0, gt=0.0)


# ── Response Schemas ─────────────────────────────────────────────


class TaskResponse(BaseModel):
    """Response body for task operations."""

    id: str
    user_id: str | None
    goal: str
    status: str
    priority: int
    search_depth: str
    output_format: str
    budget_tokens: int
    budget_seconds: int
    budget_cost: float
    current_run_id: str | None
    result_ref: str | None
    created_at: datetime
    updated_at: datetime


class EventResponse(BaseModel):
    """A single event in a task's stream."""

    id: str = Field(..., description="Redis Stream ID (e.g. '1234-0')")
    task_id: str
    type: str
    payload: dict[str, Any]
    timestamp: datetime


class EvidenceResponse(BaseModel):
    """A single evidence record."""

    id: str
    task_id: str
    source_url: str
    source_title: str | None
    locator: str | None
    content_ref: str | None
    claim_id: str | None
    quality_score: float | None
    created_at: datetime


class CancelResponse(BaseModel):
    """Response for cancel endpoint."""

    task_id: str
    status: str
    message: str
