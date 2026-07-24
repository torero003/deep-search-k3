# coding: utf-8
import asyncio
import logging
import re
import time
from collections import Counter
from urllib.parse import quote
from fastapi import APIRouter
from pydantic import BaseModel, Field
from typing import Optional
from app.config import detect_intent, detect_search_intent, get_sources_for_category, get_sources_for_intent, SOURCES, get_all_source_names, has_intent_modifier, strip_intent_modifiers, get_excluded_sources_for_domain, _ALWAYS_ON_SOURCES
from app.sources.base import SearchResult
from app.sources.edge_mcp_source import EdgeMCPSource, LOGIN_CHECKS, SEARCH_TEMPLATES
from app.validator import summarize_results, SOURCE_WEIGHT
from app.storage.sqlite_store import save_search_result, update_source_health

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/search", tags=["search"])

# Sources that only work well with short keyword queries
_COMMUNITY_SOURCES = {"zhihu", "xueqiu", "twitter", "v2ex"}
_MAX_QUERY_LEN = 30


def _normalize_query(query: str) -> str:
    """Normalize search query for better cross-source matching.

    Inserts space between letters and numbers (e.g., 'mythos5' → 'mythos 5'),
    improving YouTube and other search engines' ability to match results.
    """
    import re
    # Insert space between letter and digit: 'mythos5' → 'mythos 5', 'GPT5.5' → 'GPT 5.5'
    query = re.sub(r'([a-zA-Z])(\d)', r'\1 \2', query)
    # Collapse multiple spaces
    query = re.sub(r'\s+', ' ', query).strip()
    return query


def _shorten_query(query: str) -> str:
    """Fallback: truncate long queries."""
    if len(query) <= _MAX_QUERY_LEN:
        return query
    return query[:_MAX_QUERY_LEN]


def _parse_freshness_days(freshness: str) -> float | None:
    """Parse freshness window into days. Accepts '7d', '30d', '24h' or a bare
    number (interpreted as days). Returns None if unparseable."""
    if not freshness:
        return None
    s = str(freshness).strip().lower()
    m = re.match(r'^(\d+(?:\.\d+)?)\s*([dh]?)$', s)
    if not m:
        return None
    value = float(m.group(1))
    unit = m.group(2) or 'd'
    return value / 24.0 if unit == 'h' else value


def _parse_result_date(date_str: str):
    """Parse a published_date like 'YYYY-MM-DD' or 'YYYY-MM-DD HH:MM'.
    Returns a datetime, or None if unparseable (treated as no date)."""
    from datetime import datetime
    m = re.match(r'^\s*(\d{4})-(\d{1,2})-(\d{1,2})(?:\s+(\d{1,2}):(\d{1,2}))?', date_str or '')
    if not m:
        return None
    try:
        return datetime(
            int(m.group(1)), int(m.group(2)), int(m.group(3)),
            int(m.group(4) or 0), int(m.group(5) or 0),
        )
    except ValueError:
        return None


async def _extract_keywords_for_community(query: str) -> str:
    """Extract concise keywords for community sources (zhihu, xueqiu, twitter, v2ex).
    These sources fail on long queries — need short, focused keywords under 15 chars."""
    if len(query) <= 15:
        return query
    # Deterministic fallback: remove stop words, keep spaces between English words
    import re
    stops = {"对于", "的", "中", "在", "会", "是", "什么", "哪", "哪些", "为什么",
             "如何", "怎么", "还有", "吗", "呢", "吧", "了", "着", "过", "和",
             "与", "或", "但", "却", "而", "已", "最", "很", "非常", "超级",
             "行业", "板块", "请问", "大家", "觉得", "认为", "看好", "期待",
             "最近", "这两天", "目前", "现在", "当前", "其他", "还", "也",
             "使用", "适合", "更好", "比较", "对比", "分析", "推荐", "选择"}
    text = query
    for sw in sorted(stops, key=len, reverse=True):
        if len(sw) >= 2:
            text = text.replace(sw, '')
    # Keep alphanumeric, Chinese chars, and spaces — collapse whitespace
    text = re.sub(r'[^\w\s]', '', text).strip()
    text = re.sub(r'\s+', ' ', text).strip()
    return text[:15]


# ---------------------------------------------------------------------------
# Core subject extraction for thin retry
# ---------------------------------------------------------------------------

_CORE_NOISE_ZH = {
    "的", "中", "在", "会", "是", "什么", "哪", "哪些", "为什么",
    "如何", "怎么", "还有", "吗", "呢", "吧", "了", "着", "过", "和",
    "与", "或", "但", "却", "而", "已", "最", "很", "非常", "超级",
    "行业", "板块", "请问", "大家", "觉得", "认为", "看好", "期待",
    "最近", "这两天", "目前", "现在", "当前", "其他", "还", "也",
    "使用", "适合", "更好", "比较", "对比", "分析", "推荐", "选择",
    "评测", "评价", "教程", "指南", "入门", "实战", "经验",
}


def _extract_core_subject(topic: str) -> str:
    """Extract core subject from a query for thin source retry.
    Strips noise words, returns compact keyword string."""
    if len(topic) <= 15:
        return topic
    text = topic
    for sw in sorted(_CORE_NOISE_ZH, key=len, reverse=True):
        if len(sw) >= 2:
            text = text.replace(sw, '')
    text = re.sub(r'[^\w\s]', '', text).strip()
    text = re.sub(r'\s+', ' ', text).strip()
    return text[:20] or topic[:20]


def _extract_english_keywords(query: str) -> str:
    """Extract English keywords from a (possibly mixed) query for Twitter search.
    Twitter is primarily English; Chinese queries return poor results.
    Returns English words/acronyms from the query, or empty string if none found."""
    # Find English words (2+ letters) and standalone numbers
    english = re.findall(r'[A-Za-z]{2,}|\d+', query)
    # Filter out common noise
    noise = {"the", "and", "for", "with", "that", "this", "from", "are", "was",
             "but", "not", "you", "all", "can", "had", "her", "was", "one",
             "our", "out", "has", "have", "been", "do", "if", "it", "no",
             "so", "up", "or", "as", "at", "by", "on", "to", "is", "in", "of"}
    english = [w for w in english if w.lower() not in noise]
    return ' '.join(english)


# ---------------------------------------------------------------------------
# Parallel search — one source per new tab
# ---------------------------------------------------------------------------

_SANITIZE_TEXT = lambda t: t.encode('utf-8', 'ignore').decode('utf-8', errors='ignore') if isinstance(t, str) else t


async def _search_one_parallel_limited(source_name: str, query: str, community_kw: str | None, semaphore: asyncio.Semaphore):
    """Semaphore-wrapped version of _search_one_parallel to limit concurrent CDP connections."""
    async with semaphore:
        try:
            return await asyncio.wait_for(
                _search_one_parallel(source_name, query, community_kw),
                timeout=30.0,
            )
        except asyncio.TimeoutError:
            logger.warning(f"{source_name}: timed out after 30s in parallel search")
            return (source_name, [], False)


async def _search_one_parallel(source_name: str, query: str, community_kw: str | None):
    """Single-source parallel search. Routes to API or CDP based on source type."""
    actual_query = query
    if source_name == "twitter":
        # Twitter: prefer English keywords for better results
        en_kw = _extract_english_keywords(query)
        if en_kw:
            actual_query = en_kw
        elif community_kw:
            actual_query = community_kw
    elif source_name in _COMMUNITY_SOURCES and community_kw:
        actual_query = community_kw

    # Cache check
    from app.cache import get_cached_results, put_cached_results
    cached = get_cached_results(actual_query, source_name)
    if cached is not None:
        return (source_name, [SearchResult(**r) for r in cached], True)

    # Route based on source type
    source_type = SOURCES.get(source_name, {}).get("type", "edge_mcp")
    logger.error(f"DEBUG _search_one_parallel({source_name}): source_type={source_type} SOURCES_entry={SOURCES.get(source_name, {})}")

    if source_type == "api":
        # Direct API call, no CDP needed
        try:
            from app.sources.api_source import get_api_source
            src = get_api_source(source_name)
            if not src:
                logger.warning(f"{source_name}: unknown API source")
                return (source_name, [], False)
            results = await src.search(actual_query)
            dicts = [r.to_dict() for r in results]
            put_cached_results(actual_query, source_name, dicts)
            return (source_name, results, True)
        except Exception as e:
            logger.error(f"_search_one_parallel({source_name}): {e}")
            return (source_name, [], False)

    # CDP-based search (edge_mcp)
    from app.sources import cdp_client
    from app.sources.cdp_client import SPA_WAIT_TIMES
    from app.sources.edge_mcp_source import BROKEN_SOURCES, EdgeMCPSource

    if source_name in BROKEN_SOURCES:
        logger.debug(f"{source_name}: broken source, skipping")
        return (source_name, [], False)

    # Login check with recovery for sources that require authentication
    if source_name in LOGIN_CHECKS:
        logged_in = await EdgeMCPSource.check_login(source_name)
        if logged_in is False:
            logger.warning(f"{source_name}: login check failed, attempting recovery")
            src = EdgeMCPSource(source_name)
            src._clear_login_cache()
            recovered = await src._attempt_login_recovery()
            if not recovered:
                logger.warning(f"{source_name}: login recovery failed in parallel search")
                return (source_name, [], False)
            logger.info(f"{source_name}: login recovery successful in parallel search")
        elif logged_in is None:
            logger.warning(f"{source_name}: login status unknown, proceeding anyway")

    conn = await cdp_client.create_parallel_connection(source_name)
    try:
        url = SEARCH_TEMPLATES.get(source_name, SEARCH_TEMPLATES["google"]).format(
            query=quote(actual_query))
        # v2ex, YouTube, Bilibili need longer wait for SPA rendering
        wait_time = SPA_WAIT_TIMES.get(source_name, 5.0 if source_name in LOGIN_CHECKS else 3.0)
        structured = await cdp_client.search_url(
            url, wait=wait_time,
            source_name=source_name, cdp=conn[0])
        results = [SearchResult(
            title=_SANITIZE_TEXT(r.get("title", "")),
            url=_SANITIZE_TEXT(r.get("url", "")),
            content=_SANITIZE_TEXT(r.get("snippet", "")),
            source=source_name,
            published_date=_SANITIZE_TEXT(r.get("published_date", ""))
        ) for r in structured if r.get("title") and r.get("url")]
        dicts = [r.to_dict() for r in results]
        put_cached_results(actual_query, source_name, dicts)
        return (source_name, results, True)
    except Exception as e:
        logger.error(f"_search_one_parallel({source_name}): {e}")
        return (source_name, [], False)
    finally:
        await cdp_client.close_parallel_connection(conn)


# ---------------------------------------------------------------------------
# Thin source retry
# ---------------------------------------------------------------------------

async def _retry_thin_sources(
    results_by_source: dict, sources_failed: list,
    valid_sources: list, topic: str, community_kw: str | None,
):
    """Retry sources with <3 results using simplified core keywords."""
    core = _extract_core_subject(topic)
    if not core or core == topic[:_MAX_QUERY_LEN]:
        return {}

    # Exclude broken/slow CDP sources from retry to save time
    from app.sources.edge_mcp_source import BROKEN_SOURCES
    _slow_cdp = {"trendforce", "yandex", "stats_gov"}  # known slow/broken
    thin = [
        s for s in valid_sources
        if s not in sources_failed
        and s not in BROKEN_SOURCES
        and s not in _slow_cdp
        and len(results_by_source.get(s, [])) < 3
    ]
    if not thin:
        return {}

    logger.info(f"[retry] thin sources: {thin}, core='{core}'")
    retry_tasks = [
        asyncio.create_task(_search_one_parallel(s, core, None))
        for s in thin
    ]
    retry_results = await asyncio.gather(*retry_tasks, return_exceptions=True)

    extra = {}
    for result in retry_results:
        if isinstance(result, Exception):
            continue
        sname, results, success = result
        if not success or not results:
            continue
        existing_urls = {r.url for r in results_by_source.get(sname, []) if r.url}
        new = [r for r in results if r.url not in existing_urls]
        if new:
            extra[sname] = new
    return extra


# ---------------------------------------------------------------------------
# Entity extraction for Phase 2 search
# ---------------------------------------------------------------------------

_ENTITY_GENERIC = {
    "elonmusk", "openai", "google", "microsoft", "apple", "meta",
    "the", "a", "an", "is", "are", "was", "were",
}


def _extract_entities_from_results(all_results: list[SearchResult]) -> list[str]:
    """Extract high-frequency entities from search results for targeted re-search."""
    entity_counts = Counter()
    for r in all_results:
        text = f"{r.title} {r.content}"
        mentions = re.findall(r'@([A-Za-z0-9_]{2,20})', text)
        for m in mentions:
            entity_counts[m] += 1
        cjk_seqs = re.findall(r'[一-鿿]{3,8}', text)
        for seq in cjk_seqs:
            entity_counts[seq] += 1
        title_phrases = re.findall(r'(?:[A-Z][a-z]+\s+){1,3}[A-Z][a-z]+', text)
        for phrase in title_phrases:
            entity_counts[phrase.strip()] += 1
    return [
        e for e, count in entity_counts.most_common(5)
        if e.lower() not in _ENTITY_GENERIC and count >= 2
    ]


class SearchRequest(BaseModel):
    query: str = Field(..., min_length=1, max_length=500)
    category: Optional[str] = None
    sources: Optional[list[str]] = None
    all_sources: bool = False
    max_results: int = Field(default=30, ge=1, le=100)
    include_structured: bool = True
    # LLM judge 默认关闭（2026-07 起）：批量场景 judge 每次 25-74s 且与下游
    # subagent 精读重复，RRF 排序直接够用。需要时显式 raw=False 开启。
    raw: bool = True
    freshness: Optional[str] = None
    language: str = "zh"


class SearchResponse(BaseModel):
    query: str
    summary: dict
    timeseries: list[dict] = []
    metadata: dict


async def _search_one_source(source_name: str, query: str, community_kw: str | None = None) -> tuple[str, list[SearchResult], bool]:
    """Search a single source with timeout. Returns (name, results, success).
    Checks cache first. If cache miss, searches and stores results.
    If community_kw is provided and source is a community source, use it instead of query.
    Twitter prefers English keywords for better results.
    Routes to API sources or EdgeMCP based on source type.
    """
    actual_query = query
    if source_name == "twitter":
        en_kw = _extract_english_keywords(query)
        if en_kw:
            actual_query = en_kw
        elif community_kw:
            actual_query = community_kw
    elif source_name in _COMMUNITY_SOURCES and community_kw:
        actual_query = community_kw

    # ── Cache check ──
    from app.cache import get_cached_results, put_cached_results
    cached = get_cached_results(actual_query, source_name)
    if cached is not None:
        logger.info(f"{source_name}: cache hit for '{actual_query[:30]}'")
        results = [SearchResult(**r) for r in cached]
        return (source_name, results, True)

    # ── Determine source type and dispatch ──
    source_type = SOURCES.get(source_name, {}).get("type", "edge_mcp")

    try:
        if source_type == "api":
            from app.sources.api_source import get_api_source
            src = get_api_source(source_name)
            if not src:
                logger.warning(f"{source_name}: unknown API source")
                return (source_name, [], False)
            results = await asyncio.wait_for(src.search(actual_query), timeout=30.0)
        else:
            src = EdgeMCPSource(source_name)
            results = await asyncio.wait_for(src.search(actual_query), timeout=30.0)

        # Store in cache
        dicts = [r.to_dict() for r in results]
        put_cached_results(actual_query, source_name, dicts)
        return (source_name, results, True)
    except asyncio.TimeoutError:
        logger.warning(f"{source_name}: search timed out after 30s")
        return (source_name, [], False)
    except Exception as e:
        logger.warning(f"{source_name}: search error: {e}")
        return (source_name, [], False)


@router.post("", response_model=SearchResponse)
async def search(req: SearchRequest):
    start = time.time()
    _timing = {}
    def _t(label):
        now = time.time()
        _timing[label] = round((now - start) * 1000)
    category = req.category or detect_intent(req.query)
    search_intent = detect_search_intent(req.query)

    # Source selection: explicit list > all_sources > intent-based
    if req.sources:
        source_names = req.sources
    elif req.all_sources:
        # all_sources: combine intent-based sources with all search engines + general sources
        # But filter out sources irrelevant to the detected intent (e.g., Hacker News for A股)
        all_names = get_all_source_names()
        # Filter: only include sources that match the detected category or intent,
        # plus always include general search engines (google, bing)
        always_include = {"google", "bing"} | _ALWAYS_ON_SOURCES
        category_sources = set(get_sources_for_category(category))
        intent_sources_set = set(get_sources_for_intent(search_intent))
        relevant = always_include | category_sources | intent_sources_set
        source_names = [s for s in all_names if s in relevant]
    else:
        # Combine domain category sources with search intent sources
        domain_sources = get_sources_for_category(category)
        intent_sources = get_sources_for_intent(search_intent)
        source_names = list(dict.fromkeys(domain_sources + intent_sources))  # dedup preserving order

    # Category-based exclusion: skip sources irrelevant to detected domain
   # (only when sources were auto-selected, not when user explicitly specified them)
    if not req.sources:
        excluded = get_excluded_sources_for_domain(category)
        if excluded:
            before = len(source_names)
            source_names = [s for s in source_names if s not in excluded]
            if before != len(source_names):
                logger.info(f"category={category}: excluded {before - len(source_names)} sources: {[s for s in source_names if s in excluded]}")

    # Always include V2EX as a general knowledge source
    # (only for auto-selected sources — an explicit --sources list is honored as-is;
    #  youtube/bilibili enter via INTENT_MAP for tech/ai domains, not here)
    if not req.sources:
        for s in _ALWAYS_ON_SOURCES:
            if s not in source_names and SOURCES.get(s):
                source_names.append(s)
        if _ALWAYS_ON_SOURCES & set(source_names[-len(_ALWAYS_ON_SOURCES):]):
            logger.info(f"always-on sources added: {_ALWAYS_ON_SOURCES}")

    # Filter to valid sources
    valid_sources = [s for s in source_names if SOURCES.get(s)]
    logger.info(f"search: {len(valid_sources)} sources for '{req.query[:40]}': {[s for s in valid_sources]} (domain={category}, intent={search_intent})")

    # Normalize query (insert spaces between letters and numbers) then shorten
    search_query = _shorten_query(_normalize_query(req.query))

    # Extract concise keywords for community sources (zhihu, xueqiu, twitter, v2ex)
    # These sources fail on long queries — need short, focused keywords
    # Normalize first so "mythos5" → "mythos 5" before keyword extraction
    has_community = any(s in _COMMUNITY_SOURCES for s in valid_sources)
    community_kw = await _extract_keywords_for_community(_normalize_query(req.query)) if has_community else None

    # ── Parallel search: each source gets its own tab ──
    # Concurrency limit: max 6 sources simultaneously per search command.
    # Each source creates a CDP connection + browser tab. Without limit,
    # 12+ sources × multiple concurrent search commands = 300+ tabs → Chromium stalls.
    _source_semaphore = asyncio.Semaphore(6)
    tasks = []
    for source_name in valid_sources:
        tasks.append(asyncio.create_task(
            _search_one_parallel_limited(source_name, search_query, community_kw, _source_semaphore)))

    raw_results = await asyncio.gather(*tasks, return_exceptions=True)
    _t("parallel_search_done")

    all_results: list[SearchResult] = []
    sources_used = set()
    sources_failed = []
    results_by_source: dict[str, list[SearchResult]] = {}

    for result in raw_results:
        if isinstance(result, BaseException):
            logger.error(f"Search task failed: {type(result).__name__}: {result}")
            continue
        sname, results, success = result
        if not success:
            sources_failed.append(sname)
            await update_source_health(sname, False)
            continue
        results_by_source[sname] = results
        all_results.extend(results)
        if results:
            sources_used.add(sname)
            await update_source_health(sname, True)
        elif sname in LOGIN_CHECKS:
            logger.warning(f"{sname}: login required but returned 0 results, possible session expiry")
            sources_failed.append(sname)
            await update_source_health(sname, False)

    # ── Thin source retry ──
    extra = await _retry_thin_sources(
        results_by_source, sources_failed, valid_sources, req.query, community_kw)
    _t("thin_retry_done")
    for sname, new_results in extra.items():
        results_by_source.setdefault(sname, []).extend(new_results)
        all_results.extend(new_results)
        sources_used.add(sname)

    # ── Entity-based Phase 2 search (all_sources mode only) ──
    # Reduced from 3 to 1 entity search to save CDP time (each = 15-45s)
    if req.all_sources and len(all_results) > 5:
        entities = _extract_entities_from_results(all_results)
        if entities:
            logger.info(f"[entity-search] entities: {entities}")
            entity_tasks = [
                asyncio.create_task(_search_one_parallel("google", entities[0], None))
            ]
            entity_results = await asyncio.gather(*entity_tasks, return_exceptions=True)
            for result in entity_results:
                if isinstance(result, Exception):
                    continue
                es_name, es_results, es_success = result
                if es_success and es_results:
                    existing_urls = {r.url for r in all_results if r.url}
                    es_new = [r for r in es_results if r.url not in existing_urls]
                    if es_new:
                        results_by_source.setdefault(es_name, []).extend(es_new)
                        all_results.extend(es_new)
            _t("entity_search_done")

    # Save ALL results to storage (before truncation)
    for r in all_results:
        await save_search_result(req.query, r.source, r.url, r.title, r.content, r.score, category)

    # ── Video transcript enrichment (after all searches complete) ──
    for video_source in ('youtube', 'bilibili'):
        video_results = results_by_source.get(video_source, [])
        if video_results:
            try:
                from app.sources.video_transcript import enrich_results_with_transcripts
                logger.info(f"Starting transcript enrichment for {video_source}: {len(video_results)} results")
                dicts = [r.to_dict() for r in video_results]
                await enrich_results_with_transcripts(dicts, video_source, max_videos=3)
                # Update content with enriched snippets
                enriched = 0
                for r, d in zip(video_results, dicts):
                    if d.get('content') and d['content'] != r.content:
                        r.content = d['content']
                        enriched += 1
                logger.info(f"Transcript enrichment for {video_source}: {enriched} results updated")
            except Exception as e:
                logger.warning(f"transcript enrichment for {video_source}: {e}")
    _t("transcript_done")

    # Process results with full data for cross-validation
    results_dict_all = [r.to_dict() for r in all_results]
    summary = summarize_results(results_dict_all, category) if req.include_structured else {
        "results": results_dict_all[:req.max_results],
        "total_results": len(results_dict_all),
        "sources_used": len(sources_used),
    }
    _t("crossval_done")

    # ── RRF Fusion ──
    from app.fusion import rrf_fuse

    # Build source weights from SOURCE_WEIGHT (higher = more trusted)
    fusion_weights = {src: SOURCE_WEIGHT.get(src, 1) for src in results_by_source}

    # Convert SearchResult lists to dict lists for fusion
    dicts_by_source = {
        src: [r.to_dict() for r in results]
        for src, results in results_by_source.items()
    }

    total_before = sum(len(v) for v in dicts_by_source.values())
    ranked = rrf_fuse(dicts_by_source, fusion_weights, query=req.query, max_results=req.max_results * 3)
    duplicates_removed = total_before - len(ranked) - sum(1 for r in ranked if r.get("_rrf_score", 0) == 0)

    # Demote results with no content — code snippets without context are low value
    for r in ranked:
        if not r.get('content', '').strip():
            r['_rrf_score'] = r.get('_rrf_score', 0) * 0.3

    # ── Freshness filter (after fusion, before judge) ──
    # Drop results whose published_date parses and is older than the cutoff;
    # results without a parseable date are kept.
    freshness_filtered = 0
    freshness_days = _parse_freshness_days(req.freshness) if req.freshness else None
    if freshness_days is not None:
        from datetime import datetime, timedelta
        cutoff = datetime.now() - timedelta(days=freshness_days)
        kept = []
        for r in ranked:
            dt = _parse_result_date(r.get('published_date', ''))
            if dt is not None and dt < cutoff:
                freshness_filtered += 1
                continue
            kept.append(r)
        ranked = kept
        if freshness_filtered:
            logger.info(f"freshness={req.freshness}: filtered {freshness_filtered} stale results")

    # ── LLM Judge Scoring (skip in --raw mode) ──
    if not req.raw:
        from app.judge import judge_relevance, apply_judge_ranking

        ranked = await judge_relevance(req.query, ranked)
        apply_judge_ranking(ranked)
    _t("judge_done")

    # Boost results with transcript content — video subtitles provide actual content
    # rather than just metadata, making them more valuable than typical search results
    for r in ranked:
        if 'Transcript:' in r.get('content', ''):
            r['_judge_score'] = r.get('_judge_score', 0) * 2.0

    # Clean internal fields from ranked results
    for r in ranked:
        r.pop("_rrf_score", None)
        r.pop("_rank", None)
        r.pop("_sources", None)
        r.setdefault("published_date", "")
        # Keep judge_score in output for transparency
        # r["_judge_score"] is kept as "judge_score"
        if "_judge_score" in r:
            r["judge_score"] = r.pop("_judge_score")

    fusion_metadata = {
        "total_before_fusion": total_before,
        "total_after_fusion": len(ranked),
        "sources_contributed": len(sources_used),
        "duplicates_removed": max(0, duplicates_removed),
        "search_intent": search_intent,
    }

    # Add ranked results and fusion metadata to summary
    summary["ranked_results"] = ranked[:req.max_results]
    summary["fusion_metadata"] = fusion_metadata
    # Raw (pre-fusion) results per source, capped at 10 each — consumed by
    # query.py and competitors.py
    summary["raw_results_by_source"] = {
        src: [r.to_dict() for r in results[:10]]
        for src, results in results_by_source.items()
    }

    # Limit returned results to max_results
    all_results_truncated = all_results[:req.max_results]

    _t("end")

    return SearchResponse(
        query=req.query,
        summary=summary,
        metadata={
            "query_time_ms": int((time.time() - start) * 1000),
            "timing_ms": _timing,
            "sources_used": len(sources_used),
            "sources_failed": len(sources_failed),
            "total_results": len(all_results),
            "returned_results": len(all_results_truncated),
            "all_sources_mode": req.all_sources,
            "community_keywords": community_kw,
            "community_sources": [s for s in valid_sources if s in _COMMUNITY_SOURCES],
            "search_intent": search_intent,
            "fusion_metadata": fusion_metadata,
            "freshness": req.freshness,
            "freshness_filtered": freshness_filtered,
        },
    )
