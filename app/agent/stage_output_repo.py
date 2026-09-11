"""Persistence for accepted stage outputs (retrieval-fallback store).

The staged graph's ReAct subgraph runs on its own checkpointer thread and cannot
read the outer graph's ``AgentState``. This module durably stores each stage's
accepted conclusion + findings + evidence index, so the executor's retrieval
tools can reach prior outputs even when the planner under-declared a dependency.

Persistence failures never fail the graph — matching ``evidence_repo`` and
``app.evaluation.repository``.
"""

from __future__ import annotations

import logging
import uuid
from typing import Any

from app.storage.mysql.database import SessionLocal
from app.storage.mysql.models import StageOutput

logger = logging.getLogger(__name__)


def save_stage_output(
    *,
    task_id: str,
    run_id: str,
    stage_index: int,
    objective: str,
    conclusion: str,
    key_findings: list[str],
    evidence_index: list[dict[str, Any]],
) -> None:
    """Persist one accepted stage output. Never raises into the graph."""
    try:
        db = SessionLocal()
        try:
            db.add(StageOutput(
                id=uuid.uuid4().hex,
                task_id=task_id,
                run_id=run_id,
                stage_index=stage_index,
                objective=objective,
                conclusion=conclusion,
                key_findings=key_findings or None,
                evidence_index=evidence_index or None,
            ))
            db.commit()
        finally:
            db.close()
    except Exception:
        logger.exception("failed to persist stage_output for task %s stage %s", task_id, stage_index)


def list_stage_outputs(task_id: str) -> list[dict[str, Any]]:
    """Return prior stage outputs as an overview index (no full conclusions)."""
    try:
        db = SessionLocal()
        try:
            rows = (
                db.query(StageOutput)
                .filter(StageOutput.task_id == task_id)
                .order_by(StageOutput.stage_index, StageOutput.created_at)
                .all()
            )
            return [
                {
                    "stage_index": r.stage_index,
                    "objective": r.objective,
                    "key_findings": r.key_findings or [],
                    "evidence_index": r.evidence_index or [],
                }
                for r in rows
            ]
        finally:
            db.close()
    except Exception:
        logger.exception("failed to list stage outputs for task %s", task_id)
        return []


def get_stage_output(task_id: str, stage_index: int) -> dict[str, Any] | None:
    """Return one stage's full conclusion + objective, or None."""
    try:
        db = SessionLocal()
        try:
            row = (
                db.query(StageOutput)
                .filter(StageOutput.task_id == task_id, StageOutput.stage_index == stage_index)
                .order_by(StageOutput.created_at.desc())
                .first()
            )
            if row is None:
                return None
            return {"objective": row.objective, "conclusion": row.conclusion}
        finally:
            db.close()
    except Exception:
        logger.exception("failed to get stage output %s for task %s", stage_index, task_id)
        return None
