"""Event persistence model — append-only log of task events."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import BigInteger, DateTime, String
from sqlalchemy.dialects.mysql import JSON as MySQLJSON
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.sql import func

from app.storage.mysql.database import Base


class TaskEvent(Base):
    """Persistent event log for a task (audit / replay / historical query)."""

    __tablename__: str = "task_event"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    task_id: Mapped[str] = mapped_column(String(64), index=True, nullable=False)
    stream_id: Mapped[str] = mapped_column(String(64), nullable=False, comment="Redis Stream ID")
    type: Mapped[str] = mapped_column(String(64), index=True, nullable=False)
    payload: Mapped[dict[str, object]] = mapped_column(MySQLJSON, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), nullable=False
    )
