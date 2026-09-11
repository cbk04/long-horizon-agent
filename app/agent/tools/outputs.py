"""Retrieval-fallback tools — reach prior stages' accepted outputs.

The staged executor normally gets a bounded handoff (``depends_on`` conclusions +
key findings) baked into its prompt. But the planner can under-declare a
dependency. These tools are the safety net: they read the durable
``stage_output`` store directly, so the current stage can pull any earlier
stage's conclusions without the planner having predicted the need.
"""

from __future__ import annotations

from typing import Annotated

from langchain_core.runnables import RunnableConfig
from langchain_core.tools import InjectedToolArg, tool

from app.agent import stage_output_repo


def _task_id(config: RunnableConfig) -> str:
    return (config.get("configurable") or {}).get("task_id", "")


@tool
def list_prior_outputs(config: Annotated[RunnableConfig, InjectedToolArg]) -> str:
    """列出所有前序阶段已通过的结论概览。

    返回每个已完成阶段的目标、关键发现和证据 id 列表(不含完整结论)。当你需要
    某个前序阶段的完整结论时,用该阶段的 stage_index 调用 get_stage_output。

    Args:
        (无) — 仅依赖当前任务上下文。
    """
    task_id = _task_id(config)
    if not task_id:
        return "无法确定当前任务上下文,未能列出前序阶段输出。"
    rows = stage_output_repo.list_stage_outputs(task_id)
    if not rows:
        return "暂无前序阶段的输出记录。"
    lines: list[str] = []
    for r in rows:
        kf = "；".join(r.get("key_findings") or [])
        ev_ids = "、".join(str(ev.get("id", "")) for ev in (r.get("evidence_index") or []))
        lines.append(
            f"- 阶段{r['stage_index'] + 1}（{r['objective']}）\n"
            f"  关键发现：{kf or '(无)'}\n"
            f"  证据 id：{ev_ids or '(无)'}"
        )
    return "\n".join(lines)


@tool
def get_stage_output(stage_index: int, config: Annotated[RunnableConfig, InjectedToolArg]) -> str:
    """取回某个前序阶段已通过的完整结论。

    当 list_prior_outputs 的概览不足以支撑当前阶段、而你又确实需要某个前序
    阶段的完整结论时调用本工具。stage_index 从 0 开始(阶段1 = 0)。

    Args:
        stage_index: 目标阶段的下标(0 起)。
    """
    task_id = _task_id(config)
    if not task_id:
        return "无法确定当前任务上下文,未能检索前序阶段结论。"
    row = stage_output_repo.get_stage_output(task_id, stage_index)
    if row is None:
        return f"未找到阶段{stage_index + 1}的结论(可能尚未完成或下标有误)。"
    return f"阶段{stage_index + 1}目标：{row['objective']}\n\n阶段结论：\n{row['conclusion']}"
