"""MessagePersistenceMiddleware — business-side persistence of the conversation.

The checkpointer keeps execution state in opaque blobs; this middleware mirrors
every message of a react thread into the ``agent_message`` table so the
frontend / audit can read the dialogue without touching checkpoint internals.

Design points:
- Hook: ``after_model`` fires after every model call with the thread's full
  message list; rows are deduped by the LangChain message id, so re-scanning
  the whole list each time is idempotent (a message is inserted exactly once).
- Side-channel: persistence failures are logged, never raised — a business
  mirror must not kill the agent run (the checkpoint keeps state safe).
- task/run/stage context comes in via ``configurable`` keys set by the call
  sites (``task_id`` / ``run_id`` / ``stage_index``); ``task_id`` falls back
  to parsing the staged thread naming (``{task_id}-stage-{n}``).
"""

from __future__ import annotations

import json
import logging
from typing import Any

from langchain.agents.middleware import AgentMiddleware
from langchain_core.messages import AIMessage, ToolMessage
from langgraph.config import get_config

from app.storage.mysql.database import SessionLocal
from app.storage.mysql.models import AgentMessage

logger = logging.getLogger(__name__)


def _content_to_text(content: Any) -> str:
    """String content passes through; multimodal block lists become JSON."""
    if isinstance(content, str):
        return content
    return json.dumps(content, ensure_ascii=False, default=str)


def _serialize_tool_calls(msg: AIMessage) -> list[dict[str, Any]] | None:
    if not msg.tool_calls:
        return None
    return [
        {"id": call.get("id"), "name": call.get("name"), "args": call.get("args")}
        for call in msg.tool_calls
    ]


def _task_id_from_thread(thread_id: str) -> str:
    """Recover the task id from a thread id.

    Staged stage threads are ``{task_id}-stage-{n}``; the task's main thread
    id is the task id itself.
    """
    return thread_id.rsplit("-stage-", 1)[0]


def persist_thread_messages(
    db,
    *,
    task_id: str,
    run_id: str | None,
    thread_id: str,
    stage_index: int | None,
    messages: list,
) -> int:
    """Insert the not-yet-persisted messages of one thread. Returns row count.

    Idempotent: existing message ids for the thread are fetched once and
    skipped, so calling this after every model call with the full list is safe.
    """
    if not messages:
        return 0

    candidate_ids = [m.id for m in messages if getattr(m, "id", None)]
    existing: set[str] = set()
    if candidate_ids:
        existing = {
            row[0]
            for row in db.query(AgentMessage.message_id).filter(
                AgentMessage.thread_id == thread_id,
                AgentMessage.message_id.in_(candidate_ids),
            )
        }

    rows = []
    for seq, msg in enumerate(messages):
        message_id = getattr(msg, "id", None)
        if not message_id or message_id in existing:
            continue
        kwargs = getattr(msg, "additional_kwargs", None)
        rows.append(
            AgentMessage(
                task_id=task_id,
                run_id=run_id,
                thread_id=thread_id,
                seq=seq,
                message_id=message_id,
                role=msg.type,
                content=_content_to_text(msg.content),
                reasoning_content=(
                    kwargs.get("reasoning_content")
                    if isinstance(kwargs, dict) and kwargs.get("reasoning_content")
                    else None
                ),
                tool_name=msg.name if isinstance(msg, ToolMessage) else None,
                tool_call_id=msg.tool_call_id if isinstance(msg, ToolMessage) else None,
                tool_calls=(
                    _serialize_tool_calls(msg) if isinstance(msg, AIMessage) else None
                ),
            )
        )

    if rows:
        db.add_all(rows)
        db.commit()
    return len(rows)


class MessagePersistenceMiddleware(AgentMiddleware):
    """Mirror every message of the react thread into ``agent_message``."""

    def after_model(self, state: Any, runtime: Any) -> dict[str, Any] | None:
        # Always a no-op state update: this hook observes, never mutates.
        # ``config`` is not part of the hook signature; read it from the
        # current LangGraph context instead.
        try:
            config = get_config()
            conf = (config or {}).get("configurable", {}) or {}
            thread_id = conf.get("thread_id")
            if not thread_id:
                return None
            task_id = conf.get("task_id") or _task_id_from_thread(thread_id)
            db = SessionLocal()
            try:
                persist_thread_messages(
                    db,
                    task_id=task_id,
                    run_id=conf.get("run_id"),
                    thread_id=thread_id,
                    stage_index=conf.get("stage_index"),
                    messages=state.get("messages", []),
                )
            finally:
                db.close()
        except Exception:
            # Side-channel only: a persistence failure must not fail the run.
            logger.warning(
                "MessagePersistenceMiddleware: failed to persist messages "
                "(thread=%s)",
                locals().get("thread_id", "?"),
                exc_info=True,
            )
        return None
