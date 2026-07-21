"""Competitor auto-discovery for comparison queries.

When the user asks "X 哪个更好" without specifying entities,
this module discovers competitors via web search.
"""

from __future__ import annotations

import logging
import re
from collections import Counter

logger = logging.getLogger(__name__)

# Generic terms to filter out
_GENERIC_TERMS = {
    "the", "a", "an", "is", "are", "was", "were", "best", "top",
    "vs", "versus", "compare", "compared", "difference", "between",
    "which", "one", "two", "three", "four", "five",
    "对比", "比较", "哪个", "更好", "区别", "推荐", "选择",
    "和", "与", "或", "但", "却", "而", "已", "最", "很",
}


def _generate_queries(topic: str) -> list[str]:
    """Generate search queries for competitor discovery."""
    return [
        f"{topic} competitors",
        f"{topic} alternatives",
        f"{topic} vs",
        f"{topic} 对比",
        f"{topic} 竞品",
    ]


def _extract_entities(text: str) -> list[str]:
    """Extract potential entity names from text.

    Strategy:
    1. Capitalized English phrases (brand names, including compound like "LMDeploy")
    2. Short all-caps acronyms (2-4 chars like "TGI", "LLM")
    3. CJK sequences (3+ chars) that look like product/company names
    """
    entities = []

    # English: words starting with uppercase (handles "vLLM", "LMDeploy", "Cursor")
    words = re.findall(r'\b([A-Z][A-Za-z]{1,20})\b', text)
    for w in words:
        if w.lower() not in _GENERIC_TERMS and len(w) >= 2:
            entities.append(w)

    # Multi-word Title-cased phrases
    phrases = re.findall(r'(?:[A-Z][a-zA-Z]+(?:\s+[A-Z][a-zA-Z]+){1,3})', text)
    for p in phrases:
        p = p.strip()
        if p.lower() not in _GENERIC_TERMS and len(p) >= 4:
            entities.append(p)

    # CJK sequences (3-8 chars)
    cjk = re.findall(r'[一-鿿]{3,8}', text)
    for c in cjk:
        entities.append(c)

    return entities


async def discover_competitors(topic: str, count: int = 3) -> list[str]:
    """Discover competitors/alternatives for a topic via web search.

    Purely deterministic: generates search queries, extracts entities,
    ranks by cross-query frequency.

    Args:
        topic: The primary topic (e.g. "大模型推理框架", "AI coding tool")
        count: Number of competitors to return

    Returns:
        List of competitor names, sorted by frequency across queries.
    """
    queries = _generate_queries(topic)
    topic_tokens = set(re.findall(r'\b\w+\b', topic.lower()))
    topic_tokens = {t for t in topic_tokens if len(t) >= 2}

    entity_counts = Counter()
    seen_in_queries = {}  # entity -> set of query indices

    for qi, query in enumerate(queries):
        try:
            from app.api.search import search, SearchRequest
            req = SearchRequest(
                query=query,
                sources=["google", "bing"],
                include_structured=True,
                all_sources=False,
                max_results=10,
            )
            result = await search(req)
            raw = result.summary.get("raw_results_by_source", {})
            for source, results in raw.items():
                for r in results[:5]:
                    text = r.get("title", "") + " " + r.get("content", "")
                    entities = _extract_entities(text)
                    for ent in entities:
                        ent_lower = ent.lower()
                        # Filter: must not overlap with topic tokens
                        ent_tokens = set(re.findall(r'\b\w+\b', ent_lower))
                        if ent_tokens and ent_tokens <= topic_tokens:
                            continue  # entirely made of topic words
                        if ent_lower in _GENERIC_TERMS:
                            continue
                        entity_counts[ent] += 1
                        if ent not in seen_in_queries:
                            seen_in_queries[ent] = set()
                        seen_in_queries[ent].add(qi)
        except Exception as e:
            logger.debug(f"Competitor discovery query '{query}' failed: {e}")
            continue

    # Score by: number of different queries the entity appeared in
    scored = []
    for ent, total_count in entity_counts.most_common(10):
        query_count = len(seen_in_queries.get(ent, set()))
        # Only keep entities appearing in >= 1 query and >= 2 results total
        if query_count >= 1 and total_count >= 2:
            scored.append((ent, query_count, total_count))

    # Sort by query_count (cross-query presence) then total_count
    scored.sort(key=lambda x: (-x[1], -x[2]))

    # Deduplicate: case-insensitive unique
    seen = set()
    result = []
    for ent, _, _ in scored:
        if ent.lower() not in seen:
            seen.add(ent.lower())
            result.append(ent)
        if len(result) >= count:
            break

    if result:
        logger.info(f"Competitor discovery for '{topic}': {result}")
    return result
