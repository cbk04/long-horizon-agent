"""Persistence for stage evaluation rows (three-phase evaluator output).

One row per evaluate-node pass — full pipeline, rule short-circuit, DEFEND
verdict, or post-defense re-adjudication. The task's final score is the
latest PASSED row of its ``is_final`` stage.
"""

from __future__ import annotations

import uuid
from typing import Any

from app.storage.mysql.database import SessionLocal
from app.storage.mysql.models import StageEvaluation

# verdict status → row status (COMPLETED under a blown eval fuse degrades).
_STATUS_MAP = {"COMPLETED": "PASSED", "INCOMPLETE": "RETRY", "DEFEND": "DEFEND"}


def save_stage_evaluation(
    *,
    task_id: str,
    run_id: str,
    stage_index: int,
    attempt: int,
    defense_round: int,
    verdict: dict[str, Any],
    acceptance_snapshot: list[dict[str, Any]],
) -> None:
    """Persist one evaluator pass. Never raises into the graph: a persistence
    failure must not fail a task whose evaluation already succeeded."""
    phase2 = verdict.get("phase2") or {}
    phase3 = verdict.get("phase3") or {}
    status = _STATUS_MAP.get(verdict.get("status", ""), "RETRY")
    if status == "PASSED" and verdict.get("eval_degraded"):
        status = "DEGRADED_PASS"

    try:
        db = SessionLocal()
        try:
            db.add(StageEvaluation(
                id=uuid.uuid4().hex,
                task_id=task_id,
                run_id=run_id,
                stage_index=stage_index,
                attempt=attempt,
                defense_round=defense_round,
                status=status,
                rule_result=verdict.get("phase1") or None,
                forward_result=phase2.get("forward"),
                reverse_result=phase2.get("reverse"),
                criteria_scores=phase3.get("criteria_scores") or None,
                weighted_score=phase3.get("weighted_score"),
                feedback=verdict.get("feedback") or None,
                acceptance_snapshot=acceptance_snapshot or None,
            ))
            db.commit()
        finally:
            db.close()
    except Exception:
        import logging

        logging.getLogger(__name__).exception(
            "failed to persist stage_evaluation for task %s stage %s", task_id, stage_index
        )
