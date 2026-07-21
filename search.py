#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Smart-search CLI — direct invocation, no API server needed.

Usage:
    python search.py "搜索关键词" [--sources youtube,reddit,hacker_news] [--max 20] [--json]

Outputs structured JSON or formatted markdown to stdout.
"""

import argparse
import asyncio
import codecs
import json
import os
import sys

# Ensure project root is on path
PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, PROJECT_ROOT)
os.chdir(PROJECT_ROOT)

# UTF-8 output for Windows
sys.stdout = codecs.getwriter("utf-8")(sys.stdout.buffer, "replace")
sys.stderr = codecs.getwriter("utf-8")(sys.stderr.buffer, "replace")


def _sanitize(obj):
    """Remove surrogate pairs from strings."""
    if isinstance(obj, str):
        return obj.encode("utf-8", "ignore").decode("utf-8", "ignore")
    if isinstance(obj, dict):
        return {k: _sanitize(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_sanitize(v) for v in obj]
    return obj


async def run_search(query: str, sources: list[str] | None = None,
                     max_results: int = 20, all_sources: bool = False,
                     include_structured: bool = True, raw: bool = False,
                     freshness: str | None = None):
    """Run the search pipeline and return results dict."""
    from app.storage.sqlite_store import init_db
    from app.cache import cleanup_stale
    init_db()
    cleanup_stale()

    # Import the search function
    from app.api.search import search as search_endpoint
    from app.api.search import SearchRequest

    req = SearchRequest(
        query=query,
        sources=sources,
        all_sources=all_sources,
        max_results=max_results,
        include_structured=include_structured,
        raw=raw,
        freshness=freshness,
    )

    # Call the async search function directly
    result = await search_endpoint(req)

    # Clean up unused browser tabs after each search
    try:
        from app.sources.cdp_client import cleanup_unused_tabs
        await cleanup_unused_tabs()
    except Exception:
        pass

    return {
        "query": result.query,
        "summary": result.summary,
        "timeseries": result.timeseries,
        "metadata": result.metadata,
    }


def format_markdown(data: dict) -> str:
    """Format search results as readable markdown."""
    ranked = data.get("summary", {}).get("ranked_results", [])
    meta = data.get("metadata", {})
    fm = data.get("summary", {}).get("fusion_metadata", {})
    key_findings = data.get("summary", {}).get("key_findings", [])
    conflicts = data.get("summary", {}).get("conflicts", [])

    lines = []
    lines.append(f"搜索结果（共 {len(ranked)} 条，来自 {meta.get('sources_used', 0)} 个源，耗时 {meta.get('query_time_ms', 0)}ms）\n")

    for i, r in enumerate(ranked[:15], 1):
        source = r.get("source", "")
        title = r.get("title", "")[:100]
        eng = r.get("engagement", {})
        eng_parts = []
        for k in ("views", "upvotes", "reactions", "likes", "comments"):
            if eng.get(k):
                eng_parts.append(f"{k}={eng[k]:,}")
        eng_str = f" ({', '.join(eng_parts)})" if eng_parts else ""
        date = r.get("published_date", "")
        date_str = f" [{date}]" if date else ""
        content = r.get("content", "")[:200]

        lines.append(f"{i}. [{source}]{date_str} {title}{eng_str}")
        if content:
            lines.append(f"   {content}")
        url = r.get("url", "")
        if url:
            lines.append(f"   {url}")
        lines.append("")

    if key_findings:
        lines.append("关键发现：")
        for f in key_findings[:10]:
            if isinstance(f, dict):
                lines.append(f"  - [{f.get('confidence', 'Unknown')}][{f.get('verified', 'Unverified')}] {f.get('fact', '')}")
            else:
                lines.append(f"  - {f}")
        lines.append("")

    if conflicts:
        lines.append("数据冲突：")
        for c in conflicts[:5]:
            if isinstance(c, dict):
                lines.append(f"  - {c.get('entity', '')}: {c.get('conflict', '')}")
            else:
                lines.append(f"  - {c}")
        lines.append("")

    return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser(description="Smart-search CLI")
    parser.add_argument("query", help="Search query")
    parser.add_argument("--sources", help="Comma-separated source names (e.g. youtube,reddit,hacker_news)")
    parser.add_argument("--max", type=int, default=30, help="Max results (default: 30)")
    parser.add_argument("--all", action="store_true", help="Search all sources")
    parser.add_argument("--json", action="store_true", help="Output raw JSON")
    parser.add_argument("--raw", action="store_true", help="Skip LLM judge (faster, no relevance scoring)")
    parser.add_argument("--freshness", help="Only keep results newer than this window (e.g. 7d, 30d, 24h, or days as a number)")
    args = parser.parse_args()

    sources = None
    if args.sources:
        sources = [s.strip() for s in args.sources.split(",") if s.strip()]

    result = asyncio.run(run_search(
        query=args.query,
        sources=sources,
        max_results=args.max,
        all_sources=args.all,
        raw=args.raw,
        freshness=args.freshness,
    ))

    result = _sanitize(result)

    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        print(format_markdown(result))


if __name__ == "__main__":
    main()
