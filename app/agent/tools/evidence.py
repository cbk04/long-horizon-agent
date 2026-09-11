"""``get_evidence`` tool — retrieve a prior stage's evidence by id.

The executor keeps a cheap evidence index (id + url + summary) in its handoff
prompt. This tool fetches the full extracted content of one indexed item from
the evidence store, so a later stage can pull the detail it needs without
re-searching or re-fetching the web.
"""

from __future__ import annotations

from typing import Annotated

from langchain_core.runnables import RunnableConfig
from langchain_core.tools import InjectedToolArg, tool

from app.agent.evidence_repo import fetch_evidence


@tool
def get_evidence(evidence_id: str, config: Annotated[RunnableConfig, InjectedToolArg]) -> str:
    """取回此前阶段已抓取的一条证据的完整内容。

    先前的执行阶段会保留一个证据索引(每条含 id、出处 url、一句话摘要)。当你需要
    索引里某条证据的完整内容时,用它的 id 调用本工具;不要为已经抓过的内容重新
    web_search / web_fetch。

    Args:
        evidence_id: 证据索引里的 id。
    """
    task_id = (config.get("configurable") or {}).get("task_id", "")
    if not task_id:
        return "无法确定当前任务上下文,未能检索证据。"
    row = fetch_evidence(task_id, evidence_id)
    if row is None:
        return f"未找到 id 为 {evidence_id} 的证据(可能已被清理或 id 有误)。"
    url = row["url"]
    header = f"出处:{url}" if url else "出处:(无)"
    return f"{header}\n\n摘要:{row['summary']}\n\n完整内容:\n{row['content']}"
