"""Tests for the /tasks/{task_id}/messages data path.

Covers the thread-grouping helpers (pure) and ``task_manager.list_messages``
ordering/filtering against an in-memory SQLite session.
"""

from __future__ import annotations

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.api.tasks import _thread_sort_key, _thread_stage_index
from app.harness import task_manager
from app.storage.mysql.database import Base
from app.storage.mysql.models import AgentMessage


def _make_session():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine, tables=[AgentMessage.__table__])
    return sessionmaker(bind=engine)()


def test_thread_stage_index():
    assert _thread_stage_index("task-1", "task-1") is None
    assert _thread_stage_index("task-1-stage-2", "task-1") == 2
    assert _thread_stage_index("task-1-stage-x", "task-1") is None
    # A different task's thread id is not the main thread
    assert _thread_stage_index("task-2", "task-1") is None
    # Retry threads carry an ``-attempt-{m}`` suffix; the stage number is the
    # leading token.
    assert _thread_stage_index("task-1-stage-2-attempt-1", "task-1") == 2
    assert _thread_stage_index("task-1-stage-2-attempt-3", "task-1") == 2
    # The adversarial reverse lane maps to its stage too.
    assert _thread_stage_index("task-1-stage-2-reverse", "task-1") == 2


def test_thread_sort_key_orders_main_then_stages_numerically():
    threads = ["task-1-stage-10", "task-1", "task-1-stage-2", "task-1-stage-1"]
    ordered = sorted(threads, key=lambda t: _thread_sort_key(t, "task-1"))
    assert ordered == ["task-1", "task-1-stage-1", "task-1-stage-2", "task-1-stage-10"]


def test_list_messages_filters_by_task_and_orders():
    db = _make_session()
    rows = [
        AgentMessage(
            task_id="t1", run_id="r", thread_id="t1-stage-1", seq=1,
            message_id="m1", role="ai", content="a",
        ),
        AgentMessage(
            task_id="t1", run_id="r", thread_id="t1-stage-0", seq=0,
            message_id="m0", role="human", content="h",
        ),
        AgentMessage(
            task_id="t1", run_id="r", thread_id="t1-stage-0", seq=1,
            message_id="m2", role="tool", content="t",
        ),
        AgentMessage(
            task_id="t2", run_id="r", thread_id="t2", seq=0,
            message_id="m-other", role="human", content="x",
        ),
    ]
    db.add_all(rows)
    db.commit()

    got = task_manager.list_messages(db, "t1")
    # only t1's messages, ordered by (thread_id, seq)
    assert [(m.thread_id, m.seq) for m in got] == [
        ("t1-stage-0", 0),
        ("t1-stage-0", 1),
        ("t1-stage-1", 1),
    ]
