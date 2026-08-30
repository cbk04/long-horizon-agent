"""End-to-end integration test for MVP: create task → worker runs → events.

No lease/heartbeat/recovery — simple poll → execute → complete.
"""

from __future__ import annotations

import json
import os
import sys
import time
import uuid
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from app.harness import budget, event_bus
from app.harness.task_manager import cancel_task, create_task, get_task, update_status
from app.harness.worker import _process_task
from app.storage.mysql.database import SessionLocal
from app.config import get_settings


def test_create_and_run_task():
    """Create a task, run it through the worker, verify completion."""
    settings = get_settings()
    worker_id = f"test-worker-{uuid.uuid4().hex[:6]}"

    # 1. Create task
    db = SessionLocal()
    try:
        task = create_task(
            db,
            goal="Test MVP end-to-end pipeline",
            priority=5,
            search_depth="normal",
            output_format="report",
            budget_tokens=50_000,
            budget_seconds=300,
            budget_cost=1.0,
        )
        task_id = task.id
        print(f"[1] Created task: {task_id} (status={task.status})")
        assert task.status == "QUEUED"
    finally:
        db.close()

    # 2. Process task (run stub agent)
    print(f"[2] Running worker process for {task_id}...")
    _process_task(task_id, worker_id)
    print(f"[2] Worker process completed")

    # 3. Verify final state
    db = SessionLocal()
    try:
        task = get_task(db, task_id)
        print(f"[3] Final task status: {task.status}, result_ref: {task.result_ref}")
        assert task.status == "COMPLETED", f"Expected COMPLETED, got {task.status}"
        assert task.result_ref is not None
    finally:
        db.close()

    # 4. Verify events were persisted
    db = SessionLocal()
    try:
        from app.harness.models import TaskEvent

        events = (
            db.query(TaskEvent)
            .filter(TaskEvent.task_id == task_id)
            .order_by(TaskEvent.id.asc())
            .all()
        )
        print(f"[4] Total events persisted: {len(events)}")
        event_types = [e.type for e in events]
        print(f"    Event types: {event_types}")
        assert "task.created" in event_types
        assert "agent_run.started" in event_types
        assert "task.completed" in event_types
    finally:
        db.close()

    # 5. Verify Redis stream
    from app.storage.redis.client import redis_client

    stream_len = redis_client.xlen(f"task:{task_id}:events")
    print(f"[5] Redis stream length: {stream_len}")
    assert stream_len > 0

    # 6. Verify budget was tracked
    usage = budget.get_usage(task_id)
    print(f"[6] Budget usage: input={usage.input_tokens}, output={usage.output_tokens}, tool_calls={usage.tool_calls}")
    assert usage.input_tokens > 0
    assert usage.tool_calls > 0

    # Cleanup
    event_bus.cleanup(task_id)

    print(f"\n=== TEST PASSED: Task {task_id} completed end-to-end ===")


def test_cancel_queued_task():
    """Create task, cancel it while QUEUED, verify immediate cancel."""
    db = SessionLocal()
    try:
        task = create_task(db, goal="Test cancel while queued")
        task_id = task.id
        print(f"[1] Created task: {task_id} (status={task.status})")
        assert task.status == "QUEUED"
    finally:
        db.close()

    db = SessionLocal()
    try:
        task, message = cancel_task(db, task_id)
        print(f"[2] After cancel: status={task.status}, message={message}")
        assert task.status == "CANCELLED"
    finally:
        db.close()

    # Cleanup
    event_bus.cleanup(task_id)
    from app.storage.redis.client import redis_client

    redis_client.delete(f"task:{task_id}:cancel")

    print(f"=== TEST PASSED: Cancel queued task works ===")


if __name__ == "__main__":
    print("=" * 60)
    print("MVP Integration Tests (no lease/heartbeat)")
    print("=" * 60)
    test_create_and_run_task()
    print()
    test_cancel_queued_task()
    print()
    print("=" * 60)
    print("ALL TESTS PASSED")
    print("=" * 60)
