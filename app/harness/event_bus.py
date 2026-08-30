"""Event bus — dual writes to Redis Stream and MySQL task_event.

Redis Stream is the source for real-time SSE delivery.
MySQL task_event is the durable log for audit / replay.
"""

from __future__ import annotations

import json
from collections.abc import Generator
from datetime import datetime, timezone
from typing import Any, cast

from sqlalchemy.orm import Session

from app.harness.models import TaskEvent
from app.storage.redis.client import redis_client


# Redis key template for per-task event stream
EVENTS_KEY = "task:{task_id}:events"
# Default retention for completed task streams (24 hours)
DEFAULT_STREAM_TTL = 86400


def _events_key(task_id: str) -> str:
    return EVENTS_KEY.format(task_id=task_id)


def publish(
    db: Session,
    task_id: str,
    event_type: str,
    payload: dict[str, Any],
) -> str:
    """Publish an event. Returns the Redis Stream ID.

    Writes to both Redis Stream (real-time) and MySQL (durable).
    """
    now = datetime.now(timezone.utc)
    payload_json = json.dumps(payload, ensure_ascii=False, default=str)

    # 1. Redis Stream — source for SSE
    raw_id = redis_client.xadd(
        _events_key(task_id),
        {
            "type": event_type,
            "payload": payload_json,
            "ts": now.isoformat(),
        },
        maxlen=10_000,  # Cap stream length to avoid unbounded growth
        approximate=True,
    )
    # decode_responses=True in client.py, so xadd returns str.
    stream_id: str = raw_id if isinstance(raw_id, str) else str(raw_id)

    # 2. MySQL — durable log
    event = TaskEvent(
        task_id=task_id,
        stream_id=stream_id,
        type=event_type,
        payload=payload,
    )
    db.add(event)
    db.commit()

    return stream_id


def consume_stream(
    task_id: str,
    last_id: str = "0",
    block_ms: int = 5000,
    count: int = 100,
) -> list[dict[str, Any]]:
    """Read events from a task's stream after last_id (exclusive)."""
    result = redis_client.xread(
        {_events_key(task_id): last_id},
        block=block_ms,
        count=count,
    )
    if not result:
        return []
    # Result: [(stream_key, [(stream_id, {field: value}), ...])]
    # redis-py's type stubs are overly broad; with decode_responses=True the actual
    # runtime type is tuple[str, list[tuple[str, dict[str, str]]]].
    raw: object = result[0]
    first_result = cast(tuple[str, list[tuple[str, dict[str, str]]]], raw)
    _, entries = first_result
    events: list[dict[str, Any]] = []
    for entry in entries:
        entry_id, fields = entry
        events.append(
            {
                "id": entry_id,
                "task_id": task_id,
                "type": fields.get("type", ""),
                "payload": json.loads(fields.get("payload", "{}")),
                "timestamp": fields.get("ts", ""),
            }
        )
    return events


def stream(
    task_id: str,
    last_id: str = "0",
) -> Generator[dict[str, Any], None, None]:
    """Generator that yields events as they arrive. For SSE consumption."""
    current_id = last_id
    while True:
        events = consume_stream(task_id, current_id, block_ms=5000, count=100)
        if not events:
            # Heartbeat: send a ping to keep SSE connection alive
            yield {
                "id": f"{current_id}-ping",
                "task_id": task_id,
                "type": "ping",
                "payload": {},
                "timestamp": datetime.now(timezone.utc).isoformat(),
            }
            continue
        for event in events:
            current_id = event["id"]
            yield event


def cleanup(task_id: str) -> int:
    """Delete all Redis keys for a task. Returns number of keys deleted."""
    keys = redis_client.keys(f"task:{task_id}:*")
    if keys:
        return redis_client.delete(*keys)
    return 0
