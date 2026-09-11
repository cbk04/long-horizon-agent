"""Debug entry point: run the staged agent directly, bypassing the API + worker poll.

Run under the VS Code debugger (configuration "Debug Agent (direct)") or with::

    uv run python scripts/debug_agent.py

It replicates what ``app.harness.worker._process_task`` does — create a task, run the
plan pass (which pauses at the approval gate), then resume with approval to run every
stage to the final answer — but calls ``execute_agent`` synchronously so you can set
breakpoints anywhere in ``app/agent/staged/*`` and step straight into the graph.

Prereqs: MySQL + Redis must be running. ``publish`` (common.py), ``budget``, and the
MySQL checkpointer all depend on them. Set CHECKPOINT_BACKEND=memory in .env if you'd
rather not touch the MySQL checkpointer tables (publish/budget still need both).
"""

from __future__ import annotations

import logging

from app.agent.staged import AwaitingApproval, execute_agent
from app.harness import budget
from app.harness.task_manager import create_task
from app.storage.mysql.database import SessionLocal

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)

# TODO: 改成你要调试的目标问题
GOAL = "调研 2026 年大模型 Agent 在长程任务规划上的现状，并给出可落地的方案"

# 与 create_task 的 budget 保持一致，避免过早触发熔断
BUDGET_TOKENS = 50_000
BUDGET_SECONDS = 600
BUDGET_TOOL_CALLS = 20
BUDGET_COST = 5.0


def main() -> None:
    db = SessionLocal()
    try:
        task = create_task(
            db,
            goal=GOAL,
            priority=0,
            search_depth="deep",
            output_format="report",
            budget_tokens=BUDGET_TOKENS,
            budget_seconds=BUDGET_SECONDS,
            budget_cost=BUDGET_COST,
        )
    finally:
        db.close()

    task_id = task.id
    run_id = f"run-{task_id}"

    limits = budget.BudgetLimits(
        max_tokens=BUDGET_TOKENS,
        max_seconds=BUDGET_SECONDS,
        max_tool_calls=BUDGET_TOOL_CALLS,
        max_cost=BUDGET_COST,
    )

    # Pass 1: plan 节点 → 在 approval gate 处 interrupt，抛 AwaitingApproval。
    # 在这里打断点可以观察 planner 产出的结构化 plan。
    try:
        execute_agent(task_id, run_id, GOAL, limits)
    except AwaitingApproval:
        print(f"[debug] plan produced; task {task_id} awaiting approval")
    else:
        print("[debug] plan pass finished without the approval gate")

    # Pass 2: 批准后跑完所有 stage（react → evaluate 循环）到最终答案。
    # 把断点下在 app/agent/staged/executor.py 或 evaluator.py 里。
    result = execute_agent(task_id, run_id, GOAL, limits, resume={"approved": True})

    print("\n" + "=" * 60)
    print("FINAL RESULT:")
    print(result)
    print("=" * 60)


if __name__ == "__main__":
    main()
