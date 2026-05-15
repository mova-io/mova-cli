"""Implementation for the __SKILL_NAME__ skill — web search via DuckDuckGo.

Why DuckDuckGo's HTML endpoint:

* **No API key required.** Self-contained demo; prospects can run
  `mdk run rag-qa ...` without provisioning credentials.
* **Stable URL surface.** `https://html.duckduckgo.com/html/?q=...`
  has stayed remarkably consistent for years.
* **Easy parsing.** Each result is an `<a class="result__a">` anchor
  followed by an `<a class="result__snippet">` paragraph — no
  JavaScript rendering needed.

The trade-offs vs. a real API (Serper, Brave, Google CSE):

* DDG occasionally rate-limits aggressive callers — we add a UA
  header to look like a normal browser.
* HTML structure could shift; if it does, the parser breaks loudly
  (clear stack trace, not a silent empty list).
* No structured metadata (publish date, source rank) — just title,
  snippet, URL.

For production use, swap the body to call a real search API; the
input + output schemas stay identical so the agent.yaml doesn't
need to change.
"""

from __future__ import annotations

import re
from html import unescape
from typing import TYPE_CHECKING, Any

import httpx

if TYPE_CHECKING:
    from movate.core.skill_backend import SkillExecutionContext


# DuckDuckGo's no-JS HTML search endpoint. Returns a single static
# document; results sit in DOM nodes we extract via regex (heavyweight
# DOM parsers like BeautifulSoup would be overkill for this shape).
_DDG_URL = "https://html.duckduckgo.com/html/"

# Browser-like UA. DDG's HTML endpoint will throttle empty / library-
# default UA strings; this header keeps the throughput friendly.
_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_0) "
        "AppleWebKit/605.1.15 (KHTML, like Gecko) "
        "Version/17.0 Safari/605.1.15"
    ),
}

# Per-result block regex. DDG renders each hit as:
#   <a class="result__a" href="<url>">...title html...</a>
#   ...
#   <a class="result__snippet" ...>...snippet html...</a>
# The result__a anchor's href is the canonical link. We capture title
# inner text + the snippet body in one pass.
_RESULT_RE = re.compile(
    r'<a[^>]*class="result__a"[^>]*href="(?P<url>[^"]+)"[^>]*>'
    r"(?P<title>.+?)</a>"
    r".*?"
    r'<a[^>]*class="result__snippet"[^>]*>(?P<snippet>.+?)</a>',
    flags=re.DOTALL,
)

# Strip-tags helper for the captured title/snippet HTML. Searches
# return small fragments only, so a one-line regex beats pulling in
# bleach/html.parser for the same outcome.
_TAG_RE = re.compile(r"<[^>]+>")

# Floor for top_n. The LLM sometimes asks for 0 results to "save
# tokens" — that's never the right call; the search is the value.
_DEFAULT_TOP_N = 5
_MAX_TOP_N = 10


def _strip_html(text: str) -> str:
    """Strip HTML tags and decode entities, collapse whitespace."""
    no_tags = _TAG_RE.sub("", text)
    decoded = unescape(no_tags)
    return " ".join(decoded.split())


async def run(input: dict[str, Any], ctx: SkillExecutionContext) -> dict[str, Any]:
    """Run a web search and return ``top_n`` results.

    Returns ``{"results": [{"title": ..., "url": ..., "snippet": ...}]}``
    matching the output schema. An empty list is a valid response —
    no results for the query is information too, and the agent should
    handle it rather than crashing.

    Failure modes (each → empty results + a ``warning`` field):

    * Network error / timeout
    * DDG returned a non-2xx response
    * HTML structure shifted and the regex stopped matching
    """
    query = input["query"]
    top_n = input.get("top_n") or _DEFAULT_TOP_N
    top_n = max(1, min(_MAX_TOP_N, int(top_n)))

    # Honor the agent's call_ms_budget if set. Falls back to a
    # generous 15s for network round-trips.
    timeout_ms = ctx.call_ms_budget or 15_000
    timeout_s = timeout_ms / 1000.0

    try:
        async with httpx.AsyncClient(timeout=timeout_s, follow_redirects=True) as client:
            response = await client.post(
                _DDG_URL,
                data={"q": query},
                headers=_HEADERS,
            )
    except (httpx.HTTPError, httpx.TimeoutException) as exc:
        return {
            "results": [],
            "warning": f"network error contacting DuckDuckGo: {exc}",
        }

    if response.status_code != 200:  # noqa: PLR2004
        return {
            "results": [],
            "warning": (
                f"DuckDuckGo returned HTTP {response.status_code}; the HTML "
                f"endpoint may have rate-limited this request"
            ),
        }

    matches = _RESULT_RE.finditer(response.text)
    results: list[dict[str, str]] = []
    for match in matches:
        if len(results) >= top_n:
            break
        results.append(
            {
                "title": _strip_html(match.group("title")),
                "url": match.group("url"),
                "snippet": _strip_html(match.group("snippet")),
            }
        )

    if not results:
        return {
            "results": [],
            "warning": (
                "no results parsed — DuckDuckGo's HTML structure may have "
                "changed; consider switching to a JSON-API backend"
            ),
        }

    return {"results": results}
