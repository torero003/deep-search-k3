# deep-search k3

**20+ 个信息源，一次并行搜索。搜索结果由点赞、评论、转发和交叉验证排序——不是 SEO。**

> k3 是 datasearch/platform（smart-search）的修复增强版（2026-07-21），原项目保持不变。变更明细见下文「k3 变更」。

Google 聚合编辑，Bing 聚合广告。smart-search 聚合人：知乎上 2000+ 赞同的回答，雪球里机构投资者的深度分析，GitHub 上 3000+ stars 的 Issue，YouTube 上 45 分钟技术视频的字幕全文，Reddit 热帖的顶级评论，Twitter 上第一时间反应。25 个源并行搜索，RRF 融合，LLM 评分，交叉验证——输出一个结构化简报。

**零付费 API。零配置。你的浏览器就是入口。**

```bash
python search.py "HBM 价格走势" --all --max 50 --json
```

30 秒后，你得到的不是 10 个链接。你得到的是 TrendForce 的 DRAM 合约价、雪球上机构投资者的分析、东方财富的公告、Yahoo Finance 的实时行情——四源交叉验证后，告诉你哪些数据是一致的，哪些有冲突，哪些该优先采信。

## k3 变更（2026-07-21）

**修复**
- `raw_results_by_source` 断链：`/search` 响应从不返回该字段，导致 `/query` 证据链与竞品发现恒空——已接通（每源最多 10 条）
- `/query` 的 `_merge_findings` 去重 bug：正则提取的事实除第一条外被静默丢弃——已修复
- yahoo_finance 把发布者名当日期、sogou_wechat 把公众号名当日期——已改为真实时间戳
- 新浪 A 股硬编码表：招商银行误映射万科代码 sz000002 → sh600036；三一重工非法前缀 sz600031 → sh600031；删除无法服务的港股（腾讯/美团）
- `_github_resolve_token` 的 `mem_file.exists` 漏括号永真——已修复

**新增**
- **全源发布日期**：eastmoney / bilibili / zhihu / xueqiu 的 CDP 提取器现在抓取页面日期（含"3天前"等相对时间归一化为 `YYYY-MM-DD`）；API 源原本已有。每条结果的 `published_date` 键恒存在（未知为 `""`）
- **`--freshness` 时效过滤**：如 `--freshness 7d`（也支持 `24h`、纯数字天数）。丢弃日期可解析且过期的条目，无日期条目保留；metadata 记录 `freshness_filtered` 条数
- **结构化 key_findings**：交叉验证事实保留 `entity / attribute / value / urls` 字段，不再压扁成纯文本
- `summary.raw_results_by_source`：按源分组的原始结果（每源 10 条）

**已知限制**
- google / bing / twitter / v2ex 页面无可靠日期节点，结果日期为空
- `extract_facts` 的正则覆盖有限（如"上涨20%至30%"这类区间表述抓不到），key_findings 经常为空
- timeseries 表与 `/history` 端点仍是空壳；sources.yml 与代码漂移（以代码为准）
- yandex / stats_gov / rsshub / sec_edgar / trendforce 在代码中标记为不可用（BROKEN_SOURCES），实际可用约 20 源

## 为什么存在

信息分散在 25 个平台里。每个平台有自己的围墙：知乎需要登录，雪球需要登录，Twitter 需要登录，YouTube 有 API 但不给全文字幕，GitHub 有 API 但不跨平台。没有哪个搜索引擎能同时触达它们。

Google 搜不到知乎回答。ChatGPT 的训练数据落后几个月。Claude 不能直接搜索雪球。每个平台都是孤岛。

但你已经在 Edge 浏览器里登录了这些账号。smart-search 通过 CDP 直连你的浏览器，用你的登录态搜索所有平台——然后 RRF 融合、LLM 评分、交叉验证，告诉你什么最重要。

技术调研、投资分析、政策追踪、突发新闻——一个命令搞定。

## 信息源（约 20 个可用，5 个已标记不可用）

| 源 | 它告诉你什么 |
|---|---|
| **Google** | 全网覆盖的基线。新闻、博客、文档，什么都搜得到 |
| **Bing** | 微软生态 + 英文新闻的第一选择 |
| **知乎** | 中文深度分析。2000+ 赞同的回答比任何博客都靠谱 |
| **雪球** | 中国投资者的真实判断。机构持仓、个股讨论、行业分析 |
| **东方财富** | A 股公告、研报、资金流向。一手财经数据 |
| **V2EX** | 中文技术社区。开发者在讨论什么，这里最先知道 |
| **Twitter (X)** | 第一时间反应。专家推文、突发新闻、行业 KOL 观点 |
| **YouTube** | 45 分钟的技术深潜。不只是标题，还有完整字幕全文 |
| **Bilibili** | 中文技术视频。UP 主的架构解析、项目实战、产品评测 |
| **GitHub** | 代码世界的脉搏。3000+ reactions 的 Issue 比任何评测都有说服力 |
| **Hacker News** | 开发者共识。825 points, 899 comments——技术人真正争论的地方 |
| **Reddit** | 英文社区的无过滤声音。热帖 + 顶级评论，带 upvote 计数 |
| **TrendForce** | 半导体/存储产业价格与产能。机构级数据 |
| **CoinGecko** | 加密货币实时价格和市值 |
| **Binance** | 交易所实时行情和交易量 |
| **Yahoo Finance** | 全球股市行情、财报、分析师评级 |
| **巨潮资讯** | 中国上市公司法定披露 |
| **新浪财经** | 中文财经新闻和实时行情 |
| **世界银行** | 全球宏观经济数据 |
| **SEC EDGAR** | 美国上市公司法定文件 |
| **国家统计局** | 中国官方统计数据 |
| **GitHub Trending** | 本周最火的开源项目 |
| **搜狗微信** | 微信公众号文章搜索引擎 |
| **RSSHub** | 聚合各平台 RSS  feed |

一个知乎 2000+ 赞同的回答比一个无人访问的博客更有信号。一个 300 万 views 的 YouTube 视频比一篇公关稿更能告诉你什么是文化热点。TrendForce 的产业数据比分析师猜测更难反驳。

smart-search 不只看相关性。它看人们实际参与了什么。

## 人们用它做什么

**技术选型前。** `search.py "Kubernetes 和 Docker 的区别"` — GitHub 上高赞 Issue 讨论实际部署痛点，V2EX 上有中国开发者的容器化经验，HN 上 800+ 分的架构辩论，YouTube 上有 45 分钟的系统设计深潜（带完整字幕）。不是 10 个链接，是多源融合后的结构化对比。

**投资决策前。** `search.py "HBM 价格走势"` — TrendForce 给出 DRAM 合约价上涨 3-5%，雪球上机构投资者讨论产能瓶颈，东方财富有相关公司公告，Yahoo Finance 有美光股价。四源交叉验证：三源一致的标 `Verified`，冲突的按源权重排序。

**追踪政策变化。** `search.py "最新半导体扶持政策"` — 国家统计局数据 + 搜索引擎新闻 + 搜狗微信公众号文章。政策原文、解读、影响分析，一次搜索全拿到。

**突发新闻。** `search.py "GPT-5 发布"` — Twitter 第一时间反应，HN 技术社区讨论，Reddit 热帖评论，YouTube 评测视频。10 分钟内知道发生了什么，30 秒内知道什么最重要。

**Agent 集成。** 不需要启动服务器。`python search.py` 直接输出 JSON，Agent 直接读取。零 HTTP 开销。

## 工作原理

1. **你输入一个查询。** 人、公司、产品、技术、"X vs Y"。任何话题。
2. **平台识别意图。** 8 种意图（事实查询、产品对比、概念解释、观点征集、教程指南、比较分析、突发新闻、趋势预测）+ 4 大领域（投资、AI、技术、政策）。技术问题自动排除财经源，投资问题自动排除技术社区。
3. **所有相关源并行搜索。** CDP 浏览器通道（Google、知乎、雪球、Twitter、B站...）和直接 API 通道（YouTube yt-dlp、GitHub API、HN Algolia、Reddit RSS...）同时执行。
4. **RRF 跨源融合。** 25 个源的排名结果通过 Reciprocal Rank Fusion 融合，经过五阶段去重（URL 去重 → 近似去重 → 实体锚定降权 → 作者去重 → 源多样性保证）。
5. **LLM 评分 + 交叉验证。** 本地 LLM 对每条结果 0-100 评分，结合 engagement（点赞、评论、转发）归一化排序。同时 8 种正则模式提取结构化事实，多源交叉比对。
6. **输出结构化结果。** JSON 或 Markdown。每条结果标注来源、置信度、验证状态。

## 核心优势

### 意图驱动的智能源选择

不是所有查询都需要所有源。平台自动识别查询意图和领域，排除不相关源：

- `Rust vs Go 微服务` → GitHub / V2EX / 知乎 / YouTube / B站，排除雪球、东方财富
- `HBM 价格走势` → 雪球 / 东方财富 / TrendForce / Yahoo Finance，排除 HN、GitHub Trending
- `Kubernetes 和 Docker 的区别` → 全源并行（技术和通用交叉）

### RRF 跨源融合 + 五阶段去重

基于 Reciprocal Rank Fusion (k=60)，五个处理阶段确保结果质量：

1. **URL 级去重** — 同 URL 跨源出现时合并 RRF 分数，保留更完整的摘要
2. **近似去重** — n-gram Jaccard + token Jaccard 双阈值，中英文近重复内容都能识别
3. **实体锚定降权** — 结果文本与查询关键词零重叠的结果降权 10 倍
4. **作者去重** — 同一作者最多保留 3 条，防止单一声音垄断
5. **源多样性保证** — 截断前确保每个有结果的源至少保留 2 条

### LLM 相关性评分

本地 LLM（Qwen3.6-27B）对融合后的结果进行 0-100 相关性评分，结合 engagement min-max 归一化：

```
综合得分 = 0.70 × LLM评分 + 0.15 × 参与度 + 0.10 × 源权重 + 0.05 × 内容长度
```

LLM 不可用时自动降级为确定性评分（实体匹配 + 标题加分 + 源质量 + 内容长度）。

### 交叉验证引擎

8 种正则模式从搜索结果中提取结构化事实（价格变动、产能利用率、市占率、营收规模、增长率、时间节点等），多源交叉比对：

- **3 源以上一致** → `High` 置信度 + `Verified`
- **2 源一致** → `Medium` + `Verified`
- **单源** → `Low` + `Unverified`
- **冲突数据** → 按源权重排序，标注优先级

### 视频不只是标题

YouTube 通过 yt-dlp 自动下载并解析字幕（优先中文，其次英文），Bilibili 通过 CDP 浏览器提取字幕数据。45 分钟的视频不只是标题和链接——字幕全文附加在结果中，可被交叉验证和引用。

### 主题聚类

观点征集、比较分析、突发新闻、趋势预测四类查询，自动按主题聚类。每个聚类通过 MMR 选择最具代表性的 3 条结果。同一故事在知乎、Twitter、HN 同时出现？合并成一个聚类，不是三个独立条目。

## 快速开始

```bash
# 前置条件：Edge 浏览器以 CDP 模式运行
# 启动 Edge: msedge.exe --remote-debugging-port=9222

# 全源搜索（自动智能选源）
python search.py "Kubernetes 和 Docker 的区别" --all --max 50 --json

# 指定源搜索
python search.py "HBM 价格走势" --sources xueqiu,eastmoney,cninfo --max 20 --json

# 时效过滤：只保留 7 天内的结果（无日期的条目保留）
python search.py "HBM 价格走势" --all --max 30 --json --freshness 7d

# 人类可读输出
python search.py "最新 AI 大模型" --all --max 20
```

## 架构

```
用户查询
  │
  ▼
search.py (CLI 入口)
  │
  ▼
意图识别 (8 类意图 + 4 大领域) → 智能源选择 → 排除不相关源
  │
  ▼
并行搜索 (asyncio.gather)
  ├── CDP 浏览器通道  ──→ Google, Bing, 知乎, 雪球, Twitter, B站, ...
  └── 直接 API 通道    ──→ YouTube(yt-dlp), GitHub, HN(Algolia),
                          Reddit(RSS), CoinGecko, Yahoo Finance, ...
  │
  ▼
RRF 融合 (k=60) → 五阶段去重 → 实体锚定降权
  │
  ▼
LLM 相关性评分 → engagement 归一化 → 综合排序
  │
  ▼
交叉验证 (事实提取 + 多源比对) + 主题聚类
  │
  ▼
结构化 JSON / Markdown 输出
```

## 项目结构

```
├── search.py                    # CLI 入口脚本
├── config/
│   ├── sources.yml              # 25+ 源配置（类型/优先级/分类/搜索模板）
│   └── extraction_rules.yml     # 各源数据提取规则
├── app/
│   ├── api/
│   │   └── search.py            # 核心搜索管线
│   ├── sources/
│   │   ├── api_source.py        # 所有 API 源实现（YouTube/GitHub/HN/Reddit/财经等）
│   │   ├── cdp_client.py        # CDP 浏览器直连客户端
│   │   ├── edge_mcp_source.py   # CDP 浏览器源基类（登录检测/自动恢复）
│   │   ├── reddit_source.py     # Reddit RSS + .json 双通道
│   │   └── video_transcript.py  # YouTube/B站字幕提取
│   ├── config.py                # 意图识别、源能力标签、分类排除规则
│   ├── fusion.py                # RRF 融合 + 五阶段去重
│   ├── judge.py                 # LLM 相关性评分 + engagement 归一化
│   ├── validator.py             # 交叉验证引擎（事实提取 + 多源比对）
│   ├── cluster.py               # 主题聚类 + MMR 代表选择
│   ├── llm_client.py            # 本地 LLM 客户端
│   ├── cache.py                 # SQLite 缓存（每源独立 TTL）
│   └── storage/                 # SQLite 存储层
└── requirements.txt
```

## 技术栈

- **CDP (Chrome DevTools Protocol)** — WebSocket 直连 Edge 浏览器，自动化搜索和登录检测
- **yt-dlp** — YouTube 搜索和字幕提取，无需浏览器
- **Asyncio** — 所有搜索源并行执行，单查询 30-120 秒完成
- **SQLite** — 每源独立缓存（TTL 60s-3600s），减少重复请求

## 配置

- `config/sources.yml` — 源注册表：类型（`api` / `edge_mcp`）、优先级、分类、搜索模板、登录配置
- `config/extraction_rules.yml` — 各源专属数据提取规则

## 开源

MIT 许可证。无追踪。无遥测。你的搜索数据留在你的机器上。
