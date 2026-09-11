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


class ApproveResponse(BaseModel):
    """Response for plan approve/reject endpoints."""

    task_id: str
    status: str
    message: str


class ResultResponse(BaseModel):
    """Response body for GET /tasks/{task_id}/result — the durable final answer."""

    task_id: str
    run_id: str
    run_status: str
    final_output: str | None


class MessageResponse(BaseModel):
    """One conversation message from the ``agent_message`` mirror.

    ``content`` carries the message text (or the tool's returned result for a
    ToolMessage); ``reasoning_content`` the model's chain-of-thought when the
    backend emits it; ``tool_calls`` the tool invocations an AIMessage made.
    """

    id: int
    role: str
    content: str
    reasoning_content: str | None
    tool_name: str | None
    tool_call_id: str | None
    tool_calls: list | None
    seq: int
    created_at: datetime


class ThreadMessagesResponse(BaseModel):
    """One conversation thread (main graph or a stage) with its messages.

    ``stage_index`` is None for the main graph thread (``thread_id == task_id``)
    and the 0-based stage number for a ``{task_id}-stage-{n}`` thread.
    """

    thread_id: str
    stage_index: int | None
    messages: list[MessageResponse]


class RunResponse(BaseModel):
    """One ``agent_run`` row — one execution of the agent for a task."""

    id: str
    task_id: str
    thread_id: str
    status: str
    started_at: datetime | None
    ended_at: datetime | None
    checkpoint_ref: str | None
    retry_count: int
    error_message: str | None
    final_output: str | None
    created_at: datetime


class EvaluationResponse(BaseModel):
    """One ``stage_evaluation`` row, normalized to the ``stage.evaluated`` shape.

    ``phase1``/``phase2``/``phase3`` reconstruct the evaluator's three-phase
    verdict from the table's flat columns (``rule_result`` / ``forward_result``
    & ``reverse_result`` / ``criteria_scores`` & ``weighted_score``) so the
    dashboard can render DB rows exactly like the live SSE payload.
    """

    id: str
    task_id: str
    run_id: str
    stage_index: int
    attempt: int
    defense_round: int
    status: str
    phase1: dict[str, Any]
    phase2: dict[str, Any] | None
    phase3: dict[str, Any] | None
    weighted_score: float | None
    feedback: list | None
    acceptance_snapshot: list | None
    created_at: datetime


class BudgetUsageResponse(BaseModel):
    """Accumulated budget consumption for a task."""

    task_id: str
    input_tokens: int
    output_tokens: int
    tool_calls: int
    elapsed_ms: int
    estimated_cost: float
    updated_at: datetime
