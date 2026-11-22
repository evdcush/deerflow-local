"""DeerFlow ``web_search`` / ``web_fetch`` backed by Search Gateway 2.0.

The gateway (``http://search-gateway:8080``, project ``search-gateway``)
owns search (SearXNG), rendering (Crawl4AI), PDF parsing, cleaning,
caching and URL safety; these tools only call its JSON API and format
the reply for the model. Tool names stay ``web_search`` / ``web_fetch``
so DeerFlow's prompts and result middlewares apply to them unchanged.
Errors are returned as text starting with ``Error:`` (DeerFlow's tool
error convention), never raised.

Config (``config.yaml``, under each tool entry): ``base_url``,
``api_key`` (``$SEARCH_GATEWAY_API_KEY``), ``timeout`` (s),
``max_results`` (web_search), ``max_chars`` (web_fetch).

Tree
----
DEFAULT_BASE_URL                        str
DEFAULT_TIMEOUT                         float
settings(name) -> dict
call(cfg, path, body) -> dict | str                     async
format_results(reply) -> str
format_page(reply) -> str
search(query, time_range, cfg) -> str                   async
fetch(url, query, offset, cfg) -> str                   async
web_search_tool(query, time_range=None) -> str          async tool
web_fetch_tool(url, query=None, offset=0) -> str        async tool
"""
from __future__ import annotations

import json
from typing import Literal

import httpx

DEFAULT_BASE_URL = 'http://search-gateway:8080'
DEFAULT_TIMEOUT = 60.0


def settings(name: str) -> dict:
    """Extra keys of tool ``name`` in DeerFlow's config (``{}`` if unset)."""
    from deerflow.config import get_app_config
    config = get_app_config().get_tool_config(name)
    return dict(config.model_extra or {}) if config else {}


async def call(cfg: dict, path: str, body: dict) -> dict | str:
    """POST ``body`` to the gateway; the JSON reply or an ``Error:`` text."""
    key = cfg.get('api_key') or ''
    headers = {'Authorization': f'Bearer {key}'} if key else {}
    url = str(cfg.get('base_url') or DEFAULT_BASE_URL).rstrip('/') + path
    try:
        async with httpx.AsyncClient(
                timeout=float(cfg.get('timeout') or DEFAULT_TIMEOUT)) as c:
            res = await c.post(url, json=body, headers=headers)
    except httpx.HTTPError as e:
        return f'Error: search gateway unreachable: {e!r}'
    try:
        reply = res.json()
    except ValueError:
        reply = None
    if res.status_code != 200 or not isinstance(reply, dict):
        detail = reply.get('detail') if isinstance(reply, dict) else None
        return (f'Error: search gateway HTTP {res.status_code}: '
                f'{json.dumps(detail) if detail else res.text[:300]}')
    return reply


def format_results(reply: dict) -> str:
    """``/v2/search`` reply -> JSON list of ``{title, url, snippet}``."""
    results = [{'title': s.get('title', ''), 'url': s.get('url', ''),
                'snippet': s.get('snippet', '')}
               for s in reply.get('sources') or []]
    if not results:
        down = reply.get('unresponsive_engines') or []
        return json.dumps({'results': [], 'note': 'no results' + (
            f'; engines not responding: {down}' if down else '')})
    return json.dumps(results, indent=2, ensure_ascii=False)


def format_page(reply: dict) -> str:
    """``/v2/fetch`` reply -> Markdown with a short header and paging."""
    if reply.get('status') != 'success':
        err = reply.get('error') or {}
        return (f"Error: could not read {reply.get('url', 'the page')}: "
                f"{err.get('code', reply.get('status'))}: "
                f"{err.get('detail', '')}")
    head = [f"URL: {reply.get('final_url') or reply.get('url')}"]
    if reply.get('title'):
        head.append(f"Title: {reply['title']}")
    if reply.get('warnings'):
        head.append('Warnings: ' + ', '.join(reply['warnings']))
    total = reply.get('total_chars', 0)
    if 'excerpts' in reply:
        head.append(f"{len(reply['excerpts'])} excerpts matching the "
                    f'query from {total} characters. To read on from one, '
                    'call web_fetch again with offset=<its offset> and no '
                    'query.')
        body = '\n\n…\n\n'.join(f"[offset {e['offset']}]\n{e['text']}"
                                for e in reply['excerpts'])
    else:
        start = reply.get('offset') or 0
        body = reply.get('content', '')
        head.append(f'Characters {start}-{start + len(body)} of {total}.')
        if reply.get('next_offset') is not None:
            body += ('\n\n[more: call web_fetch again with '
                     f"offset={reply['next_offset']}]")
    return '\n'.join(head) + '\n\n' + body


async def search(query: str, time_range: str | None, cfg: dict) -> str:
    """Search via ``POST /v2/search`` (snippets only; fast)."""
    body = {'query': query, 'fetch_top': 0,
            'max_results': int(cfg.get('max_results') or 8)}
    if time_range:
        body['time_range'] = time_range
    reply = await call(cfg, '/v2/search', body)
    return reply if isinstance(reply, str) else format_results(reply)


async def fetch(url: str, query: str | None, offset: int, cfg: dict) -> str:
    """Read one URL via ``POST /v2/fetch``."""
    body = {'url': url, 'offset': max(0, int(offset)),
            'max_chars': int(cfg.get('max_chars') or 12000)}
    if query:
        body['query'] = query
    reply = await call(cfg, '/v2/fetch', body)
    return reply if isinstance(reply, str) else format_page(reply)


try:  # the tool wrappers need DeerFlow's runtime (langchain)
    from langchain.tools import tool
except ImportError:  # unit tests import the helpers only
    tool = None

if tool is not None:
    @tool('web_search', parse_docstring=True)
    async def web_search_tool(
            query: str,
            time_range: Literal['day', 'week', 'month', 'year'] | None = None
    ) -> str:
        """Search the web (self-hosted SearXNG via the search gateway).

        Returns a JSON list of {title, url, snippet}. Read promising
        results in full with web_fetch.

        Args:
            query: The query to search for.
            time_range: Optional publication window. Use only when the
                request needs recent results.
        """
        return await search(query, time_range, settings('web_search'))

    @tool('web_fetch', parse_docstring=True)
    async def web_fetch_tool(url: str, query: str | None = None,
                             offset: int = 0) -> str:
        """Read a web page or PDF as clean Markdown.

        Only fetch EXACT URLs given by the user or returned by web_search
        or web_fetch. URLs must include the scheme (https://...). Long
        documents are paged: continue with the offset shown in the
        result. Pass query to get only the passages relevant to it.

        Args:
            url: Absolute http(s) URL to read.
            query: Optional: return only passages relevant to this.
            offset: Character offset to continue reading from.
        """
        return await fetch(url, query, offset, settings('web_fetch'))
