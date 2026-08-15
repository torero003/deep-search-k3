#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Smart-search CLI — direct invocation, no API server needed.

Usage:
    python search.py "搜索关键词" [--sources youtube,reddit,hacker_news] [--max 20] [--json]
    python search.py --batch tasks.json [--concurrency 6] [--tab-budget 12]

Batch mode (2026-08): one process runs many queries concurrently. Compared to
spawning one process per query this pays startup once, shares the login-state
cache across queries, shares a process-wide CDP tab budget, and skips the
optional stages (thin-retry / entity / transcript) by default — the stages
whose value is re-done by downstream LLM reading anyway. Use --batch-full to
keep all enhancements.
Outputs structured JSON or formatted markdown to stdout.
"""

import argparse
import asyncio
import codecs
import json
import os
import sys
import time

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


def _init_once():
    from app.storage.sqlite_store import init_db
    from app.cache import cleanup_stale
    init_db()
    cleanup_stale()


def _build_request(args, query: str, task: dict | None = None):
    from app.api.search import SearchRequest

    task = task or {}
    sources = task.get("sources")
    if sources is None and args.sources:
        sources = [s.strip() for s in args.sources.split(",") if s.strip()]
    lite = args.batch and not args.batch_full  # batch 默认精简增强阶段
    return SearchRequest(
        query=query,
        sources=sources,
        all_sources=bool(task.get("all", args.all)),
        max_results=int(task.get("max", args.max)),
        include_structured=True,
        raw=args.raw,
        freshness=task.get("freshness", args.freshness),
        no_retry=lite or args.no_retry,
        no_entity=lite or args.no_entity,
        no_transcript=lite or args.no_transcript,
        source_timeout=args.source_timeout,
    )


async def run_search(query: str, sources: list[str] | None = None,
                     max_results: int = 20, all_sources: bool = False,
                     include_structured: bool = True, raw: bool = True,
                     freshness: str | None = None,
                     no_retry: bool = False, no_entity: bool = False,
                     no_transcript: bool = False, source_timeout: float = 30.0):
    """Run the search pipeline and return results dict."""
    _init_once()

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
        no_retry=no_retry,
        no_entity=no_entity,
        no_transcript=no_transcript,
        source_timeout=source_timeout,
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


# ---------------------------------------------------------------------------
# Batch mode
# ---------------------------------------------------------------------------

def _valid_output(path: str) -> bool:
    """output 已存在且是合法非空 JSON（断点续跑判断）。"""
    if not path or not os.path.exists(path) or os.path.getsize(path) < 100:
        return False
    try:
        with open(path, encoding="utf-8") as f:
            d = json.load(f)
        return len(d.get("summary", {}).get("ranked_results", [])) > 0
    except Exception:
        return False


async def run_batch(tasks: list[dict], args) -> dict:
    """Run many queries concurrently in ONE process.

    Wins vs process-per-query: startup paid once, login-state cache shared,
    one process-wide CDP tab budget, optional stages skipped by default.
    """
    from app.api.search import search as search_endpoint, set_source_semaphore

    _init_once()
    set_source_semaphore(asyncio.Semaphore(args.tab_budget))
    query_sem = asyncio.Semaphore(args.concurrency)

    async def one(task: dict) -> dict:
        out = task.get("output", "")
        label = f"{task.get('sector', '')}/{task.get('search_id', '')}".strip("/") or task["query"][:30]
        if _valid_output(out):
            return {**task, "status": "skipped", "elapsed": 0}
        async with query_sem:
            t0 = time.time()
            try:
                req = _build_request(args, task["query"], task)
                result = await asyncio.wait_for(search_endpoint(req), timeout=args.batch_timeout)
                data = _sanitize({
                    "query": result.query,
                    "summary": result.summary,
                    "timeseries": result.timeseries,
                    "metadata": result.metadata,
                })
                n = len(result.summary.get("ranked_results", []))
                if out:
                    os.makedirs(os.path.dirname(os.path.abspath(out)), exist_ok=True)
                    with open(out, "w", encoding="utf-8") as f:
                        json.dump(data, f, ensure_ascii=False)
                try:
                    from app.sources.cdp_client import cleanup_unused_tabs
                    await cleanup_unused_tabs()
                except Exception:
                    pass
                print(f"[batch] OK {label} ({time.time()-t0:.1f}s, {n} results)", file=sys.stderr)
                return {**task, "status": "ok", "elapsed": round(time.time() - t0, 1), "results": n}
            except Exception as e:
                print(f"[batch] FAIL {label}: {e}", file=sys.stderr)
                return {**task, "status": "failed", "elapsed": round(time.time() - t0, 1),
                        "error": str(e)[:300]}

    results = await asyncio.gather(*(one(t) for t in tasks))
    ok = sum(1 for r in results if r["status"] == "ok")
    skipped = sum(1 for r in results if r["status"] == "skipped")
    failed = [r for r in results if r["status"] == "failed"]
    return {
        "total": len(results), "ok": ok, "skipped": skipped,
        "failed_count": len(failed),
        "failed": [{"sector": r.get("sector"), "search_id": r.get("search_id"),
                    "query": r.get("query"), "error": r.get("error")} for r in failed],
        "tasks": [{k: r.get(k) for k in ("sector", "search_id", "query", "output",
                                         "status", "elapsed", "results")} for r in results],
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
    parser.add_argument("query", nargs="?", help="Search query (omit when --batch)")
    parser.add_argument("--sources", help="Comma-separated source names (e.g. youtube,reddit,hacker_news)")
    parser.add_argument("--max", type=int, default=30, help="Max results (default: 30)")
    parser.add_argument("--all", action="store_true", help="Search all sources")
    parser.add_argument("--json", action="store_true", help="Output raw JSON")
    parser.add_argument("--raw", action="store_true",
                        help="Skip LLM judge (DEFAULT since 2026-07; kept for backward compat)")
    parser.add_argument("--judge", dest="raw", action="store_false",
                        help="Enable LLM judge re-ranking (adds 25-74s; useful for single ad-hoc searches)")
    parser.set_defaults(raw=True)
    parser.add_argument("--freshness", help="Only keep results newer than this window (e.g. 7d, 30d, 24h, or days as a number)")
    # ── 加速开关（单搜也可用；默认全关，行为与旧版一致）──
    parser.add_argument("--no-retry", action="store_true",
                        help="Skip thin-source second-round retry (saves ~20-30s)")
    parser.add_argument("--no-entity", action="store_true",
                        help="Skip entity phase-2 google search in --all mode (saves ~15-25s)")
    parser.add_argument("--no-transcript", action="store_true",
                        help="Skip video transcript enrichment (saves ~30s, tech queries only)")
    parser.add_argument("--source-timeout", type=float, default=30.0,
                        help="Per-source timeout seconds (default 30; lower for batch)")
    # ── 批量模式 ──
    parser.add_argument("--batch", metavar="TASKS.json",
                        help='Batch mode: JSON array of {"query": ..., "output": ..., "sector"/"search_id" optional}')
    parser.add_argument("--concurrency", type=int, default=6,
                        help="Batch: concurrent queries (default 6)")
    parser.add_argument("--tab-budget", type=int, default=12,
                        help="Batch: process-wide CDP tab budget shared by all queries (default 12)")
    parser.add_argument("--batch-timeout", type=float, default=300.0,
                        help="Batch: per-query timeout seconds (default 300)")
    parser.add_argument("--batch-full", action="store_true",
                        help="Batch: keep thin-retry/entity/transcript stages (default: skipped)")
    args = parser.parse_args()

    if args.batch:
        with open(args.batch, encoding="utf-8") as f:
            tasks = json.load(f)
        t0 = time.time()
        manifest = asyncio.run(run_batch(tasks, args))
        manifest["total_elapsed"] = round(time.time() - t0, 1)
        print(json.dumps(manifest, ensure_ascii=False, indent=2))
        return

    if not args.query:
        parser.error("query is required unless --batch is given")

    result = asyncio.run(run_search(
        query=args.query,
        sources=[s.strip() for s in args.sources.split(",") if s.strip()] if args.sources else None,
        max_results=args.max,
        all_sources=args.all,
        raw=args.raw,
        freshness=args.freshness,
        no_retry=args.no_retry,
        no_entity=args.no_entity,
        no_transcript=args.no_transcript,
        source_timeout=args.source_timeout,
    ))

    result = _sanitize(result)

    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        print(format_markdown(result))


if __name__ == "__main__":
    main()
