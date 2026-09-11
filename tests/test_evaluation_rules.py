"""Unit tests for the evaluator's pure-logic modules: phase-1 rule checks and
counterexample strength adjudication. Zero LLM, zero I/O.
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from app.evaluation.adjudication import (
    CounterexampleAssessment,
    aggregate_tier,
    counterexample_strength,
    render_counterexamples,
)
from app.evaluation.rules import run_rule_checks

_LONG = "这是一段足够长的分析内容,用于通过基础长度下限的规则检查。" * 10  # ~460 chars


def _stage(**overrides) -> dict:
    stage = {
        "objective": "目标",
        "scope_excludes": [],
        "acceptance": [],
        "is_final": False,
        "output_contract": None,
    }
    stage.update(overrides)
    return stage


# ── phase 1: rule checks ─────────────────────────────────────────────────────

def test_empty_output_violates():
    assert run_rule_checks("", _stage()) == ["阶段产出为空"]
    assert run_rule_checks("   \n  ", _stage()) == ["阶段产出为空"]


def test_short_output_violates_length_floor():
    violations = run_rule_checks("# 标题\n太短", _stage())
    assert any("过短" in v for v in violations)


def test_output_without_heading_violates_structure():
    violations = run_rule_checks(_LONG, _stage())  # long but no markdown heading
    assert any("标题结构" in v for v in violations)
    assert not any("过短" in v for v in violations)


def test_compliant_output_passes():
    assert run_rule_checks(f"# 分析\n{_LONG}", _stage()) == []


def test_final_stage_requires_report_structure():
    body = f"# 结论\n{_LONG}"  # one heading only
    violations = run_rule_checks(body, _stage(is_final=True))
    assert any("最终报告" in v for v in violations)
    # Two headings read as a report skeleton.
    assert run_rule_checks(f"# 报告\n## 第一节\n{_LONG}", _stage(is_final=True)) == []


def test_contract_min_length_enforced():
    body = f"# 分析\n{_LONG}"  # ~470 chars
    stage = _stage(output_contract={"min_length": 1000, "required_sections": []})
    violations = run_rule_checks(body, stage)
    assert any("契约长度" in v for v in violations)


def test_contract_required_sections_enforced():
    body = f"# 分析\n{_LONG}"
    stage = _stage(output_contract={"min_length": 0, "required_sections": ["成本结构", "对比"]})
    violations = run_rule_checks(body, stage)
    assert any("成本结构" in v for v in violations)
    assert any("对比" in v for v in violations)
    assert run_rule_checks(f"# 成本结构与对比\n{_LONG}", stage) == []


# ── strength adjudication ────────────────────────────────────────────────────

def _assessment(**overrides) -> CounterexampleAssessment:
    kwargs = dict(
        claim="断言A",
        valid=True,
        timeliness="high",
        conflict="direct",
        logic="high",
        reason="",
    )
    kwargs.update(overrides)
    return CounterexampleAssessment(**kwargs)


def test_invalid_entries_map_to_invalid():
    assert counterexample_strength(_assessment(valid=False)) == "invalid"


def test_low_timeliness_or_logic_caps_at_low():
    assert counterexample_strength(_assessment(timeliness="low")) == "low"
    assert counterexample_strength(_assessment(logic="low")) == "low"


def test_perspective_conflict_caps_at_medium():
    """换视角不构成推翻:即使时效与逻辑双高,视角差异也封顶 medium。"""
    assert counterexample_strength(_assessment(conflict="perspective")) == "medium"


def test_direct_conflict_with_full_strength_is_high():
    assert counterexample_strength(_assessment()) == "high"


def test_direct_conflict_with_medium_dimension_is_medium():
    assert counterexample_strength(_assessment(timeliness="medium")) == "medium"


def test_aggregation_takes_the_max_not_the_average():
    strengths = ["low", "medium", "low"]
    assert aggregate_tier(strengths) == "medium"
    assert aggregate_tier(["low", "high", "low"]) == "high"


def test_aggregation_ignores_invalid_and_empty():
    assert aggregate_tier(["invalid", "invalid"]) == "low"
    assert aggregate_tier([]) == "low"


def test_render_skips_invalid_and_carries_strength():
    counterexamples = [
        {"claim": "断言A", "counter_evidence": "反例1", "source_url": "https://a", "date": "2026-01"},
        {"claim": "断言B", "counter_evidence": "反例2", "source_url": "", "date": ""},
    ]
    assessments = [
        _assessment(claim="断言A"),
        _assessment(claim="断言B", valid=False),
    ]
    lines = render_counterexamples(counterexamples, assessments)
    assert len(lines) == 1
    assert "medium" not in lines[0] or "强度" in lines[0]
    assert "断言A" in lines[0]
    assert "https://a" in lines[0]
    # 强度 high 渲染进反馈行
    assert "强度 high" in lines[0]
