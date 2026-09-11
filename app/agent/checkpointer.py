"""Checkpointer setup for LangGraph state persistence.

The checkpointer snapshots the full graph state after every super-step,
keyed by ``thread_id = task_id``. One mechanism, three consumers: the
approval-gate interrupt/resume, state carried across the worker's two
passes (plan pass → approval → resume pass), and — once the backend is
durable — crash recovery after a worker restart.

Backends (``settings.checkpoint_backend``):
- ``mysql``  — PyMySQLSaver writing to the same MySQL database as the
  business tables. The saver owns four ``checkpoint_*`` tables; it creates
  and migrates them itself via ``.setup()`` (idempotent, runs at first
  access). Requires MySQL >= 8.0.19 or MariaDB >= 10.7.1.
- ``memory`` — MemorySaver, in-process only; snapshots die with the worker
  (dev/test fallback).

Usage in agent:
    graph.compile(checkpointer=get_checkpointer())
    graph.invoke(inputs, config={"configurable": {"thread_id": task_id}})
"""

from __future__ import annotations

import logging
from typing import Any

from app.config import Settings, get_settings

logger = logging.getLogger(__name__)

# Module-level singleton — created on first access
_checkpointer: Any | None = None


def get_checkpointer() -> Any:
    """Return the checkpointer singleton for the configured backend."""
    global _checkpointer
    if _checkpointer is not None:
        return _checkpointer

    settings = get_settings()
    if settings.checkpoint_backend == "mysql":
        _checkpointer = _build_mysql_checkpointer(settings)
    else:
        from langgraph.checkpoint.memory import MemorySaver

        _checkpointer = MemorySaver()
        logger.info("Checkpointer: MemorySaver (in-process)")
    return _checkpointer


def _build_mysql_checkpointer(settings: Settings) -> Any:
    """Build a PyMySQLSaver over a dedicated raw connection.

    The saver needs a raw DBAPI connection (not the SQLAlchemy engine) with
    ``autocommit=True`` — its writes and ``.setup()`` commit directly. The
    connection lives as long as the singleton, i.e. the worker process.
    """
    import pymysql
    from langgraph.checkpoint.mysql.pymysql import PyMySQLSaver

    conn = pymysql.connect(
        host=settings.mysql_host,
        port=settings.mysql_port,
        user=settings.mysql_user,
        password=settings.mysql_password,
        database=settings.mysql_database,
        autocommit=True,
        charset="utf8mb4",
    )
    checkpointer = PyMySQLSaver(conn)
    checkpointer.setup()
    logger.info("Checkpointer: PyMySQLSaver (durable, mysql backend)")
    return checkpointer
