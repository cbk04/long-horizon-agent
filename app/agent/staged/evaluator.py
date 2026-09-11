"""``evaluate`` node: three-phase pipeline (rules → logic → scoring).

Phase 1 (zero LLM): rule checks — malformed/empty output short-circuits
straight back to the executor. Phase 2 (same-tier model): a forward audit
scores how well the evidence supports each conclusion, while a search-enabled
reverse agent hunts fact-based counterexamples from the outside world; a
separate adjudicator grades counterexample strength. Phase 3 (weak model):
weighted 0/0.5/1 scoring against the plan's acceptance criteria.

Every LLM output maps to a deterministic routing rule — the pipeline emits a
verdict (COMPLETED / INCOMPLETE / DEFEND) but never routes. Emits
``stage.evaluated`` (and ``stage.defended`` on re-adjudication), persists one
``stage_evaluation`` row per pass.
"""

from __future__ import annotations

import json
import logging
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Literal

from langchain_core.messages import HumanMessage, SystemMessage
from langchain_core.runnables import RunnableConfig
from pydantic import BaseModel, Field

from app.agent.llm import call_structured, get_llm, get_scoring_llm
from app.agent.react import _check_budget_and_cancel, stream_agent
from app.agent.staged.common import AgentState, format_acceptance, publish
from app.config import get_settings
from app.evaluation.adjudication import (
    Counterexample,
    CounterexampleAssessment,
    aggregate_tier,
    counterexample_strength,
    render_counterexamples,
)
from app.evaluation.rules import run_rule_checks

logger = logging.getLogger(__name__)

# ── prompts ──────────────────────────────────────────────────────────────────

_FORWARD_PROMPT = """\
You are a conclusion-grounding auditor. You are given the stage objective, the
evidence the executor retrieved, and the executor's conclusions. Judge the
conclusion AS A WHOLE: is it logically related to and grounded in the evidence?

Do NOT grade every sentence against the evidence. Extended or inferential
conclusions are legitimate — a conclusion may go beyond the raw evidence as
long as it is still logically anchored in it. Judge the whole, not the parts.

- high: the conclusion is logically derived from or well supported by the
  evidence (reasonable extensions and inferences belong here);
- medium: broadly related to the evidence but partly thin or resting on a
  stretch — still overall anchored in the evidence;
- low: the conclusion is overall disconnected from the evidence, or its main
  assertions have no grounding at all (looks fabricated).

Only in the "low" case, list in `unsupported` the specific assertions that have
no evidence behind them — leave it empty otherwise, and never list ordinary
extended/inferential conclusions there.

Judge ONLY the evidence→conclusion relation: do not judge whether conclusions
are "true" from your own knowledge, and do not use outside information.
"""

_REVERSE_PROMPT = """\
你是对抗性事实核查员。你会收到一段研究结论。你的唯一任务:用 web_search / web_fetch
从外部世界找出与结论冲突、或削弱其证据权威性/时效性的事实反例。

要求:
1. 只输出有真实来源的反例:每条必须来自你实际搜索/抓取到的网页,给出 source_url、
   quote(来源中支撑反例的原文引文)和该事实的 date;
2. 严禁编造:找不到真实来源就不要输出该条;一个反例都找不到就输出空数组 [];
3. 每条反例的 claim 要引用结论中被挑战的具体断言原文;
4. 最多 5 条,优先强反例;
5. 最后一条消息只输出 JSON 数组,不要其他文字,格式:
[{"claim": "...", "counter_evidence": "...", "source_url": "...", "quote": "...", "date": "..."}]
"""

_ADJUDICATOR_PROMPT = """\
You are the counterexample-strength adjudicator. You are given: the conclusion
under review, a list of counterexamples (each with a source quote), the
executor's own evidence excerpts, optional task context, and — when present —
the executor's response to the counterexamples.

Grade each counterexample independently, in the order given:
1. valid: citation check — does the quote genuinely support the
   counter_evidence? Missing source, or a quote that does not say what the
   entry claims → valid=false (the entry is discarded and never counts).
2. timeliness: the counterexample fact's date against the task's time window
   (from the task context) — high / medium / low.
3. conflict: "direct" (the counterexample fact and the assertion cannot both
   be true) or "perspective" (a different caliber/viewpoint that can coexist
   with the assertion).
4. logic: the even-if-true test — assuming the counterexample is true, how
   substantively is the conclusion overturned? high (core claim falls) /
   medium (materially weakened) / low (peripheral detail only).
5. reason: one or two sentences justifying the grades.

Output one assessment per counterexample, in order. Do NOT output an overall
verdict — strength aggregation is done by system rules, not by you.
"""

_SCORING_PROMPT = """\
You are the stage quality scorer. Score the stage output against each
acceptance item on a discrete scale: 0 (not met) / 0.5 (partially met) /
1 (met).

Rules:
- Score each item independently; every score MUST cite where in the output the
  evidence sits (evidence_ref — a section heading or short quote).
- criteria_scores must match the acceptance list one-to-one, in order.
- Also extract key_findings: this stage's key conclusions that later stages
  must know (3-6 items).
- Judge strictly against the stated criteria; no external preferences.
"""


# ── structured-output schemas ────────────────────────────────────────────────

class ForwardAudit(BaseModel):
    """Forward audit result: one holistic logical-relevance judgment.

    Not per-assertion scoring — extended/inferential conclusions are legitimate
    as long as the conclusion as a whole stays anchored in the evidence.
    """

    logical_relevance: Literal["high", "medium", "low"] = Field(
        description=(
            "结论整体与证据的逻辑相关性:high=由证据合理推导/支撑(含合理延伸);"
            "medium=大体相关但部分薄弱或存在延伸;low=整体脱节或主要断言无凭据"
        )
    )
    reason: str = Field(description="整体判断理由,一两句")
    unsupported: list[str] = Field(
        default_factory=list,
        description="仅在 low 时列出:完全无证据支撑、疑似凭空得出的断言原文(正常延伸性结论不列)",
    )


class Adjudication(BaseModel):
    """Adjudicator's per-counterexample assessments, in the order given."""

    assessments: list[CounterexampleAssessment] = Field(default_factory=list)


class CriterionScore(BaseModel):
    """One acceptance item's discrete score with evidence citation."""

    dimension: str = Field(description="对应的验收维度")
    criteria: str = Field(description="对应的验收标准")
    score: float = Field(ge=0, le=1, description="离散三档:0 / 0.5 / 1")
    comment: str = Field(description="打分评语")
    evidence_ref: str = Field(default="", description="产出中支撑该评分的证据位置")


class StageScore(BaseModel):
    """Weighted scoring of a stage against its acceptance list."""

    criteria_scores: list[CriterionScore] = Field(default_factory=list)
    key_findings: list[str] = Field(default_factory=list, description="本阶段关键结论,供后续阶段交接")


# ── reverse agent (search-enabled, dedicated singleton) ──────────────────────

_reverse_agent: Any | None = None


def get_reverse_agent() -> Any:
    """Dedicated react agent for the reverse challenge (own thread per stage)."""
    global _reverse_agent
    if _reverse_agent is None:
        from langchain.agents import create_agent

        from app.agent.checkpointer import get_checkpointer
        from app.agent.tools.search import TOOLS

        _reverse_agent = create_agent(
            model=get_llm(temperature=0.0),
            tools=TOOLS,
            checkpointer=get_checkpointer(),
        )
    return _reverse_agent


# ── helpers ──────────────────────────────────────────────────────────────────

def _format_evidence(evidence: list[dict[str, Any]]) -> str:
    """Render harvested evidence, keeping its originating queries."""
    if not evidence:
        return "(无)"
    lines = []
    for i, item in enumerate(evidence, 1):
        query = item.get("query")
        header = f"[{i}] {item['tool']}(查询:{query})" if query else f"[{i}] {item['tool']}"
        lines.append(f"{header}\n{item['content']}")
    return "\n".join(lines)


def _snap_score(score: float) -> float:
    """Snap a free float to the discrete {0, 0.5, 1} scale deterministically."""
    return min((0.0, 0.5, 1.0), key=lambda v: abs(v - score))


def _parse_counterexamples(text: str) -> list[Counterexample]:
    """Parse the reverse agent's final JSON array; malformed output → empty."""
    if not text:
        return []
    start, end = text.find("["), text.rfind("]")
    if start == -1 or end == -1 or end <= start:
        logger.warning("reverse agent produced no parsable counterexample array")
        return []
    try:
        data = json.loads(text[start:end + 1])
    except json.JSONDecodeError:
        logger.warning("reverse agent counterexample JSON failed to parse")
        return []
    parsed: list[Counterexample] = []
    for item in data if isinstance(data, list) else []:
        try:
            parsed.append(Counterexample.model_validate(item))
        except Exception:
            logger.warning("discarding malformed counterexample entry: %r", item)
    return parsed


def _make_verdict(
    status: Literal["COMPLETED", "INCOMPLETE", "DEFEND"],
    feedback: list[str],
    key_findings: list[str],
    *,
    phase1: dict[str, Any] | None = None,
    phase2: dict[str, Any] | None = None,
    phase3: dict[str, Any] | None = None,
    dispute: bool = False,
    degraded: bool = False,
) -> dict[str, Any]:
    """Assemble the verdict dict consumed by the controller / events / DB."""
    return {
        "status": status,
        "gaps": list(feedback),
        "feedback": list(feedback),
        "key_findings": list(key_findings),
        "dispute": dispute,
        "eval_degraded": degraded,
        "phase1": phase1 or {},
        "phase2": phase2,
        "phase3": phase3,
    }


def _record(
    task_id: str,
    run_id: str,
    state: AgentState,
    verdict: dict[str, Any],
    config: RunnableConfig,
    defense_round: int = 0,
) -> None:
    """Publish the verdict and persist one stage_evaluation row."""
    from app.evaluation.repository import save_stage_evaluation

    stage_index = state["current_stage_index"]
    stage = state["plan"][stage_index]

    publish(task_id, "stage.evaluated", {
        "run_id": run_id,
        "stage_id": stage_index,
        "attempt": state.get("stage_attempts", 1),
        "defense_round": defense_round,
        **{
            k: verdict[k]
            for k in (
                "status", "feedback", "key_findings", "dispute",
                "eval_degraded", "phase1", "phase2", "phase3",
            )
        },
    })
    save_stage_evaluation(
        task_id=task_id,
        run_id=run_id,
        stage_index=stage_index,
        attempt=state.get("stage_attempts", 1),
        defense_round=defense_round,
        verdict=verdict,
        acceptance_snapshot=stage.get("acceptance") or [],
    )


# ── phase 2 lanes ────────────────────────────────────────────────────────────

def _run_forward_audit(
    stage: dict[str, Any], stage_result: str, evidence: list[dict[str, Any]], task_id: str
) -> ForwardAudit:
    """Score evidence→conclusion support per assertion (one structured call)."""
    audit, _ = call_structured(
        get_llm(temperature=0.0),
        ForwardAudit,
        [
            SystemMessage(content=_FORWARD_PROMPT),
            HumanMessage(
                content=(
                    f"阶段目标:{stage['objective']}\n\n"
                    f"【证据】\n{_format_evidence(evidence)}\n\n"
                    f"【结论·整体评估逻辑相关性】\n{stage_result}"
                )
            ),
        ],
        task_id=task_id,
        purpose="eval.forward_audit",
    )
    return audit


def _run_reverse_challenge(
    stage_result: str, task_id: str, stage_index: int, limits: Any
) -> tuple[list[dict[str, Any]], int]:
    """Hunt fact-based counterexamples with a dedicated search agent.

    Input is the conclusion ONLY — no evidence, no reasoning: its job is to
    attack from the outside world. Resilient by design: any failure in this
    lane degrades to "no counterexamples" rather than failing the stage.
    Returns (counterexamples as dicts, model-call count).
    """
    settings = get_settings()
    agent = get_reverse_agent()
    thread = f"{task_id}-stage-{stage_index}-reverse"
    counted = {"model": 0}

    def _on_node(node: str) -> None:
        if node == "model":
            counted["model"] += 1

    try:
        result_state = stream_agent(
            agent,
            {
                "messages": [
                    SystemMessage(content=_REVERSE_PROMPT),
                    HumanMessage(content=f"待核查结论:\n{stage_result}"),
                ]
            },
            config={
                "configurable": {"thread_id": thread},
                # Each tool call costs ~2 super-steps (model + tools); cap the loop.
                "recursion_limit": settings.reverse_max_tool_calls * 2 + 4,
            },
            task_id=task_id,
            limits=limits,
            on_node=_on_node,
        )
    except Exception:
        logger.exception("reverse challenge failed for stage %s; treating as no counterexamples", stage_index)
        return [], counted["model"]

    messages = result_state.get("messages", [])
    final = ""
    for msg in reversed(messages):
        content = getattr(msg, "content", "")
        if content and not getattr(msg, "tool_calls", None):
            final = str(content)
            break
    return [ce.model_dump() for ce in _parse_counterexamples(final)], counted["model"]


def _run_adjudication(
    stage_result: str,
    counterexamples: list[dict[str, Any]],
    evidence: list[dict[str, Any]],
    state: AgentState,
    task_id: str,
    defense: str | None = None,
) -> Adjudication:
    """Grade counterexample strength; independent of the reverse agent."""
    payload = (
        f"结论:\n{stage_result}\n\n"
        f"反例(按此顺序逐条评估):\n{json.dumps(counterexamples, ensure_ascii=False, indent=2)}\n\n"
        f"任务上下文(含时间窗信息):{state.get('goal', '')}\n\n"
        f"执行者的证据摘录:\n{_format_evidence(evidence)}"
    )
    if defense:
        payload += f"\n\n执行者对反例的回应(裁决时纳入考量):\n{defense}"
    adjudication, _ = call_structured(
        get_llm(temperature=0.0),
        Adjudication,
        [
            SystemMessage(content=_ADJUDICATOR_PROMPT),
            HumanMessage(content=payload),
        ],
        task_id=task_id,
        purpose="eval.adjudication",
    )
    return adjudication


# ── phase 3 ──────────────────────────────────────────────────────────────────

def _run_scoring(
    stage: dict[str, Any], stage_result: str, evidence: list[dict[str, Any]], task_id: str
) -> StageScore:
    """Weighted discrete scoring on the weak-model lane (one structured call)."""
    score, _ = call_structured(
        get_scoring_llm(temperature=0.0),
        StageScore,
        [
            SystemMessage(content=_SCORING_PROMPT),
            HumanMessage(
                content=(
                    f"阶段目标:{stage['objective']}\n"
                    f"验收清单:\n{format_acceptance(stage['acceptance'])}\n\n"
                    f"【阶段产出·评审对象】\n{stage_result}\n\n"
                    f"【证据·供引用证据位置】\n{_format_evidence(evidence)}"
                )
            ),
        ],
        task_id=task_id,
        purpose="eval.scoring",
    )
    return score


def _score_verdict(
    stage: dict[str, Any],
    score: StageScore,
    *,
    degraded: bool,
    dispute: bool = False,
    feedback_extra: list[str] | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Turn a StageScore into the pass/fail verdict + phase3 record.

    Deterministic pass rule: weighted sum ≥ threshold AND no criterion whose
    weight exceeds ``heavy_criteria_weight`` scored 0 AND every item scored.
    """
    settings = get_settings()
    items = stage["acceptance"]
    scored = score.criteria_scores
    feedback = list(feedback_extra or [])
    rows: list[dict[str, Any]] = []
    weighted = 0.0

    for i, item in enumerate(items):
        weight = float(item.get("weight") or 0.0)
        if i < len(scored):
            s = _snap_score(scored[i].score)
            weighted += s * weight
            if s == 0:
                feedback.append(
                    f"验收维度「{item['dimension']}」未达成(权重 {weight:.2f}):{scored[i].comment}"
                )
            elif s < 1:
                feedback.append(
                    f"验收维度「{item['dimension']}」部分达成: {scored[i].comment}"
                )
            rows.append({
                "dimension": item["dimension"],
                "criteria": item["criteria"],
                "weight": weight,
                "score": s,
                "comment": scored[i].comment,
                "evidence_ref": scored[i].evidence_ref,
            })
        else:
            feedback.append(f"评分缺失:验收维度「{item['dimension']}」未被打分,按 0 分计")
            rows.append({
                "dimension": item["dimension"],
                "criteria": item["criteria"],
                "weight": weight,
                "score": 0.0,
                "comment": "scoring output incomplete",
                "evidence_ref": "",
            })

    heavy_zero = any(
        row["score"] == 0 and row["weight"] > settings.heavy_criteria_weight for row in rows
    )
    passed = (
        len(scored) >= len(items)
        and weighted >= settings.score_pass_threshold
        and not heavy_zero
    )
    phase3 = {"criteria_scores": rows, "weighted_score": round(weighted, 4)}
    status: Literal["COMPLETED", "INCOMPLETE"] = "COMPLETED" if passed else "INCOMPLETE"
    return (
        _make_verdict(status, feedback, score.key_findings, phase3=phase3, dispute=dispute, degraded=degraded),
        phase3,
    )


# ── node entry ───────────────────────────────────────────────────────────────

def evaluate_node(state: AgentState, config: RunnableConfig) -> dict[str, Any]:
    """Run the three-phase pipeline (or defend re-adjudication) for the stage."""
    task_id = config["configurable"]["thread_id"]
    limits = config["configurable"].get("budget_limits")
    if limits is not None:
        _check_budget_and_cancel(task_id, limits)

    if state.get("stage_defense"):
        return _evaluate_after_defense(state, config)
    return _evaluate_full(state, config)


def _evaluate_full(state: AgentState, config: RunnableConfig) -> dict[str, Any]:
    """Full pipeline: rules → (forward ∥ reverse → adjudicate) → scoring."""
    task_id = config["configurable"]["thread_id"]
    run_id = config["configurable"].get("run_id", "")
    limits = config["configurable"].get("budget_limits")
    settings = get_settings()
    stage = state["plan"][state["current_stage_index"]]
    stage_result = state.get("stage_result") or ""
    evidence = state.get("stage_evidence") or []
    call_count = state.get("eval_call_count", 0) or 0
    degraded = call_count >= settings.max_eval_llm_calls_per_task

    def _finish(verdict: dict[str, Any], counterexamples: list[dict[str, Any]] | None) -> dict[str, Any]:
        _record(task_id, run_id, state, verdict, config)
        return {
            "evaluation": verdict,
            "eval_call_count": call_count,
            "eval_degraded": verdict.get("eval_degraded", False),
            "stage_counterexamples": counterexamples,
        }

    # Phase 1 — rules; violations short-circuit everything else.
    violations = run_rule_checks(stage_result, stage)
    if violations:
        return _finish(
            _make_verdict("INCOMPLETE", violations, [], phase1={"violations": violations}, degraded=degraded),
            None,
        )

    if degraded:
        # Fuse blown: skip the logic phase entirely, keep rules + scoring.
        score = _run_scoring(stage, stage_result, evidence, task_id)
        call_count += 1
        verdict, _ = _score_verdict(stage, score, degraded=True)
        return _finish(verdict, None)

    # Phase 2 — forward audit and reverse challenge in parallel.
    with ThreadPoolExecutor(max_workers=2) as pool:
        forward_future = pool.submit(_run_forward_audit, stage, stage_result, evidence, task_id)
        reverse_future = pool.submit(
            _run_reverse_challenge, stage_result, task_id, state["current_stage_index"], limits
        )
        forward = forward_future.result()
        counterexamples, reverse_calls = reverse_future.result()
    call_count += 1 + reverse_calls  # forward is one call; reverse counted per model node

    assessments: list[CounterexampleAssessment] = []
    if counterexamples:
        adjudication = _run_adjudication(stage_result, counterexamples, evidence, state, task_id)
        assessments = adjudication.assessments
        call_count += 1
    tier = aggregate_tier([counterexample_strength(a) for a in assessments])

    forward_lines: list[str] = []
    if forward.logical_relevance == "low":
        forward_lines = [f"结论整体与证据逻辑脱节:{forward.reason}"]
        forward_lines += [f"无凭据断言:「{u}」" for u in forward.unsupported]
    counterexample_lines = render_counterexamples(counterexamples, assessments)
    phase2 = {
        "forward": {
            "logical_relevance": forward.logical_relevance,
            "reason": forward.reason,
            "unsupported": forward.unsupported,
        },
        "reverse": {
            "counterexamples": counterexamples,
            "tier": tier,
            "assessments": [a.model_dump() for a in assessments],
        },
    }

    # Priority: RETRY beats DEFEND beats pass. A conclusion that is overall
    # disconnected from the evidence, and high-strength counterexamples, merge
    # into one feedback so a single redo covers both.
    if forward_lines or tier == "high":
        return _finish(
            _make_verdict(
                "INCOMPLETE", forward_lines + counterexample_lines, [], phase2=phase2, degraded=degraded
            ),
            None,
        )

    if tier == "medium":
        # Contested: one defense round — counterexamples kept for re-adjudication.
        return _finish(
            _make_verdict("DEFEND", counterexample_lines, [], phase2=phase2, dispute=True, degraded=degraded),
            counterexamples,
        )

    # Phase 3 — nothing challenges the stage; score it.
    score = _run_scoring(stage, stage_result, evidence, task_id)
    call_count += 1
    verdict, _ = _score_verdict(stage, score, degraded=degraded)
    return _finish(verdict, None)


def _evaluate_after_defense(state: AgentState, config: RunnableConfig) -> dict[str, Any]:
    """Re-adjudicate after a defense round: only the strength lane re-runs.

    The defense does not change the evidence, so the forward audit gains
    nothing from a rerun. If the stage survives (medium/low), phase 3 scoring
    still runs — every passed stage must leave with a weighted score.
    """
    task_id = config["configurable"]["thread_id"]
    run_id = config["configurable"].get("run_id", "")
    stage = state["plan"][state["current_stage_index"]]
    stage_result = state.get("stage_result") or ""
    evidence = state.get("stage_evidence") or []
    counterexamples = state.get("stage_counterexamples") or []
    defense = state.get("stage_defense") or ""
    call_count = state.get("eval_call_count", 0) or 0

    adjudication = _run_adjudication(
        stage_result, counterexamples, evidence, state, task_id, defense=defense
    )
    call_count += 1
    assessments = adjudication.assessments
    tier = aggregate_tier([counterexample_strength(a) for a in assessments])
    counterexample_lines = render_counterexamples(counterexamples, assessments)
    phase2 = {
        "reverse": {
            "counterexamples": counterexamples,
            "tier": tier,
            "defended": True,
            "assessments": [a.model_dump() for a in assessments],
        }
    }

    publish(task_id, "stage.defended", {
        "run_id": run_id,
        "stage_id": state["current_stage_index"],
        "defense_round": state.get("stage_defense_rounds", 0),
        "tier": tier,
        "counterexamples": counterexample_lines,
    })

    if tier == "high":
        # The response backfired: the counterexample survived as a strong one.
        verdict = _make_verdict("INCOMPLETE", counterexample_lines, [], phase2=phase2)
        _record(task_id, run_id, state, verdict, config, defense_round=state.get("stage_defense_rounds", 0))
        return {
            "evaluation": verdict,
            "eval_call_count": call_count,
            "stage_defense": None,
            "stage_counterexamples": None,
        }

    score = _run_scoring(stage, stage_result, evidence, task_id)
    call_count += 1
    verdict, _ = _score_verdict(stage, score, degraded=False, dispute=(tier == "medium"))
    _record(task_id, run_id, state, verdict, config, defense_round=state.get("stage_defense_rounds", 0))
    return {
        "evaluation": verdict,
        "eval_call_count": call_count,
        "eval_degraded": False,
        "stage_defense": None,
        "stage_counterexamples": None,
    }
