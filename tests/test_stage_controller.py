"""Unit tests for the stage controller state machine (pure, no I/O).

The controller is the sole decision layer of the staged graph: these tests
cover every routing branch — including the defend lane and configurable
budgets — without touching an LLM, the DB or Redis.
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from app.agent.controller import (
    Action,
    ControllerLimits,
    Decision,
    StageFailedError,
    route_decision,
)

LIMITS = ControllerLimits(max_stage_retries=2, max_defense_rounds=1)


def _plan(n: int = 2) -> list[dict]:
    return [
        {
            "objective": f"stage {i} objective",
            "scope_excludes": [f"not stage {i} work"],
            "acceptance": [
                {"dimension": f"dimension {i}", "criteria": f"criteria {i}", "weight": 1.0}
            ],
            "is_final": i == n - 1,
        }
        for i in range(n)
    ]


def _verdict(status: str, feedback: list[str] | None = None, dispute: bool = False) -> dict:
    return {"status": status, "feedback": feedback or [], "dispute": dispute}


def test_first_entry_dispatches_current_stage():
    decision = route_decision(_plan(2), 0, 0, None, limits=LIMITS)
    assert decision.action == Action.START_STAGE
    assert decision.stage_index == 0
    assert decision.attempt == 1
    assert decision.feedback is None


def test_completed_stage_advances_to_next():
    decision = route_decision(_plan(2), 0, 1, _verdict("COMPLETED"), limits=LIMITS)
    assert decision.action == Action.ADVANCE
    assert decision.stage_index == 1
    assert decision.attempt == 1
    assert decision.feedback is None


def test_completed_last_stage_completes_graph():
    decision = route_decision(_plan(2), 1, 1, _verdict("COMPLETED"), limits=LIMITS)
    assert decision.action == Action.COMPLETE
    assert decision.stage_index == -1


def test_incomplete_stage_retries_with_feedback():
    decision = route_decision(_plan(2), 0, 1, _verdict("INCOMPLETE", ["缺少数据源X"]), limits=LIMITS)
    assert decision.action == Action.RETRY_STAGE
    assert decision.stage_index == 0
    assert decision.attempt == 2
    assert decision.feedback == ["缺少数据源X"]


def test_incomplete_falls_back_to_gaps_field():
    """Backward compatibility: verdicts carrying only ``gaps`` still route."""
    verdict = {"status": "INCOMPLETE", "gaps": ["缺口"], "key_findings": []}
    decision = route_decision(_plan(2), 0, 1, verdict, limits=LIMITS)
    assert decision.action == Action.RETRY_STAGE
    assert decision.feedback == ["缺口"]


def test_retry_budget_exhausted_fails_task():
    decision = route_decision(_plan(2), 0, 2, _verdict("INCOMPLETE", ["仍未完成"]), limits=LIMITS)
    assert decision.action == Action.FAIL
    assert decision.stage_index == 0
    assert decision.feedback == ["仍未完成"]


def test_max_retries_bounds_attempts():
    verdict = _verdict("INCOMPLETE")
    assert route_decision(_plan(2), 0, 1, verdict, limits=LIMITS).action == Action.RETRY_STAGE
    assert route_decision(_plan(2), 0, 2, verdict, limits=LIMITS).action == Action.FAIL


def test_retry_limit_is_configurable():
    verdict = _verdict("INCOMPLETE")
    tight = ControllerLimits(max_stage_retries=1, max_defense_rounds=1)
    assert route_decision(_plan(2), 0, 1, verdict, limits=tight).action == Action.FAIL


def test_defend_verdict_dispatches_defense_without_consuming_attempt():
    verdict = _verdict("DEFEND", ["反例(强度 medium):……"])
    decision = route_decision(
        _plan(2), 0, 1, verdict, stage_defense_rounds=0, limits=LIMITS
    )
    assert decision.action == Action.DEFEND_STAGE
    assert decision.stage_index == 0
    assert decision.attempt == 1  # a defense round is not a stage attempt
    assert decision.feedback == ["反例(强度 medium):……"]


def test_defend_budget_exhausted_passes_with_dispute():
    verdict = _verdict("DEFEND", ["反例"], dispute=True)
    decision = route_decision(
        _plan(2), 0, 1, verdict, stage_defense_rounds=1, limits=LIMITS
    )
    assert decision.action == Action.ADVANCE
    assert decision.stage_index == 1


def test_defend_budget_exhausted_on_last_stage_completes():
    verdict = _verdict("DEFEND", dispute=True)
    decision = route_decision(
        _plan(2), 1, 1, verdict, stage_defense_rounds=1, limits=LIMITS
    )
    assert decision.action == Action.COMPLETE


def test_defense_budget_is_configurable():
    verdict = _verdict("DEFEND")
    strict = ControllerLimits(max_stage_retries=2, max_defense_rounds=0)
    decision = route_decision(_plan(2), 0, 1, verdict, stage_defense_rounds=0, limits=strict)
    # Zero defense budget: DEFEND verdict passes straight through with dispute.
    assert decision.action == Action.ADVANCE


def test_stage_failed_error_message_carries_context():
    error = StageFailedError(0, "广度搜索", ["缺X"])
    assert error.stage_index == 0
    assert "广度搜索" in str(error)
    assert "缺X" in str(error)


def test_single_stage_plan_completes_immediately():
    decision = route_decision(_plan(1), 0, 1, _verdict("COMPLETED"), limits=LIMITS)
    assert decision.action == Action.COMPLETE
