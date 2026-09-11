"""FastAPI application entry point."""

from __future__ import annotations

from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import text

from app.api.tasks import router as tasks_router
from app.storage.mysql.database import engine
from app.storage.redis.client import redis_client

STATIC_DIR = Path(__file__).parent / "static"


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    # Startup: verify connectivity
    try:
        with engine.connect() as conn:
            _ = conn.execute(text("SELECT 1"))
    except Exception as e:
        print(f"[startup] MySQL connect failed: {e}")
    try:
        _ = redis_client.ping()
    except Exception as e:
        print(f"[startup] Redis connect failed: {e}")
    yield
    # Shutdown: dispose engine
    engine.dispose()


app = FastAPI(
    title="Long-Horizon DeepSearch Agent Harness",
    version="0.1.0",
    lifespan=lifespan,
)

app.include_router(tasks_router)


@app.get("/", include_in_schema=False)
async def index():
    """Serve the chat frontend."""
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/dashboard", include_in_schema=False)
async def dashboard():
    """Serve the standalone observability dashboard."""
    return FileResponse(STATIC_DIR / "dashboard.html")


app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@app.get("/health")
async def health():
    """Health check: verify MySQL and Redis connectivity."""
    status: dict[str, str] = {"status": "ok", "mysql": "unknown", "redis": "unknown"}
    try:
        with engine.connect() as conn:
            _ = conn.execute(text("SELECT 1"))
        status["mysql"] = "ok"
    except Exception as e:
        status["mysql"] = f"error: {e}"
        status["status"] = "degraded"
    try:
        _ = redis_client.ping()
        status["redis"] = "ok"
    except Exception as e:
        status["redis"] = f"error: {e}"
        status["status"] = "degraded"
    return status
