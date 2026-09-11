"""Whole-graph tests for the staged plan-execute graph with fake LLMs.

The compiled graph is driven from its entry point with the planner/evaluator
models and the react agents replaced by scripted fakes. Event publishing,
budget accounting and evaluation-row persistence are stubbed out — no MySQL,
Redis or LLM involved.

Covers: research → plan generation (weights/is_final), plan validation retry,
stage ordering with the three-phase evaluator, rule short-circuit, forward
logical-relevance retry, high-strength counterexample retry, medium →
defend → pass-with-dispute, retry exhaustion → failure, the evaluator fuse
(degraded mode), and the approval interrupt/resume behaviour.
"""

from __future__ import annotations

import json
import os
import sys
import uuid

import pytest
from langchain_core.messages import AIMessage, ToolMessage
from langgraph.types import Command
from pydantic import ValidationError

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import app.agent.controller as controller_mod
import app.agent.evidence_repo as evidence_repo
import app.agent.stage_output_repo as stage_output_repo
import app.evaluation.repository as eval_repo
import app.harness.budget as budget_mod
import app.harness.event_bus as event_bus_mod
from app.agent.controller import ControllerLimits, StageFailedError
from app.agent.staged import common as staged_common
from app.agent.staged import evaluator as staged_evaluator
from app.agent.staged import executor as staged_executor
from app.agent.staged import graph as staged_graph
from app.agent.staged import planner as staged_planner
from app.agent.staged.evaluator import (
    Adjudication,
    CriterionScore,
    ForwardAudit,
    StageScore,
)
from app.agent.staged.executor import _build_evidence_records, _harvest_stage_artifacts
from app.agent.staged.planner import _PLAN_ATTEMPTS, AcceptanceItem, Plan, Stage
from app.agent.tools.search import (
    _fact_score,
    _paragraph_outline,
    _split_sentences,
    build_fetch_digest,
    parse_fetch_digest,
)
from app.evaluation.adjudication import CounterexampleAssessment

_CONCLUSION_BODY = "这是充分长的阶段分析正文,覆盖多个子话题并给出有据支撑的论断。" * 10


class FakeLLM:
    """All model calls land on ``.invoke``; the schema instruction that
    ``call_structured`` prepends (a "JSON Schema" system message) distinguishes
    structured calls from plain ones."""

    def __init__(self, queue: list, plain: list[str] | None = None):
        self._queue = queue                      # structured pydantic objects
        self._plain = list(plain or [])
        self.structured_invocations: list[list] = []
        self.plan_invocations: list[list] = []   # planner lane only
        self.plain_invocations: list[list] = []

    def invoke(self, messages):
        first = str(messages[0].content) if messages else ""
        if "JSON Schema" in first:
            obj = self._queue.pop(0)
            self.structured_invocations.append(list(messages))
            if '"title": "Plan"' in first:
                self.plan_invocations.append(list(messages))
            return AIMessage(content=obj.model_dump_json())
        self.plain_invocations.append(list(messages))
        return AIMessage(content=self._plain.pop(0))


class FakeAgent:
    """Stands in for the react subgraph; records prompts and thread ids.

    Conclusions are rule-compliant by default (long enough, one heading);
    ``short=True`` produces output that fails phase-1 rule checks.
    """

    def __init__(self, short: bool = False):
        self.prompts: list[str] = []
        self.threads: list[str] = []
        self._n = 0
        self._short = short

    def invoke(self, state, config):
        self.prompts.append(state["messages"][-1].content)
        self.threads.append(config["configurable"]["thread_id"])
        self._n += 1
        conclusion = (
            "太短"
            if self._short
            # Two headings: satisfies the final-report stage's ≥2-heading rule
            # as well as the ordinary ≥1 rule.
            else f"# 阶段{self._n}报告\n\n## 关键发现\n\n{_CONCLUSION_BODY}"
        )
        return {"messages": [
            AIMessage(
                content=f"先查第{self._n}个问题",
                tool_calls=[{"name": "web_search", "args": {"query": f"问题{self._n}"}, "id": f"call-{self._n}"}],
            ),
            ToolMessage(content=f"第{self._n}次执行的证据", name="web_search", tool_call_id=f"call-{self._n}"),
            AIMessage(content=conclusion),
        ]}

    def stream(self, state, config, stream_mode=None):
        """Same run as invoke(), yielded in the real graph's list-mode shape."""
        final = self.invoke(state, config)
        yield ("updates", {"model": {"messages": final["messages"]}})
        yield ("values", final)


class FakeReverseAgent:
    """Stands in for the search-enabled counterexample hunter."""

    def __init__(self, responses: list[str]):
        self.responses = list(responses)
        self.threads: list[str] = []

    def stream(self, state, config, stream_mode=None):
        self.threads.append(config["configurable"]["thread_id"])
        text = self.responses.pop(0) if self.responses else "[]"
        final = {"messages": [AIMessage(content=text)]}
        yield ("updates", {"model": {"messages": []}})
        yield ("updates", {"tools": {"messages": []}})
        yield ("values", final)


def _stage(
    i: int,
    n: int = 3,
    weights: list[float] | None = None,
    is_final: bool = False,
    depends_on: list[int] | None = None,
) -> Stage:
    weights = weights or [0.4, 0.3, 0.3]
    return Stage(
        objective=f"阶段{i}目标",
        scope_excludes=[f"非阶段{i}的工作"],
        acceptance=[
            AcceptanceItem(dimension=f"维度{i}-{j + 1}", criteria=f"标准{i}-{j + 1}", weight=weights[j])
            for j in range(n)
        ],
        is_final=is_final,
        depends_on=depends_on or [],
    )


def _default_plan(num_stages: int = 2) -> Plan:
    return Plan(stages=[
        _stage(i, is_final=(i == num_stages - 1)) for i in range(num_stages)
    ])


def _invalid_plan() -> Plan:
    """A plan that fails Plan's semantic validator (built past validation)."""
    bad_stage = Stage.model_construct(objective="", scope_excludes=[], acceptance=[])
    return Plan.model_construct(stages=[bad_stage])


def _forward(relevance: str = "high") -> ForwardAudit:
    return ForwardAudit(logical_relevance=relevance, reason="整体由证据支撑")


def _score(scores: tuple[float, ...] = (1.0, 1.0, 0.5), stage_index: int = 0) -> StageScore:
    return StageScore(
        criteria_scores=[
            CriterionScore(
                dimension=f"维度{stage_index}-{i + 1}",
                criteria=f"标准{stage_index}-{i + 1}",
                score=s,
                comment="ok",
                evidence_ref="第1节",
            )
            for i, s in enumerate(scores)
        ],
        key_findings=[f"阶段{stage_index + 1}关键结论"],
    )


def _assess(**overrides) -> Adjudication:
    """Adjudicator output for the single scripted counterexample."""
    kwargs = dict(claim="核心断言", valid=True, timeliness="high", conflict="direct", logic="high", reason="直接对立")
    kwargs.update(overrides)
    return Adjudication(assessments=[CounterexampleAssessment(**kwargs)])


_CE = json.dumps(
    [{"claim": "核心断言", "counter_evidence": "反例事实", "source_url": "https://x", "quote": "原文引文", "date": "2026-01"}],
    ensure_ascii=False,
)


@pytest.fixture()
def published(monkeypatch):
    events: list[tuple[str, dict]] = []
    recorder = lambda task_id, type_, payload: events.append((type_, payload))
    # publish is imported by name into each node module — patch every site.
    for module in (staged_planner, staged_evaluator, staged_graph, staged_common):
        monkeypatch.setattr(module, "publish", recorder)
    return events


@pytest.fixture()
def external(monkeypatch):
    """Stub Redis budget accounting and evaluation-row persistence."""
    # call_llm/call_structured emit through event_bus and budget by module
    # attribute — silence both so no Redis/MySQL is ever touched.
    monkeypatch.setattr(event_bus_mod, "publish", lambda *a, **k: None)
    monkeypatch.setattr(budget_mod, "add_input_tokens", lambda *a, **k: None)
    monkeypatch.setattr(budget_mod, "add_output_tokens", lambda *a, **k: None)
    monkeypatch.setattr(budget_mod, "add_elapsed_ms", lambda *a, **k: None)
    saved_rows: list[dict] = []
    monkeypatch.setattr(eval_repo, "save_stage_evaluation", lambda **kwargs: saved_rows.append(kwargs))
    monkeypatch.setattr(evidence_repo, "save_evidence", lambda **kwargs: None)
    monkeypatch.setattr(stage_output_repo, "save_stage_output", lambda **kwargs: None)
    return saved_rows


def _install(
    monkeypatch,
    structured: list,
    reverse_responses: list[str] | None = None,
    num_stages: int = 2,
    plans: list[Plan] | None = None,
    plain: list[str] | None = None,
    short_executor: bool = False,
):
    plans_out = [_default_plan(num_stages)] if plans is None else list(plans)
    fake_llm = FakeLLM(plans_out + list(structured), plain=plain)
    fake_agent = FakeAgent(short=short_executor)
    fake_reverse = FakeReverseAgent(reverse_responses or [])
    # Every LLM lane (planner, forward/adjudication, weak-model scoring, and
    # the defend one-shot) resolves to the same scripted fake.
    fake_llm_getter = lambda **kwargs: fake_llm  # noqa: E731
    monkeypatch.setattr(staged_planner, "get_llm", fake_llm_getter)
    monkeypatch.setattr(staged_evaluator, "get_llm", fake_llm_getter)
    monkeypatch.setattr(staged_evaluator, "get_scoring_llm", fake_llm_getter)
    monkeypatch.setattr(staged_executor, "get_llm", fake_llm_getter)
    monkeypatch.setattr(staged_planner, "get_agent", lambda: fake_agent)
    monkeypatch.setattr(staged_executor, "get_agent", lambda: fake_agent)
    monkeypatch.setattr(staged_evaluator, "get_reverse_agent", lambda: fake_reverse)
    monkeypatch.setattr(staged_planner, "_check_budget_and_cancel", lambda *a, **k: None)
    monkeypatch.setattr(staged_executor, "_check_budget_and_cancel", lambda *a, **k: None)
    monkeypatch.setattr(staged_evaluator, "_check_budget_and_cancel", lambda *a, **k: None)
    # Pin the retry/defense budgets the graph's controller reads, so these
    # integration tests don't drift with the settings default.
    monkeypatch.setattr(
        controller_mod,
        "default_limits",
        lambda: ControllerLimits(max_stage_retries=2, max_defense_rounds=1),
    )
    return fake_llm, fake_agent, fake_reverse


def _task_id() -> str:
    return f"test-{uuid.uuid4().hex[:8]}"


def _resume_invoke(graph, task_id: str, extra_state: dict | None = None):
    """Run the graph past the approval gate and return the final state."""
    config = {"configurable": {"thread_id": task_id, "run_id": "run-test"}}
    first = graph.invoke(
        {"messages": [{"role": "user", "content": "研究目标"}], **(extra_state or {})},
        config=config,
    )
    # LangGraph >= 1.x: interrupts surface as the __interrupt__ state key.
    assert first.get("__interrupt__"), "expected the graph to pause at the approval gate"
    return graph.invoke(Command(resume={"approved": True}), config=config)


# ── happy path ───────────────────────────────────────────────────────────────

def test_two_stages_run_in_order_with_scores(monkeypatch, published, external):
    fake_llm, agent, reverse = _install(
        monkeypatch,
        structured=[_forward(), _score(stage_index=0), _forward(), _score(stage_index=1)],
        reverse_responses=["[]", "[]"],
    )
    graph = staged_graph._build_graph()
    task_id = _task_id()

    final_state = _resume_invoke(graph, task_id)

    # Research first, then each stage on its own thread with a reverse lane.
    assert agent.threads == [
        f"{task_id}-research",
        f"{task_id}-stage-0-attempt-1",
        f"{task_id}-stage-1-attempt-1",
    ]
    assert reverse.threads == [
        f"{task_id}-stage-0-reverse",
        f"{task_id}-stage-1-reverse",
    ]

    assert "阶段3报告" in final_state["final_result"]
    assert final_state["last_action"] == "complete"
    assert [r["stage_index"] for r in final_state["stage_results"]] == [0, 1]
    # Weighted score archived per stage; no dispute on the happy path.
    assert final_state["stage_results"][0]["weighted_score"] == 0.85
    assert final_state["stage_results"][0]["dispute"] is False
    # Budget counter: forward 1 + reverse 1 + scoring 1 per stage.
    assert final_state["eval_call_count"] == 6

    evaluated = [e for e in published if e[0] == "stage.evaluated"]
    assert len(evaluated) == 2
    assert evaluated[0][1]["status"] == "COMPLETED"
    assert evaluated[0][1]["phase3"]["weighted_score"] == 0.85
    assert [e[0] for e in published].count("stage.completed") == 2

    # The research notes (the research agent's conclusion) reached the planner.
    planner_invocation = fake_llm.structured_invocations[0]
    assert "阶段1报告" in planner_invocation[-1].content


def test_dependency_aware_handoff_inlines_full_conclusion(monkeypatch, published, external):
    """Stage 1 depends on stage 0 → its prompt carries stage 0's FULL conclusion
    instead of the compressed key_findings."""
    _, agent, _ = _install(
        monkeypatch,
        structured=[_forward(), _score(stage_index=0), _forward(), _score(stage_index=1)],
        reverse_responses=["[]", "[]"],
        plans=[Plan(stages=[_stage(0), _stage(1, depends_on=[0], is_final=True)])],
    )
    graph = staged_graph._build_graph()
    task_id = _task_id()

    final_state = _resume_invoke(graph, task_id)

    # prompts = [research, stage-0, stage-1]; stage 1 inlines stage 0's conclusion.
    stage1_prompt = agent.prompts[2]
    assert "完整结论" in stage1_prompt
    assert "阶段2报告" in stage1_prompt  # stage 0's harvested conclusion
    # The archived stage_results entry carries the full conclusion + evidence index.
    assert "阶段2报告" in final_state["stage_results"][0]["conclusion"]
    idx = final_state["stage_results"][0]["evidence_index"]
    assert len(idx) == 1
    assert idx[0]["url"] == ""
    assert idx[0]["summary"] == "第2次执行的证据"
    assert final_state["last_action"] == "complete"


# ── phase 1 short-circuit ────────────────────────────────────────────────────

def test_rule_violation_short_circuits_before_any_llm_evaluation(monkeypatch, published, external):
    fake_llm, agent, reverse = _install(monkeypatch, structured=[], short_executor=True)
    graph = staged_graph._build_graph()
    task_id = _task_id()

    with pytest.raises(StageFailedError):
        _resume_invoke(graph, task_id)

    # No evaluator LLM call ever happened: rules rejected the output directly.
    assert len(fake_llm.structured_invocations) == 1  # planner only
    assert reverse.threads == []
    assert agent.threads == [
        f"{task_id}-research",
        f"{task_id}-stage-0-attempt-1",
        f"{task_id}-stage-0-attempt-2",
    ]

    evaluated = [e for e in published if e[0] == "stage.evaluated"]
    assert evaluated[0][1]["phase1"]["violations"]
    assert any("过短" in line for line in evaluated[0][1]["feedback"])
    # Rows persisted with RETRY status.
    assert all(row["verdict"]["status"] == "INCOMPLETE" for row in external)


# ── phase 2: forward insufficient-support retry ──────────────────────────────

def test_insufficient_support_retries_with_feedback(monkeypatch, published, external):
    _, agent, reverse = _install(
        monkeypatch,
        structured=[_forward("low"), _forward(), _score(stage_index=0), _forward(), _score(stage_index=1)],
        reverse_responses=["[]", "[]", "[]"],
    )
    graph = staged_graph._build_graph()
    task_id = _task_id()

    final_state = _resume_invoke(graph, task_id)

    assert agent.threads == [
        f"{task_id}-research",
        f"{task_id}-stage-0-attempt-1",
        f"{task_id}-stage-0-attempt-2",
        f"{task_id}-stage-1-attempt-1",
    ]
    # The retry prompt carries the overall-disconnect feedback.
    assert "逻辑脱节" in agent.prompts[2]
    # The retry also hands back the rejected conclusion + prior evidence index
    # so the fresh thread doesn't redo the previous attempt's work.
    assert "上一轮被驳回的结论" in agent.prompts[2]
    assert "阶段2报告" in agent.prompts[2]  # previous attempt's harvested conclusion
    assert "第2次执行的证据" in agent.prompts[2]  # previous attempt's evidence summary
    assert "get_evidence" in agent.prompts[2]
    # ...and the first attempt's prompt does NOT (fresh context, no handback).
    assert "上一轮被驳回的结论" not in agent.prompts[1]
    assert final_state["last_action"] == "complete"


# ── phase 2: high-strength counterexample retry ─────────────────────────────

def test_high_counterexample_retries_with_evidence(monkeypatch, published, external):
    _, agent, reverse = _install(
        monkeypatch,
        structured=[
            _forward(),
            _assess(),  # direct + timely + logical → high
            _forward(),
            _assess(valid=False),  # citation check fails → discarded → low
            _score(stage_index=0),
            _forward(),
            _score(stage_index=1),
        ],
        reverse_responses=[_CE, _CE, "[]"],
    )
    graph = staged_graph._build_graph()
    task_id = _task_id()

    final_state = _resume_invoke(graph, task_id)

    assert agent.threads == [
        f"{task_id}-research",
        f"{task_id}-stage-0-attempt-1",
        f"{task_id}-stage-0-attempt-2",
        f"{task_id}-stage-1-attempt-1",
    ]
    # The retry prompt carries the rendered counterexample.
    assert "反例" in agent.prompts[2]
    assert final_state["last_action"] == "complete"


# ── phase 2: medium → defend → pass with dispute ────────────────────────────

def test_medium_counterexample_defends_then_passes_with_dispute(monkeypatch, published, external):
    fake_llm, agent, reverse = _install(
        monkeypatch,
        structured=[
            _forward(),
            _assess(conflict="perspective", reason="口径不同"),  # → medium → DEFEND
            _assess(conflict="perspective", reason="仍为视角差异"),  # re-adjudication → medium
            _score(stage_index=0),
            _forward(),
            _score(stage_index=1),
        ],
        reverse_responses=[_CE, "[]"],
        plain=["对反例的逐条回应:此点应限定适用范围。"],
    )
    graph = staged_graph._build_graph()
    task_id = _task_id()

    final_state = _resume_invoke(graph, task_id)

    # The stage was NOT redone: defend is a lightweight append.
    assert agent.threads == [
        f"{task_id}-research",
        f"{task_id}-stage-0-attempt-1",
        f"{task_id}-stage-1-attempt-1",
    ]
    assert reverse.threads == [f"{task_id}-stage-0-reverse", f"{task_id}-stage-1-reverse"]

    # The defend pass ran as a plain LLM call with the counterexamples.
    assert len(fake_llm.plain_invocations) == 1
    assert "反例" in fake_llm.plain_invocations[0][0].content

    defended = [e for e in published if e[0] == "stage.defended"]
    assert len(defended) == 1
    assert defended[0][1]["tier"] == "medium"
    assert defended[0][1]["defense_round"] == 1

    # Dispute settled as "contested but not fatal" — passed and flagged.
    assert final_state["last_action"] == "complete"
    assert final_state["stage_results"][0]["dispute"] is True
    completed = [e for e in published if e[0] == "stage.completed"]
    assert completed[0][1]["dispute"] is True


# ── retry exhaustion ─────────────────────────────────────────────────────────

def test_retry_exhaustion_fails_the_task(monkeypatch, published, external):
    _, agent, _ = _install(
        monkeypatch,
        structured=[_forward("low"), _forward("low")],
        reverse_responses=["[]", "[]"],
    )
    graph = staged_graph._build_graph()
    task_id = _task_id()

    with pytest.raises(StageFailedError) as exc_info:
        _resume_invoke(graph, task_id)

    assert agent.threads == [
        f"{task_id}-research",
        f"{task_id}-stage-0-attempt-1",
        f"{task_id}-stage-0-attempt-2",
    ]
    assert exc_info.value.stage_index == 0
    assert "逻辑脱节" in str(exc_info.value)


# ── evaluator fuse (degraded mode) ──────────────────────────────────────────

def test_eval_fuse_skips_the_logic_phase(monkeypatch, published, external):
    _, agent, reverse = _install(
        monkeypatch,
        structured=[_score(stage_index=0), _score(stage_index=1)],
    )
    graph = staged_graph._build_graph()
    task_id = _task_id()

    final_state = _resume_invoke(graph, task_id, extra_state={"eval_call_count": 10**6})

    # Phase 2 never ran; each stage went rules → scoring only.
    assert reverse.threads == []
    assert agent.threads == [
        f"{task_id}-research",
        f"{task_id}-stage-0-attempt-1",
        f"{task_id}-stage-1-attempt-1",
    ]
    evaluated = [e for e in published if e[0] == "stage.evaluated"]
    assert all(e[1]["eval_degraded"] for e in evaluated)
    assert all(e[1]["phase2"] is None for e in evaluated)
    assert final_state["last_action"] == "complete"
    # The stubbed repository sees raw verdicts: COMPLETED but flagged degraded
    # (the real repository maps that pair to a DEGRADED_PASS row status).
    assert [row["verdict"]["status"] for row in external] == ["COMPLETED", "COMPLETED"]
    assert all(row["verdict"]["eval_degraded"] for row in external)


# ── approval gate & plan validation ─────────────────────────────────────────

def test_plan_interrupts_for_approval_before_any_execution(monkeypatch, published, external):
    _install(monkeypatch, [])
    graph = staged_graph._build_graph()
    task_id = _task_id()

    result = graph.invoke(
        {"messages": [{"role": "user", "content": "研究目标"}]},
        config={"configurable": {"thread_id": task_id}},
    )
    assert result.get("__interrupt__")

    # Nothing executes before approval: only the plan event was emitted.
    assert [e[0] for e in published] == ["plan.generated"]
    stages = published[0][1]["stages"]
    assert [s["objective"] for s in stages] == ["阶段0目标", "阶段1目标"]
    # Acceptance items carry weights; only the last stage is final.
    assert stages[0]["acceptance"][0] == {"dimension": "维度0-1", "criteria": "标准0-1", "weight": 0.4}
    assert [s["is_final"] for s in stages] == [False, True]


def test_invalid_plan_retries_with_error_feedback_then_succeeds(monkeypatch, published, external):
    structured = [_forward(), _score(stage_index=0), _forward(), _score(stage_index=1)]
    fake_llm, _agent, _reverse = _install(
        monkeypatch,
        structured=structured,
        reverse_responses=["[]", "[]"],
        plans=[_invalid_plan(), _default_plan(2)],
    )
    graph = staged_graph._build_graph()
    task_id = _task_id()

    final_state = _resume_invoke(graph, task_id)

    # The planner was invoked twice; the retry carries the validation errors.
    assert len(fake_llm.plan_invocations) == 2
    retry_message = fake_llm.plan_invocations[1][-1].content
    assert "未通过校验" in retry_message
    assert "scope_excludes 不能为空" in retry_message
    assert "3-6 条" in retry_message

    stages = [e for e in published if e[0] == "plan.generated"][0][1]["stages"]
    assert [s["objective"] for s in stages] == ["阶段0目标", "阶段1目标"]
    assert final_state["last_action"] == "complete"


def test_invalid_plan_exhaustion_fails_before_any_execution(monkeypatch, published, external):
    fake_llm, _agent, _reverse = _install(
        monkeypatch, structured=[], plans=[_invalid_plan() for _ in range(_PLAN_ATTEMPTS)]
    )
    graph = staged_graph._build_graph()
    task_id = _task_id()

    with pytest.raises(ValueError, match="valid plan"):
        _resume_invoke(graph, task_id)

    assert len(fake_llm.structured_invocations) == _PLAN_ATTEMPTS
    assert published == []


# ── plan semantic validators (pure) ─────────────────────────────────────────

def test_plan_requires_exactly_one_final_stage_in_last_position():
    with pytest.raises(ValidationError, match="恰好一个"):
        Plan(stages=[_stage(0), _stage(1)])
    with pytest.raises(ValidationError, match="最后"):
        Plan(stages=[_stage(0, is_final=True), _stage(1)])
    with pytest.raises(ValidationError, match="恰好一个"):
        Plan(stages=[_stage(0, is_final=True), _stage(1, is_final=True)])
    # The valid shape.
    Plan(stages=[_stage(0), _stage(1, is_final=True)])


def test_plan_acceptance_weights_must_sum_to_one():
    with pytest.raises(ValidationError, match="权重之和"):
        Plan(stages=[_stage(0, weights=[0.5, 0.5, 0.5]), _stage(1, is_final=True)])


def test_plan_acceptance_count_bounds():
    with pytest.raises(ValidationError, match="3-6 条"):
        Plan(stages=[_stage(0, n=2, weights=[0.6, 0.4]), _stage(1, is_final=True)])


def test_plan_rejects_out_of_range_dependency():
    with pytest.raises(ValidationError, match="越界"):
        Plan(stages=[_stage(0), _stage(1, depends_on=[5], is_final=True)])


def test_plan_rejects_dependency_on_later_or_self():
    # Stage 0 depending on stage 1 (a later stage).
    with pytest.raises(ValidationError, match="更早"):
        Plan(stages=[_stage(0, depends_on=[1]), _stage(1), _stage(2, is_final=True)])
    # Stage 1 depending on itself.
    with pytest.raises(ValidationError, match="更早"):
        Plan(stages=[_stage(0), _stage(1, depends_on=[1], is_final=True)])


def test_plan_accepts_valid_forward_dependency():
    Plan(stages=[_stage(0), _stage(1, depends_on=[0], is_final=True)])


# ── artifact harvest (unchanged behaviour) ──────────────────────────────────

def test_harvest_classifies_conclusion_evidence_reasoning():
    from langchain_core.messages import HumanMessage

    long_page = "网页正文" * 500  # 2000 chars, exceeds the snippet bound
    messages = [
        HumanMessage(content="handoff"),
        AIMessage(
            content="",
            additional_kwargs={"reasoning_content": "深层思考链"},
            tool_calls=[{"name": "web_search", "args": {"query": "Q1"}, "id": "c1"}],
        ),
        ToolMessage(content=long_page, name="web_search", tool_call_id="c1"),
        AIMessage(
            content="中间点评",
            tool_calls=[{"name": "web_fetch", "args": {"url": "https://x"}, "id": "c2"}],
        ),
        ToolMessage(content="正文X", name="web_fetch", tool_call_id="c2"),
        AIMessage(content="最终结论"),
    ]

    artifacts = _harvest_stage_artifacts(messages)

    assert artifacts["conclusion"] == "最终结论"
    assert artifacts["reasoning"] == ["深层思考链", "中间点评"]
    assert artifacts["evidence"][0]["query"] == {"query": "Q1"}
    assert artifacts["evidence"][0]["content"].endswith("…[截断]")
    assert artifacts["evidence"][1] == {
        "tool": "web_fetch", "query": {"url": "https://x"}, "content": "正文X",
    }


def test_harvest_falls_back_to_last_message_on_odd_ending():
    from langchain_core.messages import HumanMessage

    messages = [
        HumanMessage(content="handoff"),
        ToolMessage(content="预算截断的工具输出", name="web_search", tool_call_id="c1"),
    ]

    artifacts = _harvest_stage_artifacts(messages)

    assert artifacts["conclusion"] == "预算截断的工具输出"
    assert artifacts["evidence"][0]["content"] == "预算截断的工具输出"
    assert artifacts["reasoning"] == []


def test_build_evidence_records_splits_records_and_index():
    evidence = [
        {"tool": "web_fetch", "query": {"url": "https://x"}, "content": "正文X"},
        {"tool": "web_search", "query": {"query": "Q1"}, "content": "摘要正文"},
    ]
    records, index = _build_evidence_records(evidence)

    assert len(records) == 2 and len(index) == 2
    # The fetch carries its URL; the search has none.
    assert records[0]["url"] == "https://x"
    assert records[1]["url"] == ""
    # Full content stays in the record, not the index.
    assert records[0]["content"] == "正文X"
    assert "content" not in index[0]
    assert index[0]["summary"] == "正文X"
    assert index[0]["id"] == records[0]["id"]


def test_fetch_digest_round_trips_id_and_head():
    text = ("第一段开头。" + "正文" * 50 + "\n\n第二段：包含关键数字 1.2 亿。" + "。" * 40) * 3
    digest = build_fetch_digest("abc123", "https://x", text)
    eid, head = parse_fetch_digest(digest)

    assert eid == "abc123"
    assert head and head in digest
    assert "get_evidence" in digest
    assert "【全文结构概览】" in digest
    # A non-digest string parses to nothing.
    assert parse_fetch_digest("普通文本") == (None, None)


def test_split_sentences_keeps_decimals_intact():
    assert _split_sentences("营收 1.2 亿元。增长 3.5%。") == ["营收 1.2 亿元。", "增长 3.5%。"]


def test_fact_score_ranks_numbers_and_quotes_above_prose():
    assert _fact_score("综上所述，整体表现良好。") == 0
    assert _fact_score("2024年营收 1.2 亿元，同比增长 18%。") > 0


def test_outline_prioritizes_ledes_then_high_score_facts():
    text = (
        "第一段首句。第一段含有 100 和 200 两个数字。\n\n"
        "第二段首句。第二段含有 300 这个数字。"
    )
    # Tight budget: only the two ledes fit — facts are dropped, not the structure.
    tight = _paragraph_outline(text, len("第一段首句。") + len("第二段首句。") + 2)
    assert tight == "- 第一段首句。\n- 第二段首句。"

    # Roomy budget: leftover space goes to the fact sentences...
    roomy = _paragraph_outline(text, 200)
    assert "100" in roomy and "300" in roomy
    # ...and the ledes still lead, in document order.
    assert roomy.splitlines()[0] == "- 第一段首句。"


def test_outline_spreads_ledes_when_they_exceed_budget():
    # 20 paragraphs, ledes alone far exceed a 60-char budget. The outline must
    # still describe the tail of the page, not just its opening.
    text = "\n\n".join(f"第{i}节首句。补充句含 {i}00 元。" for i in range(1, 21))
    lines = [line for line in _paragraph_outline(text, 60).splitlines() if line.strip()]

    assert lines
    assert any(any(f"第{n}节" in line for n in (16, 17, 18, 19, 20)) for line in lines), (
        "outline dropped the page's tail entirely"
    )


def test_outline_orders_leftover_facts_by_score():
    # Both non-lede sentences fit; the higher-scoring one is selected first, so
    # under a budget that admits only one, the fact-dense sentence wins.
    text = "段首句。据报告，2024年营收 1.2 亿元，同比增长 18%。段末句，无事实。"
    only_one = _paragraph_outline(text, len("段首句。") + len("据报告，2024年营收 1.2 亿元，同比增长 18%。"))
    assert "1.2 亿元" in only_one
    assert "段末句，无事实。" not in only_one


def test_harvest_reuses_fetch_time_evidence_id():
    from langchain_core.messages import HumanMessage

    digest = build_fetch_digest("abc123", "https://x", "开头摘录内容。")
    messages = [
        HumanMessage(content="handoff"),
        AIMessage(
            content="",
            tool_calls=[{"name": "web_fetch", "args": {"url": "https://x"}, "id": "c1"}],
        ),
        ToolMessage(content=digest, name="web_fetch", tool_call_id="c1"),
        AIMessage(content="最终结论"),
    ]

    artifacts = _harvest_stage_artifacts(messages)
    assert artifacts["evidence"][0]["id"] == "abc123"

    records, index = _build_evidence_records(artifacts["evidence"])
    # The full text was already persisted at fetch time -> no duplicate record,
    # only an index entry pointing at the same id.
    assert records == []
    assert index == [{
        "id": "abc123",
        "url": "https://x",
        "summary": "开头摘录内容。",
        "tool": "web_fetch",
    }]
