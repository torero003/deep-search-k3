"""LLM Relevance Judge — scores search results 0-100 for query relevance.

Ported from last30days rerank.py. Uses local LLM (Qwen 3.6 27B) for scoring.
Fallback: deterministic scoring when LLM fails.
Engagement scoring for community sources (zhihu, xueqiu, twitter, etc.).
"""

from __future__ import annotations

import logging
import math
import re

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Engagement scoring
# ---------------------------------------------------------------------------

def _parse_engagement_num(s: str | int | float) -> int:
    """Parse a number that may be a string like '1.2k', '3.5K', '10万'."""
    if isinstance(s, (int, float)):
        return int(s)
    s = str(s).strip().lower()
    multipliers = {"k": 1000, "m": 1000000, "万": 10000, "亿": 100000000}
    for suffix, mult in multipliers.items():
        if s.endswith(suffix):
            s = s[:-len(suffix)]
            try:
                return int(float(s) * mult)
            except ValueError:
                return 0
    try:
        return int(float(s))
    except ValueError:
        return 0


def engagement_score(result: dict) -> float | None:
    """Compute log1p-weighted engagement score from a result's engagement dict.

    Returns a raw score (higher = more engagement) or None if no engagement data.
    """
    eng = result.get("engagement")
    if not eng:
        return None
    score = 0.0
    for key in ("upvotes", "likes", "votes", "stars"):
        val = eng.get(key)
        if val:
            score += 0.5 * math.log1p(_parse_engagement_num(val))
    for key in ("comments", "replies"):
        val = eng.get(key)
        if val:
            score += 0.35 * math.log1p(_parse_engagement_num(val))
    for key in ("reposts", "shares", "retweets", "forks", "quotes"):
        val = eng.get(key)
        if val:
            score += 0.15 * math.log1p(_parse_engagement_num(val))
    for key in ("views",):
        val = eng.get(key)
        if val:
            score += 0.1 * math.log1p(_parse_engagement_num(val))
    return score if score > 0 else None


def _normalize_engagement(candidates: list[dict]) -> None:
    """Min-max normalize engagement scores to 0-100, store as '_engagement_norm'."""
    scores = []
    for c in candidates:
        s = engagement_score(c)
        c["_eng_raw"] = s
        if s is not None:
            scores.append(s)
    if not scores or len(scores) < 2:
        # All same or no data — set to 50 (neutral) for those with data
        for c in candidates:
            c["_engagement_norm"] = 50.0 if c.get("_eng_raw") else 0.0
        return
    min_s, max_s = min(scores), max(scores)
    range_s = max_s - min_s if max_s > min_s else 1.0
    for c in candidates:
        raw = c.get("_eng_raw")
        if raw is not None:
            c["_engagement_norm"] = ((raw - min_s) / range_s) * 100
        else:
            c["_engagement_norm"] = 0.0


async def judge_relevance(query: str, candidates: list[dict]) -> list[dict]:
    """Score each candidate 0-100 for relevance to query.

    Args:
        query: The original search query
        candidates: List of result dicts with 'title' and 'content' fields

    Returns:
        Same list, each dict gets '_judge_score' (0-100 float)
    """
    if not candidates:
        return candidates

    # Try LLM scoring
    scored = await _llm_judge(query, candidates)
    if all(c.get("_judge_score") is not None for c in scored):
        return scored

    # Fallback: deterministic scoring
    logger.info("Judge: using deterministic fallback")
    return _deterministic_judge(query, candidates)


async def _llm_judge(query: str, candidates: list[dict]) -> list[dict]:
    """Score candidates via LLM. Returns list with _judge_score set."""
    try:
        from app.llm_client import achat_json

        # Build compact input: id + title + short snippet
        items = []
        for i, c in enumerate(candidates[:30], 1):  # Max 30 to avoid context overflow
            title = c.get("title", "")[:80]
            snippet = c.get("content", "")[:150]
            items.append({"id": i, "title": title, "snippet": snippet})

        system_msg = (
            "You are a relevance judge. Score each search result 0-100 for relevance "
            "to the query. Criteria:\n"
            "90-100: Directly answers the query, strong evidence\n"
            "70-89: Highly relevant, useful information\n"
            "40-69: Somewhat relevant, tangential\n"
            "0-39: Weakly relevant, off-topic, or no mention of key entities\n\n"
            "Rules:\n"
            "- If the result doesn't mention the main entity from the query, score <= 30\n"
            "- Comparison queries: prefer head-to-head comparisons\n"
            "- Opinion queries: prefer personal experience, not marketing\n"
            "- Output ONLY JSON array: [{\"id\": 1, \"score\": 85, \"reason\": \"...\"}]\n"
            "- Keep reasons short (10 words max)"
        )

        result = await achat_json([
            {"role": "system", "content": system_msg},
            {"role": "user", "content": f"Query: {query}\n\nResults:\n" +
             "\n".join(f"{it['id']}. {it['title']}\n   {it['snippet']}" for it in items)},
        ], max_tokens=2000)

        if isinstance(result, list) and len(result) > 0:
            # Map scores back to candidates
            score_map = {}
            for item in result:
                if isinstance(item, dict) and "id" in item and "score" in item:
                    score_map[item["id"]] = float(item["score"])

            # Apply scores to candidates that got scored
            all_scored = True
            for i, c in enumerate(candidates[:30], 1):
                if i in score_map:
                    c["_judge_score"] = score_map[i]
                else:
                    c["_judge_score"] = None
                    all_scored = False

            # For candidates beyond 30 or not scored, leave None
            for c in candidates[30:]:
                c["_judge_score"] = None

            if all_scored and len(score_map) >= len(candidates) * 0.8:
                return candidates

    except Exception as e:
        logger.warning(f"LLM judge failed: {e}")

    return candidates


def _deterministic_judge(query: str, candidates: list[dict]) -> list[dict]:
    """Deterministic relevance scoring when LLM is unavailable.

    Score = 0.5 * entity_match + 0.3 * text_relevance + 0.2 * source_quality
    """
    import re
    from app.validator import SOURCE_WEIGHT

    # Normalize query: insert space between letters and numbers for better matching
    # e.g., "mythos5" → "mythos 5" so it matches "mythos 5" in content
    query = re.sub(r'([a-zA-Z])(\d)', r'\1 \2', query)
    query_tokens = set(re.findall(r'\b\w+\b', query.lower()))
    # Keep only meaningful tokens
    query_tokens = {t for t in query_tokens if len(t) >= 2 and not t.isdigit()}

    for c in candidates:
        title = c.get("title", "")
        content = c.get("content", "")
        source = c.get("source", "")
        text = (title + " " + content).lower()
        text_tokens = set(re.findall(r'\b\w+\b', text))

        # Entity match: what fraction of query tokens appear in result
        if query_tokens and text_tokens:
            entity_match = len(query_tokens & text_tokens) / len(query_tokens)
        else:
            entity_match = 0.0

        # Title bonus: query tokens in title are worth more
        title_tokens = set(re.findall(r'\b\w+\b', title.lower()))
        if query_tokens and title_tokens:
            title_match = len(query_tokens & title_tokens) / len(query_tokens)
        else:
            title_match = 0.0

        # Source quality
        source_q = min(1.0, SOURCE_WEIGHT.get(source, 1) / 10.0)

        score = (
            0.35 * entity_match +
            0.25 * title_match +
            0.20 * source_q +
            0.20 * min(1.0, len(content) / 500)  # Longer content = more useful
        ) * 100

        # Entity penalty: if NO query tokens match, heavy demotion
        if query_tokens and not (query_tokens & text_tokens):
            score *= 0.1

        # Empty content penalty: results with no content are low value
        if not content.strip():
            score *= 0.5

        c["_judge_score"] = round(min(100.0, max(0.0, score)), 1)

    return candidates


def apply_judge_ranking(candidates: list[dict]):
    """Re-sort candidates by composite score: judge + engagement + source_quality + RRF.
    Formula: 0.70*judge + 0.15*engagement_norm + 0.10*source_quality + 0.05*content_length
    Modifies list in place.
    """
    from app.validator import SOURCE_WEIGHT

    if not candidates:
        return

    # Normalize engagement scores across the batch
    _normalize_engagement(candidates)

    # Compute composite score for each candidate
    for c in candidates:
        judge = (c.get("_judge_score") or 0) / 100.0
        engagement = c.get("_engagement_norm", 0) / 100.0
        source_q = min(1.0, SOURCE_WEIGHT.get(c.get("source", ""), 1) / 10.0)
        content_len = min(1.0, len(c.get("content", "")) / 500)

        c["_final_score"] = (
            0.70 * judge +
            0.15 * engagement +
            0.10 * source_q +
            0.05 * content_len
        )

    candidates.sort(key=lambda c: -(c.get("_final_score", 0)))
