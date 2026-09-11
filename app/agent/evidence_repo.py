"""Persistence for harvested stage evidence (progressive-disclosure store).

Evidence harvested from a stage's ReAct thread is written here as
``id + url + summary + content``: the cheap ``summary`` stays in the graph
state as an index, while the ``content`` is retrieved on demand by the
``get_evidence`` tool only when a later stage actually needs it. Persistence
failures must never fail the graph — they are swallowed, matching
``app.evaluation.repository``.
"""

from __future__ import annotations

import logging
from typing import Any

from app.storage.mysql.database import SessionLocal
from app.storage.mysql.models import Evidence

logger = logging.getLogger(__name__)


def save_evidence(
    *,
    task_id: str,
    run_id: str,
    stage_index: int,
    records: list[dict[str, Any]],
) -> None:
    """Persist harvested evidence rows. Never raises into the graph.

    Each record carries ``id``, ``url``, ``summary``, ``content`` (and
    optionally ``tool``, ignored here — the tool name lives in the state index).
    """
    if not records:
        return
    try:
        db = SessionLocal()
        try:
            for rec in records:
                db.add(Evidence(
                    id=rec["id"],
                    task_id=task_id,
                    source_url=rec.get("url") or "",
                    summary=rec.get("summary"),
                    content=rec.get("content"),
                    run_id=run_id,
                    stage_index=stage_index,
                ))
            db.commit()
        finally:
            db.close()
    except Exception:
        logger.exception("failed to persist evidence for task %s stage %s", task_id, stage_index)


def find_evidence_by_url(task_id: str, url: str) -> dict[str, Any] | None:
    """Return an already-persisted evidence row for this task+url, or None.

    Lets ``web_fetch`` dedupe: a URL already fetched this task is served from
    the store (``{id, url, summary, content}``) instead of being re-downloaded.
    Persistence failures are swallowed — a dedup miss is never fatal.
    """
    if not url:
        return None
    try:
        db = SessionLocal()
        try:
            row = (
                db.query(Evidence)
                .filter(Evidence.task_id == task_id, Evidence.source_url == url)
                .order_by(Evidence.created_at.desc())
                .first()
            )
            if row is None:
                return None
            return {
                "id": row.id,
                "url": row.source_url or "",
                "summary": row.summary or "",
                "content": row.content or "",
            }
        finally:
            db.close()
    except Exception:
        logger.exception("failed to find evidence by url for task %s", task_id)
        return None


def fetch_evidence(task_id: str, evidence_id: str) -> dict[str, Any] | None:
    """Return one evidence row as ``{url, summary, content}``, or None."""
    try:
        db = SessionLocal()
        try:
            row = db.get(Evidence, evidence_id)
            if row is None or row.task_id != task_id:
                return None
            return {
                "url": row.source_url or "",
                "summary": row.summary or "",
                "content": row.content or "",
            }
        finally:
            db.close()
    except Exception:
        logger.exception("failed to fetch evidence %s for task %s", evidence_id, task_id)
        return None


def list_evidence(task_id: str) -> list[dict[str, Any]]:
    """List a task's evidence index (id + url + summary), cheapest first."""
    try:
        db = SessionLocal()
        try:
            rows = (
                db.query(Evidence)
                .filter(Evidence.task_id == task_id)
                .order_by(Evidence.stage_index, Evidence.created_at)
                .all()
            )
            return [
                {"id": r.id, "url": r.source_url or "", "summary": r.summary or ""}
                for r in rows
            ]
        finally:
            db.close()
    except Exception:
        logger.exception("failed to list evidence for task %s", task_id)
        return []
