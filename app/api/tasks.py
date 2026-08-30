"""FastAPI routes for task management and SSE event streaming."""

from __future__ import annotations

import json

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sse_starlette.sse import EventSourceResponse
from sqlalchemy.orm import Session

from app.api.schemas import (
    CancelResponse,
    CreateTaskRequest,
    EvidenceResponse,
    TaskResponse,
)
from app.harness import event_bus, task_manager
from app.harness.models import TaskEvent
from app.storage.mysql.database import get_db
from app.storage.mysql.models import Task

router = APIRouter(prefix="/api/v1", tags=["tasks"])


def _task_to_response(t: Task) -> TaskResponse:
    return TaskResponse(
        id=t.id,
        user_id=t.user_id,
        goal=t.goal,
        status=t.status,
        priority=t.priority,
        search_depth=t.search_depth,
        output_format=t.output_format,
        budget_tokens=t.budget_tokens,
        budget_seconds=t.budget_seconds,
        budget_cost=t.budget_cost,
        current_run_id=t.current_run_id,
        result_ref=t.result_ref,
        created_at=t.created_at,
        updated_at=t.updated_at,
    )


@router.post("/tasks", response_model=TaskResponse, status_code=201)
def create_task(req: CreateTaskRequest, db: Session = Depends(get_db)):
    """Create a new task. Returns immediately with task_id."""
    task = task_manager.create_task(
        db,
        goal=req.goal,
        priority=req.priority,
        search_depth=req.search_depth,
        output_format=req.output_format,
        budget_tokens=req.budget_tokens,
        budget_seconds=req.budget_seconds,
        budget_cost=req.budget_cost,
    )
    return _task_to_response(task)


@router.get("/tasks/{task_id}", response_model=TaskResponse)
def get_task(task_id: str, db: Session = Depends(get_db)):
    task = task_manager.get_task(db, task_id)
    if not task:
        raise HTTPException(404, "Task not found")
    return _task_to_response(task)


@router.post("/tasks/{task_id}/cancel", response_model=CancelResponse)
def cancel_task(task_id: str, db: Session = Depends(get_db)):
    task, message = task_manager.cancel_task(db, task_id)
    if not task:
        raise HTTPException(404, "Task not found")
    return CancelResponse(task_id=task_id, status=task.status, message=message)


@router.get("/tasks/{task_id}/stream")
async def stream_events(
    task_id: str,
    request: Request,
    last_event_id: str | None = Query(default=None, alias="Last-Event-ID"),
    db: Session = Depends(get_db),
):
    """SSE stream of task events. Supports Last-Event-ID for resume."""
    task = task_manager.get_task(db, task_id)
    if not task:
        raise HTTPException(404, "Task not found")

    last_id = last_event_id or "0"

    async def event_generator():
        # First, flush any pending events from MySQL log (for resume)
        if last_id != "0":
            try:
                historical = (
                    db.query(TaskEvent)
                    .filter(TaskEvent.task_id == task_id, TaskEvent.stream_id > last_id)
                    .order_by(TaskEvent.id.asc())
                    .limit(500)
                    .all()
                )
                for ev in historical:
                    yield {
                        "id": ev.stream_id,
                        "event": ev.type,
                        "data": json.dumps(
                            {
                                "task_id": ev.task_id,
                                "type": ev.type,
                                "payload": ev.payload,
                                "timestamp": ev.created_at.isoformat(),
                            },
                            default=str,
                        ),
                    }
            except Exception:
                pass

        # Then consume live events from Redis Stream
        current_id = last_id
        for event in event_bus.stream(task_id, current_id):
            if await request.is_disconnected():
                break
            current_id = event["id"]
            yield {
                "id": event["id"],
                "event": event["type"],
                "data": json.dumps(event, default=str),
            }

    return EventSourceResponse(event_generator())


@router.get("/tasks/{task_id}/events")
def list_events(
    task_id: str,
    limit: int = Query(100, ge=1, le=1000),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
):
    """List historical events from MySQL (durable log)."""
    task = task_manager.get_task(db, task_id)
    if not task:
        raise HTTPException(404, "Task not found")
    events = task_manager.list_events(db, task_id, limit=limit, offset=offset)
    return [
        {
            "id": e.stream_id,
            "task_id": e.task_id,
            "type": e.type,
            "payload": e.payload,
            "timestamp": e.created_at.isoformat(),
        }
        for e in events
    ]


@router.get("/tasks/{task_id}/evidence", response_model=list[EvidenceResponse])
def list_evidence(
    task_id: str,
    limit: int = Query(100, ge=1, le=1000),
    db: Session = Depends(get_db),
):
    task = task_manager.get_task(db, task_id)
    if not task:
        raise HTTPException(404, "Task not found")
    items = task_manager.list_evidence(db, task_id, limit=limit)
    return [
        EvidenceResponse(
            id=e.id,
            task_id=e.task_id,
            source_url=e.source_url,
            source_title=e.source_title,
            locator=e.locator,
            content_ref=e.content_ref,
            claim_id=e.claim_id,
            quality_score=e.quality_score,
            created_at=e.created_at,
        )
        for e in items
    ]
