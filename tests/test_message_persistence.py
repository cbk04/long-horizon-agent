"""Unit tests for the message persistence middleware's DB seam.

Runs ``persist_thread_messages`` against an in-memory SQLite session: verifies
role/field mapping, position (seq) stability, and idempotency across repeated
calls — the property the after_model hook relies on when it re-scans the whole
thread after every model call.
"""

from __future__ import annotations

import json

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.agent.middleware.message_persistence import (
    _task_id_from_thread,
    persist_thread_messages,
)
from app.storage.mysql.database import Base
from app.storage.mysql.models import AgentMessage


def _make_session():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine, tables=[AgentMessage.__table__])
    return sessionmaker(bind=engine)()


def test_task_id_from_thread():
    assert _task_id_from_thread("task-abc") == "task-abc"
    assert _task_id_from_thread("task-abc-stage-2") == "task-abc"


def test_persists_all_roles_with_fields():
    db = _make_session()
    messages = [
        HumanMessage(id="m-h", content="研究一下X"),
        AIMessage(
            id="m-ai-1",
            content="我先搜索",
            tool_calls=[{"id": "call-1", "name": "web_search", "args": {"query": "X"}, "type": "tool_call"}],
            additional_kwargs={"reasoning_content": "思考过程"},
        ),
        ToolMessage(id="m-tool-1", content="搜索结果", name="web_search", tool_call_id="call-1"),
        AIMessage(id="m-ai-2", content=[{"type": "text", "text": "结论"}]),
    ]

    inserted = persist_thread_messages(
        db,
        task_id="task-1",
        run_id="run-1",
        thread_id="task-1-stage-0",
        stage_index=0,
        messages=messages,
    )
    assert inserted == 4

    rows = db.query(AgentMessage).order_by(AgentMessage.seq).all()
    assert [r.role for r in rows] == ["human", "ai", "tool", "ai"]
    assert [r.seq for r in rows] == [0, 1, 2, 3]
    assert all(r.task_id == "task-1" for r in rows)
    assert all(r.thread_id == "task-1-stage-0" for r in rows)

    ai1 = rows[1]
    assert ai1.tool_calls == [{"id": "call-1", "name": "web_search", "args": {"query": "X"}}]
    assert ai1.reasoning_content == "思考过程"
    assert ai1.tool_name is None

    tool = rows[2]
    assert tool.tool_name == "web_search"
    assert tool.tool_call_id == "call-1"
    assert tool.tool_calls is None

    # Non-string content (multimodal block list) is JSON-encoded, not lost
    assert json.loads(rows[3].content) == [{"type": "text", "text": "结论"}]


def test_idempotent_on_rescan_and_append():
    db = _make_session()
    base = [HumanMessage(id=f"h-{i}", content=f"msg-{i}") for i in range(3)]
    kw = dict(
        task_id="task-1",
        run_id=None,
        thread_id="task-1",
        stage_index=None,
    )

    assert persist_thread_messages(db, messages=base, **kw) == 3
    # Same thread re-scanned after the next model call → nothing new
    assert persist_thread_messages(db, messages=base, **kw) == 0
    # One message appended (the new model reply) → exactly one new row
    extended = base + [AIMessage(id="a-1", content="done")]
    assert persist_thread_messages(db, messages=extended, **kw) == 1
    assert db.query(AgentMessage).count() == 4


def test_messages_without_id_are_skipped():
    db = _make_session()
    assert (
        persist_thread_messages(
            db,
            task_id="t",
            run_id=None,
            thread_id="t",
            stage_index=None,
            messages=[HumanMessage(id=None, content="no id")],
        )
        == 0
    )
    assert db.query(AgentMessage).count() == 0
