"""Worker — background process that picks up QUEUED tasks and runs the agent.

MVP: no lease/heartbeat/recovery. Simple poll → execute → complete.
V2 will add crash recovery via lease expiry + LangGraph checkpoint resume.
"""

from __future__ import annotations

import logging
import signal
import sys
import threading
import uuid
from datetime import datetime, timezone
from typing import Any

from sqlalchemy.orm import Session

from app.agent.staged import AwaitingApproval
from app.config import get_settings
from app.harness import budget, event_bus
from app.harness.task_manager import update_status
from app.storage.mysql.database import SessionLocal
from app.storage.mysql.models import AgentRun, Task
from app.storage.redis.client import redis_client

logger = logging.getLogger(__name__)

# Global flag to coordinate graceful shutdown
_shutdown = threading.Event()


def _claim_next_queued(db: Session) -> Task | None:
    """Atomically claim the next QUEUED task.

    Uses SELECT ... FOR UPDATE SKIP LOCKED for safe concurrent claiming.
    Sets status to RUNNING and returns the task.
    """
    from sqlalchemy import text

    row = db.execute(
        text(
            """
            SELECT id FROM task
            WHERE status = 'QUEUED'
            ORDER BY priority DESC, created_at ASC
            LIMIT 1
            FOR UPDATE SKIP LOCKED
            """
        )
    ).fetchone()
    if not row:
        return None

    task = db.query(Task).filter(Task.id == row[0]).one()
    task.status = "RUNNING"
    task.updated_at = datetime.now(timezone.utc)
    db.commit()
    return task


def _create_agent_run(db: Session, task_id: str) -> str:
    """Create a new AgentRun record, return run_id."""
    run_id = f"run-{uuid.uuid4().hex[:16]}"
    run = AgentRun(
        id=run_id,
        task_id=task_id,
        thread_id=task_id,
        status="RUNNING",
        started_at=datetime.now(timezone.utc),
    )
    db.add(run)
    task = db.query(Task).filter(Task.id == task_id).one()
    task.current_run_id = run_id
    db.commit()
    return run_id


def _finalize_run(
    db: Session,
    run_id: str,
    status: str,
    final_output: str | None = None,
    error_message: str | None = None,
) -> None:
    """Close out an AgentRun: terminal status, timestamps, and the durable
    final answer text (the only place the full result is persisted)."""
    run = db.query(AgentRun).filter(AgentRun.id == run_id).one()
    run.status = status
    run.ended_at = datetime.now(timezone.utc)
    if final_output is not None:
        run.final_output = final_output
    if error_message is not None:
        run.error_message = error_message
    db.commit()


def _execute_agent(
    task_id: str,
    run_id: str,
    goal: str,
    budget_limits: budget.BudgetLimits,
    resume: dict[str, Any] | None = None,
) -> str:
    """Run the plan-and-execute agent for a task.

    Returns the agent's final answer text (resume pass only).
    """
    from app.agent.staged import execute_agent

    return execute_agent(task_id, run_id, goal, budget_limits, resume=resume)


def _process_task(task_id: str, worker_id: str) -> None:
    """Process a single task: create run → execute → finalize."""
    db = SessionLocal()
    try:
        resume_flag = redis_client.get(f"task:{task_id}:resume")
        resume = {"approved": True} if resume_flag == "approved" else None

        task = db.query(Task).filter(Task.id == task_id).one()
        goal = task.goal
        budget_limits = budget.BudgetLimits(
            max_tokens=task.budget_tokens,
            max_seconds=task.budget_seconds,
            max_tool_calls=get_settings().default_max_tool_calls,
            max_cost=task.budget_cost,
        )

        if resume is None:
            run_id = _create_agent_run(db, task_id)
        else:
            run_id = task.current_run_id or _create_agent_run(db, task_id)
    finally:
        db.close()

    try:
        result_text = _execute_agent(task_id, run_id, goal, budget_limits, resume)
        if resume is not None:
            redis_client.delete(f"task:{task_id}:resume")
        db = SessionLocal()
        try:
            update_status(db, task_id, "COMPLETED", result_ref=f"run:{run_id}")
            _finalize_run(db, run_id, "COMPLETED", final_output=result_text)
            event_bus.publish(db, task_id, "task.completed", {
                "run_id": run_id,
                "result": result_text,
                "result_preview": result_text[:500],
            })
        finally:
            db.close()
    except AwaitingApproval:
        logger.info(f"[{task_id}] plan awaiting approval")
        db = SessionLocal()
        try:
            update_status(db, task_id, "PAUSED")
            event_bus.publish(db, task_id, "task.awaiting_approval", {"run_id": run_id})
        finally:
            db.close()
    except budget.BudgetExceeded as e:
        logger.warning(f"[{task_id}] budget exceeded: {e}")
        db = SessionLocal()
        try:
            update_status(db, task_id, "FAILED", error_message=f"Budget exceeded: {e.reason}")
            _finalize_run(db, run_id, "FAILED", error_message=f"Budget exceeded: {e.reason}")
            event_bus.publish(db, task_id, "task.budget_exceeded", {
                "run_id": run_id,
                "reason": e.reason,
                "usage": {
                    "input_tokens": e.usage.input_tokens,
                    "output_tokens": e.usage.output_tokens,
                    "tool_calls": e.usage.tool_calls,
                    "estimated_cost": e.usage.estimated_cost,
                },
            })
        finally:
            db.close()
    except Exception as e:
        # Check if it was a user cancellation. LangGraph may wrap the exception
        # raised inside a node, so fall back to the cancel flag as the source
        # of truth (a stray transient error must not mask an explicit cancel).
        from app.agent.errors import CancelledByUser
        from app.harness.task_manager import is_cancelled

        cancelled = isinstance(e, CancelledByUser)
        if not cancelled:
            try:
                cancelled = is_cancelled(task_id)
            except Exception:
                cancelled = False
        if cancelled:
            logger.info(f"[{task_id}] cancelled by user")
            db = SessionLocal()
            try:
                update_status(db, task_id, "CANCELLED")
                _finalize_run(db, run_id, "CANCELLED", error_message="cancelled by user")
                event_bus.publish(db, task_id, "task.cancelled", {
                    "run_id": run_id,
                    "reason": "user_requested",
                })
            finally:
                db.close()
        else:
            logger.exception(f"[{task_id}] execution error: {e}")
            db = SessionLocal()
            try:
                update_status(db, task_id, "FAILED", error_message=str(e))
                _finalize_run(db, run_id, "FAILED", error_message=str(e))
                event_bus.publish(db, task_id, "task.failed", {"run_id": run_id, "error": str(e)})
            finally:
                db.close()


def run_worker() -> None:
    """Main worker loop. Runs until SIGINT/SIGTERM."""
    settings = get_settings()
    worker_id = settings.worker_id
    logger.info(f"Worker {worker_id} starting...")

    def _on_signal(signum: int, frame: Any) -> None:
        logger.info(f"Worker received signal {signum}, shutting down...")
        _shutdown.set()

    _ = signal.signal(signal.SIGINT, _on_signal)
    _ = signal.signal(signal.SIGTERM, _on_signal)

    while not _shutdown.is_set():
        db = SessionLocal()
        try:
            task = _claim_next_queued(db)
            task_id = task.id if task else None
        finally:
            db.close()

        if task_id is None:
            if _shutdown.wait(timeout=5):
                break
            continue

        logger.info(f"Worker {worker_id} claimed task {task_id}")
        try:
            _process_task(task_id, worker_id)
        except Exception as e:
            logger.exception(f"Error processing task {task_id}: {e}")

    logger.info(f"Worker {worker_id} stopped")


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    run_worker()
    sys.exit(0)
