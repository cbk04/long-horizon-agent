"""Live token / tool streaming to the frontend.

The LLM layer (``app.agent.llm``) runs every model with ``streaming=True`` and
a ``StreamingCallbackHandler`` attached, so each model call — the react agent's
turns, the planner, the evaluator — flows through here. A ``contextvars``
context tells the handler which task a call belongs to and whether its raw
tokens should be surfaced (content streams, e.g. the react agent's answer) or
suppressed (structured-output calls, whose JSON fragments would only confuse).

Live events are written to Redis only (``event_bus.publish_live``): they are
transient by design and never enter the durable MySQL event log. Durable
progress events keep going through ``event_bus.publish``.
"""

from __future__ import annotations

import json
import logging
import threading
import time
import uuid
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any, Iterator

from langchain_core.callbacks import BaseCallbackHandler

from app.agent.errors import CancelledByUser
from app.harness import event_bus
from app.harness.task_manager import is_cancelled

logger = logging.getLogger(__name__)

# contextvar: {"task_id": str, "stream_tokens": bool}
_stream_context: ContextVar[dict[str, Any]] = ContextVar("stream_context", default={})

# Research tools whose activity is worth surfacing in the feed. Structured-output
# tool calls (planning / evaluation schemas) are deliberately excluded.
_VISIBLE_TOOLS = {"web_search", "web_fetch"}

# Throttle token flushes: at most every ~150ms, or as soon as ~80 chars buffer.
_FLUSH_INTERVAL_S = 0.15
_FLUSH_CHARS = 80
_CANCEL_CHECK_INTERVAL_S = 0.5


@contextmanager
def stream_context(task_id: str | None, *, stream_tokens: bool = False) -> Iterator[None]:
    """Bind ``task_id`` (and whether to surface tokens) to the current context.

    Wraps one synchronous model/run call. Nested calls stack and restore
    correctly via ``ContextVar`` semantics.
    """
    token = _stream_context.set({"task_id": task_id, "stream_tokens": stream_tokens})
    try:
        yield
    finally:
        _stream_context.reset(token)


def _is_cancelled(task_id: str) -> bool:
    """Cancel probe that never breaks streaming on a transient Redis error."""
    try:
        return is_cancelled(task_id)
    except Exception:
        return False


class _Stream:
    """Per-model-call token buffer keyed by the LangChain run id."""

    __slots__ = ("task_id", "stream_id", "buffer", "last_flush", "last_cancel_check")

    def __init__(self, task_id: str) -> None:
        self.task_id = task_id
        self.stream_id: str | None = None
        self.buffer: list[str] = []
        self.last_flush = 0.0
        self.last_cancel_check = 0.0


class StreamingCallbackHandler(BaseCallbackHandler):
    """Emit ``token`` / ``stream.*`` / ``tool.*`` live events.

    - Token streams are created lazily on the first non-empty token, so empty
      model turns (a react step that only emits a tool call) produce no noise.
    - Cancel is probed on the throttled flush cadence; raising there aborts the
      in-flight model call promptly instead of waiting for the whole call.
    """

    def __init__(self) -> None:
        self._streams: dict[str, _Stream] = {}
        self._tool_names: dict[str, str] = {}
        self._lock = threading.Lock()

    def _ctx(self) -> dict[str, Any]:
        return _stream_context.get() or {}

    def _flush(self, run_id: str, st: _Stream, *, final: bool) -> None:
        now = time.monotonic()
        if final or now - st.last_cancel_check >= _CANCEL_CHECK_INTERVAL_S:
            st.last_cancel_check = now
            if _is_cancelled(st.task_id):
                raise CancelledByUser(st.task_id)
        text = "".join(st.buffer)
        if text:
            event_bus.publish_live(
                st.task_id, "token", {"stream_id": st.stream_id, "text": text}
            )
            st.buffer = []
            st.last_flush = now
        if final:
            event_bus.publish_live(st.task_id, "stream.ended", {"stream_id": st.stream_id})

    # ── LLM callbacks ────────────────────────────────────────────────────────

    def on_llm_start(self, serialized: dict, prompts: list[str], **kwargs: Any) -> None:
        ctx = self._ctx()
        if not ctx.get("stream_tokens") or not ctx.get("task_id"):
            return
        run_id = kwargs.get("run_id")
        if not run_id:
            return
        with self._lock:
            self._streams[run_id] = _Stream(ctx["task_id"])

    def on_llm_new_token(
        self,
        token: str | list[str | dict[str, Any]],
        *,
        run_id: Any = None,
        **kwargs: Any,
    ) -> None:
        # Only surface plain string tokens; some backends emit token lists here.
        if not isinstance(token, str) or run_id is None:
            return
        with self._lock:
            st = self._streams.get(run_id)
        if st is None or not token:
            return
        if st.stream_id is None:
            st.stream_id = uuid.uuid4().hex
            event_bus.publish_live(st.task_id, "stream.started", {"stream_id": st.stream_id})
        st.buffer.append(token)
        if len(st.buffer) >= _FLUSH_CHARS or (time.monotonic() - st.last_flush) >= _FLUSH_INTERVAL_S:
            self._flush(run_id, st, final=False)

    def on_llm_end(self, response: Any, *, run_id: Any = None, **kwargs: Any) -> None:
        if run_id is None:
            return
        with self._lock:
            st = self._streams.pop(run_id, None)
        if st is None:
            return
        if st.stream_id is not None:
            self._flush(run_id, st, final=True)

    # ── Tool callbacks ───────────────────────────────────────────────────────

    def on_tool_start(self, serialized: dict, input_str: str, **kwargs: Any) -> None:
        ctx = self._ctx()
        task_id = ctx.get("task_id")
        name = serialized.get("name") if isinstance(serialized, dict) else None
        if not task_id or name not in _VISIBLE_TOOLS:
            return
        args: dict[str, Any] = {}
        try:
            args = json.loads(input_str) if input_str else {}
        except (json.JSONDecodeError, TypeError):
            pass
        run_id = kwargs.get("run_id")
        if run_id:
            with self._lock:
                self._tool_names[run_id] = name
        event_bus.publish_live(task_id, "tool.started", {"name": name, "args": args, "run_id": run_id})

    def on_tool_end(self, output: Any, **kwargs: Any) -> None:
        ctx = self._ctx()
        task_id = ctx.get("task_id")
        if not task_id:
            return
        run_id = kwargs.get("run_id")
        with self._lock:
            name = self._tool_names.pop(run_id, None) if run_id else None
        summary = ""
        try:
            if isinstance(output, list):
                summary = f"{len(output)} 条结果"
            elif isinstance(output, str):
                summary = f"{len(output)} 字符"
        except Exception:
            pass
        event_bus.publish_live(
            task_id, "tool.completed", {"name": name, "summary": summary, "run_id": run_id}
        )


_handler: StreamingCallbackHandler | None = None


def stream_handler() -> StreamingCallbackHandler:
    """Return the process-wide handler singleton (attached to every model)."""
    global _handler
    if _handler is None:
        _handler = StreamingCallbackHandler()
    return _handler
