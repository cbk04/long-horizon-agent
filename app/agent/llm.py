"""LLM client wrapper — unified entry point for model calls.

Wraps ChatOpenAI with:
- Token counting & budget tracking
- Event emission (model_call.started / model_call.completed)
"""

from __future__ import annotations

import json
import logging
import re
import time
from typing import Any

from langchain_core.messages import BaseMessage, HumanMessage, SystemMessage
from langchain_core.outputs import LLMResult
from langchain_openai import ChatOpenAI
from pydantic import ValidationError

from app.agent.streaming import stream_context, stream_handler
from app.config import get_settings

logger = logging.getLogger(__name__)


def get_llm(
    temperature: float = 0.1,
    *,
    model: str | None = None,
    api_key: str | None = None,
    base_url: str | None = None,
) -> ChatOpenAI:
    """Return a configured ChatOpenAI instance.

    Override args fall back to the main lane's settings when omitted — the
    evaluator's weak-model lane passes its own triple here.
    """
    settings = get_settings()
    return ChatOpenAI(
        model=model or settings.llm_model,
        temperature=temperature,
        api_key=api_key or settings.openai_api_key,
        base_url=base_url or settings.openai_base_url,
        streaming=True,
        callbacks=[stream_handler()],
    )


def get_scoring_llm(temperature: float = 0.0) -> ChatOpenAI:
    """Weak-model lane for mechanical rubric scoring (evaluator phase 3).

    Falls back to the main lane per-field when the evaluator_score_* settings
    are empty, so an unconfigured deployment keeps working.
    """
    settings = get_settings()
    return get_llm(
        temperature,
        model=settings.evaluator_score_model or None,
        api_key=settings.evaluator_score_api_key or None,
        base_url=settings.evaluator_score_base_url or None,
    )


def extract_token_usage(result: BaseMessage | LLMResult | Any) -> tuple[int, int]:
    """Extract (input_tokens, output_tokens) from a LangChain LLM response.

    Works with both .invoke() (returns BaseMessage) and .generate() (returns LLMResult).
    """
    # ChatOpenAI attaches usage_metadata on the message since langchain-openai >= 0.2
    if isinstance(result, BaseMessage):
        usage = getattr(result, "usage_metadata", None)
        if usage:
            return int(usage.get("input_tokens", 0)), int(usage.get("output_tokens", 0))
        # Fallback: response_metadata -> token_usage
        resp_meta = getattr(result, "response_metadata", {})
        token_usage = resp_meta.get("token_usage", {}) if isinstance(resp_meta, dict) else {}
        return int(token_usage.get("prompt_tokens", 0)), int(token_usage.get("completion_tokens", 0))

    if isinstance(result, LLMResult):
        for gen in result.generations:
            for g in gen:
                msg = getattr(g, "message", None)
                if msg is None:
                    continue
                usage = getattr(msg, "usage_metadata", None)
                if usage:
                    return int(usage.get("input_tokens", 0)), int(usage.get("output_tokens", 0))

    return 0, 0


def call_llm(
    llm: ChatOpenAI,
    messages: list[BaseMessage],
    *,
    task_id: str,
    purpose: str = "reasoning",
) -> BaseMessage:
    """Call LLM and emit events. Returns the response message.

    This is a thin wrapper around llm.invoke() that adds:
    - model_call.started event
    - model_call.completed event (with token counts, latency)
    - Budget tracking (tokens)
    """
    from app.harness import budget, event_bus
    from app.storage.mysql.database import SessionLocal

    model_name = llm.model_name if hasattr(llm, "model_name") else str(getattr(llm, "model", "unknown"))
    db = SessionLocal()
    try:
        event_bus.publish(db, task_id, "model_call.started", {
            "model": model_name,
            "purpose": purpose,
        })
    finally:
        db.close()

    t0 = time.monotonic()
    with stream_context(task_id, stream_tokens=True):
        response = llm.invoke(messages)
    latency_ms = int((time.monotonic() - t0) * 1000)

    input_tokens, output_tokens = extract_token_usage(response)

    # Update budget
    budget.add_input_tokens(task_id, input_tokens)
    budget.add_output_tokens(task_id, output_tokens)
    budget.add_elapsed_ms(task_id, latency_ms)

    db = SessionLocal()
    try:
        event_bus.publish(db, task_id, "model_call.completed", {
            "model": model_name,
            "purpose": purpose,
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "latency_ms": latency_ms,
        })
    finally:
        db.close()

    return response


def _extract_json_text(text: Any) -> str:
    """Pull the first balanced JSON object out of a model response.

    Handles markdown code fences and any preamble/trailing prose the model adds.
    """
    text = str(text).strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z]*\s*", "", text)
        text = re.sub(r"\s*```\s*$", "", text)
        text = text.strip()
    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end <= start:
        raise ValueError(f"response contains no JSON object: {text[:200]!r}")
    return text[start : end + 1]


def _json_schema_instruction(schema: type) -> str:
    """Prompt text that pins the model's reply to ``schema``'s JSON shape."""
    schema_json = json.dumps(schema.model_json_schema(), ensure_ascii=False, indent=2)
    return (
        "你必须输出一个符合以下 JSON Schema 的 JSON 对象,作为对该任务的最终回答。\n"
        "只输出 JSON 本身,不要包含任何解释、前言或 Markdown 代码块标记。\n\n"
        f"```json\n{schema_json}\n```"
    )


def call_structured(
    llm: ChatOpenAI,
    schema: type,
    messages: list[BaseMessage],
    *,
    task_id: str,
    purpose: str,
    attempts: int = 2,
) -> tuple[Any, BaseMessage | None]:
    """Structured-output call via JSON prompting (no forced ``tool_choice``).

    Thinking-mode backends (DeepSeek reasoner family) reject a forced
    ``tool_choice`` — which is what ``with_structured_output(..., method=
    "function_calling")`` sends — with a 400 "Thinking mode does not support
    this tool_choice". Instead we append the JSON schema to the prompt, call
    the model normally, then parse and validate the JSON, feeding any
    validation error back to the model for a bounded retry.

    Returns ``(parsed, raw)``; ``raw`` is the response message (token usage
    survives). Raises on repeated failure.
    """
    from app.harness import budget, event_bus
    from app.storage.mysql.database import SessionLocal

    model_name = llm.model_name if hasattr(llm, "model_name") else str(getattr(llm, "model", "unknown"))
    msgs: list[BaseMessage] = [SystemMessage(content=_json_schema_instruction(schema)), *messages]

    last_error: Exception | None = None
    for _ in range(attempts):
        db = SessionLocal()
        try:
            event_bus.publish(db, task_id, "model_call.started", {
                "model": model_name,
                "purpose": purpose,
            })
        finally:
            db.close()

        t0 = time.monotonic()
        try:
            with stream_context(task_id, stream_tokens=False):
                raw = llm.invoke(msgs)
        except Exception as e:  # transport/API errors — retry within bounds
            last_error = e
            continue

        latency_ms = int((time.monotonic() - t0) * 1000)
        input_tokens, output_tokens = extract_token_usage(raw)

        budget.add_input_tokens(task_id, input_tokens)
        budget.add_output_tokens(task_id, output_tokens)
        budget.add_elapsed_ms(task_id, latency_ms)

        try:
            parsed = schema.model_validate_json(_extract_json_text(raw.content))
        except (ValidationError, ValueError) as e:
            last_error = e
            parse_ok = False
            msgs.append(HumanMessage(content=(
                f"上一次输出未通过校验,错误如下:\n{e}\n"
                "请修正后重新输出完整的 JSON 对象(不要包含任何其他文字)。"
            )))
        else:
            parse_ok = True

        db = SessionLocal()
        try:
            event_bus.publish(db, task_id, "model_call.completed", {
                "model": model_name,
                "purpose": purpose,
                "input_tokens": input_tokens,
                "output_tokens": output_tokens,
                "latency_ms": latency_ms,
                "parse_ok": parse_ok,
            })
        finally:
            db.close()

        if parse_ok:
            return parsed, raw

    raise RuntimeError(f"call_structured({purpose}) failed after {attempts} attempts: {last_error}")
