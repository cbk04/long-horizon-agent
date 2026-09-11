"""Web search & fetch tools for the ReAct agent.

Search is provider-agnostic and resilient:

- Tavily (``SEARCH_PROVIDER=tavily`` or ``auto`` with ``TAVILY_API_KEY`` set)
  is used when a key is available — a proper API, no scraping.
- Bing (``auto`` with no key, or ``SEARCH_PROVIDER=bing``) scrapes
  ``www.bing.com`` organic results; reachable from most networks incl. mainland
  China, where DuckDuckGo is blocked.
- DuckDuckGo (``auto`` with no key, or ``SEARCH_PROVIDER=ddg``) scrapes two
  endpoints (``lite`` then ``html``) with retries/backoff and rotating
  user-agents, because DDG actively rate-limits automated requests.

On total failure, ``web_search`` returns an **empty** list rather than a
fabricated ``{"title": "Search failed", ...}`` row, so the agent never treats
a failed search as evidence.
"""

from __future__ import annotations

import logging
import random
import re
import time
import uuid
from typing import Annotated, Any

import httpx
from langchain_core.runnables import RunnableConfig
from langchain_core.tools import InjectedToolArg, tool
from trafilatura import extract as trafilatura_extract

from app.agent import evidence_repo
from app.config import get_settings

logger = logging.getLogger(__name__)

_DDG_LITE_URL = "https://lite.duckduckgo.com/lite/"
_DDG_HTML_URL = "https://html.duckduckgo.com/html/"
_BING_URL = "https://www.bing.com/search"
_TAVILY_URL = "https://api.tavily.com/search"

_RETRIES = 1
_MAX_RESULTS = 10

# Rotate user-agents to reduce the chance DDG blocks us.
_USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Safari/605.1.15",  # noqa: E501
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/119.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:121.0) Gecko/20100101 Firefox/121.0",
]


def _headers() -> dict[str, str]:
    return {"User-Agent": random.choice(_USER_AGENTS)}


def _attr_to_str(value: Any) -> str:
    """Coerce a BeautifulSoup attribute value (str | list[str] | None) to str."""
    return value if isinstance(value, str) else ""


def _http_with_retries(method: str, url: str, *, timeout: float = 15.0, **kwargs: Any) -> httpx.Response:
    """HTTP request with exponential backoff; raises the last error on failure."""
    last_err: Exception | None = None
    for attempt in range(_RETRIES + 1):
        try:
            resp = httpx.request(
                method, url, headers=_headers(), timeout=timeout, follow_redirects=True, **kwargs
            )
            resp.raise_for_status()
            return resp
        except Exception as e:  # network / HTTP / timeout
            last_err = e
            if attempt < _RETRIES:
                time.sleep(0.5 * (2 ** attempt))
    assert last_err is not None
    raise last_err


def _parse_ddg(html: str) -> list[dict[str, str]]:
    """Parse DuckDuckGo HTML (both ``lite`` and ``html`` layouts)."""
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(html, "html.parser")
    results: list[dict[str, str]] = []

    # lite layout: <a class="result-link" href="...">Title</a> (+ snippet rows)
    links = soup.select("a.result-link")
    snippets = soup.select(".result-snippet")
    for i, row in enumerate(links[:_MAX_RESULTS]):
        href = _attr_to_str(row.get("href"))
        title = row.get_text(strip=True)
        snippet = snippets[i].get_text(strip=True) if i < len(snippets) else ""
        if title or href:
            results.append({"title": title, "url": href, "snippet": snippet})
    if results:
        return results

    # html layout: .result with nested .result__title / .result__snippet / .result__a
    for item in soup.select(".result")[:_MAX_RESULTS]:
        title_tag = item.select_one(".result__title")
        snippet_tag = item.select_one(".result__snippet")
        title = title_tag.get_text(strip=True) if title_tag else ""
        snippet = snippet_tag.get_text(strip=True) if snippet_tag else ""
        url = ""
        link_tag = item.select_one(".result__a")
        if link_tag and link_tag.get("href"):
            href = _attr_to_str(link_tag.get("href"))
            # DDG wraps real URLs in //duckduckgo.com/l/?uddg=ENCODED_URL
            if "uddg=" in href:
                from urllib.parse import parse_qs, urlparse

                parsed = urlparse(href)
                qs = parse_qs(parsed.query)
                if "uddg" in qs:
                    url = qs["uddg"][0]
            else:
                url = href
        if title or url:
            results.append({"title": title, "url": url, "snippet": snippet})
    return results


def _search_tavily(query: str, api_key: str) -> list[dict[str, str]]:
    resp = httpx.post(
        _TAVILY_URL,
        json={"api_key": api_key, "query": query, "max_results": _MAX_RESULTS, "search_depth": "basic"},
        timeout=20,
    )
    resp.raise_for_status()
    data = resp.json()
    return [
        {"title": r.get("title") or "", "url": r.get("url") or "", "snippet": r.get("content") or ""}
        for r in data.get("results", [])
        if r.get("url")
    ]


def _search_ddg(query: str) -> list[dict[str, str]]:
    """Try the lite endpoint first, then the html endpoint; raise on total failure.

    Uses a short timeout: DDG is unreachable from many networks (e.g. mainland
    China) and we must not stall the agent on a doomed TLS handshake.
    """
    last_err: Exception | None = None
    for method, url, kwargs in (
        ("get", _DDG_LITE_URL, {"params": {"q": query}}),
        ("post", _DDG_HTML_URL, {"data": {"q": query, "b": ""}}),
    ):
        try:
            resp = _http_with_retries(method, url, timeout=5.0, **kwargs)
            results = _parse_ddg(resp.text)
            if results:
                return results
        except Exception as e:
            last_err = e
    if last_err is not None:
        raise last_err
    return []


def _decode_bing_url(href: str) -> str:
    """Extract the real target URL from a Bing ``/ck/a`` redirect.

    Bing wraps result links in ``https://www.bing.com/ck/a?...&u=a1<base64url>``;
    the ``u`` parameter is the destination URL, base64url-encoded with an
    ``a1``/``a2`` prefix.
    """
    try:
        from base64 import urlsafe_b64decode
        from urllib.parse import parse_qs, urlparse

        qs = parse_qs(urlparse(href).query)
        u = qs.get("u", [""])[0]
        if not u:
            return href
        u = re.sub(r"^a[12]", "", u)
        u += "=" * (-len(u) % 4)
        return urlsafe_b64decode(u).decode("utf-8", "ignore")
    except Exception:
        return href


def _parse_bing(html: str) -> list[dict[str, str]]:
    """Parse Bing's organic result blocks (``li.b_algo``)."""
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(html, "html.parser")
    results: list[dict[str, str]] = []
    for item in soup.select("li.b_algo")[:_MAX_RESULTS]:
        a = item.select_one("h2 a")
        if a is None:
            continue
        title = a.get_text(strip=True)
        url = _decode_bing_url(_attr_to_str(a.get("href")))
        caption = item.select_one(".b_caption p") or item.select_one("p")
        snippet = caption.get_text(strip=True) if caption else ""
        if title or url:
            results.append({"title": title, "url": url, "snippet": snippet})
    return results


def _search_bing(query: str) -> list[dict[str, str]]:
    resp = _http_with_retries("get", _BING_URL, params={"q": query}, timeout=8.0)
    return _parse_bing(resp.text)


def _search(query: str) -> list[dict[str, str]]:
    """Provider-aware search. Never raises — degrades to an empty result set.

    Priority: Tavily (real API, needs a key) → Bing (reachable from most
    networks, incl. mainland China) → DuckDuckGo (scraped, often rate-limited).
    """
    settings = get_settings()
    provider = settings.search_provider

    if provider in ("tavily", "auto") and settings.tavily_api_key:
        try:
            return _search_tavily(query, settings.tavily_api_key)
        except Exception as e:
            logger.warning("tavily search failed for '%s': %s", query, e)
            if provider == "tavily":
                return []

    if provider == "tavily":
        logger.warning("tavily search requested but no TAVILY_API_KEY configured")
        return []

    backends: list[tuple[str, Any]] = []
    if provider in ("auto", "bing"):
        backends.append(("bing", _search_bing))
    if provider in ("auto", "ddg"):
        backends.append(("ddg", _search_ddg))
    for name, fn in backends:
        try:
            results = fn(query)
            if results:
                return results
        except Exception as e:
            logger.warning("%s search failed for '%s': %s", name, query, e)
    return []


@tool
def web_search(query: str) -> list[dict[str, str]]:
    """Search the web for a query. Returns a list of results with title, url, and snippet.

    Args:
        query: The search query string.
    """
    results = _search(query)
    logger.info("web_search('%s'): %d results", query, len(results))
    return results


# ── bounded digest for web_fetch ─────────────────────────────────────────────
# web_fetch persists the FULL extracted text (evidence_repo) but returns only a
# bounded digest to the agent's context: head excerpt + paragraph outline + tail
# excerpt + the id for on-demand retrieval. This keeps the live context small
# without losing the ability to pull the full text via get_evidence(id).

_EVIDENCE_SUMMARY_CHARS = 200
_ID_MARKER = "【证据ID】"
_SENTENCE_END = "。！？!?."


def _split_sentences(text: str) -> list[str]:
    """Split into sentences, keeping each terminator with its sentence.

    A decimal point between digits (``1.2 亿``) is not a sentence boundary.
    """
    out: list[str] = []
    buf: list[str] = []
    for i, ch in enumerate(text):
        buf.append(ch)
        if ch not in _SENTENCE_END:
            continue
        if ch == "." and 0 < i < len(text) - 1 and text[i - 1].isdigit() and text[i + 1].isdigit():
            continue
        out.append("".join(buf))
        buf = []
    if buf:
        out.append("".join(buf))
    return out


# Fact markers: numbers/amounts, dates, quoted text, comparisons, attribution.
# A sentence carrying these is a "concatenable fact" — the kind a later stage
# needs to quote or combine across pages — so it outranks connective prose.
_FACT_PATTERNS = (
    re.compile(r"\d+(?:\.\d+)?"),
    re.compile(r"\d{4}\s*年|\d{1,2}\s*月"),
    re.compile(r"[「“\"'][^」”\"']{2,}[」”\"']"),
    re.compile(r"同比|环比|超过|低于|首次|唯一|最多|最少|增长|下降"),
    re.compile(r"据|根据|报告|指出|声明|公布|发表"),
)


def _fact_score(sentence: str) -> int:
    """Count fact markers in a sentence (0 = connective prose)."""
    return sum(len(p.findall(sentence)) for p in _FACT_PATTERNS)


def _paragraph_outline(text: str, budget: int) -> str:
    """Paragraph ledes first (document order), then remaining budget by fact score.

    Ledes describe the page's structure and outweigh raw fact density, so they
    are always kept first; only what is left of the budget goes to the
    highest-scoring remaining sentences, so concatenable facts (numbers, quotes,
    comparisons) survive the cut instead of being lost to an arbitrary prefix.
    Output is rendered in document order so the result reads as an outline.
    """
    paragraphs = [p.strip() for p in re.split(r"\n\s*\n|\n", text) if p.strip()]

    ledes: list[tuple[int, str]] = []   # (document order, sentence)
    rest: list[tuple[int, str]] = []
    order = 0
    for p in paragraphs:
        sentences = [s.strip() for s in _split_sentences(p) if s.strip()]
        if not sentences:
            continue
        ledes.append((order, sentences[0]))
        order += 1
        for s in sentences[1:]:
            rest.append((order, s))
            order += 1

    selected: list[tuple[int, str]] = []
    used = 0
    # Phase 1: every paragraph lede. When they all fit, keep them all; when
    # there are more ledes than the budget allows, sample them evenly across
    # the document — taking the first N in order would describe only the
    # page's opening and silently drop every later section.
    if ledes:
        avg = max(1, sum(len(s) for _, s in ledes) // len(ledes))
        capacity = max(1, budget // avg)
        if capacity >= len(ledes):
            picked = ledes
        else:
            step = len(ledes) / capacity
            picked = [ledes[int(i * step)] for i in range(capacity)]
        for order_i, lede in picked:
            if used + len(lede) > budget:
                break
            selected.append((order_i, lede))
            used += len(lede)

    # Phase 2: fill what's left with the highest-scoring non-lede sentences.
    if used < budget:
        chosen = {s for _, s in selected}
        ranked = sorted(
            ((o, s, _fact_score(s)) for o, s in rest if s not in chosen),
            key=lambda r: (-r[2], r[0]),
        )
        for order_i, sent, score in ranked:
            if score <= 0:
                break
            if used + len(sent) > budget:
                break
            selected.append((order_i, sent))
            used += len(sent)

    selected.sort(key=lambda r: r[0])
    return "\n".join(f"- {s}" for _, s in selected)


def build_fetch_digest(eid: str, url: str, text: str, *, already_fetched: bool = False) -> str:
    """Render the bounded digest handed back to the agent for one fetch."""
    head_chars, tail_chars, outline_chars = (
        get_settings().web_fetch_digest_head_chars,
        get_settings().web_fetch_digest_tail_chars,
        get_settings().web_fetch_digest_outline_chars,
    )
    text = (text or "").strip()
    head = text[:head_chars]
    tail = ""
    if len(text) > head_chars + tail_chars:
        tail = text[-tail_chars:]
    outline = _paragraph_outline(text, outline_chars)

    source = f"【出处】{url}"
    if already_fetched:
        source += "（本任务已抓取过此 URL，未重复下载）"
    lines = [f"{_ID_MARKER}{eid}", source, f"【开头摘录】{head}"]
    if outline:
        lines.append(f"【全文结构概览】\n{outline}")
    if tail:
        lines.append(f"【结尾摘录】{tail}")
    lines.append(f"全文已保存，需要完整内容时用 get_evidence(\"{eid}\") 取回。")
    return "\n\n".join(lines)


def parse_fetch_digest(content: str) -> tuple[str | None, str | None]:
    """Parse a web_fetch digest into ``(evidence_id, head_excerpt)``.

    Returns ``(None, None)`` when the content is not a digest. The stage-end
    harvest uses this to reuse the fetch-time evidence id (pointing at the
    already-persisted full text) instead of re-persisting a duplicate row.
    """
    content = str(content or "")
    eid: str | None = None
    m = re.search(r"【证据ID】([0-9a-f]+)", content)
    if m:
        eid = m.group(1)
    head: str | None = None
    m = re.search(
        r"【开头摘录】\s*(.*?)\s*(?=【全文结构概览】|【结尾摘录】|全文已保存)",
        content,
        re.DOTALL,
    )
    if m:
        head = m.group(1).strip()
    return eid, head


@tool
def web_fetch(url: str, config: Annotated[RunnableConfig, InjectedToolArg]) -> str:
    """抓取一个网页,把全文落库,只把有界摘要塞回上下文。

    返回摘要(证据 id + 开头摘录 + 段落提纲 + 结尾摘录),而非整页正文;全文已保存,
    需要细节时用其 id 调用 get_evidence 取回。同一 URL 本任务内不会重复下载。

    Args:
        url: 要抓取的网页 URL。
    """
    task_id = (config.get("configurable") or {}).get("task_id", "")

    # URL-level dedup: a URL already fetched this task is served from the store.
    if task_id:
        existing = evidence_repo.find_evidence_by_url(task_id, url)
        if existing:
            logger.info("web_fetch('%s'): dedup hit -> evidence %s", url, existing["id"])
            return build_fetch_digest(
                existing["id"], url, existing.get("content") or "", already_fetched=True
            )

    try:
        resp = _http_with_retries("get", url, timeout=20)
    except Exception as e:
        logger.warning("web_fetch failed for '%s': %s", url, e)
        return f"Failed to fetch {url}: {e}"

    text = trafilatura_extract(resp.text, include_comments=False, include_tables=True)
    if not text:
        from bs4 import BeautifulSoup

        soup = BeautifulSoup(resp.text, "html.parser")
        text = soup.get_text(separator="\n", strip=True)[:5000]
    else:
        text = text[:8000]

    logger.info("web_fetch('%s'): %d chars extracted", url, len(text))

    if not text:
        return f"Failed to fetch {url}: no extractable text."

    # Persist full text (best-effort — a store failure must not fail the run),
    # then hand back only the digest + id for on-demand retrieval.
    eid = uuid.uuid4().hex
    if task_id:
        evidence_repo.save_evidence(
            task_id=task_id,
            run_id=(config.get("configurable") or {}).get("run_id"),
            stage_index=(config.get("configurable") or {}).get("stage_index"),
            records=[{
                "id": eid,
                "url": url,
                "summary": text[:_EVIDENCE_SUMMARY_CHARS],
                "content": text,
            }],
        )
    return build_fetch_digest(eid, url, text)


# Export tools list for agent creation
TOOLS: list[Any] = [web_search, web_fetch]
