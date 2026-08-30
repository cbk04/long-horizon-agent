"""Worker — background process that picks up QUEUED tasks and runs the agent.

MVP: no lease/heartbeat/recovery. Simple poll → execute → complete.
V2 will add crash recovery via lease expiry + LangGraph checkpoint resume.
"""

from __future__ import annotations

import logging
import signal
import sys
import threading
import time
import uuid
from datetime import datetime, timezone
from typing import Any

from sqlalchemy.orm import Session

from app.config import get_settings
from app.harness import budget, event_bus
from app.harness.task_manager import update_status
from app.storage.mysql.database import SessionLocal
from app.storage.mysql.models import AgentRun, Task

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


def _execute_agent_stub(task_id: str, run_id: str) -> None:
    """MVP stub: simulate agent execution with a few events.

    Phase 3 will replace this with LangGraph ReAct loop.
    """
    db = SessionLocal()
    try:
        event_bus.publish(db, task_id, "agent_run.started", {"run_id": run_id})

        for step_no in range(1, 4):
            event_bus.publish(
                db,
                task_id,
                "step.started",
                {"run_id": run_id, "step_no": step_no, "phase": "search" if step_no == 1 else "synthesize"},
            )
            budget.add_tool_call(task_id)
            budget.add_input_tokens(task_id, 1000)
            budget.add_output_tokens(task_id, 200)
            time.sleep(0.2)
            event_bus.publish(
                db,
                task_id,
                "step.completed",
                {"run_id": run_id, "step_no": step_no, "input_tokens": 1000, "output_tokens": 200},
            )

        result = f"Stub result for task {task_id} — Phase 3 will produce real research output."
        event_bus.publish(db, task_id, "agent_run.completed", {"run_id": run_id, "result": result})
    finally:
        db.close()


def _process_task(task_id: str, worker_id: str) -> None:
    """Process a single task: create run → execute → finalize."""
    db = SessionLocal()
    try:
        run_id = _create_agent_run(db, task_id)
    finally:
        db.close()

    try:
        _execute_agent_stub(task_id, run_id)
        db = SessionLocal()
        try:
            update_status(db, task_id, "COMPLETED", result_ref=f"run:{run_id}")
            event_bus.publish(db, task_id, "task.completed", {"run_id": run_id})
        finally:
            db.close()
    except Exception as e:
        logger.exception(f"[{task_id}] execution error: {e}")
        db = SessionLocal()
        try:
            update_status(db, task_id, "FAILED", error_message=str(e))
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
        finally:
            db.close()

        if task is None:
            if _shutdown.wait(timeout=5):
                break
            continue

        logger.info(f"Worker {worker_id} claimed task {task.id}")
        try:
            _process_task(task.id, worker_id)
        except Exception as e:
            logger.exception(f"Error processing task {task.id}: {e}")

    logger.info(f"Worker {worker_id} stopped")


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    run_worker()
    sys.exit(0)
