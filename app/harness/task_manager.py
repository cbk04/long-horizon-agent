"""Task manager — high-level task lifecycle operations."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

from sqlalchemy.orm import Session

from app.harness import event_bus
from app.harness.models import TaskEvent
from app.storage.mysql.models import Evidence, Task


def create_task(
    db: Session,
    goal: str,
    priority: int = 0,
    search_depth: str = "deep",
    output_format: str = "report",
    budget_tokens: int = 200_000,
    budget_seconds: int = 1800,
    budget_cost: float = 5.0,
    user_id: str | None = None,
) -> Task:
    """Create a new task in QUEUED state and emit task.created event."""
    task_id = f"task-{uuid.uuid4().hex[:16]}"

    task = Task(
        id=task_id,
        user_id=user_id,
        goal=goal,
        status="QUEUED",
        priority=priority,
        search_depth=search_depth,
        output_format=output_format,
        budget_tokens=budget_tokens,
        budget_seconds=budget_seconds,
        budget_cost=budget_cost,
    )
    db.add(task)
    db.commit()
    db.refresh(task)

    event_bus.publish(
        db,
        task_id,
        "task.created",
        {
            "goal": goal,
            "priority": priority,
            "search_depth": search_depth,
            "output_format": output_format,
            "budget": {
                "tokens": budget_tokens,
                "seconds": budget_seconds,
                "cost": budget_cost,
            },
        },
    )

    return task


def get_task(db: Session, task_id: str) -> Task | None:
    return db.query(Task).filter(Task.id == task_id).one_or_none()


def cancel_task(db: Session, task_id: str) -> tuple[Task | None, str]:
    """Cancel a task. If queued, mark cancelled immediately. If running, set cancel flag."""
    from app.storage.redis.client import redis_client

    task = get_task(db, task_id)
    if not task:
        return None, "Task not found"
    if task.status in ("COMPLETED", "FAILED", "CANCELLED"):
        return task, f"Task already in terminal state: {task.status}"

    # Set cancel flag in Redis (Worker checks this)
    _ = redis_client.set(f"task:{task_id}:cancel", "1", ex=86400)
    event_bus.publish(db, task_id, "task.cancel_requested", {"reason": "user_requested"})

    # If still queued, mark cancelled immediately
    if task.status == "QUEUED":
        task.status = "CANCELLED"
        task.updated_at = datetime.now(timezone.utc)
        db.commit()
        event_bus.publish(db, task_id, "task.cancelled", {"reason": "user_requested_while_queued"})

    return task, "Cancel requested"


def is_cancelled(task_id: str) -> bool:
    """Check if a task has been cancelled."""
    from app.storage.redis.client import redis_client

    return redis_client.get(f"task:{task_id}:cancel") is not None


def list_events(db: Session, task_id: str, limit: int = 100, offset: int = 0) -> list[TaskEvent]:
    return (
        db.query(TaskEvent)
        .filter(TaskEvent.task_id == task_id)
        .order_by(TaskEvent.id.asc())
        .offset(offset)
        .limit(limit)
        .all()
    )


def list_evidence(db: Session, task_id: str, limit: int = 100) -> list[Evidence]:
    """List evidence for a task."""
    return (
        db.query(Evidence)
        .filter(Evidence.task_id == task_id)
        .order_by(Evidence.created_at.asc())
        .limit(limit)
        .all()
    )


def update_status(
    db: Session,
    task_id: str,
    status: str,
    result_ref: str | None = None,
    error_message: str | None = None,
) -> None:
    """Update task status. Should be paired with an event publish."""
    task = get_task(db, task_id)
    if not task:
        return
    task.status = status
    task.updated_at = datetime.now(timezone.utc)
    if result_ref is not None:
        task.result_ref = result_ref
    if error_message is not None:
        task.result_ref = f"ERROR: {error_message}"
    db.commit()
