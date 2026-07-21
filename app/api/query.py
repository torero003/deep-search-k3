# coding: utf-8
import re
import time
import logging
from fastapi import APIRouter
from pydantic import BaseModel, Field
from typing import Optional

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/query", tags=["query"])

EN_SOURCES = {"google", "bing", "yandex", "github", "twitter", "trendforce"}
ZH_SOURCES = {"zhihu", "xueqiu", "sogou_wechat", "v2ex", "stats_gov", "eastmoney"}

_STOP_WORDS = {
    "对于", "的", "中", "在", "会", "是", "什么", "哪", "哪些", "为什么",
    "如何", "怎么", "还有", "吗", "呢", "吧", "了", "着", "过", "和",
    "与", "或", "但", "却", "而", "已", "最", "很", "非常", "超级",
    "行业", "板块", "请问", "大家", "觉得", "认为", "看好", "期待",
    "最近", "这两天", "目前", "现在", "当前", "其他", "还", "也",
}


def _extract_keywords_simple(question):
    """Fallback: remove punctuation and multi-char stop words, keep first 25 chars.
    Preserves spaces between English words to avoid merging e.g. 'Claude Code' into 'ClaudeCode'.
    """
    if len(question) <= 20:
        return question
    # Remove punctuation but keep spaces and word characters
    text = re.sub(r'[^\w\s]', ' ', question)
    # Collapse multiple spaces
    text = re.sub(r'\s+', ' ', text).strip()
    # Remove multi-char stop words (len>=2 only, to avoid breaking words like '中国')
    multi_stop = sorted([s for s in _STOP_WORDS if len(s) >= 2], key=len, reverse=True)
    for sw in multi_stop:
        text = text.replace(sw, ' ').replace('  ', ' ')
    text = text.strip()
    return text[:25]


async def _extract_search_keywords(question):
    """Extract zh/en keyword pairs (backward compat wrapper around _plan_search_queries)."""
    plan = await _plan_search_queries(question)
    subs = plan.get("subqueries", [])
    if subs:
        return {"zh": subs[0].get("zh_kw", ""), "en": subs[0].get("en_kw", "")}
    zh = _extract_keywords_simple(question)
    return {"zh": zh, "en": zh}


async def _plan_search_queries(question):
    """Plan 2-4 subqueries from a user question. Each subquery has a different
    search angle (zh keywords, en keywords, weight). Returns:
    {
      "intent": "comparison|opinion|...",
      "subqueries": [
        {"query": "...", "zh_kw": "...", "en_kw": "...", "weight": 1.0},
        ...
      ]
    }
    Uses deterministic planning for all 8 intents (no LLM needed).
    """
    from app.config import detect_search_intent, has_intent_modifier, strip_intent_modifiers
    intent = detect_search_intent(question)

    zh = _extract_keywords_simple(question)
    core = zh[:12]

    # ── Comparison: extract entities from "A vs B" / "A 对比 B" ──
    if intent == "comparison":
        entities = re.split(r"\bvs\.?\b|\bversus\b|\b对比\b|\b比较\b|/|哪个更好|\b区别\b",
                            question, flags=re.I)
        entities = [e.strip().strip(" ?.,;:!()[]{}\"'") for e in entities if e.strip()]
        if len(entities) >= 2:
            subs = []
            for ent in entities[:3]:
                ent_zh = re.sub(r'[^\w]', '', ent)[:12] or ent[:12]
                subs.append({
                    "query": ent,
                    "zh_kw": ent_zh,
                    "en_kw": ent[:20],
                    "weight": 0.7,
                })
            subs.append({
                "query": question[:30],
                "zh_kw": zh[:15],
                "en_kw": question[:30],
                "weight": 1.0,
            })
            logger.info(f"Comparison plan: {len(entities)} entities, {len(subs)} subqueries")
            return {"intent": intent, "subqueries": subs}
        # Only 1 entity found — try auto-discovering competitors
        core_subject = strip_intent_modifiers(question)[:12]
        if core_subject:
            try:
                from app.competitors import discover_competitors
                discovered = await discover_competitors(core_subject, count=3)
                if discovered:
                    entities.extend(discovered)
                    subs = []
                    for ent in discovered[:3]:
                        ent_zh = re.sub(r'[^\w]', '', ent)[:12] or ent[:12]
                        subs.append({
                            "query": ent,
                            "zh_kw": ent_zh,
                            "en_kw": ent[:20],
                            "weight": 0.6,
                        })
                    subs.append({
                        "query": question[:30],
                        "zh_kw": zh[:15],
                        "en_kw": question[:30],
                        "weight": 1.0,
                    })
                    logger.info(f"Comparison plan (auto-discovered): {discovered}, {len(subs)} subqueries")
                    return {"intent": intent, "subqueries": subs}
            except Exception as e:
                logger.warning(f"Competitor discovery failed: {e}")

    # ── Opinion: factual + opinion angles ──
    if intent == "opinion":
        return {"intent": intent, "subqueries": [
            {"query": question[:30], "zh_kw": zh[:15], "en_kw": question[:30], "weight": 1.0},
            {"query": f"{core} 评价 体验", "zh_kw": core[:10], "en_kw": f"{core} review experience", "weight": 0.7},
        ]}

    # ── How_to: tutorial + practical angles ──
    if intent == "how_to":
        return {"intent": intent, "subqueries": [
            {"query": question[:30], "zh_kw": zh[:15], "en_kw": question[:30], "weight": 1.0},
            {"query": f"{core} 教程 步骤", "zh_kw": core[:10], "en_kw": f"{core} tutorial step by step", "weight": 0.7},
        ]}

    # ── Breaking news: latest + reaction ──
    if intent == "breaking_news":
        return {"intent": intent, "subqueries": [
            {"query": question[:30], "zh_kw": zh[:15], "en_kw": question[:30], "weight": 1.0},
            {"query": f"{core} 最新 动态", "zh_kw": core[:10], "en_kw": f"{core} latest news update", "weight": 0.7},
        ]}

    # ── Intent modifier: paraphrase fanout ──
    if has_intent_modifier(question):
        core_mod = strip_intent_modifiers(question)[:12]
        logger.info(f"Query plan: intent={intent}, modifier fanout for '{core_mod}'")
        return {
            "intent": intent,
            "subqueries": [
                {"query": question[:30], "zh_kw": zh[:15], "en_kw": question[:30], "weight": 1.0},
                {"query": f"{core_mod} 评测 体验", "zh_kw": core_mod[:10],
                 "en_kw": f"{core_mod} review experience", "weight": 0.6},
                {"query": f"{core_mod} 实战 使用", "zh_kw": core_mod[:10],
                 "en_kw": f"{core_mod} practical use cases", "weight": 0.55},
                {"query": f"{core_mod} 入门 指南", "zh_kw": core_mod[:10],
                 "en_kw": f"{core_mod} getting started guide", "weight": 0.5},
            ],
        }

    # ── Fallback for factual/product/concept/prediction: single query ──
    logger.info(f"Query plan: intent={intent}, 1 subquery")
    return {
        "intent": intent,
        "subqueries": [{
            "query": question[:30],
            "zh_kw": zh[:15],
            "en_kw": question[:30],
            "weight": 1.0,
        }],
    }


class QueryRequest(BaseModel):
    question: str = Field(..., min_length=1, max_length=500)
    context: Optional[str] = None
    all_sources: bool = False
    max_results: int = Field(default=15, ge=5, le=100)


class QueryResponse(BaseModel):
    answer: str
    data_points: list[dict]
    sources: list[str]
    confidence: float
    query_time_ms: int


@router.post("", response_model=QueryResponse)
async def query(req: QueryRequest):
    start = time.time()

    # Plan subqueries
    plan = await _plan_search_queries(req.question)
    intent = plan.get("intent", "concept")
    subqueries = plan.get("subqueries", [])

    logger.info(f"Query plan for '{req.question[:40]}': intent={intent}, "
                f"subqueries={len(subqueries)}")

    from app.api.search import search, SearchRequest

    # Track results per subquery: {subquery_index: {source: [results]}}
    all_raw_by_group = {}
    all_findings = []
    all_conflicts = []

    # Execute each subquery
    for sub_idx, sub in enumerate(subqueries):
        zh_kw = sub.get("zh_kw", req.question[:30])
        en_kw = sub.get("en_kw", req.question[:30])
        sub_weight = sub.get("weight", 1.0)

        for search_kw in [zh_kw, en_kw]:
            search_req = SearchRequest(
                query=search_kw,
                include_structured=True,
                all_sources=req.all_sources,
                max_results=req.max_results,
            )
            try:
                result = await search(search_req)
                raw = result.summary.get("raw_results_by_source", {})
                if sub_idx not in all_raw_by_group:
                    all_raw_by_group[sub_idx] = {}
                for source, results in raw.items():
                    if source not in all_raw_by_group[sub_idx]:
                        all_raw_by_group[sub_idx][source] = []
                    seen_urls = {r.get("url", "") for r in all_raw_by_group[sub_idx][source]}
                    for r in results:
                        if r.get("url", "") not in seen_urls:
                            all_raw_by_group[sub_idx][source].append(r)
                            seen_urls.add(r.get("url", ""))
                    all_raw_by_group[sub_idx][source] = all_raw_by_group[sub_idx][source][:req.max_results]
                all_findings.extend(result.summary.get("key_findings", []))
                all_conflicts.extend(result.summary.get("conflicts", []))
            except Exception as e:
                logger.error(f"Search failed for query={search_kw!r}: {e}")

    # Flatten into {source: [results]} for backward compatibility,
    # and build group_weights for RRF fusion
    all_raw_by_source = {}
    for sub_idx, sources in all_raw_by_group.items():
        for source, results in sources.items():
            if source not in all_raw_by_source:
                all_raw_by_source[source] = []
            seen_urls = {r.get("url", "") for r in all_raw_by_source[source]}
            for r in results:
                if r.get("url", "") not in seen_urls:
                    all_raw_by_source[source].append(r)
                    seen_urls.add(r.get("url", ""))
            all_raw_by_source[source] = all_raw_by_source[source][:req.max_results]

    # Build group_weights: {source: subquery_weight} — uses highest weight subquery per source
    group_weights = {}
    for sub_idx, sources in all_raw_by_group.items():
        sub_weight = subqueries[sub_idx].get("weight", 1.0)
        for source in sources:
            group_weights[source] = max(group_weights.get(source, 0), sub_weight)

    llm_facts = await _llm_extract_facts(req.question, all_raw_by_source)
    merged_findings = _merge_findings(all_findings, llm_facts)

    # Get ranked results from the first search result's fusion
    ranked_results = []
    if all_findings or all_raw_by_source:
        # Re-fuse all collected results for final synthesis
        from app.fusion import rrf_fuse
        from app.validator import SOURCE_WEIGHT
        fusion_weights = {src: SOURCE_WEIGHT.get(src, 1) for src in all_raw_by_source}
        ranked_results = rrf_fuse(
            all_raw_by_source, fusion_weights,
            query=req.question, max_results=20,
            group_weights=group_weights,
        )
        # Clean internal fields
        for r in ranked_results:
            r.pop("_rrf_score", None)
            r.pop("_rank", None)
            r.pop("_sources", None)

    # Cluster results by topic for applicable intents
    synth_clusters = None
    if ranked_results and intent in ("opinion", "comparison", "breaking_news", "prediction"):
        from app.cluster import cluster_results
        synth_clusters = cluster_results(ranked_results, intent=intent)

    answer = await _llm_synthesize(
        question=req.question,
        findings=merged_findings,
        conflicts=all_conflicts,
        raw_by_source=all_raw_by_source,
        ranked_results=ranked_results,
        context=req.context,
        clusters=synth_clusters,
    )

    all_sources = list(set(
        s for f in merged_findings for s in f.get("sources", [])
    )) or list(all_raw_by_source.keys())

    high_conf = any(f.get("confidence") == "high" for f in merged_findings)
    med_conf = any(f.get("confidence") == "medium" for f in merged_findings)
    has_llm_facts = len(llm_facts) > 0

    base_conf = 0.85 if high_conf else (0.6 if med_conf else 0.4)
    if has_llm_facts and not high_conf and not med_conf:
        base_conf = max(base_conf, 0.55)

    return QueryResponse(
        answer=answer,
        data_points=merged_findings[:10],
        sources=all_sources,
        confidence=base_conf,
        query_time_ms=int((time.time() - start) * 1000),
    )


def _merge_findings(regex_findings, llm_facts):
    # Dedup key must distinguish facts: regex findings may lack entity/attribute,
    # LLM facts may lack attribute — fall back to fact/value text.
    def _key(f):
        return (
            f.get("entity") or f.get("fact", "")[:20],
            f.get("attribute") or f.get("value", ""),
        )

    seen = set()
    merged = []
    for f in llm_facts:
        key = _key(f)
        if key not in seen:
            seen.add(key)
            merged.append({
                "fact": f.get("fact", f"{f.get('entity', '')} {f.get('attribute', '')}: {f.get('value', '')}"),
                "confidence": f.get("confidence", "low"),
                "sources": f.get("sources", []),
                "source_count": len(f.get("sources", [])),
                "verified": f.get("verified", False),
                "conflict": f.get("conflict", False),
                "entity": f.get("entity", ""),
                "attribute": f.get("attribute", ""),
                "value": f.get("value", ""),
            })
    for f in regex_findings:
        key = _key(f)
        if key not in seen:
            seen.add(key)
            merged.append(f)
    return merged


async def _llm_extract_facts(question, raw_by_source):
    text_parts = []
    for source, results in raw_by_source.items():
        for r in results[:10]:
            title = r.get("title", "")[:100]
            snippet = r.get("content", "")[:200]
            if title or snippet:
                text_parts.append(f"[{source}] {title} {snippet}")
    if not text_parts:
        return []
    from app.llm_client import achat_json
    result = await achat_json([
        {"role": "system", "content": "Extract structured facts from search results. Each fact: entity, attribute, value, sources, confidence(high/medium/low). Output JSON array only."},
        {"role": "user", "content": f"Question: {question}\n\nResults:\n" + "\n".join(text_parts[:40])},
    ], max_tokens=4000)
    if isinstance(result, list):
        facts = []
        for f in result:
            if not isinstance(f, dict):
                continue
            f.setdefault("fact", f"{f.get('entity', '')} {f.get('attribute', '')}: {f.get('value', '')}")
            f.setdefault("verified", len(f.get("sources", [])) >= 2)
            f.setdefault("conflict", False)
            facts.append(f)
        return facts
    if isinstance(result, dict):
        return [result]
    return []


async def _llm_synthesize(question, findings, conflicts, raw_by_source,
                          ranked_results=None, context=None, clusters=None):
    # Ranked results (fusion-sorted) first, then raw by source
    ranked_lines = []
    if ranked_results:
        for i, r in enumerate(ranked_results[:15], 1):
            title = r.get("title", "")[:80]
            snippet = r.get("content", "")[:200]
            url = r.get("url", "")[:100]
            source = r.get("source", "")
            ranked_lines.append(f"①[{source}] {title}\n   {snippet}\n   {url}")
        ranked_lines.append("")

    # Clustered results — organized by topic
    cluster_lines = []
    if clusters:
        for ci, cluster in enumerate(clusters[:5], 1):
            title = cluster.get("title", "")[:80]
            sources = ", ".join(cluster.get("sources", []))
            cluster_lines.append(
                f"## Cluster {ci}: {title} "
                f"({cluster.get('size', 0)} results, sources: {sources})"
            )
            for ri, rep in enumerate(cluster.get("representatives", [])[:3], 1):
                rep_title = rep.get("title", "")[:80]
                rep_snippet = rep.get("content", "")[:200]
                rep_source = rep.get("source", "")
                cluster_lines.append(
                    f"  [{ri}] [{rep_source}] {rep_title}\n"
                    f"      {rep_snippet}"
                )
            cluster_lines.append("")

    raw_lines = []
    for source, results in raw_by_source.items():
        for r in results[:5]:
            title = r.get("title", "")[:80]
            snippet = r.get("content", "")[:200]
            url = r.get("url", "")[:100]
            raw_lines.append(f"[{source}] {title}\n  {snippet}\n  {url}")
        raw_lines.append("")

    findings_text = ""
    if findings:
        parts = []
        for f in findings:
            conf_label = {"high": "High", "medium": "Med", "low": "Low"}.get(f.get("confidence", "low"), "Low")
            verified = "Verified" if f.get("verified") else "Unverified"
            parts.append(
                f"- [{conf_label}][{verified}] {f.get('fact', '')} "
                f"(sources: {', '.join(f.get('sources', []))})"
            )
        findings_text = "\n".join(parts)
    else:
        findings_text = "(no structured data)"

    conflicts_text = ""
    if conflicts:
        parts = []
        for c in conflicts:
            claims = "; ".join(
                f"{cl['source']}: {cl['value']}" for cl in c.get("claims", [])
            )
            parts.append(f"- {c['topic']}: {claims}")
        conflicts_text = "\n".join(parts)

    ranked_hint = ""
    if ranked_results:
        ranked_hint = (
            f"## Ranked Results (RRF fusion-sorted, top {min(len(ranked_results), 15)} across all sources)\n"
            "These are the most relevant results after cross-source dedup and ranking.\n"
        )

    cluster_hint = ""
    if clusters:
        cluster_hint = (
            f"## Topic Clusters ({len(clusters)} groups)\n"
            "Results are grouped by topic/perspective. Each cluster represents a distinct angle.\n"
        )

    system_prompt = """You are a professional information analysis assistant.
1. Answer the user question based on search results
2. Distinguish facts, opinions, and speculation
3. Mark information sources and confidence levels
4. If data conflicts, present both sides objectively
5. If search results are insufficient, state "insufficient information"

Format:
- Core conclusion first (1-2 sentences)
- Detailed analysis
- Key data points (with sources)
- Conflicts and judgment basis
- Summary

Rules:
- Do not fabricate data, use only provided search results
- Distinguish "verified" (multi-source) from "unverified" (single source)
- If results are poor or irrelevant, state "no effective information found"
- The ranked results are fusion-sorted across sources — prioritize them
- If results are clustered by topic, analyze each cluster as a distinct perspective
- Reply in Chinese"""

    user_prompt = f"""## Question
{question}

## Key Findings (Cross-validated)
{findings_text}

## Data Conflicts
{conflicts_text if conflicts_text else "None"}

{cluster_hint}{chr(10).join(cluster_lines) if cluster_lines else ""}
{ranked_hint}
## Raw Search Results (by source)
{chr(10).join(ranked_lines[:20] + raw_lines[:30])}

## Additional Context
{context or "None"}

Please analyze and answer based on the above information."""

    try:
        from app.llm_client import achat
        answer = await achat([
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ], max_tokens=4000, temperature=0.3)
        return answer if answer else "(LLM returned no answer)"
    except Exception as e:
        return f"(LLM call failed: {str(e)})\n\nFallback summary:\n\n" + _fallback_answer(question, findings, raw_by_source)


def _fallback_answer(question, findings, raw_by_source):
    parts = [f"Search results for: {question}\n"]
    if findings:
        for f in findings[:5]:
            conf = f.get("confidence", "low")
            parts.append(f"- [{conf}] {f.get('fact', '')} (sources: {', '.join(f.get('sources', []))})")
        parts.append("")
    for source, results in raw_by_source.items():
        if results:
            parts.append(f"[{source}]")
            for r in results[:3]:
                parts.append(f"- {r.get('title', '')[:80]}")
            parts.append("")
    return "\n".join(parts) if parts else "No relevant information found"
