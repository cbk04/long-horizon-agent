"""``plan`` node: research pass, then structured plan generation.

Two phases — a ReAct research agent gathers planning notes first, then a
structured-output LLM turns goal + notes into a stage list (objective +
scope boundary + acceptance items per stage), with a bounded validation
retry. Emits ``plan.generated``.
"""

from __future__ import annotations

import logging
from typing import Any

from langchain_core.messages import HumanMessage, SystemMessage
from langchain_core.runnables import RunnableConfig
from pydantic import BaseModel, Field, model_validator

from app.agent.llm import call_structured, get_llm
from app.agent.react import _check_budget_and_cancel, get_agent, stream_agent
from app.agent.staged.common import AgentState, format_acceptance, publish

logger = logging.getLogger(__name__)

_RESEARCH_PROMPT = """\
你是执行计划的前期调研助手。围绕下面的研究目标做一次快速调研,为后续的阶段划分提供依据:
1. 识别目标中的关键概念和背景信息;
2. 找出主要的数据来源(网站、文献、报告);
3. 归纳评估这类目标产出质量的常见验收维度;
4. 指出可能的难点或风险。

请用简洁的要点输出调研笔记。只做调研,不要试图完成研究目标本身。
"""

_PLANNER_PROMPT = """\
You are a research planner. Design an ordered execution plan for the user's goal,
informed by the research notes provided.

Requirements:
- Split the goal into 2-5 stages that are COMPLETELY non-overlapping: each stage
  owns a distinct slice of the work, and its scope_excludes must declare what it
  deliberately does NOT cover so adjacent stages have disjoint boundaries.
- Stages form a strict sequence: a stage may only consume outputs of EARLIER
  stages. Never make a stage depend on the output of a later one.
- If a stage genuinely reads and builds on an earlier stage's output, list that
  earlier stage's 0-based index in the stage's `depends_on`. Declare a
  dependency only when this stage must consume the earlier stage's conclusion;
  leave `depends_on` empty for ordinary background context.
- The LAST stage must be the final-report stage (is_final=true): it synthesizes
  all prior findings into the deliverable report, and its acceptance MUST
  include readability / ease-of-understanding dimensions of the report itself.
  No other stage may set is_final.
- Each stage has 3-5 acceptance items — a dimension to evaluate plus measurable
  criteria an evaluator can check the executor's output against, each with a
  weight expressing its relative importance. Weights across one stage's items
  MUST sum to 1.0. Phrase each item as a decidable proposition (the evaluator
  scores it 0 / 0.5 / 1).
- For every stage, provide an output_contract when the output shape is
  predictable: min_length (minimum characters of the stage's conclusion) and
  required_sections (keywords that must appear in the output's headings).
  Omit the contract only when genuinely unpredictable.
- Keep the plan focused; do not include stages the executor cannot perform
  with its tools (web search / web fetch / synthesis).
- Return only the structured plan.
"""

# Plan generation attempts: 1 initial + bounded validation retries.
_PLAN_ATTEMPTS = 3


class AcceptanceItem(BaseModel):
    """One acceptance dimension plus its weighted pass criteria for a stage."""

    dimension: str = Field(description="验收维度:评估本阶段产出时的一个独立视角")
    criteria: str = Field(description="该维度下的验收标准:满足什么条件才算通过,表述为可判定命题")
    weight: float = Field(
        gt=0,
        le=1,
        description="该维度在本阶段验收中的权重;(0,1],同一阶段所有维度的权重之和必须为 1",
    )


class OutputContract(BaseModel):
    """Rule-checkable shape contract for a stage's output (evaluator phase 1)."""

    min_length: int = Field(
        default=0,
        ge=0,
        description="阶段产出的最小字符数;0 表示不设下限",
    )
    required_sections: list[str] = Field(
        default_factory=list,
        description="必须在产出标题/小节中出现的关键词列表",
    )


class Stage(BaseModel):
    """A single execution stage produced by the planner."""

    objective: str = Field(description="本阶段要达成的目标:做什么、产出什么")
    scope_excludes: list[str] = Field(
        description="本阶段明确不做的内容;用于与其他阶段划清边界,保证阶段间完全不重叠"
    )
    acceptance: list[AcceptanceItem] = Field(
        description="验收清单,3-5 条带权重的可判定命题;评估者据此打分并判定本阶段是否完成"
    )
    is_final: bool = Field(
        default=False,
        description="是否为最终报告阶段;整份计划必须恰好一个,且必须是最后一个阶段",
    )
    output_contract: OutputContract | None = Field(
        default=None,
        description="产出形态契约(最小长度、必需小节);供规则评估使用,不可预测时可省略",
    )
    depends_on: list[int] = Field(
        default_factory=list,
        description=(
            "本阶段依赖的前序阶段索引(0-based,必须小于自身索引);"
            "仅当本阶段确实要消费其产出时才列出,泛泛关联不要填"
        ),
    )


class Plan(BaseModel):
    """Structured research plan produced by the planner node."""

    stages: list[Stage] = Field(description="Ordered execution stages")

    @model_validator(mode="after")
    def _check_semantics(self) -> "Plan":
        """Semantic constraints beyond the JSON schema: non-empty fields,
        disjoint stage scopes, weighted acceptance, and a single final stage
        in the last position."""
        errors: list[str] = []
        if len(self.stages) < 2:
            errors.append("计划至少需要 2 个阶段")
        final_positions = [i for i, stage in enumerate(self.stages) if stage.is_final]
        if len(final_positions) != 1:
            errors.append("计划必须恰好一个 is_final=true 的最终报告阶段")
        elif final_positions[0] != len(self.stages) - 1:
            errors.append("最终报告阶段必须是最后一个阶段")
        for i, stage in enumerate(self.stages):
            if not stage.objective.strip():
                errors.append(f"阶段{i + 1} objective 不能为空")
            if not stage.scope_excludes:
                errors.append(f"阶段{i + 1} scope_excludes 不能为空(需声明本阶段不做什么)")
            for dep in stage.depends_on:
                if not (0 <= dep < len(self.stages)):
                    errors.append(
                        f"阶段{i + 1} 的 depends_on 索引 {dep} 越界(共 {len(self.stages)} 个阶段)"
                    )
                elif dep >= i:
                    errors.append(f"阶段{i + 1} 只能依赖更早阶段,索引 {dep} 不合法(必须 < {i})")
            if not 3 <= len(stage.acceptance) <= 6:
                errors.append(f"阶段{i + 1} acceptance 需要 3-6 条,当前 {len(stage.acceptance)} 条")
            else:
                weight_sum = sum(item.weight for item in stage.acceptance)
                if abs(weight_sum - 1.0) > 0.01:
                    errors.append(
                        f"阶段{i + 1} acceptance 权重之和必须为 1,当前为 {weight_sum:.2f}"
                    )
        # Disjointness heuristic: a later stage's objective must not fall inside
        # an earlier stage's declared exclusion zone.
        for i in range(1, len(self.stages)):
            for j in range(i):
                for excluded in self.stages[j].scope_excludes:
                    if excluded and excluded in self.stages[i].objective:
                        errors.append(
                            f"阶段{i + 1} 目标与阶段{j + 1} 的排除范围「{excluded}」重叠;"
                            "阶段划分必须互不重叠"
                        )
        if errors:
            raise ValueError(";".join(errors))
        return self


def _run_research(goal: str, task_id: str, limits, on_node=None) -> str:
    """Phase 1: ReAct research pass that produces planning notes."""
    if limits is not None:
        _check_budget_and_cancel(task_id, limits)

    agent = get_agent()
    result_state = stream_agent(
        agent,
        {"messages": [HumanMessage(content=f"研究目标:{goal}\n\n{_RESEARCH_PROMPT}")]},
        config={"configurable": {"thread_id": f"{task_id}-research"}},
        task_id=task_id,
        limits=limits,
        on_node=on_node,
        stream_tokens=True,
    )

    messages_out = result_state.get("messages", [])
    return (
        messages_out[-1].content if messages_out and hasattr(messages_out[-1], "content") else ""
    )


def _generate_plan(goal: str, research_notes: str, task_id: str) -> Plan:
    """Phase 2: structured plan generation with bounded validation retry.

    ``call_structured`` pins the model to ``Plan``'s JSON schema via prompting
    (no forced ``tool_choice`` — thinking-mode backends reject it), parses and
    re-validates, and feeds any violation back for a bounded retry.
    """
    messages: list = [
        SystemMessage(content=_PLANNER_PROMPT),
        HumanMessage(
            content=f"研究目标:{goal}\n\n前期调研笔记:\n{research_notes}\n\n请生成执行计划。"
        ),
    ]

    try:
        plan, _raw = call_structured(
            get_llm(temperature=0.1),
            Plan,
            messages,
            task_id=task_id,
            purpose="plan.generation",
            attempts=_PLAN_ATTEMPTS,
        )
    except RuntimeError as e:
        raise ValueError(
            f"Planner failed to produce a valid plan after {_PLAN_ATTEMPTS} attempts: {e}"
        ) from e
    return plan


def plan_node(state: AgentState, config: RunnableConfig) -> dict[str, Any]:
    """Research the goal, then produce a validated structured plan."""
    goal = state["messages"][-1].content
    task_id = config["configurable"]["thread_id"]
    run_id = config["configurable"].get("run_id", "")
    limits = config["configurable"].get("budget_limits")

    research_notes = _run_research(goal, task_id, limits)
    plan = _generate_plan(goal, research_notes, task_id)

    publish(task_id, "plan.generated", {
        "run_id": run_id,
        "stages": [stage.model_dump() for stage in plan.stages],
    })

    plan_text = "\n".join(
        f"{i + 1}. 目标:{stage.objective}\n"
        f"   不做:{'、'.join(stage.scope_excludes)}\n"
        f"   验收:\n{format_acceptance([item.model_dump() for item in stage.acceptance])}"
        for i, stage in enumerate(plan.stages)
    )
    return {
        "goal": goal,
        "plan": [stage.model_dump() for stage in plan.stages],
        "current_stage_index": 0,
        "stage_attempts": 0,
        "messages": [HumanMessage(content=f"执行计划如下：\n{plan_text}\n\n请按此计划执行并给出最终结果。")],
    }
