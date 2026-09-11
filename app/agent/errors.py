"""Agent-level exceptions shared across the runtime.

``CancelledByUser`` lives here (not in ``react.py``) so that both the budget
guard and the streaming callback handler can raise/import it without a
circular import.
"""

from __future__ import annotations


class CancelledByUser(Exception):
    """Raised when the user has cancelled the task mid-execution."""

    def __init__(self, task_id: str):
        self.task_id = task_id
        super().__init__(f"Task {task_id} cancelled by user")
