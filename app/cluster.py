"""Result clustering — groups ranked results by topic.

Greedy leader-based clustering with MMR representative selection,
ported from last30days cluster.py.
"""

from __future__ import annotations

import re

# Thresholds per intent — tuned for Chinese web search results
# (lower than last30days' 0.42/0.48 because diverse sources produce
#  less exact phrasing overlap than social media posts)
_CLUSTER_THRESHOLDS = {
    "breaking_news": 0.12,
    "opinion": 0.15,
    "comparison": 0.15,
    "prediction": 0.15,
}

# Intents that benefit from clustering
_CLUSTERABLE_INTENTS = set(_CLUSTER_THRESHOLDS.keys())

# MMR lambda: 0.75 favors relevance, 0.25 diversity
_MMR_LAMBDA = 0.75

# Max representatives per cluster
_MAX_REPRESENTATIVES = 3

# Entity overlap threshold for merging small clusters
_ENTITY_MERGE_THRESHOLD = 0.45

# Max cluster size for entity merge
_MAX_MERGE_SIZE = 3


def _ngrams(text: str, n: int = 3) -> set[str]:
    """Character-level n-grams."""
    norm = re.sub(r'[^\w\s]', ' ', text.lower())
    norm = re.sub(r'\s+', ' ', norm).strip()
    if len(norm) < n:
        return {norm} if norm else set()
    return {norm[i:i + n] for i in range(len(norm) - n + 1)}


def _token_set(text: str) -> set[str]:
    """Split text into lowercase tokens."""
    return set(re.findall(r'\b\w+\b', text.lower()))


def _jaccard(a: set[str], b: set[str]) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def _text_similarity(a: str, b: str) -> float:
    """Hybrid similarity: max of n-gram Jaccard and token Jaccard."""
    if not a or not b:
        return 0.0
    ng_a, ng_b = _ngrams(a), _ngrams(b)
    ng_sim = _jaccard(ng_a, ng_b) if ng_a and ng_b else 0.0
    tok_sim = _jaccard(_token_set(a), _token_set(b))
    return max(ng_sim, tok_sim)


def _extract_entities(text: str) -> set[str]:
    """Extract significant words/entities from text for overlap comparison."""
    # Capitalized words, words with digits, long words (4+ chars)
    tokens = re.findall(r'\b[A-Za-z]+\b', text)
    entities = set()
    for t in tokens:
        if len(t) >= 4 or t.isupper() or any(c.isdigit() for c in t):
            entities.add(t.lower())
    # CJK sequences (3+ chars)
    cjk = re.findall(r'[一-鿿]{3,8}', text)
    entities.update(cjk)
    return entities


def _entity_overlap(a: set[str], b: set[str]) -> float:
    """Overlap coefficient: |intersection| / min(|a|, |b|)."""
    if not a or not b:
        return 0.0
    return len(a & b) / min(len(a), len(b))


def cluster_results(
    candidates: list[dict],
    intent: str = "concept",
    threshold: float | None = None,
) -> list[dict]:
    """Cluster ranked results by topic similarity.

    Only clusters for: opinion, comparison, breaking_news, prediction.
    Returns list of clusters sorted by score descending.

    Each cluster:
    {
      "title": str,         # leader's title (cluster topic)
      "score": float,       # max judge_score in cluster
      "members": [dict],    # all member results
      "representatives": [dict],  # up to 3 MMR-selected
      "sources": [str],     # unique source names
      "size": int,
    }
    """
    if not candidates or intent not in _CLUSTERABLE_INTENTS:
        return []

    if threshold is None:
        threshold = _CLUSTER_THRESHOLDS.get(intent, 0.48)

    # Pre-compute text for each candidate
    texts = []
    for c in candidates:
        text = (c.get("title", "") + " " + c.get("content", "")).strip()
        texts.append(text)

    # Phase 1: Greedy leader-based clustering
    clusters = []  # [{"leader_idx": int, "members": [idx], "texts": [str]}]

    for i, text in enumerate(texts):
        if not text:
            # No text — standalone cluster
            clusters.append({"leader_idx": i, "members": [i]})
            continue

        best_cluster = None
        best_sim = 0.0

        for cluster in clusters:
            leader_text = texts[cluster["leader_idx"]]
            sim = _text_similarity(text, leader_text)
            if sim > best_sim:
                best_sim = sim
                best_cluster = cluster

        if best_cluster and best_sim >= threshold:
            best_cluster["members"].append(i)
        else:
            clusters.append({"leader_idx": i, "members": [i]})

    # Phase 2: Entity overlap merge for small clusters
    clusters = _merge_entity_clusters(clusters, candidates, texts)

    # Phase 3: Build output
    result = []
    for cluster in clusters:
        members = [candidates[idx] for idx in cluster["members"]]
        sources = list(set(m.get("source", "") for m in members))
        scores = [m.get("_judge_score") or m.get("judge_score") or m.get("_rrf_score", 0) * 100
                  for m in members]
        max_score = max(scores) if scores else 0

        representatives = _mmr_representatives(members, cluster["members"], candidates, texts)

        result.append({
            "title": candidates[cluster["leader_idx"]].get("title", ""),
            "score": max_score,
            "members": members,
            "representatives": representatives,
            "sources": sorted(sources),
            "size": len(cluster["members"]),
        })

    # Sort by score descending
    result.sort(key=lambda c: -c["score"])
    return result


def _merge_entity_clusters(clusters, candidates, texts):
    """Merge small clusters (<=3 members) from different sources if entity overlap >= threshold."""
    if len(clusters) <= 1:
        return clusters

    entity_sets = []
    for cluster in clusters:
        all_text = " ".join(texts[idx] for idx in cluster["members"])
        entity_sets.append(_extract_entities(all_text))

    merged = set()
    merge_passes = 0
    max_passes = 5

    while merge_passes < max_passes:
        made_merge = False
        for i in range(len(clusters)):
            if i in merged or len(clusters[i]) > _MAX_MERGE_SIZE:
                continue
            for j in range(i + 1, len(clusters)):
                if j in merged or len(clusters[j]) > _MAX_MERGE_SIZE:
                    continue
                # Check different sources
                sources_i = {candidates[idx].get("source", "") for idx in clusters[i]["members"]}
                sources_j = {candidates[idx].get("source", "") for idx in clusters[j]["members"]}
                if sources_i == sources_j:
                    continue
                # Check entity overlap
                overlap = _entity_overlap(entity_sets[i], entity_sets[j])
                if overlap >= _ENTITY_MERGE_THRESHOLD:
                    # Merge j into i
                    clusters[i]["members"].extend(clusters[j]["members"])
                    entity_sets[i] = entity_sets[i] | entity_sets[j]
                    merged.add(j)
                    made_merge = True
                    break
            if made_merge:
                break
        if not made_merge:
            break
        merge_passes += 1

    # Remove merged clusters
    return [c for i, c in enumerate(clusters) if i not in merged]


def _mmr_representatives(members: list[dict], member_indices: list[int],
                         all_candidates: list[dict], all_texts: list[str],
                         limit: int = _MAX_REPRESENTATIVES) -> list[dict]:
    """Select diverse representatives using Maximal Marginal Relevance.

    First pick: highest scored in cluster.
    Subsequent picks: greedy MMR = lambda * score - (1-lambda) * max_sim_to_selected.
    """
    if not members:
        return []
    if len(members) <= limit:
        return list(members)

    # Score each member
    scores = []
    for m in members:
        score = m.get("_judge_score") or m.get("judge_score") or m.get("_rrf_score", 0) * 100
        scores.append(score)

    # Normalize scores to 0-100
    max_score = max(scores) if scores else 1
    if max_score > 0:
        scores = [s / max_score * 100 for s in scores]

    # First pick: highest score
    selected = [scores.index(max(scores))]
    selected_set = set(selected)

    # Get text for each member
    member_texts = []
    for idx in member_indices:
        member_texts.append(all_texts[idx])

    # Greedy MMR for remaining picks
    while len(selected) < limit:
        best_idx = None
        best_mmr = -1

        for i, score in enumerate(scores):
            if i in selected_set:
                continue
            # Max similarity to already selected
            max_sim = 0.0
            for j in selected:
                sim = _text_similarity(member_texts[i], member_texts[j])
                if sim > max_sim:
                    max_sim = sim
            mmr = _MMR_LAMBDA * (score / 100) - (1 - _MMR_LAMBDA) * max_sim
            if mmr > best_mmr:
                best_mmr = mmr
                best_idx = i

        if best_idx is not None:
            selected.append(best_idx)
            selected_set.add(best_idx)

    return [members[i] for i in selected]
