"""End-to-end integration test for MVP: create task → worker runs → events.

Stubs the agent (no LLM / network) and drives the real worker lifecycle:
create → plan pass (PAUSED at approval gate) → approve → resume → COMPLETED,
verifying events, Redis stream, budget accounting, and that the final answer
is persisted on the AgentRun.
"""

from __future__ import annotations

import os
import sys
import uuid

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import pytest

from app.agent.staged import AwaitingApproval
from app.harness import budget, event_bus
from app.harness.task_manager import approve_plan, cancel_task, create_task, get_task
from app.harness.worker import _process_task
from app.storage.mysql.database import SessionLocal
from app.storage.mysql.models import AgentRun

FAKE_RESULT = "MVP 端到端验证完成：所有阶段通过，最终报告生成。"


@pytest.fixture()
def stub_agent(monkeypatch):
    """Replace the real staged-graph execution with the worker contract:
    first pass pauses at the approval gate, resume pass returns the answer."""
    from app.agent.staged.common import publish

    def fake_execute_agent(task_id, run_id, goal, budget_limits, resume=None):
        if resume is None:
            publish(task_id, "agent_run.started", {"run_id": run_id, "model": "stub"})
            publish(task_id, "plan.generated", {"run_id": run_id, "stages": []})
            raise AwaitingApproval(task_id)
        budget.add_input_tokens(task_id, 1200)
        budget.add_output_tokens(task_id, 800)
        budget.add_tool_call(task_id)
        return FAKE_RESULT

    monkeypatch.setattr("app.harness.worker._execute_agent", fake_execute_agent)


def test_create_and_run_task(stub_agent):
    """Create a task, run it through the worker with the approval gate, verify
    completion and that the final answer is persisted on the AgentRun."""
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

    # 2. First pass: plan generation, then pause at the approval gate
    print(f"[2] Running worker process for {task_id} (plan pass)...")
    _process_task(task_id, worker_id)

    db = SessionLocal()
    try:
        task = get_task(db, task_id)
        print(f"[2] After plan pass: status={task.status}")
        assert task.status == "PAUSED", f"Expected PAUSED at approval gate, got {task.status}"
    finally:
        db.close()

    # 3. Approve the plan, worker resumes and runs all stages
    db = SessionLocal()
    try:
        task, message = approve_plan(db, task_id)
        print(f"[3] Approved: status={task.status}, message={message}")
        assert task.status == "QUEUED"
    finally:
        db.close()

    _process_task(task_id, worker_id)
    print(f"[3] Worker resume completed")

    # 4. Verify final state + persisted final answer
    db = SessionLocal()
    try:
        task = get_task(db, task_id)
        print(f"[4] Final task status: {task.status}, result_ref: {task.result_ref}")
        assert task.status == "COMPLETED", f"Expected COMPLETED, got {task.status}"
        assert task.result_ref is not None

        run = db.query(AgentRun).filter(AgentRun.id == task.current_run_id).one()
        print(f"[4] AgentRun {run.id}: status={run.status}, final_output={len(run.final_output or '')} chars")
        assert run.status == "COMPLETED"
        assert run.final_output == FAKE_RESULT, "final_output must be persisted on completion"
    finally:
        db.close()

    # 5. Verify events were persisted
    db = SessionLocal()
    try:
        from app.harness.models import TaskEvent

        events = (
            db.query(TaskEvent)
            .filter(TaskEvent.task_id == task_id)
            .order_by(TaskEvent.id.asc())
            .all()
        )
        print(f"[5] Total events persisted: {len(events)}")
        event_types = [e.type for e in events]
        print(f"    Event types: {event_types}")
        assert "task.created" in event_types
        assert "agent_run.started" in event_types
        assert "task.awaiting_approval" in event_types
        assert "plan.approved" in event_types
        assert "task.completed" in event_types
    finally:
        db.close()

    # 6. Verify Redis stream
    from app.storage.redis.client import redis_client

    stream_len = redis_client.xlen(f"task:{task_id}:events")
    print(f"[6] Redis stream length: {stream_len}")
    assert stream_len > 0

    # 7. Verify budget was tracked
    usage = budget.get_usage(task_id)
    print(f"[7] Budget usage: input={usage.input_tokens}, output={usage.output_tokens}, tool_calls={usage.tool_calls}")
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
