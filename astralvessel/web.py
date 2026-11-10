# The webfetch and websearch tools, which reach the internet only through the egress proxy.
# Results are reduced to readable text so the model sees content, not markup.

from __future__ import annotations

import html
import os
import re
from html.parser import HTMLParser
from typing import Any
from urllib.parse import parse_qs, unquote, urlparse

import httpx

MAX_BYTES = 4 * 1024 * 1024
USER_AGENT = "AstralVessel/0.1 (+https://github.com/EvanDamron/AstralVessel)"


class _Text(HTMLParser):
    SKIP = {"script", "style", "noscript", "svg", "head", "nav", "footer"}
    BLOCK = {"p", "div", "br", "li", "h1", "h2", "h3", "h4", "tr", "section", "article"}

    def __init__(self) -> None:
        super().__init__()
        self.parts: list[str] = []
        self.depth = 0

    def handle_starttag(self, tag: str, attrs: Any) -> None:
        if tag in self.SKIP:
            self.depth += 1
        elif tag in self.BLOCK:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in self.SKIP and self.depth:
            self.depth -= 1

    def handle_data(self, data: str) -> None:
        if not self.depth:
            self.parts.append(data)


def readable(markup: str) -> str:
    parser = _Text()
    parser.feed(markup)
    text = re.sub(r"[ \t]+", " ", "".join(parser.parts))
    return re.sub(r"\n\s*\n+", "\n\n", text).strip()


def _client() -> httpx.AsyncClient:
    proxy = os.environ.get("VESSEL_WEB_PROXY") or None
    return httpx.AsyncClient(proxy=proxy, timeout=httpx.Timeout(30, connect=10), follow_redirects=True,
                             max_redirects=5, headers={"User-Agent": USER_AGENT})


async def _get(url: str) -> tuple[str, str]:
    async with _client() as client, client.stream("GET", url) as response:
        response.raise_for_status()
        chunks, size = [], 0
        async for chunk in response.aiter_bytes():
            size += len(chunk)
            if size > MAX_BYTES:
                break
            chunks.append(chunk)
        return b"".join(chunks).decode(response.encoding or "utf-8", "replace"), response.headers.get(
            "content-type", "")


async def webfetch(args: dict[str, Any]) -> tuple[str, bool]:
    url = str(args.get("url", ""))
    if urlparse(url).scheme not in ("http", "https"):
        return "only http and https URLs can be fetched", True
    try:
        body, kind = await _get(url)
    except httpx.HTTPStatusError as exc:
        return f"the page returned HTTP {exc.response.status_code}", True
    except httpx.HTTPError:
        return "the page could not be fetched (blocked or unreachable)", True
    return (readable(body) if "html" in kind or "<html" in body[:500].lower() else body), False


async def websearch(args: dict[str, Any]) -> tuple[str, bool]:
    query = str(args.get("query", "")).strip()
    if not query:
        return "an empty query cannot be searched", True
    cap = min(max(int(args.get("max_results") or 8), 1), 20)
    try:
        async with _client() as client:
            response = await client.post("https://html.duckduckgo.com/html/", data={"q": query})
            response.raise_for_status()
    except httpx.HTTPError:
        return "the search service could not be reached", True
    results = []
    for block in re.findall(r'<div class="result results_links.*?</div>\s*</div>', response.text, re.DOTALL):
        link = re.search(r'class="result__a" href="([^"]+)"[^>]*>(.*?)</a>', block, re.DOTALL)
        if not link:
            continue
        href = html.unescape(link.group(1))
        if "uddg=" in href:
            href = unquote(parse_qs(urlparse(href).query).get("uddg", [href])[0])
        snippet = re.search(r'class="result__snippet"[^>]*>(.*?)</a>', block, re.DOTALL)
        clean = lambda s: html.unescape(re.sub(r"<[^>]+>", "", s)).strip()
        results.append(f"{clean(link.group(2))}\n{href}\n{clean(snippet.group(1)) if snippet else ''}".strip())
        if len(results) >= cap:
            break
    return ("\n\n".join(results) or "no results"), False
