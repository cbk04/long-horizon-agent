"""ReAct agent — LangChain create_agent with budget enforcement and event emission.

This module builds a LangGraph ReAct agent that:
1. Uses ChatOpenAI as the LLM
2. Has web_search and web_fetch tools
3. Uses MemorySaver checkpointer (thread_id = task_id)
4. Emits structured events for each model/tool call
5. Checks budget before each step; stops if exceeded
6. Checks cancel flag before each step

The agent is invoked by the Worker's _execute_agent() which replaces the MVP stub.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from typing import Any

from langchain.agents import create_agent
from langchain_core.messages import HumanMessage, SystemMessage
from langchain_core.runnables import RunnableConfig

from app.agent.checkpointer import get_checkpointer
from app.agent.llm import get_llm
from app.agent.middleware import MessagePersistenceMiddleware
from app.agent.streaming import stream_context
from app.agent.tools.evidence import get_evidence
from app.agent.tools.outputs import get_stage_output, list_prior_outputs
from app.agent.tools.search import TOOLS
from app.harness import budget, event_bus
from app.harness.budget import BudgetExceeded, BudgetLimits, BudgetVerdict
from app.harness.task_manager import is_cancelled
from app.storage.mysql.database import SessionLocal

logger = logging.getLogger(__name__)

# System prompt for the ReAct agent
_SYSTEM_PROMPT = """\
You are a thorough research assistant. Your goal is to investigate the user's
question deeply using web search and web fetch tools.

Instructions:
1. Break down the research question into sub-questions if needed.
2. Use web_search to find relevant sources.
3. Use web_fetch to read promising sources in detail. web_fetch returns a
   bounded digest (an evidence ID + head excerpt + paragraph outline + tail
   excerpt), not the full page.
4. Cross-reference information from multiple sources.
5. Provide a comprehensive, well-structured answer with citations.
6. The full text of every fetched page is stored and retrievable by its ID.
   Before you quote, compare, or combine a specific claim from a page, call
   get_evidence(id) to read the full text — do not rely on the digest alone for
   exact figures or verbatim quotes, and never re-fetch a URL you already
   fetched (use get_evidence instead).

Be efficient with tool calls — each search and fetch costs budget. Prefer high-quality sources.

When you have gathered enough information, provide your final answer with source citations.
"""


def _build_agent() -> Any:
    """Build and return the LangGraph ReAct agent."""
    llm = get_llm(temperature=0.1)
    checkpointer = get_checkpointer()

    agent = create_agent(
        model=llm,
        tools=TOOLS + [get_evidence, list_prior_outputs, get_stage_output],
        checkpointer=checkpointer,
        middleware=[MessagePersistenceMiddleware()],
    )
    return agent


# Module-level singleton
_agent: Any | None = None


def get_agent() -> Any:
    """Return the agent singleton."""
    global _agent
    if _agent is None:
        _agent = _build_agent()
    return _agent


def _check_budget_and_cancel(task_id: str, limits: BudgetLimits) -> None:
    """Pre-step guard: check budget and cancel flag.

    Raises BudgetExceeded if budget is exhausted.
    Raises CancelledError if user cancelled.
    """
    # Check cancel
    if is_cancelled(task_id):
        raise CancelledByUser(task_id)

    # Check budget
    verdict = budget.check(task_id, limits)
    if verdict == BudgetVerdict.STOP:
        usage = budget.get_usage(task_id)
        raise BudgetExceeded(task_id, "hard limit exceeded", usage)


def stream_agent(
    runnable: Any,
    inputs: Any,
    config: RunnableConfig,
    *,
    task_id: str,
    limits: BudgetLimits | None = None,
    on_node: Callable[[str], None] | None = None,
    stream_tokens: bool = False,
) -> dict[str, Any]:
    """Run a LangGraph runnable with .stream() instead of .invoke().

    Identical execution and final state, but:
    - ``on_node`` fires once per completed graph node (real-time observation);
    - the budget/cancel guard runs between every super-step, not just once
      before the run — long runs stop promptly on cancel or budget exhaustion.

    Uses ``stream_mode=["updates", "values"]``: updates chunks carry the node
    names, values chunks the authoritative reduced state — the last values
    chunk is exactly what .invoke() would have returned.
    """
    final_state: dict[str, Any] = {}
    with stream_context(task_id, stream_tokens=stream_tokens):
        for mode, chunk in runnable.stream(inputs, config, stream_mode=["updates", "values"]):
            if mode == "updates":
                for node in chunk:
                    if node != "__interrupt__" and on_node is not None:
                        on_node(node)
            else:
                final_state = chunk
            if limits is not None:
                _check_budget_and_cancel(task_id, limits)
    return final_state


class CancelledByUser(Exception):
    """Raised when the user has cancelled the task."""

    def __init__(self, task_id: str):
        self.task_id = task_id
        super().__init__(f"Task {task_id} cancelled by user")


def execute_agent(
    task_id: str,
    run_id: str,
    goal: str,
    budget_limits: BudgetLimits,
) -> str:
    """Execute the ReAct agent for a task.

    Args:
        task_id: Task ID (also used as LangGraph thread_id for checkpointing).
        run_id: AgentRun ID.
        goal: The research goal / question.
        budget_limits: Budget constraints.

    Returns:
        The agent's final answer text.

    Raises:
        BudgetExceeded: If budget is exceeded mid-execution.
        CancelledByUser: If user cancels mid-execution.
    """
    agent = get_agent()

    # Emit agent_run.started
    db = SessionLocal()
    try:
        event_bus.publish(db, task_id, "agent_run.started", {
            "run_id": run_id,
            "model": get_llm().model_name,
            "tools": [t.name for t in TOOLS],
        })
    finally:
        db.close()

    # Pre-flight budget/cancel check
    _check_budget_and_cancel(task_id, budget_limits)

    # Build input messages
    messages = [
        SystemMessage(content=_SYSTEM_PROMPT),
        HumanMessage(content=goal),
    ]

    t0 = time.monotonic()

    # Emit plan event
    db = SessionLocal()
    try:
        event_bus.publish(db, task_id, "plan.generated", {
            "run_id": run_id,
            "strategy": "react_search_fetch_synthesize",
        })
    finally:
        db.close()

    # Run the agent via .stream(): per-node step events + per-step budget guard
    def _on_node(node: str) -> None:
        db = SessionLocal()
        try:
            event_bus.publish(db, task_id, "react.step", {
                "run_id": run_id,
                "node": node,
                "thread_id": task_id,
            })
        finally:
            db.close()

    final_state = stream_agent(
        agent,
        {"messages": messages},
        config={
            "configurable": {
                "thread_id": task_id,
                "task_id": task_id,
                "run_id": run_id,
            }
        },
        task_id=task_id,
        limits=budget_limits,
        on_node=_on_node,
    )

    elapsed_ms = int((time.monotonic() - t0) * 1000)
    budget.add_elapsed_ms(task_id, elapsed_ms)

    # Extract final answer from the last message
    messages_out = final_state.get("messages", [])
    if messages_out:
        last_msg = messages_out[-1]
        result_text = last_msg.content if hasattr(last_msg, "content") else str(last_msg)
    else:
        result_text = "No output produced."

    # Emit completion event
    usage = budget.get_usage(task_id)
    db = SessionLocal()
    try:
        event_bus.publish(db, task_id, "agent_run.completed", {
            "run_id": run_id,
            "result": result_text[:500],  # Truncate for event payload
            "elapsed_ms": elapsed_ms,
            "total_input_tokens": usage.input_tokens,
            "total_output_tokens": usage.output_tokens,
            "total_tool_calls": usage.tool_calls,
        })
    finally:
        db.close()

    return result_text
