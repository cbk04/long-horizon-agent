"""Counterexample adjudication — pure logic for the evaluator's reverse lane.

The reverse agent (search-enabled) produces counterexamples; a separate
adjudicator LLM grades each one on three dimensions. This module holds the
data shapes both sides speak plus the deterministic strength mapping and
tier aggregation — generation never grades itself, and the mapping lives
here so routing never depends on LLM self-report.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

# Strength ordinal for max-based aggregation.
_STRENGTH_ORDER: dict[str, int] = {"low": 0, "medium": 1, "high": 2}


class Counterexample(BaseModel):
    """One fact-based counterexample found by the reverse agent.

    All fields are mandatory in spirit — an entry without a source quote is
    treated as fabricated and discarded at adjudication.
    """

    claim: str = Field(description="被挑战的结论断言(引用结论原文)")
    counter_evidence: str = Field(description="与该断言冲突的事实反例内容")
    source_url: str = Field(default="", description="反例来源 URL,必须来自真实搜索结果")
    quote: str = Field(default="", description="来源中支撑反例的原文引文")
    date: str = Field(default="", description="反例事实的日期或时间,未知则留空")


class CounterexampleAssessment(BaseModel):
    """Adjudicator's grading of one counterexample, in the same order given."""

    claim: str = Field(description="对应的反例 claim 原文")
    valid: bool = Field(
        description="引用校验:quote 是否真实支撑 counter_evidence;不支持或无引用则为 false",
    )
    timeliness: Literal["high", "medium", "low"] = Field(
        description="时效性:反例日期相对任务时间窗的强度",
    )
    conflict: Literal["direct", "perspective"] = Field(
        description="冲突性:direct=反例事实与断言不可同真;perspective=不同口径/视角,可并存",
    )
    logic: Literal["high", "medium", "low"] = Field(
        description="逻辑性(even-if-true):假设反例为真,结论被实质性推翻的程度",
    )
    reason: str = Field(default="", description="评级理由,一两句")


def counterexample_strength(assessment: CounterexampleAssessment) -> str:
    """Map one assessment to a strength tier — ``invalid`` / low / medium / high.

    Deterministic rules (routing never trusts the LLM's own overall grade):
    - 引用校验不过 → invalid(不参与定档);
    - 时效性或逻辑性为 low → low;
    - 视角差异(perspective)封顶 medium——“换个角度看”不构成推翻;
    - 直接对立且时效与逻辑双高 → high;其余直接对立 → medium。
    """
    if not assessment.valid:
        return "invalid"
    if assessment.timeliness == "low" or assessment.logic == "low":
        return "low"
    if assessment.conflict == "perspective":
        return "medium"
    if assessment.timeliness == "high" and assessment.logic == "high":
        return "high"
    return "medium"


def aggregate_tier(strengths: list[str]) -> str:
    """Aggregate per-counterexample strengths into one tier: max, not average.

    One strong counterexample deserves a response round; averaging would let a
    pile of weak ones dilute it. ``invalid`` entries don't participate; with no
    valid entries the tier is ``low`` (nothing challenges the conclusion).
    """
    valid = [s for s in strengths if s in _STRENGTH_ORDER]
    if not valid:
        return "low"
    return max(valid, key=lambda s: _STRENGTH_ORDER[s])


def render_counterexamples(
    counterexamples: list[dict[str, Any]],
    assessments: list[CounterexampleAssessment],
) -> list[str]:
    """Render (counterexample, strength) pairs as actor-facing feedback lines."""
    lines: list[str] = []
    for ce, assessment in zip(counterexamples, assessments):
        strength = counterexample_strength(assessment)
        if strength == "invalid":
            continue
        source = ce.get("source_url") or "未知来源"
        date = ce.get("date") or "未知日期"
        lines.append(
            f"反例(强度 {strength}):结论断言「{ce.get('claim', '')}」与事实冲突 —— "
            f"{ce.get('counter_evidence', '')}(来源:{source};日期:{date};理由:{assessment.reason})"
        )
    return lines
