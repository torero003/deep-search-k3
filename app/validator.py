"""Cross-validation engine: fact extraction -> multi-source comparison -> confidence scoring."""
import re
from collections import defaultdict

# CJK character class: basic block + Extension A + fullwidth + halfwidth
_CJK = r"一-鿿㐀-䶿豈-﫿"

# Authority weights for sources (higher = more trusted)
SOURCE_WEIGHT = {
    "trendforce": 10,
    "stats_gov": 10,
    "google": 5,
    "bing": 5,
    "yandex": 4,
    "github": 6,
    "zhihu": 3,
    "xueqiu": 4,
    "twitter": 3,
    "v2ex": 3,
    "sogou_wechat": 2,
    "eastmoney": 4,
}

# Entity name pattern: alphanumeric + CJK chars
_ENT = rf"[A-Za-z0-9{_CJK}]{{2,10}}"
_ENT_LONG = rf"[A-Za-z0-9{_CJK}]{{3,20}}"


def extract_facts(results: list[dict]) -> list[dict]:
    """Extract factual claims from search results.
    Each fact: {entity, attribute, value, source, url}
    """
    facts = []
    seen = set()
    patterns = [
        # "X price increased/decreased by Y%"
        (
            rf"({_ENT})\s*(价格|价|合约价|现货价)\s*(上涨|下跌|增长|下降|上调|下调)\s*([0-9]+\.?[0-9]*)\s*[-~]\s*([0-9]+\.?[0-9]*)\s*%",
            lambda m, src, url: {
                "entity": m.group(1),
                "attribute": "price_change",
                "value": f"{m.group(3)}-{m.group(4)}% {m.group(2)}{m.group(3)}",
                "source": src,
                "url": url,
                "raw": m.group(0),
            },
        ),
        # "X price increased/decreased by Y%" (single number)
        (
            rf"({_ENT})\s*(价格|价|合约价|现货价)\s*(上涨|下跌|增长|下降|上调|下调)\s*([0-9]+\.?[0-9]*)\s*%",
            lambda m, src, url: {
                "entity": m.group(1),
                "attribute": "price_change",
                "value": f"{m.group(4)}% {m.group(3)}",
                "source": src,
                "url": url,
                "raw": m.group(0),
            },
        ),
        # "X capacity/utilization at Y%"
        (
            rf"({_ENT})\s*(产能|利用率|市占率|市场份额)\s*([0-9]+\.?[0-9]*)\s*%",
            lambda m, src, url: {
                "entity": m.group(1),
                "attribute": m.group(2),
                "value": f"{m.group(3)}%",
                "source": src,
                "url": url,
                "raw": m.group(0),
            },
        ),
        # "X price is $Y/GB"
        (
            rf"({_ENT})\s*(价格|价)\s*[\$￥]?\s*([0-9]+\.?[0-9]*)\s*/\s*(GB|G|MB|片)",
            lambda m, src, url: {
                "entity": m.group(1),
                "attribute": "price",
                "value": f"{m.group(3)}/{m.group(4)}",
                "source": src,
                "url": url,
                "raw": m.group(0),
            },
        ),
        # "X reached Y%"
        (
            rf"({_ENT})\s*(达到|已达|突破)\s*([0-9]+\.?[0-9]*)\s*%",
            lambda m, src, url: {
                "entity": m.group(1),
                "attribute": "milestone",
                "value": f"{m.group(3)}%",
                "source": src,
                "url": url,
                "raw": m.group(0),
            },
        ),
        # "X is Y billion/million"
        (
            rf"({_ENT})\s*(规模|收入|营收|投资|支出)\s*([0-9]+\.?[0-9]*)\s*(亿|百万|千|万)\s*(美元|元|人民币)?",
            lambda m, src, url: {
                "entity": m.group(1),
                "attribute": m.group(2),
                "value": f"{m.group(3)}{m.group(4)}{m.group(5 or '')}",
                "source": src,
                "url": url,
                "raw": m.group(0),
            },
        ),
        # "X growth rate Y%"
        (
            rf"({_ENT})\s*(增长率|增速|同比)\s*([0-9]+\.?[0-9]*)\s*%",
            lambda m, src, url: {
                "entity": m.group(1),
                "attribute": m.group(2),
                "value": f"{m.group(3)}%",
                "source": src,
                "url": url,
                "raw": m.group(0),
            },
        ),
        # Date + event: "2026年X月 X事件"
        (
            rf"([0-9]{{4}}年[0-9]{{1,2}}月)\s*({_ENT_LONG})",
            lambda m, src, url: {
                "entity": m.group(2),
                "attribute": "time_event",
                "value": m.group(1),
                "source": src,
                "url": url,
                "raw": m.group(0),
            },
        ),
    ]

    for result in results:
        text = (result.get("content", "") + " " + result.get("title", "")).strip()
        if not text:
            continue
        for pattern, extractor in patterns:
            for match in re.finditer(pattern, text):
                fact = extractor(match, result.get("source", ""), result.get("url", ""))
                key = (fact["entity"], fact["attribute"], fact["value"])
                if key not in seen:
                    seen.add(key)
                    facts.append(fact)

    return facts


def cross_validate(results: list[dict]) -> dict:
    """Cross-validate search results.
    Returns: {key_findings: [...], conflicts: [...]}
    key_findings elements are dicts: {fact, entity, attribute, value,
    confidence, sources, urls, source_count, verified, conflict}
    """
    facts = extract_facts(results)
    if not facts:
        return {"key_findings": [], "conflicts": []}

    # Group by (entity, attribute)
    groups = defaultdict(list)
    for fact in facts:
        key = (fact["entity"], fact["attribute"])
        groups[key].append(fact)

    key_findings = []
    conflicts = []

    for (entity, attr), group in groups.items():
        sources = set(f["source"] for f in group)
        values = set(f["value"] for f in group)

        # Confidence based on source count
        if len(sources) >= 3:
            confidence = "high"
        elif len(sources) == 2:
            confidence = "medium"
        else:
            confidence = "low"

        # Check for conflicts
        if len(values) > 1:
            # Different values from different sources
            claims = []
            for f in group:
                claims.append({
                    "value": f["value"],
                    "source": f["source"],
                    "weight": SOURCE_WEIGHT.get(f["source"], 1),
                    "url": f["url"],
                })
            # Sort by weight descending
            claims.sort(key=lambda c: c["weight"], reverse=True)
            best = claims[0]

            conflicts.append({
                "topic": f"{entity} {attr}",
                "claims": claims,
                "resolution": f"优先采用 {best['source']} 的数据（权重最高）",
            })

            key_findings.append({
                "fact": f"{entity} {attr}: {best['value']}",
                "entity": entity,
                "attribute": attr,
                "value": best["value"],
                "confidence": confidence,
                "sources": list(sources),
                "urls": list(dict.fromkeys(f["url"] for f in group if f.get("url"))),
                "source_count": len(sources),
                "verified": len(sources) >= 2,
                "conflict": True,
                "all_values": list(values),
            })
        else:
            # Consistent values
            val = group[0]["value"]
            key_findings.append({
                "fact": f"{entity} {attr}: {val}",
                "entity": entity,
                "attribute": attr,
                "value": val,
                "confidence": confidence,
                "sources": list(sources),
                "urls": list(dict.fromkeys(f["url"] for f in group if f.get("url"))),
                "source_count": len(sources),
                "verified": len(sources) >= 2,
                "conflict": False,
            })

    # Sort findings by confidence (high first) and source count
    confidence_order = {"high": 0, "medium": 1, "low": 2}
    key_findings.sort(key=lambda f: (confidence_order.get(f["confidence"], 3), -f["source_count"]))

    return {"key_findings": key_findings, "conflicts": conflicts}


def _source_quality_score(source: str) -> float:
    """Return a 0-1 quality score for a source based on authority weight."""
    w = SOURCE_WEIGHT.get(source, 1)
    # Map 1-10 → 0.3-1.0 with log scaling
    import math
    return min(1.0, 0.3 + 0.7 * math.log(w + 1) / math.log(11))


def summarize_results(results: list[dict], category: str = "") -> dict:
    """Full result processing: group by source, extract facts, cross-validate."""
    # Group raw results by source
    by_source = defaultdict(list)
    for r in results:
        by_source[r.get("source", "unknown")].append(r)

    # Cross-validate
    validation = cross_validate(results)

    # Source quality summary
    source_quality = {
        src: {
            "score": _source_quality_score(src),
            "weight": SOURCE_WEIGHT.get(src, 1),
            "result_count": len(res),
        }
        for src, res in by_source.items()
    }

    return {
        "key_findings": validation["key_findings"],
        "conflicts": validation["conflicts"],
        "total_results": len(results),
        "sources_used": len(by_source),
        "source_quality": source_quality,
    }
