"""FastAPI routes for task management and SSE event streaming."""

from __future__ import annotations

import json

import anyio
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sqlalchemy.orm import Session
from sse_starlette.sse import EventSourceResponse

from app.api.schemas import (
    ApproveResponse,
    BudgetUsageResponse,
    CancelResponse,
    CreateTaskRequest,
    EvaluationResponse,
    EvidenceResponse,
    MessageResponse,
    ResultResponse,
    RunResponse,
    TaskResponse,
    ThreadMessagesResponse,
)
from app.harness import event_bus, task_manager
from app.harness.models import TaskEvent
from app.storage.mysql.database import get_db
from app.storage.mysql.models import AgentRun, StageEvaluation, Task, TaskBudgetUsage

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


@router.get("/tasks", response_model=list[TaskResponse])
def list_tasks(
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
):
    """List recent tasks, newest first (frontend session sidebar)."""
    tasks = (
        db.query(Task)
        .order_by(Task.created_at.desc())
        .offset(offset)
        .limit(limit)
        .all()
    )
    return [_task_to_response(t) for t in tasks]


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


@router.get("/tasks/{task_id}/result", response_model=ResultResponse)
def get_result(task_id: str, db: Session = Depends(get_db)):
    """Fetch the full final answer text (persisted on the AgentRun)."""
    task = task_manager.get_task(db, task_id)
    if not task:
        raise HTTPException(404, "Task not found")
    run = None
    if task.current_run_id:
        run = db.query(AgentRun).filter(AgentRun.id == task.current_run_id).one_or_none()
    if not run:
        raise HTTPException(404, "No run for this task yet")
    return ResultResponse(
        task_id=task_id,
        run_id=run.id,
        run_status=run.status,
        final_output=run.final_output,
    )


@router.post("/tasks/{task_id}/cancel", response_model=CancelResponse)
def cancel_task(task_id: str, db: Session = Depends(get_db)):
    task, message = task_manager.cancel_task(db, task_id)
    if not task:
        raise HTTPException(404, "Task not found")
    return CancelResponse(task_id=task_id, status=task.status, message=message)


@router.post("/tasks/{task_id}/approve", response_model=ApproveResponse)
def approve_task(task_id: str, db: Session = Depends(get_db)):
    """Approve a pending plan; the worker will resume and execute it."""
    task, message = task_manager.approve_plan(db, task_id)
    if not task:
        raise HTTPException(404, message)
    return ApproveResponse(task_id=task_id, status=task.status, message=message)


@router.post("/tasks/{task_id}/reject", response_model=ApproveResponse)
def reject_task(task_id: str, db: Session = Depends(get_db)):
    """Reject a pending plan; the task is cancelled."""
    task, message = task_manager.reject_plan(db, task_id)
    if not task:
        raise HTTPException(404, message)
    return ApproveResponse(task_id=task_id, status=task.status, message=message)


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

        # Then consume live events from Redis Stream. The blocking Redis
        # ``xread`` runs in a worker thread so it never stalls the event loop
        # (which would freeze SSE delivery and the cancel endpoint).
        gen = event_bus.stream(task_id, last_id)
        while True:
            if await request.is_disconnected():
                break
            try:
                event = await anyio.to_thread.run_sync(next, gen)
            except StopIteration:
                break
            if event.get("type") == "ping":
                # Heartbeat with no ``id`` — must not advance Last-Event-ID.
                yield {"event": "ping", "data": json.dumps(event, default=str)}
                continue
            yield {
                "id": event["id"],
                "event": event["type"],
                "data": json.dumps(event, default=str),
            }

    return EventSourceResponse(event_generator())


def _thread_stage_index(thread_id: str, task_id: str) -> int | None:
    """Return the 0-based stage index for a stage thread, else None.

    Stage threads are named ``{task_id}-stage-{n}`` or, across retries,
    ``{task_id}-stage-{n}-attempt-{m}`` (see executor.react_node); the main
    graph thread is ``task_id`` itself.
    """
    if thread_id == task_id:
        return None
    marker = "-stage-"
    suffix = thread_id.rsplit(marker, 1)
    if len(suffix) != 2:
        return None
    # The segment after ``-stage-`` is ``{n}``, optionally ``{n}-attempt-{m}``
    # (or ``{n}-reverse`` for the adversarial lane) — the stage number is the
    # leading token.
    stage_part = suffix[1].split("-", 1)[0]
    return int(stage_part) if stage_part.isdigit() else None


def _thread_sort_key(thread_id: str, task_id: str) -> tuple[int, int]:
    """Order threads: main graph first, then stage threads by numeric index."""
    if thread_id == task_id:
        return (0, 0)
    idx = _thread_stage_index(thread_id, task_id)
    return (1, idx) if idx is not None else (2, 0)


@router.get("/tasks/{task_id}/messages", response_model=list[ThreadMessagesResponse])
def list_messages(task_id: str, db: Session = Depends(get_db)):
    """Full conversation trace grouped by thread (main graph + each stage).

    Exposes the durable ``agent_message`` mirror written by
    ``MessagePersistenceMiddleware``: the model's chain-of-thought
    (``reasoning_content``), tool-call arguments (``tool_calls``), and the raw
    tool results (ToolMessage ``content``).
    """
    task = task_manager.get_task(db, task_id)
    if not task:
        raise HTTPException(404, "Task not found")

    grouped: dict[str, list[MessageResponse]] = {}
    for m in task_manager.list_messages(db, task_id):
        grouped.setdefault(m.thread_id, []).append(
            MessageResponse(
                id=m.id,
                role=m.role,
                content=m.content,
                reasoning_content=m.reasoning_content,
                tool_name=m.tool_name,
                tool_call_id=m.tool_call_id,
                tool_calls=m.tool_calls,
                seq=m.seq,
                created_at=m.created_at,
            )
        )

    return [
        ThreadMessagesResponse(
            thread_id=thread_id,
            stage_index=_thread_stage_index(thread_id, task_id),
            messages=grouped[thread_id],
        )
        for thread_id in sorted(grouped, key=lambda t: _thread_sort_key(t, task_id))
    ]


def _eval_phase_payload(
    rule_result: dict | None,
    forward_result: dict | None,
    reverse_result: dict | None,
    criteria_scores: list | None,
    weighted_score: float | None,
) -> tuple[dict, dict | None, dict | None]:
    """Reconstruct the ``stage.evaluated`` three-phase shape from DB columns.

    Returns ``(phase1, phase2, phase3)``. ``phase2``/``phase3`` are None when
    that lane never ran (e.g. a rule short-circuit skips both LLM phases).
    """
    phase1 = rule_result or {}
    phase2 = None
    if forward_result is not None or reverse_result is not None:
        phase2 = {"forward": forward_result, "reverse": reverse_result}
    phase3 = None
    if criteria_scores is not None or weighted_score is not None:
        phase3 = {"criteria_scores": criteria_scores or [], "weighted_score": weighted_score}
    return phase1, phase2, phase3


def _evaluation_to_response(e: StageEvaluation) -> EvaluationResponse:
    phase1, phase2, phase3 = _eval_phase_payload(
        e.rule_result,
        e.forward_result,
        e.reverse_result,
        e.criteria_scores,
        e.weighted_score,
    )
    return EvaluationResponse(
        id=e.id,
        task_id=e.task_id,
        run_id=e.run_id,
        stage_index=e.stage_index,
        attempt=e.attempt,
        defense_round=e.defense_round,
        status=e.status,
        phase1=phase1,
        phase2=phase2,
        phase3=phase3,
        weighted_score=e.weighted_score,
        feedback=e.feedback,
        acceptance_snapshot=e.acceptance_snapshot,
        created_at=e.created_at,
    )


def _run_to_response(r: AgentRun) -> RunResponse:
    return RunResponse(
        id=r.id,
        task_id=r.task_id,
        thread_id=r.thread_id,
        status=r.status,
        started_at=r.started_at,
        ended_at=r.ended_at,
        checkpoint_ref=r.checkpoint_ref,
        retry_count=r.retry_count,
        error_message=r.error_message,
        final_output=r.final_output,
        created_at=r.created_at,
    )


def _budget_to_response(b: TaskBudgetUsage) -> BudgetUsageResponse:
    return BudgetUsageResponse(
        task_id=b.task_id,
        input_tokens=b.input_tokens,
        output_tokens=b.output_tokens,
        tool_calls=b.tool_calls,
        elapsed_ms=b.elapsed_ms,
        estimated_cost=b.estimated_cost,
        updated_at=b.updated_at,
    )


@router.get("/tasks/{task_id}/runs", response_model=list[RunResponse])
def list_runs(task_id: str, db: Session = Depends(get_db)):
    """Every agent run for a task, oldest first (dashboard 概览)."""
    task = task_manager.get_task(db, task_id)
    if not task:
        raise HTTPException(404, "Task not found")
    return [_run_to_response(r) for r in task_manager.list_runs(db, task_id)]


@router.get("/tasks/{task_id}/evaluations", response_model=list[EvaluationResponse])
def list_evaluations(task_id: str, db: Session = Depends(get_db)):
    """Every evaluator pass for a task (dashboard 评估页签)."""
    task = task_manager.get_task(db, task_id)
    if not task:
        raise HTTPException(404, "Task not found")
    return [_evaluation_to_response(e) for e in task_manager.list_evaluations(db, task_id)]


@router.get("/tasks/{task_id}/budget", response_model=BudgetUsageResponse)
def get_budget(task_id: str, db: Session = Depends(get_db)):
    """Accumulated budget usage for a task (dashboard 概览)."""
    task = task_manager.get_task(db, task_id)
    if not task:
        raise HTTPException(404, "Task not found")
    usage = task_manager.get_budget_usage(db, task_id)
    if not usage:
        raise HTTPException(404, "No budget usage recorded yet")
    return _budget_to_response(usage)


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
