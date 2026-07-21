"""RRF fusion + dedup + diversity — ported from last30days-skill.

Replaces flat per-source grouping with a unified ranked list.
"""

from __future__ import annotations

import re
from urllib.parse import parse_qs, urlencode, urlparse, urlunparse

# RRF smoothing constant (Cormack et al. 2009)
RRF_K = 60

# Per-author cap: no single author should dominate
_MAX_ITEMS_PER_AUTHOR = 3

# Minimum items per source before truncation (diversity)
_MIN_PER_SOURCE = 2

# Token overlap threshold for near-duplicate detection
_DEDUPE_THRESHOLD = 0.85

# Character-level n-gram threshold (more sensitive for Chinese text)
_NGRAM_DEDUPE_THRESHOLD = 0.7


def normalize_url(url: str) -> str:
    """Normalize URL for dedup: lowercase, strip www, remove tracking params."""
    try:
        parsed = urlparse(url.strip().lower())
        netloc = parsed.netloc
        for prefix in ("www.", "old.", "m."):
            if netloc.startswith(prefix):
                netloc = netloc[len(prefix):]
        params = parse_qs(parsed.query)
        clean = {k: v for k, v in params.items() if not k.startswith("utm_")}
        query = urlencode(clean, doseq=True)
        return urlunparse((parsed.scheme, netloc, parsed.path.rstrip("/"), "", query, ""))
    except Exception:
        return url.strip().lower()


def _token_set(text: str) -> set[str]:
    """Split text into lowercase tokens for Jaccard comparison."""
    return set(re.findall(r"\b\w+\b", text.lower()))


def _jaccard(a: set[str], b: set[str]) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def _ngrams(text: str, n: int = 3) -> set[str]:
    """Character-level n-grams for better Chinese text dedup."""
    norm = re.sub(r'[^\w\s]', ' ', text.lower())
    norm = re.sub(r'\s+', ' ', norm).strip()
    if len(norm) < n:
        return {norm} if norm else set()
    return {norm[i:i + n] for i in range(len(norm) - n + 1)}


def _ngram_jaccard(a: str, b: str) -> float:
    if not a or not b:
        return 0.0
    ng_a = _ngrams(a)
    ng_b = _ngrams(b)
    if not ng_a or not ng_b:
        return 0.0
    union = ng_a | ng_b
    if not union:
        return 0.0
    return len(ng_a & ng_b) / len(union)


def _is_near_duplicate(new_text: str, existing_texts: list[tuple[float, str]]) -> bool:
    """Hybrid near-duplicate: n-gram Jaccard + token Jaccard, use max as similarity."""
    new_tokens = _token_set(new_text)
    if not new_tokens:
        return False
    for _, existing in existing_texts:
        token_sim = _jaccard(new_tokens, _token_set(existing))
        ngram_sim = _ngram_jaccard(new_text, existing)
        if max(token_sim, ngram_sim) >= min(_DEDUPE_THRESHOLD, _NGRAM_DEDUPE_THRESHOLD):
            return True
    return False


def _extract_author(result: dict) -> str | None:
    """Extract author from result metadata if available."""
    for key in ("author", "handle", "username", "user"):
        if result.get(key):
            return result[key].strip().lower()
    return None


def rrf_fuse(
    results_by_source: dict[str, list[dict]],
    source_weights: dict[str, float] | None = None,
    *,
    query: str = "",
    max_results: int = 50,
    group_weights: dict[str, float] | None = None,
) -> list[dict]:
    """Fuse ranked results from multiple sources using Reciprocal Rank Fusion.

    Returns a unified list of results ranked by RRF score, with dedup,
    author cap, and source diversity.

    Each result dict gets two new fields:
      - _rrf_score: float (fusion score)
      - _rank: int (position within its source before fusion)

    group_weights: optional dict mapping source/group key to a multiplier
      (e.g. subquery weight). Applied as: score = weight * group_w / (RRF_K + rank).
    """
    if not source_weights:
        source_weights = {src: 1.0 for src in results_by_source}

    # Normalize weights
    total_w = sum(source_weights.values()) or 1.0
    norm_weights = {s: w / total_w for s, w in source_weights.items()}

    # Phase 1: RRF scoring + cross-source URL dedup
    seen_urls: dict[str, dict] = {}  # normalized_url -> best result
    for source, results in results_by_source.items():
        weight = norm_weights.get(source, 1.0 / max(len(results_by_source), 1))
        group_w = group_weights.get(source, 1.0) if group_weights else 1.0
        for rank, result in enumerate(results, start=1):
            score = weight * group_w / (RRF_K + rank)
            result["_rrf_score"] = score
            result["_rank"] = rank

            norm = normalize_url(result.get("url", ""))
            if norm and norm in seen_urls:
                existing = seen_urls[norm]
                existing["_rrf_score"] += score
                existing_sources = set(existing.get("_sources", [existing.get("source", "")]))
                existing["_sources"] = list(existing_sources | {source})
                # Keep the better snippet
                if len(result.get("content", "")) > len(existing.get("content", "")):
                    existing["content"] = result["content"]
                if len(result.get("title", "")) > len(existing.get("title", "")):
                    existing["title"] = result["title"]
            else:
                result["_sources"] = [source] if not norm else [result.get("source", ""), source]
                if not norm:
                    result["_sources"] = [result.get("source", "")]
                seen_urls[norm] = result

    fused = sorted(seen_urls.values(), key=lambda r: -r.get("_rrf_score", 0))

    # Phase 2: Near-duplicate filtering (text-based, for results without URLs)
    deduped = []
    seen_texts: list[tuple[float, str]] = []
    for r in fused:
        text = (r.get("title", "") + " " + r.get("content", "")).strip()
        if text and _is_near_duplicate(text, seen_texts):
            continue
        deduped.append(r)
        if text:
            seen_texts.append((r.get("_rrf_score", 0), text))

    # Phase 3: Entity anchoring — demote results that don't mention the query
    if query:
        query_tokens = _token_set(query)
        # Keep only meaningful tokens (length >= 2, not pure numbers)
        query_tokens = {t for t in query_tokens if len(t) >= 2 and not t.isdigit()}
        if query_tokens:
            for r in deduped:
                text = (r.get("title", "") + " " + r.get("content", "")).strip()
                text_tokens = _token_set(text)
                if text_tokens and not (query_tokens & text_tokens):
                    # No overlap with query — likely off-topic, demote heavily
                    r["_rrf_score"] *= 0.1

    # Re-sort after demotion
    deduped.sort(key=lambda r: -r.get("_rrf_score", 0))

    # Phase 4: Per-author cap
    capped = _apply_author_cap(deduped)

    # Phase 5: Source diversity + pool limit
    diverse = _ensure_source_diversity(capped, max_results)

    return diverse


def _apply_author_cap(results: list[dict], max_per_author: int = _MAX_ITEMS_PER_AUTHOR) -> list[dict]:
    """Keep at most max_per_author items from any single author."""
    author_counts: dict[str, int] = {}
    output = []
    for r in results:
        author = _extract_author(r)
        if author is None:
            output.append(r)
            continue
        count = author_counts.get(author, 0)
        if count < max_per_author:
            output.append(r)
            author_counts[author] = count + 1
    return output


def _ensure_source_diversity(results: list[dict], pool_limit: int) -> list[dict]:
    """Ensure at least _MIN_PER_SOURCE items per source survive truncation."""
    source_counts: dict[str, int] = {}
    reserved = []
    remainder = []

    # First pass: reserve min items per source
    for r in results:
        source = r.get("source", "unknown")
        count = source_counts.get(source, 0)
        if count < _MIN_PER_SOURCE:
            reserved.append(r)
            source_counts[source] = count + 1
        else:
            remainder.append(r)

    # Second pass: fill from remainder up to pool_limit
    output = list(reserved)
    seen_urls = {normalize_url(r.get("url", "")) for r in reserved}
    for r in remainder:
        if len(output) >= pool_limit:
            break
        norm = normalize_url(r.get("url", ""))
        if norm and norm in seen_urls:
            continue
        output.append(r)

    return output[:pool_limit]
