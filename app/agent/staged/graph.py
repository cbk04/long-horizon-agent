"""Staged graph assembly: approval gate, controller node, entry point.

Graph shape::

    START -> plan -> human_approval (interrupt) -> controller -> react
                                                        ^          |
                                                        |          v
                                                        +------ evaluate

- ``human_approval``: LangGraph ``interrupt`` that pauses execution until the
  user approves the plan. On resume the decision is stored in state.
- ``controller``: model-free state machine (``app.agent.controller``) — the
  sole decision layer. It dispatches react for the current stage, advances on
  a completed verdict, retries with feedback (bounded), dispatches the
  lightweight defend pass on a contested verdict, and fails the task when
  retries are exhausted.

The worker invokes this graph twice per task: first to produce the plan (which
returns with a pending ``__interrupt__`` and raises ``AwaitingApproval``), then
again with ``Command(resume=...)`` after the user approves. Both passes share
``thread_id = task_id`` via the checkpointer.
"""

from __future__ import annotations

import logging
import time
from typing import Any

from langchain_core.messages import HumanMessage
from langchain_core.runnables import RunnableConfig
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, interrupt

from app.agent import stage_output_repo
from app.agent.checkpointer import get_checkpointer
from app.agent.controller import Action, StageFailedError, route_decision
from app.agent.llm import get_llm
from app.agent.react import stream_agent
from app.agent.staged.common import AgentState, node_step_publisher, publish
from app.agent.staged.evaluator import evaluate_node
from app.agent.staged.executor import react_node
from app.agent.staged.planner import plan_node
from app.harness import budget
from app.harness.budget import BudgetLimits

logger = logging.getLogger(__name__)


class AwaitingApproval(Exception):
    """Raised after the first pass when the plan waits for human approval.

    LangGraph >= 1.x surfaces interrupts via the ``__interrupt__`` state key
    instead of raising ``GraphInterrupt`` out of ``invoke``; this converts that
    into an explicit signal for the worker to mark the task PAUSED.
    """

    def __init__(self, task_id: str):
        self.task_id = task_id
        super().__init__(f"Task {task_id} plan awaiting approval")


def human_approval_node(state: AgentState, config: RunnableConfig) -> dict[str, Any]:
    """Pause execution until the user approves the plan."""
    decision = interrupt({"plan": state.get("plan", [])})
    return {"approval": decision}


def controller_node(state: AgentState, config: RunnableConfig) -> dict[str, Any]:
    """Apply the state machine decision; the only place routing is decided."""
    decision = route_decision(
        state["plan"],
        state["current_stage_index"],
        state["stage_attempts"],
        state.get("evaluation"),
        stage_defense_rounds=state.get("stage_defense_rounds", 0) or 0,
    )

    task_id = config["configurable"]["thread_id"]
    run_id = config["configurable"].get("run_id", "")

    if decision.action == Action.FAIL:
        stage = state["plan"][decision.stage_index]
        raise StageFailedError(decision.stage_index, stage["objective"], decision.feedback or [])

    updates: dict[str, Any] = {
        "current_stage_index": decision.stage_index,
        "stage_attempts": decision.attempt,
        "stage_feedback": decision.feedback,
        "evaluation": None,
        "last_action": decision.action.value,
    }

    if decision.action == Action.DEFEND_STAGE:
        # A defense round consumes the defense budget but not a stage attempt;
        # the counterexamples stay for the evaluator's re-adjudication.
        updates["stage_defense"] = None
        updates["stage_defense_rounds"] = (state.get("stage_defense_rounds", 0) or 0) + 1
    else:
        # Any dispatch/advance clears the defend lane for the next pass.
        updates["stage_defense"] = None
        updates["stage_counterexamples"] = None
        updates["stage_defense_rounds"] = 0

    if decision.action in (Action.ADVANCE, Action.COMPLETE):
        evaluation = state.get("evaluation") or {}
        stage = state["plan"][state["current_stage_index"]]
        results = list(state.get("stage_results") or [])
        results.append({
            "stage_index": state["current_stage_index"],
            "objective": stage["objective"],
            "key_findings": list(evaluation.get("key_findings") or []),
            "conclusion": state.get("stage_result") or "",
            "evidence_index": state.get("stage_evidence_index") or [],
            "dispute": bool(evaluation.get("dispute")),
            "weighted_score": (evaluation.get("phase3") or {}).get("weighted_score"),
        })
        updates["stage_results"] = results
        stage_output_repo.save_stage_output(
            task_id=task_id,
            run_id=run_id,
            stage_index=state["current_stage_index"],
            objective=stage["objective"],
            conclusion=state.get("stage_result") or "",
            key_findings=list(evaluation.get("key_findings") or []),
            evidence_index=state.get("stage_evidence_index") or [],
        )
        publish(task_id, "stage.completed", {
            "run_id": run_id,
            "stage_id": state["current_stage_index"],
            "key_findings": evaluation.get("key_findings") or [],
            "dispute": bool(evaluation.get("dispute")),
            "weighted_score": (evaluation.get("phase3") or {}).get("weighted_score"),
        })
        if decision.action == Action.COMPLETE:
            updates["final_result"] = state.get("stage_result", "")

    if decision.action in (Action.START_STAGE, Action.RETRY_STAGE, Action.ADVANCE):
        stage = state["plan"][decision.stage_index]
        publish(task_id, "stage.started", {
            "run_id": run_id,
            "stage_id": decision.stage_index,
            "objective": stage["objective"],
            "attempt": decision.attempt,
        })

    return updates


def _after_controller(state: AgentState) -> str:
    """Edge after the controller: loop back to react or finish the graph."""
    return END if state.get("last_action") == Action.COMPLETE.value else "react"


def _build_graph() -> Any:
    graph = StateGraph(AgentState)
    graph.add_node("plan", plan_node)
    graph.add_node("human_approval", human_approval_node)
    graph.add_node("controller", controller_node)
    graph.add_node("react", react_node)
    graph.add_node("evaluate", evaluate_node)

    graph.add_edge(START, "plan")
    graph.add_edge("plan", "human_approval")
    graph.add_edge("human_approval", "controller")
    graph.add_conditional_edges("controller", _after_controller, [END, "react"])
    graph.add_edge("react", "evaluate")
    graph.add_edge("evaluate", "controller")

    return graph.compile(checkpointer=get_checkpointer())


_graph: Any | None = None


def get_graph() -> Any:
    global _graph
    if _graph is None:
        _graph = _build_graph()
    return _graph


def execute_agent(
    task_id: str,
    run_id: str,
    goal: str,
    budget_limits: BudgetLimits,
    resume: dict[str, Any] | None = None,
) -> str:
    """Run the staged plan-and-execute agent.

    First invocation (``resume=None``) runs the plan node and then raises
    ``AwaitingApproval`` at the approval gate; the worker catches it and marks
    the task PAUSED. After approval, the worker calls again with
    ``resume={"approved": True}`` and this method runs all stages to the
    final answer.

    Returns:
        The final answer text (resume pass only).
    """
    graph = get_graph()
    config: RunnableConfig = {
        "configurable": {
            "thread_id": task_id,
            "run_id": run_id,
            "budget_limits": budget_limits,
        }
    }

    on_node = node_step_publisher(task_id, run_id, task_id, event="graph.step")

    if resume is None:
        publish(task_id, "agent_run.started", {
            "run_id": run_id,
            "model": get_llm().model_name,
        })
        result = stream_agent(
            graph,
            {"messages": [HumanMessage(content=goal)]},
            config,
            task_id=task_id,
            limits=budget_limits,
            on_node=on_node,
        )
        # Interrupted runs pause at the approval gate; snapshot.next lists the
        # node the graph is waiting at (empty tuple once the graph completed).
        snapshot = graph.get_state(config)
        if snapshot.next or result.get("__interrupt__"):
            raise AwaitingApproval(task_id)
        return ""

    t0 = time.monotonic()
    final_state = stream_agent(
        graph,
        Command(resume=resume),
        config,
        task_id=task_id,
        limits=budget_limits,
        on_node=on_node,
    )
    elapsed_ms = int((time.monotonic() - t0) * 1000)
    budget.add_elapsed_ms(task_id, elapsed_ms)

    result_text = final_state.get("final_result") or "No output produced."

    usage = budget.get_usage(task_id)
    publish(task_id, "agent_run.completed", {
        "run_id": run_id,
        "result": result_text,
        "elapsed_ms": elapsed_ms,
        "total_input_tokens": usage.input_tokens,
        "total_output_tokens": usage.output_tokens,
        "total_tool_calls": usage.tool_calls,
    })

    return result_text
