"""ORM models for the 6 core tables: task, agent_run, step, evidence, artifact, task_budget_usage."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import BigInteger, DateTime, Enum, Float, Integer, String, Text
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
