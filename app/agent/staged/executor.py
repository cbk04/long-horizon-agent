"""``react`` node: per-stage execution with a bounded handoff.

Invokes the ReAct agent from ``app.agent.react`` on a dedicated thread per
stage so each stage starts from a bounded handoff (goal + prior key
conclusions + gaps) instead of the full message history. The thread's
messages are then classified into conclusion / evidence / reasoning so
downstream nodes can treat them differently.

A second, lightweight mode serves the defend lane: when the evaluator's
strength adjudication found a contested counterexample, the executor does not
redo the stage — it only answers the counterexamples point by point
(``_defend_node``), appending the response to the stage conclusion.
"""

from __future__ import annotations

import uuid
from typing import Any

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langchain_core.runnables import RunnableConfig

from app.agent import evidence_repo
from app.agent.controller import Action
from app.agent.llm import call_llm, get_llm
from app.agent.react import _check_budget_and_cancel, get_agent, stream_agent
from app.agent.staged.common import (
    AgentState,
    format_acceptance,
    format_contract,
    node_step_publisher,
)
from app.agent.tools.search import parse_fetch_digest
from app.config import get_settings

# Bounds for harvested stage artifacts so neither the evaluator prompt nor
# the checkpointed state carries full fetched pages or endless chains.
_EVIDENCE_SNIPPET_CHARS = 1200
_MAX_EVIDENCE_ITEMS = 12
_REASONING_SNIPPET_CHARS = 800
_EVIDENCE_SUMMARY_CHARS = 200
_EVIDENCE_INDEX_SUMMARY_CHARS = 120
_MAX_EVIDENCE_INDEX_LINES = 30

_DEFEND_PROMPT = """\
你是研究执行者。你此前得出的阶段结论被外部反例挑战。请针对下面每条反例逐条回应:
- 若反例成立:修正或限定你的结论表述,说明其适用范围;
- 若反例不成立或只是口径/视角差异:在你已有证据范围内给出辩驳理由;
- 严禁引入新的事实性主张——某一点确实需要新证据就写明"此点需重新核实",不要编造;
- 只输出补充说明文本,不要重复整段结论。
"""


def _truncate(text: Any, limit: int) -> str:
    """Truncate to ``limit`` chars with an explicit marker when cut."""
    text = str(text or "").strip()
    if len(text) <= limit:
        return text
    return text[:limit] + "…[截断]"


def _harvest_stage_artifacts(messages: list) -> dict[str, Any]:
    """Classify the react thread's messages into conclusion/evidence/reasoning.

    - ``ToolMessage`` → evidence, paired back to its originating query via
      ``tool_call_id``;
    - ``AIMessage`` with ``tool_calls`` → intermediate reasoning (plus the
      backend's real chain-of-thought from
      ``additional_kwargs["reasoning_content"]`` when present);
    - the final ``AIMessage`` without ``tool_calls`` → conclusion.
    """
    evidence: list[dict[str, Any]] = []
    reasoning: list[str] = []
    conclusion = ""
    pending: dict[str, Any] = {}  # tool_call_id → call args

    for msg in messages:
        kwargs = getattr(msg, "additional_kwargs", None)
        think = kwargs.get("reasoning_content") if isinstance(kwargs, dict) else None

        if isinstance(msg, ToolMessage):
            # A web_fetch digest carries its fetch-time evidence id; reuse it so
            # the index points at the already-persisted full text (no duplicate).
            if msg.name == "web_fetch":
                eid, head = parse_fetch_digest(str(msg.content))
                if eid:
                    evidence.append({
                        "tool": msg.name,
                        "query": pending.pop(msg.tool_call_id, None),
                        "content": head or _truncate(msg.content, _EVIDENCE_SNIPPET_CHARS),
                        "id": eid,
                    })
                    continue
            evidence.append({
                "tool": msg.name,
                "query": pending.pop(msg.tool_call_id, None),
                "content": _truncate(msg.content, _EVIDENCE_SNIPPET_CHARS),
            })
        elif isinstance(msg, AIMessage) and msg.tool_calls:
            for call in msg.tool_calls:
                call_id = call.get("id")
                if call_id:
                    pending[call_id] = call.get("args")
            if think:
                reasoning.append(_truncate(think, _REASONING_SNIPPET_CHARS))
            if msg.content:
                reasoning.append(_truncate(msg.content, _REASONING_SNIPPET_CHARS))
        elif isinstance(msg, AIMessage):
            conclusion = str(msg.content)
            if think:
                reasoning.append(_truncate(think, _REASONING_SNIPPET_CHARS))

    # Odd endings (e.g. a budget stop right after a tool call): fall back to
    # the last message's content so stage_result behaves as before.
    if not conclusion and messages:
        conclusion = str(getattr(messages[-1], "content", "") or "")

    return {
        "conclusion": conclusion,
        "evidence": evidence[:_MAX_EVIDENCE_ITEMS],
        "reasoning": reasoning,
    }


def _build_evidence_records(
    evidence: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Split harvested evidence into persist-bound records and a cheap index.

    Each harvested item is ``{tool, query, content}``. The record keeps the full
    content for on-demand retrieval (``get_evidence``); the index keeps only
    id + url + summary so the checkpointed state stays light.
    """
    records: list[dict[str, Any]] = []
    index: list[dict[str, Any]] = []
    for item in evidence:
        query = item.get("query")
        url = query.get("url") if isinstance(query, dict) else ""
        content = item.get("content") or ""
        summary = _truncate(content, _EVIDENCE_SUMMARY_CHARS)
        eid = item.get("id") or uuid.uuid4().hex
        index.append({
            "id": eid,
            "url": url or "",
            "summary": summary,
            "tool": item.get("tool"),
        })
        # A web_fetch digest's full text is already persisted at fetch time;
        # keep its id for the index but do not re-persist a duplicate row.
        if item.get("id"):
            continue
        records.append({
            "id": eid,
            "url": url or "",
            "summary": summary,
            "content": content,
        })
    return records, index


def _build_stage_prompt(state: AgentState) -> str:
    """Bounded handoff for the current stage: goal + prior conclusions + gaps."""
    stage = state["plan"][state["current_stage_index"]]
    parts = [
        f"总体目标：{state['goal']}",
        f"当前阶段目标：{stage['objective']}",
        f"本阶段不做（边界）：{'、'.join(stage['scope_excludes'])}",
        f"验收标准：\n{format_acceptance(stage['acceptance'])}",
    ]

    contract_text = format_contract(stage.get("output_contract"))
    if contract_text:
        parts.append(f"产出形态要求（硬性，必须满足）：{contract_text}")

    max_chars = get_settings().max_inline_conclusion_chars

    prior = state.get("stage_results") or []
    if prior:
        depends_on = set(stage.get("depends_on") or [])
        summary_lines: list[str] = []
        full_lines: list[str] = []
        index_lines: list[str] = []
        for r in prior:
            label = f"阶段{r['stage_index'] + 1}（{r['objective']}）"
            if r["stage_index"] in depends_on:
                full_lines.append(
                    f"【{label}·完整结论】\n{_truncate(r.get('conclusion') or '', max_chars)}"
                )
            else:
                kf = "；".join(r.get("key_findings") or [])
                summary_lines.append(f"- {label}：{kf}")
            for ev in r.get("evidence_index") or []:
                url = ev.get("url") or ""
                index_lines.append(
                    f"- {ev.get('id')} | {url} | {_truncate(ev.get('summary') or '', _EVIDENCE_INDEX_SUMMARY_CHARS)}"
                )
        if full_lines:
            parts.append("本阶段依赖的前序阶段完整结论：\n\n" + "\n\n".join(full_lines))
        if summary_lines:
            parts.append("其他前序阶段的关键结论：\n" + "\n".join(summary_lines))
        if index_lines:
            parts.append(
                "前序阶段已抓取的证据索引(需要某条完整内容时,用其 id 调用 get_evidence 工具)：\n"
                + "\n".join(index_lines[:_MAX_EVIDENCE_INDEX_LINES])
            )

    feedback = state.get("stage_feedback")
    if feedback:
        gaps = "\n".join(f"- {gap}" for gap in feedback)
        parts.append(f"上一轮尝试未通过评估，请重点补足以下缺口：\n{gaps}")

    # On a retry, hand back the rejected attempt's conclusion and its lightweight
    # evidence index so the fresh thread knows what was already tried and fetched
    # — without carrying the raw message history back into context.
    if (state.get("stage_attempts") or 0) > 1:
        prev_conclusion = _truncate(state.get("stage_result") or "", max_chars)
        if prev_conclusion:
            parts.append(
                "【上一轮被驳回的结论（请针对上面的缺口修正，不要复述已被否定的部分）】\n"
                + prev_conclusion
            )
        prev_index = state.get("stage_evidence_index") or []
        if prev_index:
            idx_lines = [
                f"- {ev.get('id')} | {ev.get('url') or ''} | "
                f"{_truncate(ev.get('summary') or '', _EVIDENCE_INDEX_SUMMARY_CHARS)}"
                for ev in prev_index[:_MAX_EVIDENCE_INDEX_LINES]
            ]
            parts.append(
                "上一轮已抓取的证据索引（需要完整内容时用其 id 调用 get_evidence 工具）：\n"
                + "\n".join(idx_lines)
            )

    parts.append("请执行本阶段并给出结论。")
    return "\n\n".join(parts)


def _defend_node(state: AgentState, config: RunnableConfig) -> dict[str, Any]:
    """Lightweight defend pass: answer the counterexamples, redo nothing.

    The response is budget-accounted (one LLM call) and appended to the stage
    conclusion; the evaluator re-adjudicates only the strength lane afterwards.
    """
    task_id = config["configurable"]["thread_id"]
    feedback = state.get("stage_feedback") or []
    counterexample_block = "\n".join(f"- {line}" for line in feedback) or "(无)"

    response = call_llm(
        get_llm(temperature=0.0),
        [
            HumanMessage(
                content=(
                    f"阶段目标:{state['plan'][state['current_stage_index']]['objective']}\n\n"
                    f"【被挑战的结论】\n{state.get('stage_result') or ''}\n\n"
                    f"【反例】\n{counterexample_block}\n\n请逐条回应。"
                )
            ),
        ],
        task_id=task_id,
        purpose="eval.defense",
    )
    defense_text = str(response.content)

    return {
        "stage_defense": defense_text,
        "stage_result": (
            f"{state.get('stage_result') or ''}\n\n## 对争议反例的回应\n\n{defense_text}"
        ),
        "eval_call_count": (state.get("eval_call_count") or 0) + 1,
    }


def react_node(state: AgentState, config: RunnableConfig) -> dict[str, Any]:
    """Execute the current stage with a dedicated react thread (or defend)."""
    if state.get("last_action") == Action.DEFEND_STAGE.value:
        return _defend_node(state, config)

    task_id = config["configurable"]["thread_id"]
    limits = config["configurable"].get("budget_limits")
    if limits is not None:
        _check_budget_and_cancel(task_id, limits)

    stage_index = state["current_stage_index"]
    agent = get_agent()
    # Each attempt gets its own thread so a retry starts from a fresh context
    # rather than resuming the previous attempt's full message history (which
    # would otherwise grow linearly across retries). The retry's continuity is
    # carried by the bounded handoff instead (feedback + prior conclusion + index).
    attempt = state.get("stage_attempts") or 1
    stage_thread = f"{task_id}-stage-{stage_index}-attempt-{attempt}"
    result_state = stream_agent(
        agent,
        {"messages": [HumanMessage(content=_build_stage_prompt(state))]},
        config={
            "configurable": {
                "thread_id": stage_thread,
                "task_id": task_id,
                "run_id": config["configurable"].get("run_id", ""),
                "stage_index": stage_index,
            }
        },
        task_id=task_id,
        limits=limits,
        on_node=node_step_publisher(
            task_id, config["configurable"].get("run_id", ""), stage_thread
        ),
    )

    messages_out = result_state.get("messages", [])
    artifacts = _harvest_stage_artifacts(messages_out)
    records, evidence_index = _build_evidence_records(artifacts["evidence"])
    evidence_repo.save_evidence(
        task_id=task_id,
        run_id=config["configurable"].get("run_id", ""),
        stage_index=stage_index,
        records=records,
    )
    return {
        "stage_result": artifacts["conclusion"],
        "stage_evidence": artifacts["evidence"],
        "stage_evidence_index": evidence_index,
        "stage_reasoning": artifacts["reasoning"],
    }
