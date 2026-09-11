"""Budget controller — track and enforce token / time / tool / cost limits."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from app.storage.redis.client import redis_client

BUDGET_KEY = "task:{task_id}:budget"


def _budget_key(task_id: str) -> str:
    return BUDGET_KEY.format(task_id=task_id)


class BudgetVerdict(str, Enum):
    CONTINUE = "continue"
    WARNING = "warning"   # Within 80% of limit
    STOP = "stop"         # Exceeded


@dataclass
class BudgetUsage:
    input_tokens: int = 0
    output_tokens: int = 0
    tool_calls: int = 0
    elapsed_ms: int = 0
    estimated_cost: float = 0.0


@dataclass
class BudgetLimits:
    max_tokens: int
    max_seconds: int
    max_tool_calls: int
    max_cost: float


def reset(task_id: str) -> None:
    """Reset budget counters for a task."""
    redis_client.delete(_budget_key(task_id))


def add_input_tokens(task_id: str, n: int) -> None:
    redis_client.hincrby(_budget_key(task_id), "input_tokens", n)


def add_output_tokens(task_id: str, n: int) -> None:
    redis_client.hincrby(_budget_key(task_id), "output_tokens", n)


def add_tool_call(task_id: str) -> None:
    redis_client.hincrby(_budget_key(task_id), "tool_calls", 1)


def add_elapsed_ms(task_id: str, ms: int) -> None:
    redis_client.hincrby(_budget_key(task_id), "elapsed_ms", ms)


def add_cost(task_id: str, cost: float) -> None:
    redis_client.hincrbyfloat(_budget_key(task_id), "estimated_cost", cost)


def get_usage(task_id: str) -> BudgetUsage:
    raw = redis_client.hgetall(_budget_key(task_id))
    return BudgetUsage(
        input_tokens=int(raw.get("input_tokens", 0)),
        output_tokens=int(raw.get("output_tokens", 0)),
        tool_calls=int(raw.get("tool_calls", 0)),
        elapsed_ms=int(raw.get("elapsed_ms", 0)),
        estimated_cost=float(raw.get("estimated_cost", 0.0)),
    )


def check(task_id: str, limits: BudgetLimits) -> BudgetVerdict:
    """Check whether the task is within budget.

    Returns:
        STOP — at least one limit exceeded
        WARNING — any usage >= 80% of its limit
        CONTINUE — all good
    """
    usage = get_usage(task_id)
    total_tokens = usage.input_tokens + usage.output_tokens
    elapsed_s = usage.elapsed_ms / 1000.0

    # Hard limits
    if total_tokens > limits.max_tokens:
        return BudgetVerdict.STOP
    if elapsed_s > limits.max_seconds:
        return BudgetVerdict.STOP
    if usage.tool_calls > limits.max_tool_calls:
        return BudgetVerdict.STOP
    if usage.estimated_cost > limits.max_cost:
        return BudgetVerdict.STOP

    # Soft warning (80%)
    threshold = 0.8
    if (
        total_tokens > limits.max_tokens * threshold
        or elapsed_s > limits.max_seconds * threshold
        or usage.tool_calls > limits.max_tool_calls * threshold
        or usage.estimated_cost > limits.max_cost * threshold
    ):
        return BudgetVerdict.WARNING

    return BudgetVerdict.CONTINUE


class BudgetExceeded(Exception):
    """Raised when budget is exceeded and the task must stop."""

    def __init__(self, task_id: str, reason: str, usage: BudgetUsage):
        self.task_id: str = task_id
        self.reason: str = reason
        self.usage: BudgetUsage = usage
        super().__init__(f"Budget exceeded for {task_id}: {reason}")
