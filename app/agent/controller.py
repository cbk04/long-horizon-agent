"""Stage controller — the sole decision layer of the staged execution graph.

A pure, model-free state machine. It reads the plan's stage list, the current
stage index, the number of attempts already made on the current stage, the
defense rounds already consumed, and the latest evaluation verdict, and
decides what happens next:

    START_STAGE   dispatch react for the current stage (first attempt)
    RETRY_STAGE   re-dispatch react with the evaluation's feedback
    DEFEND_STAGE  dispatch the lightweight defend pass (counterexample response)
    ADVANCE       current stage completed — move to the next stage
    COMPLETE      all stages completed — finish the graph
    FAIL          retry budget exhausted — fail the task

Verdict statuses map to actions: COMPLETED → advance; DEFEND → a defense
round while budget remains, otherwise pass-with-dispute; INCOMPLETE → retry
until the retry budget runs out. Retry and defense budgets are independent
and configurable (``ControllerLimits``).

The evaluator (evaluate node) judges but never routes; every routing decision
is produced by ``route_decision`` below. This function is the primary test
seam: it must stay deterministic and free of I/O (no LLM, no DB, no Redis) —
pass ``limits`` explicitly in tests rather than relying on settings.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any


@dataclass(frozen=True)
class ControllerLimits:
    """Budgets the controller enforces; resolved from settings by default."""

    max_stage_retries: int
    max_defense_rounds: int


def default_limits() -> ControllerLimits:
    """Read the configured budgets (cached settings; no I/O beyond env)."""
    from app.config import get_settings

    settings = get_settings()
    return ControllerLimits(
        max_stage_retries=settings.max_stage_retries,
        max_defense_rounds=settings.max_defense_rounds,
    )


class Action(str, Enum):
    START_STAGE = "start_stage"
    RETRY_STAGE = "retry_stage"
    DEFEND_STAGE = "defend_stage"
    ADVANCE = "advance"
    COMPLETE = "complete"
    FAIL = "fail"


@dataclass
class Decision:
    """The controller's routing decision for one graph step."""

    action: Action
    # Stage the controller dispatches next (-1 when complete/fail).
    stage_index: int
    # 1-based attempt number for that stage (1 on first dispatch; unchanged
    # on DEFEND — a defense round is not a stage attempt).
    attempt: int
    # Feedback fed back to react/defend; None otherwise.
    feedback: list[str] | None


def _pass_or_complete(plan: list[dict[str, Any]], current_stage_index: int) -> Decision:
    next_index = current_stage_index + 1
    if next_index >= len(plan):
        return Decision(Action.COMPLETE, -1, 0, None)
    return Decision(Action.ADVANCE, next_index, 1, None)


def route_decision(
    plan: list[dict[str, Any]],
    current_stage_index: int,
    stage_attempts: int,
    evaluation: dict[str, Any] | None,
    *,
    stage_defense_rounds: int = 0,
    limits: ControllerLimits | None = None,
) -> Decision:
    """Decide the next action from (plan, progress, latest verdict, defense budget).

    Args:
        plan: Stage list from the planner; each entry has
            objective/scope_excludes/acceptance/is_final.
        current_stage_index: 0-based index of the stage being executed.
        stage_attempts: Attempts already dispatched for the current stage.
        evaluation: Latest evaluate verdict for the current stage
            (``{"status": "COMPLETED"|"INCOMPLETE"|"DEFEND", "feedback": [...], ...}``),
            or None when the stage has not been evaluated yet.
        stage_defense_rounds: Defense rounds already consumed on this stage.
        limits: Retry/defense budgets; defaults to configured settings.
    """
    budgets = limits or default_limits()

    if evaluation is None:
        return Decision(Action.START_STAGE, current_stage_index, stage_attempts + 1, None)

    status = evaluation.get("status")
    feedback = list(evaluation.get("feedback") or evaluation.get("gaps") or [])

    if status == "COMPLETED":
        return _pass_or_complete(plan, current_stage_index)

    if status == "DEFEND":
        # Defense budget exhausted → the dispute is settled as "contested but
        # not fatal": pass with the dispute flag carried in the verdict.
        if stage_defense_rounds >= budgets.max_defense_rounds:
            return _pass_or_complete(plan, current_stage_index)
        return Decision(Action.DEFEND_STAGE, current_stage_index, stage_attempts, feedback)

    # INCOMPLETE: retry while the stage's retry budget lasts, then fail the task.
    if stage_attempts >= budgets.max_stage_retries:
        return Decision(Action.FAIL, current_stage_index, stage_attempts, feedback)
    return Decision(Action.RETRY_STAGE, current_stage_index, stage_attempts + 1, feedback)


class StageFailedError(Exception):
    """Raised when a stage exhausts its retry budget; maps to task FAILED."""

    def __init__(self, stage_index: int, objective: str, gaps: list[str]):
        self.stage_index = stage_index
        self.gaps = gaps
        super().__init__(
            f"Stage {stage_index} ({objective}) failed after exhausting its "
            f"retry budget; unresolved gaps: {gaps or 'unspecified'}"
        )
