"""End-to-end API test: create task via HTTP, verify SSE stream, query status.

Requires a running API server at http://127.0.0.1:8000 (uvicorn app.main:app).
Tests are skipped automatically when the server is unreachable.
"""

from __future__ import annotations

import json
import threading
import time
import uuid

import httpx
import pytest

API = "http://127.0.0.1:8000"
SSE_TIMEOUT_S = 10


def _server_up() -> bool:
    try:
        httpx.get(f"{API}/docs", timeout=2)
        return True
    except httpx.HTTPError:
        return False


@pytest.fixture(scope="module")
def api_server():
    if not _server_up():
        pytest.skip(f"API server not running at {API} — start it with: uvicorn app.main:app")


@pytest.fixture(scope="module")
def created_task(api_server) -> dict:
    """Create a task via POST /api/v1/tasks; shared by the tests in this module."""
    req = {
        "goal": f"API E2E test {uuid.uuid4().hex[:6]}",
        "priority": 5,
        "search_depth": "normal",
        "output_format": "report",
        "budget_tokens": 50000,
        "budget_seconds": 300,
        "budget_cost": 1.0,
    }
    r = httpx.post(f"{API}/api/v1/tasks", json=req)
    print(f"[POST /tasks] {r.status_code}: {r.text}")
    assert r.status_code == 201
    return r.json()


def test_create_via_api(created_task):
    assert created_task["status"] == "QUEUED"
    assert created_task["id"].startswith("task-")


def test_get_via_api(created_task):
    task_id = created_task["id"]
    r = httpx.get(f"{API}/api/v1/tasks/{task_id}")
    print(f"[GET /tasks/{{id}}] {r.status_code}: {r.json()}")
    assert r.status_code == 200
    assert r.json()["id"] == task_id


def test_list_events_via_api(created_task):
    task_id = created_task["id"]
    r = httpx.get(f"{API}/api/v1/tasks/{task_id}/events")
    print(f"[GET /events] {r.status_code}: {len(r.json())} events")
    assert r.status_code == 200
    events = r.json()
    assert any(e["type"] == "task.created" for e in events)


def test_sse_stream(created_task):
    """SSE stream should yield events. Worker runs in a background thread."""
    task_id = created_task["id"]

    from app.harness.worker import _process_task

    def run_worker():
        time.sleep(0.5)  # let SSE connect first
        _process_task(task_id, "api-test-worker")

    t = threading.Thread(target=run_worker, daemon=True)
    t.start()

    received = []
    with httpx.stream("GET", f"{API}/api/v1/tasks/{task_id}/stream", timeout=SSE_TIMEOUT_S) as r:
        print(f"[GET /stream] {r.status_code}")
        assert r.status_code == 200
        for line in r.iter_lines():
            if line.startswith("data:"):
                data = line[5:].strip()
                if data:
                    try:
                        evt = json.loads(data)
                        received.append(evt)
                        print(f"  [SSE] {evt.get('type')} - id={evt.get('id')}")
                    except Exception:
                        pass
            if len(received) >= 8:  # Got enough events
                break

    print(f"\n[Total SSE events received: {len(received)}]")
    types = [e.get("type") for e in received]
    print(f"Event types: {types}")
    assert received, "Expected at least one SSE event"


def test_cancel_via_api(created_task):
    task_id = created_task["id"]
    r = httpx.post(f"{API}/api/v1/tasks/{task_id}/cancel")
    print(f"[POST /cancel] {r.status_code}: {r.json()}")
    assert r.status_code == 200


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v", "-s"]))
