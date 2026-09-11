"""Interactive planner tester — REPL loop, no graph/worker, no per-test scripts.

Usage:
  python scripts/try_planner.py                # 交互模式:输入目标即测,循环往复
  python scripts/try_planner.py "研究目标"      # 单次模式:测完即退
  python scripts/try_planner.py --research     # 交互模式,默认开启调研阶段

交互模式内置命令:
  :research     开/关调研阶段(默认关)
  :debug        开/关 LangChain debug(打印每次 LLM 的完整 prompt/响应)
  :runs N       接下来每个目标重复跑 N 次
  :json 路径    保存上一次计划为 JSON
  :q            退出

Requires only .env LLM config (OPENAI_API_KEY / OPENAI_BASE_URL / LLM_MODEL).
Research mode additionally needs network access to DuckDuckGo (search tools).
No MySQL / Redis involved.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.agent.staged.planner import (  # noqa: E402
    _PLAN_ATTEMPTS,
    _generate_plan,
    _run_research,
)


def print_plan(plan, label: str = "") -> None:
    if label:
        print(f"\n{'=' * 60}\n{label}\n{'=' * 60}")
    for i, stage in enumerate(plan.stages):
        print(f"\n--- 阶段 {i + 1} ---")
        print(f"目标:   {stage.objective}")
        print(f"不涉及: {'; '.join(stage.scope_excludes)}")
        print("验收:")
        for item in stage.acceptance:
            print(f"  - [{item.dimension}] {item.criteria}")


def run_once(goal: str, research: bool):
    t0 = time.monotonic()

    if research:
        print("[1/2] ReAct 调研中……(可能需要 1-2 分钟)")
        notes = _run_research(
            goal,
            task_id="planner-test",
            limits=None,
            on_node=lambda node: print(f"  [node] {node}"),
        )
        print(f"      调研笔记 {len(notes)} 字符,用时 {time.monotonic() - t0:.0f}s")
        print(f"\n--- 调研笔记(前 800 字) ---\n{notes[:800]}\n")
    else:
        notes = "(未执行调研——只测计划生成)"
        print("[1/2] 跳过调研(:research 可开启)")

    print("[2/2] 生成计划中……")
    try:
        plan = _generate_plan(goal, notes)
    except ValueError as e:
        print(f"\n[FAIL] {_PLAN_ATTEMPTS} 次尝试后仍未通过校验:\n{e}")
        return None

    elapsed = time.monotonic() - t0
    print_plan(plan, label=f"生成的计划(共 {len(plan.stages)} 个阶段, 用时 {elapsed:.0f}s)")
    return plan


def repl(research: bool) -> None:
    print("Planner 测试 REPL。输入研究目标开始;:research/:debug/:runs/:json/:q 为内置命令。")
    state = {"research": research, "runs": 1, "last_plan": None}
    while True:
        try:
            line = input("\ngoal> ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            break
        if not line:
            continue
        if line == ":q":
            break
        if line == ":research":
            state["research"] = not state["research"]
            print(f"research = {state['research']}")
            continue
        if line == ":debug":
            from langchain.globals import set_debug

            new = not state.get("debug", False)
            set_debug(new)
            state["debug"] = new
            print(f"langchain debug = {new}")
            continue
        if line.startswith(":runs"):
            state["runs"] = int(line.split()[1])
            print(f"runs = {state['runs']}")
            continue
        if line.startswith(":json"):
            if state["last_plan"] is None:
                print("还没有生成过计划")
                continue
            path = line.split(maxsplit=1)[1]
            with open(path, "w", encoding="utf-8") as f:
                json.dump(
                    [s.model_dump() for s in state["last_plan"].stages],
                    f, ensure_ascii=False, indent=2,
                )
            print(f"已保存: {path}")
            continue
        if line.startswith(":"):
            print(f"未知命令: {line}")
            continue

        plan = None
        for _ in range(state["runs"]):
            plan = run_once(line, research=state["research"])
        state["last_plan"] = plan


def main() -> None:
    parser = argparse.ArgumentParser(description="交互式测试 planner 的效果")
    parser.add_argument("goal", nargs="?", help="研究目标;省略则进入交互模式")
    parser.add_argument("--research", action="store_true", help="开启调研阶段(默认关)")
    args = parser.parse_args()

    if args.goal:
        run_once(args.goal, research=args.research)
    else:
        repl(args.research)


if __name__ == "__main__":
    main()
