"""Shared plumbing for the staged graph's nodes.

Everything used by more than one node lives here: the state schema (the
graph's data bus), the event publisher, and acceptance formatting.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Annotated, Any, TypedDict

from langgraph.graph.message import add_messages

from app.harness import event_bus
from app.storage.mysql.database import SessionLocal


class AgentState(TypedDict):
    """State shared across the staged graph nodes.

    This is the whole graph's data bus, not just the plan: every key a node
    writes for another node to read must be declared here — LangGraph
    silently drops updates to undeclared keys.
    """

    messages: Annotated[list, add_messages]
    goal: str
    plan: list[dict[str, Any]]
    approval: dict[str, Any]
    # ── stage machine progress ──
    current_stage_index: int
    stage_attempts: int
    stage_feedback: list[str] | None
    evaluation: dict[str, Any] | None
    # ── defend lane (dispute response awaiting re-adjudication) ──
    stage_defense: str | None
    stage_defense_rounds: int
    stage_counterexamples: list[dict[str, Any]] | None
    # ── evaluator budget guard ──
    eval_call_count: int
    eval_degraded: bool
    # ── current stage execution artifacts (react → evaluate) ──
    stage_result: str
    stage_evidence: list[dict[str, Any]]
    stage_evidence_index: list[dict[str, Any]]
    stage_reasoning: list[str]
    stage_results: list[dict[str, Any]]
    last_action: str
    final_result: str


def publish(task_id: str, event_type: str, payload: dict[str, Any]) -> None:
    """Persist one agent event for the task (audit stream / frontend)."""
    db = SessionLocal()
    try:
        event_bus.publish(db, task_id, event_type, payload)
    finally:
        db.close()


def node_step_publisher(
    task_id: str, run_id: str, thread_id: str, event: str = "react.step"
) -> Callable[[str], None]:
    """Build an ``on_node`` callback for ``react.stream_agent``.

    Each completed graph node becomes one persisted event, giving the
    frontend / audit stream real-time node-level progress.
    """
    def _on_node(node: str) -> None:
        publish(task_id, event, {
            "run_id": run_id,
            "node": node,
            "thread_id": thread_id,
        })

    return _on_node


def format_acceptance(acceptance: list[dict[str, Any]]) -> str:
    """Render acceptance items as indented dimension/criteria/weight lines."""
    lines = []
    for item in acceptance:
        weight = item.get("weight")
        suffix = f"(权重:{weight:.2f})" if isinstance(weight, (int, float)) else ""
        lines.append(f"  - 维度:{item['dimension']};标准:{item['criteria']}{suffix}")
    return "\n".join(lines)


def format_contract(contract: dict[str, Any] | None) -> str:
    """Render a stage's output_contract as a concise hard-requirement line.

    Returns '' when the contract is absent or empty (no hard shape requirement).
    """
    if not contract:
        return ""
    requirements: list[str] = []
    min_length = contract.get("min_length") or 0
    if min_length:
        requirements.append(f"结论不少于 {min_length} 字")
    required_sections = contract.get("required_sections") or []
    if required_sections:
        requirements.append(f"必须包含以下小节标题：{'、'.join(required_sections)}")
    return "；".join(requirements)
