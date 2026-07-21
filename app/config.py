import os
import logging
import re
import yaml

logger = logging.getLogger(__name__)
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONFIG_DIR = os.path.join(BASE_DIR, "config")
DATA_DIR = os.path.join(BASE_DIR, "data")

SEARXNG_URL = "http://localhost:8082"
LLM_URL = os.environ.get("LLM_URL", "http://localhost:8080")

# Load source registry
try:
    with open(os.path.join(CONFIG_DIR, "sources.yml"), "r", encoding="utf-8") as f:
        SOURCES_CONFIG = yaml.safe_load(f)["sources"]
    SOURCES = {s["name"]: s for s in SOURCES_CONFIG}
except Exception as e:
    logger.error(f"Failed to load sources.yml: {e}. Using empty config.")
    SOURCES_CONFIG = []
    SOURCES = {}

# Load extraction rules
try:
    with open(os.path.join(CONFIG_DIR, "extraction_rules.yml"), "r", encoding="utf-8") as f:
        EXTRACTION_CONFIG = yaml.safe_load(f)["extraction_rules"]
except Exception as e:
    logger.error(f"Failed to load extraction_rules.yml: {e}. Using empty config.")
    EXTRACTION_CONFIG = {}

# ---------------------------------------------------------------------------
# 8-class search intent detection (regex, no LLM needed)
# Ported from last30days _infer_intent with Chinese keywords added
# ---------------------------------------------------------------------------

SEARCH_INTENTS = {
    "factual": {
        "patterns": [
            r"\b(what is|what are|who is|who acquired|when did|parameter count|release date)\b",
            r"(参数|发布日期|创始人|谁|哪家)",
        ],
    },
    "product": {
        "patterns": [
            r"\b(pricing|feature|features|best .{1,10}\b|top .{1,10}\b)\b",
            r"(功能|价格|定价|特性|配置)",
        ],
    },
    "concept": {
        "patterns": [
            r"\b(explain|concept|protocol|architecture|what does)\b",
            r"(原理|架构|概念|机制|如何工作)",
        ],
    },
    "opinion": {
        "patterns": [
            r"\b(thoughts on|worth it|should i|opinion|review)\b",
            r"(值得吗|评价|怎么样|好用吗|推荐吗|看法|评测)",
        ],
    },
    "how_to": {
        "patterns": [
            r"\b(how to|tutorial|guide|setup|step by step|deploy|install)\b",
            r"(教程|指南|部署|安装|步骤|怎么用|如何部署|如何安装)",
        ],
    },
    "comparison": {
        "patterns": [
            r"\b(vs\.?|versus|compare|compared to|difference between)\b",
            r"(对比|比较|哪个更好|区别)",
            r"[A-Z][a-z]{2,}(?:/[A-Z][a-z]{2,})+",  # "React/Vue/Svelte"
        ],
    },
    "breaking_news": {
        "patterns": [
            r"\b(latest|news|announced|just shipped|launched|released|update)\b",
            r"(最新|发布|公告|刚刚|本周|今天|trending|this week|right now|today|this month)",
        ],
    },
    "prediction": {
        "patterns": [
            r"\b(odds|predict|prediction|forecast|chance|probability|will .* win)\b",
            r"(预测|概率|会吗|趋势)",
        ],
    },
}

# Domain intent (investment/policy) for source routing — kept for backward compat
INTENT_MAP = {
    "investment": {
        "keywords": ["价格", "产能", "市占", "DRAM", "NAND", "HBM", "光模块", "存储", "芯片", "估值", "股价",
                      "ETF", "基金", "增仓", "减仓", "资金", "行情", "A股", "板块", "行业",
                      "电网", "设备", "特高压", "光伏", "储能", "锂电", "新能源", "半导体",
                      "聪明资金", "机构", "北向", "南向", "融资", "融券", "换手"],
        "sources": ["trendforce", "xueqiu", "eastmoney", "google", "bing", "sina_finance", "cninfo", "sogou_wechat"],
    },
    "ai": {
        "keywords": ["AI", "大模型", "LLM", "agent", "transformer", "机器学习", "深度学习", "GPT", "Claude",
                      "stable diffusion", "diffusion", "reinforcement learning", "RLHF", "fine-tune", "微调",
                      "prompt", "RAG", "向量数据库", "embedding", "token", "推理", "训练"],
        "sources": ["google", "github", "zhihu", "twitter", "hacker_news", "github_trending", "v2ex", "youtube", "bilibili"],
    },
    "tech": {
        "keywords": ["芯片", "半导体", "光通信", "服务器", "GPU", "CPU", "FPGA", "硬件",
                      "Docker", "Kubernetes", "K8s", "Linux", "Nginx", "Redis", "MongoDB",
                      "Python", "Go", "Golang", "Rust", "Java", "React", "Vue", "Node.js",
                      "API", "微服务", "分布式", "数据库", "架构", "开源", "框架", "库",
                      "git", "CI/CD", "DevOps", "监控", "日志", "性能优化", "算法",
                      "编程", "开发", "部署", "编译", "调试", "重构", "设计模式"],
        "sources": ["google", "github", "zhihu", "v2ex", "hacker_news", "github_trending", "bing", "youtube", "bilibili"],
    },
    "policy": {
        "keywords": ["政策", "公告", "海关", "统计", "法规", "监管", "工信部", "发改委"],
        "sources": ["stats_gov", "google", "bing"],
    },
}

# ---------------------------------------------------------------------------
# Category exclusion — skip sources irrelevant to the detected domain
# ---------------------------------------------------------------------------

# Sources purely for finance/investment (skip when domain is tech/ai)
_FINANCE_ONLY_SOURCES = {
    "xueqiu", "eastmoney", "trendforce", "coingecko", "binance",
    "fear_greed", "sina_finance", "cninfo", "yahoo_finance",
}

# Sources purely for tech (skip when domain is investment)
# Note: v2ex, youtube, bilibili are NOT here — they are always-on general sources
_TECH_ONLY_SOURCES = {
    "hacker_news", "github_trending",
}

# Sources always included regardless of domain (community + video knowledge)
_ALWAYS_ON_SOURCES = {"v2ex", "youtube", "bilibili"}


def get_excluded_sources_for_domain(domain: str) -> set[str]:
    """Return source names to exclude based on detected domain intent.

    Prevents searching finance sources for tech questions and vice versa.
    """
    if domain in ("tech", "ai"):
        return _FINANCE_ONLY_SOURCES
    elif domain == "investment":
        return _TECH_ONLY_SOURCES
    return set()

# ---------------------------------------------------------------------------
# Source capabilities — each source tagged with its strengths
# ---------------------------------------------------------------------------

SOURCE_CAPABILITIES = {
    "google": {"search", "web"},
    "bing": {"search", "web"},
    "yandex": {"search", "web"},
    "github": {"code", "discussion"},
    "zhihu": {"community", "discussion", "chinese"},
    "xueqiu": {"investment", "discussion", "chinese"},
    "twitter": {"social", "discussion", "breaking"},
    "trendforce": {"investment", "data"},
    "eastmoney": {"investment", "data", "chinese"},
    "v2ex": {"community", "discussion", "chinese"},
    "sogou_wechat": {"social", "chinese"},
    "stats_gov": {"policy", "data", "chinese"},
    # API sources
    "hacker_news": {"community", "discussion", "tech"},
    "github_trending": {"code", "discussion", "tech"},
    "rsshub": {"social", "search", "chinese", "breaking"},
    "yahoo_finance": {"investment", "data", "search"},
    "coingecko": {"investment", "data"},
    "binance": {"investment", "data"},
    "fear_greed": {"investment", "data"},
    "world_bank": {"policy", "data"},
    "sec_edgar": {"investment", "data", "policy"},
    "cninfo": {"investment", "data", "policy", "chinese"},
    "sina_finance": {"investment", "data", "chinese"},
    # Video sources
    "youtube": {"search", "video", "tech"},
    "bilibili": {"search", "video", "tech", "chinese"},
}

# Intent → preferred capabilities → source selection
INTENT_CAPABILITY_MAP = {
    "factual": {"search", "web", "data"},
    "product": {"search", "web", "community"},
    "concept": {"search", "web", "discussion", "video"},
    "opinion": {"community", "discussion", "social"},
    "how_to": {"search", "web", "code", "discussion", "video"},
    "comparison": {"community", "discussion", "social"},
    "breaking_news": {"social", "search", "breaking"},
    "prediction": {"social", "search", "investment"},
    "default": {"search", "web"},
}

DEFAULT_SOURCES = ["google", "bing", "sogou_wechat"]


def get_all_source_names() -> list[str]:
    """Return all registered source names."""
    return list(SOURCES.keys())


def detect_search_intent(query: str) -> str:
    """Detect search intent from query using regex patterns (no LLM).
    Returns one of: factual, product, concept, opinion, how_to, comparison,
    breaking_news, prediction, concept (default).
    """
    text = query.lower().strip()
    # Order matters: more specific intents first
    for intent in ("comparison", "prediction", "how_to", "breaking_news",
                   "opinion", "factual", "product", "concept"):
        info = SEARCH_INTENTS.get(intent)
        if not info:
            continue
        for pattern in info["patterns"]:
            if re.search(pattern, text):
                return intent
    # Default: concept (evergreen_ok freshness — safer than breaking_news)
    return "concept"


def detect_intent(query: str) -> str:
    """Detect query intent from keywords. Returns domain category name.
    Kept for backward compatibility with existing code.
    """
    query_lower = query.lower()
    for category, info in INTENT_MAP.items():
        for kw in info["keywords"]:
            if kw.lower() in query_lower:
                return category
    return "general"


def get_sources_for_intent(intent: str) -> list[str]:
    """Select sources by intent → capability matching.
    Returns sources that match the intent's preferred capabilities,
    sorted by priority from SOURCES config.
    """
    target_caps = INTENT_CAPABILITY_MAP.get(intent, INTENT_CAPABILITY_MAP["default"])
    matched = [
        (s.get("priority", 99), name)
        for name, s in SOURCES.items()
        if SOURCE_CAPABILITIES.get(name, set()) & target_caps
    ]
    if not matched:
        return DEFAULT_SOURCES
    matched.sort()
    return [name for _, name in matched]


def get_sources_for_category(category: str) -> list[str]:
    """Get prioritized source list for a domain category.
    Tries search intent first, then domain category, then default.
    """
    if category in INTENT_MAP:
        return INTENT_MAP[category]["sources"]
    return DEFAULT_SOURCES


# ---------------------------------------------------------------------------
# Intent modifier stripping — prevent literal echo failure
# (e.g. "Hermes Agent Use Cases" → "Hermes Agent", "Claude Code 使用案例" → "Claude Code")
# ---------------------------------------------------------------------------

_INTENT_MODIFIER_PATTERNS_ZH = (
    "使用案例", "使用场景", "工作流程", "教程", "指南",
    "评测", "评价", "对比", "比较", "推荐", "选择",
    "实战", "实践", "经验", "心得", "入门",
)

_INTENT_MODIFIER_PATTERNS_EN = (
    "use cases", "use case", "workflows", "workflow",
    "examples", "example", "tutorial", "tutorials",
    "review", "reviews", "comparison", "applications",
    "in practice", "production use", "production",
)


def has_intent_modifier(query: str) -> bool:
    """Check if query contains an intent modifier phrase."""
    text = query.strip()
    text_lower = text.lower()
    for mod in _INTENT_MODIFIER_PATTERNS_ZH:
        if mod in text:
            return True
    for mod in _INTENT_MODIFIER_PATTERNS_EN:
        if mod in text_lower:
            return True
    return False


def strip_intent_modifiers(query: str) -> str:
    """Strip intent modifier phrases from query, return core subject.
    Prevents literal echo failure.
    """
    text = query.strip()
    text_lower = text.lower()

    for mod in _INTENT_MODIFIER_PATTERNS_ZH:
        if mod in text:
            text = text.replace(mod, '').strip()
            break
    else:
        for mod in _INTENT_MODIFIER_PATTERNS_EN:
            if mod in text_lower:
                idx = text_lower.index(mod)
                text = text[:idx] + text[idx + len(mod):]
                text = text.strip()
                break

    return text or query
