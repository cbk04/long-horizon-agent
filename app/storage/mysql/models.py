"""ORM models for the core tables.

task, agent_run, step, evidence, artifact, task_budget_usage, task_event,
stage_evaluation, agent_message.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import JSON, BigInteger, DateTime, Enum, Float, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.sql import func

from app.storage.mysql.database import Base


class Task(Base):
    """Task — user-submitted long-horizon goal. Source of truth for task metadata."""

    __tablename__: str = "task"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    goal: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(
        Enum(
            "CREATED",
            "QUEUED",
            "RUNNING",
            "PAUSED",
            "FAILED",
            "COMPLETED",
            "CANCELLED",
            name="task_status",
        ),
        default="CREATED",
        nullable=False,
    )
    priority: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    search_depth: Mapped[str] = mapped_column(String(16), default="deep", nullable=False)
    output_format: Mapped[str] = mapped_column(String(16), default="report", nullable=False)

    # Budget limits (set at creation)
    budget_tokens: Mapped[int] = mapped_column(Integer, nullable=False)
    budget_seconds: Mapped[int] = mapped_column(Integer, nullable=False)
    budget_cost: Mapped[float] = mapped_column(Float, nullable=False)

    # Runtime (lease fields removed in migration c1f29a4b9156; MVP has no lease/heartbeat)
    current_run_id: Mapped[str | None] = mapped_column(String(64), nullable=True)

    # Final result
    result_ref: Mapped[str | None] = mapped_column(Text, nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now(), nullable=False
    )


class AgentRun(Base):
    """AgentRun — one execution of the LangGraph runtime for a task."""

    __tablename__: str = "agent_run"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    task_id: Mapped[str] = mapped_column(String(64), index=True, nullable=False)
    thread_id: Mapped[str] = mapped_column(String(128), nullable=False)
    status: Mapped[str] = mapped_column(
        Enum("PENDING", "RUNNING", "COMPLETED", "FAILED", "CANCELLED", name="run_status"),
        default="PENDING",
        nullable=False,
    )
    started_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    ended_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    checkpoint_ref: Mapped[str | None] = mapped_column(String(256), nullable=True)
    retry_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Full final answer text, written by the worker on task completion
    final_output: Mapped[str | None] = mapped_column(Text, nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), nullable=False)


class Step(Base):
    """Step — one logical step within an agent run."""

    __tablename__: str = "step"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    run_id: Mapped[str] = mapped_column(String(64), index=True, nullable=False)
    step_no: Mapped[int] = mapped_column(Integer, nullable=False)
    phase: Mapped[str] = mapped_column(String(32), nullable=False)
    status: Mapped[str] = mapped_column(
        Enum("PENDING", "RUNNING", "COMPLETED", "FAILED", name="step_status"),
        default="PENDING",
        nullable=False,
    )
    action_type: Mapped[str | None] = mapped_column(String(64), nullable=True)
    action_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    ended_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    error_code: Mapped[str | None] = mapped_column(String(128), nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), nullable=False)


class Evidence(Base):
    """Evidence — a fragment extracted from a source, attributable to a claim."""

    __tablename__: str = "evidence"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    task_id: Mapped[str] = mapped_column(String(64), index=True, nullable=False)
    source_url: Mapped[str] = mapped_column(Text, nullable=False)
    source_title: Mapped[str | None] = mapped_column(Text, nullable=True)
    locator: Mapped[str | None] = mapped_column(String(256), nullable=True)
    content_ref: Mapped[str | None] = mapped_column(Text, nullable=True)
    claim_id: Mapped[str | None] = mapped_column(String(64), index=True, nullable=True)
    quality_score: Mapped[float | None] = mapped_column(Float, nullable=True)
    # Stage-handoff fields: which stage harvested this evidence, plus a cheap
    # summary for the index and the extracted content for on-demand retrieval.
    run_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    stage_index: Mapped[int | None] = mapped_column(Integer, nullable=True)
    summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    content: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), nullable=False)


class Artifact(Base):
    """Artifact — large object (webpage snapshot, raw document) stored in object storage."""

    __tablename__: str = "artifact"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    task_id: Mapped[str] = mapped_column(String(64), index=True, nullable=False)
    type: Mapped[str] = mapped_column(String(32), nullable=False)
    object_key: Mapped[str] = mapped_column(String(512), nullable=False)
    mime_type: Mapped[str | None] = mapped_column(String(128), nullable=True)
    size: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    checksum: Mapped[str | None] = mapped_column(String(128), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), nullable=False)


class TaskBudgetUsage(Base):
    """TaskBudgetUsage — accumulated budget consumption for a task."""

    __tablename__: str = "task_budget_usage"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    task_id: Mapped[str] = mapped_column(String(64), index=True, nullable=False)
    input_tokens: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    output_tokens: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    tool_calls: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    elapsed_ms: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    estimated_cost: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now(), nullable=False
    )


class StageEvaluation(Base):
    """StageEvaluation — one evaluator pass over one stage (three-phase pipeline).

    Every evaluate-node run writes a row: full pipeline, rule short-circuit,
    DEFEND verdict, or post-defense re-adjudication. ``acceptance_snapshot``
    freezes the acceptance list (with weights) used at scoring time, since the
    plan itself only lives in the event stream.
    """

    __tablename__: str = "stage_evaluation"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    task_id: Mapped[str] = mapped_column(String(64), index=True, nullable=False)
    run_id: Mapped[str] = mapped_column(String(64), nullable=False)
    stage_index: Mapped[int] = mapped_column(Integer, nullable=False)
    attempt: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    defense_round: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    status: Mapped[str] = mapped_column(
        Enum("PASSED", "RETRY", "DEFEND", "DEGRADED_PASS", name="stage_eval_status"),
        nullable=False,
    )
    rule_result: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    forward_result: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    reverse_result: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    criteria_scores: Mapped[list | None] = mapped_column(JSON, nullable=True)
    weighted_score: Mapped[float | None] = mapped_column(Float, nullable=True)
    feedback: Mapped[list | None] = mapped_column(JSON, nullable=True)
    acceptance_snapshot: Mapped[list | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), nullable=False)


class StageOutput(Base):
    """StageOutput — the accepted output of one completed stage.

    One row per stage that advanced (the conclusion + its distilled findings +
    evidence index). The stage's ReAct subgraph runs on its own thread and cannot
    read the outer graph's ``AgentState``, so this durable copy is what the
    retrieval tools (``list_prior_outputs`` / ``get_stage_output``) query — the
    safety net when the planner under-declared a ``depends_on``.
    """

    __tablename__: str = "stage_output"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    task_id: Mapped[str] = mapped_column(String(64), index=True, nullable=False)
    run_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    stage_index: Mapped[int] = mapped_column(Integer, nullable=False)
    objective: Mapped[str] = mapped_column(Text, nullable=False)
    conclusion: Mapped[str] = mapped_column(Text, nullable=False)
    key_findings: Mapped[list | None] = mapped_column(JSON, nullable=True)
    evidence_index: Mapped[list | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), nullable=False)


class AgentMessage(Base):
    """AgentMessage — one conversation message of a react thread, queryable.

    Business-level persistence of the agent's conversation, written by the
    ``MessagePersistenceMiddleware`` after each model call (side-channel: the
    checkpoint remains the execution source of truth; rows here exist so the
    frontend / audit can read the dialogue without parsing checkpoint blobs).
    Dedup key is the LangChain message id — re-persisting a thread is a no-op.
    """

    __tablename__: str = "agent_message"

    id: Mapped[int] = mapped_column(
        # INTEGER variant only so SQLite tests can autoincrement; MySQL DDL stays BIGINT
        BigInteger().with_variant(Integer(), "sqlite"),
        primary_key=True,
        autoincrement=True,
    )
    task_id: Mapped[str] = mapped_column(String(64), index=True, nullable=False)
    run_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    thread_id: Mapped[str] = mapped_column(String(128), nullable=False)
    # Position of the message within its thread (index in the state's message list)
    seq: Mapped[int] = mapped_column(Integer, nullable=False)
    # LangChain message id — unique per message, stable across re-invocations
    message_id: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    # human | ai | tool | system
    role: Mapped[str] = mapped_column(String(16), nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    # Model chain-of-thought (additional_kwargs["reasoning_content"]) when the backend emits it
    reasoning_content: Mapped[str | None] = mapped_column(Text, nullable=True)
    tool_name: Mapped[str | None] = mapped_column(String(64), nullable=True)
    tool_call_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    tool_calls: Mapped[list | None] = mapped_column(JSON, nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), nullable=False)
