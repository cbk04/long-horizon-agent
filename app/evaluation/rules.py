"""Phase-1 rule checks for the evaluator — pure functions, zero LLM.

Given a stage's raw output and the planner's per-stage metadata, decide
whether the output is even worth an LLM evaluation: non-empty, long enough,
structurally complete markdown, and compliant with the stage's
``output_contract``. Any violation short-circuits phases 2/3 — the stage is
sent straight back to the executor with the precise violation list.
"""

from __future__ import annotations

import re

# Base rules (global): every stage output must clear these regardless of plan.
_BASE_MIN_CHARS = 200
_MIN_HEADINGS = 1
# The final-report stage must look like a report: several sections, not one blob.
_FINAL_MIN_HEADINGS = 2

_HEADING_RE = re.compile(r"^#{1,6}\s+\S", re.MULTILINE)


def _heading_count(text: str) -> int:
    return len(_HEADING_RE.findall(text))


def run_rule_checks(stage_result: str, stage: dict) -> list[str]:
    """Return the list of rule violations for one stage output (empty = pass).

    Args:
        stage_result: The executor's raw conclusion text for this stage.
        stage: The plan's stage dict (objective/acceptance/is_final/
            output_contract).
    """
    violations: list[str] = []
    text = (stage_result or "").strip()
    is_final = bool(stage.get("is_final"))

    if not text:
        return ["阶段产出为空"]
    if len(text) < _BASE_MIN_CHARS:
        violations.append(f"阶段产出过短:仅 {len(text)} 字,基础下限 {_BASE_MIN_CHARS} 字")

    headings = _heading_count(text)
    min_headings = _FINAL_MIN_HEADINGS if is_final else _MIN_HEADINGS
    if headings < min_headings:
        kind = "最终报告" if is_final else "阶段产出"
        violations.append(
            f"{kind}缺少标题结构:检测到 {headings} 个 markdown 标题,至少需要 {min_headings} 个"
        )

    contract = stage.get("output_contract") or {}
    min_length = contract.get("min_length") or 0
    if min_length and len(text) < min_length:
        violations.append(f"阶段产出低于契约长度:仅 {len(text)} 字,契约要求 ≥ {min_length} 字")
    for section in contract.get("required_sections") or []:
        if section and section not in text:
            violations.append(f"缺少契约要求的必需小节「{section}」")

    return violations
