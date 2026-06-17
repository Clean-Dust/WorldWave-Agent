"""ww/tools/web_tools.py — network and searchtool

Dependencies: None (pure stdlib: urllib + html.parser)
Purpose: Web search, HTTP request
"""

from __future__ import annotations
import urllib.parse
import urllib.request
import urllib.error
from html.parser import HTMLParser

from tools.registry import ToolRegistry, ToolDef


# ── DuckDuckGo HTML resolve  ──

class DDGSearchParser(HTMLParser):
    """Parse DuckDuckGo HTML search results."""

    def __init__(self):
        super().__init__()
        self.results = []
        self._current = {}
        self._in_result = False
        self._in_link = False
        self._in_snippet = False
        self._skip_depth = 0

    def handle_starttag(self, tag, attrs):
        attrs_dict = dict(attrs)
        classes = attrs_dict.get("class", "")

        # Result wrapper: <div class="result" ...>
        if tag == "div" and "result" in classes and "result__body" not in classes:
            if self._in_result:
                self._skip_depth += 1
                return
            self._in_result = True
            self._current = {}
            return

        if not self._in_result or self._skip_depth > 0:
            return

        # Result link: <a class="result__a" href="...">
        if tag == "a" and "result__a" in classes:
            self._in_link = True
            href = attrs_dict.get("href", "")
            # Extract URL from DuckDuckGo redirect
            if "uddg=" in href:
                parsed = urllib.parse.parse_qs(urllib.parse.urlparse(href).query)
                if "uddg" in parsed:
                    self._current["url"] = parsed["uddg"][0]
            elif href.startswith("http"):
                self._current["url"] = href

        # Result snippet: <a class="result__snippet" ...>
        if tag == "a" and "result__snippet" in classes:
            self._in_snippet = True

    def handle_endtag(self, tag):
        if self._skip_depth > 0:
            self._skip_depth -= 1
            return
        if tag == "a" and self._in_link:
            self._in_link = False
        if tag == "a" and self._in_snippet:
            self._in_snippet = False
        if tag == "div" and self._in_result and self._skip_depth == 0:
            if self._current.get("title") or self._current.get("snippet"):
                self.results.append(self._current)
            self._in_result = False
            self._current = {}

    def handle_data(self, data):
        if not self._in_result or self._skip_depth > 0:
            return
        data = data.strip()
        if not data:
            return
        if self._in_link:
            self._current["title"] = data
        elif self._in_snippet:
            if "snippet" not in self._current:
                self._current["snippet"] = ""
            self._current["snippet"] += data


def register_tools(registry: ToolRegistry):
    """Register web tools with the given registry."""

    # ── web_search ────────────────────────────────────

    def handle_web_search(query: str, num_results: int = 5, **kwargs) -> dict:
        """Search the web using DuckDuckGo."""
        try:
            url = "https://html.duckduckgo.com/html/"
            data = urllib.parse.urlencode({"q": query}).encode()
            req = urllib.request.Request(
                url,
                data=data,
                headers={
                    "User-Agent": (
                        "Mozilla/5.0 (X11; Linux x86_64) "
                        "AppleWebKit/537.36 (KHTML, like Gecko) "
                        "Chrome/120.0.0.0 Safari/537.36"
                    ),
                },
            )
            with urllib.request.urlopen(req, timeout=15) as resp:
                html_content = resp.read().decode("utf-8", errors="replace")

            parser = DDGSearchParser()
            parser.feed(html_content)
            results = parser.results[:num_results]

            return {
                "result": [
                    {
                        "title": r.get("title", ""),
                        "url": r.get("url", ""),
                        "snippet": r.get("snippet", ""),
                    }
                    for r in results
                ],
                "total": len(results),
                "query": query,
            }
        except urllib.error.HTTPError as e:
            return {"error": f"HTTP {e.code}: {e.reason}"}
        except Exception as e:
            return {"error": str(e)}

    registry.register(ToolDef(
        name="web_search",
        description="Search the web using DuckDuckGo. Returns titles, URLs, and snippets.",
        handler=handle_web_search,
        parameters={
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Search query"},
                "num_results": {"type": "integer", "description": "Max results", "default": 5},
            },
            "required": ["query"],
        },
        examples=[
            "web_search(query='python asyncio tutorial')",
            "web_search(query='latest AI news', num_results=3)",
        ],
        category="web",
    ))

    # ── fetch_url ─────────────────────────────────────

    def handle_fetch_url(url: str, timeout: int = 30, **kwargs) -> dict:
        """Fetch a URL and return its content."""
        try:
            req = urllib.request.Request(
                url,
                headers={
                    "User-Agent": (
                        "Mozilla/5.0 (X11; Linux x86_64) "
                        "AppleWebKit/537.36 (KHTML, like Gecko) "
                        "Chrome/120.0.0.0 Safari/537.36"
                    ),
                },
            )
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                content = resp.read().decode("utf-8", errors="replace")
                return {
                    "result": content[:50000],  # Cap at 50KB
                    "status": resp.status,
                    "content_type": resp.headers.get("Content-Type", ""),
                    "truncated": len(content) > 50000,
                }
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", errors="replace")[:500]
            return {"error": f"HTTP {e.code}: {e.reason}", "body": body}
        except urllib.error.URLError as e:
            return {"error": f"URL error: {e.reason}"}
        except Exception as e:
            return {"error": str(e)}

    registry.register(ToolDef(
        name="fetch_url",
        description="Fetch the content of a URL. Returns up to 50KB of text.",
        handler=handle_fetch_url,
        parameters={
            "type": "object",
            "properties": {
                "url": {"type": "string", "description": "URL to fetch"},
                "timeout": {"type": "integer", "description": "Timeout in seconds", "default": 30},
            },
            "required": ["url"],
        },
        category="web",
    ))
