<p align="center">
  <img src="docs/logo.svg" width="120" alt="DeepSearch K3">
</p>

<h1 align="center">DeepSearch K3</h1>

<p align="center">
  <img src="https://img.shields.io/badge/sources-25%20integrated-ff6b4a?style=flat-square" alt="sources">
  <img src="https://img.shields.io/badge/APIs-0%20paid-00c853?style=flat-square" alt="no paid apis">
  <img src="https://img.shields.io/badge/privacy-local--only-00b8d4?style=flat-square" alt="local only">
  <img src="https://img.shields.io/badge/python-3.9+-3776ab?style=flat-square" alt="python">
  <img src="https://img.shields.io/badge/license-MIT-4c956c?style=flat-square" alt="license">
</p>

<p align="center">
  <b>25 个信息源，一次并行搜索。按点赞、评论、转发和跨源交叉验证排序——不是 SEO。</b><br>
  Google 聚合广告，Google 编辑聚合排名。K3 聚合<b>人</b>。
</p>

---

## 它给你什么

一条命令，30-120 秒，你得到的不是 10 个链接，而是一份**结构化情报简报**：

- 知乎上 **2000+ 赞同**的回答、雪球机构投资者的深度分析
- GitHub 上 **3000+ reactions** 的 Issue、Hacker News 上 825 分的架构辩论
- YouTube **45 分钟技术视频的完整字幕全文**、Reddit 热帖的顶级评论
- TrendForce 的 DRAM 合约价、东方财富的公告、Yahoo Finance 的实时行情
- 四源交叉验证后告诉你：**哪些数据一致，哪些冲突，该优先采信谁**

```bash
python search.py "HBM 价格走势" --all --max 50 --json
```

**零付费 API。零服务器（可选）。你的浏览器登录态就是数据入口。**

## 为什么存在

信息分散在 25 个平台，每个平台都有围墙：

| 围墙 | K3 的解法 |
|---|---|
| 知乎 / 雪球 / Twitter 需要登录 | CDP 直连你的浏览器，**用你自己的登录态**搜索 |
| YouTube API 不给完整字幕 | yt-dlp 提取字幕全文，视频不再只是标题 |
| 中文平台海外 API 不可达 | 浏览器通道 + 直连 API 双轨并行 |
| 每个搜索引擎只索引自己的世界 | 25 源并行，RRF 融合，没有单源盲区 |
| LLM 训练数据滞后 | 实时抓、实时交叉验证，标注发布日期 |

## 架构

```
        你的查询
            │
            ▼
   ┌─────────────────────┐
   │  意图识别            │  8 类意图 × 4 大领域（纯规则，毫秒级）
   │  智能源选择          │  技术问题 → 排除财经源；投资问题 → 排除技术社区
   └──────────┬──────────┘
              │
     ┌────────┴────────┐
     ▼                 ▼
┌─────────────┐  ┌──────────────────┐
│ CDP 浏览器   │  │ 直接 API          │  asyncio 全并行
│ 通道        │  │ 通道              │
│ Google/Bing │  │ YouTube(yt-dlp)  │
│ 知乎/雪球    │  │ GitHub / HN      │
│ Twitter/B站  │  │ Reddit / 巨潮    │
│ 东方财富 ...  │  │ CoinGecko ...    │
└──────┬──────┘  └────────┬─────────┘
       └────────┬─────────┘
                ▼
   ┌─────────────────────┐
   │ RRF 融合 (k=60)      │  25 源排名 → 统一分数量纲
   │ 五阶段去重            │  URL → 近似 → 实体锚定 → 作者 → 源多样性
   └──────────┬──────────┘
              ▼
   ┌─────────────────────┐
   │ LLM 评分 + 交叉验证   │  0.70×LLM + 0.15×参与度 + 0.10×源权重 + 0.05×长度
   │ + 主题聚类 (MMR)      │  8 类正则提取结构化事实，多源比对
   └──────────┬──────────┘
              ▼
   结构化 JSON / Markdown
   每条结果：标题 · URL · 摘要 · 来源 · 发布日期 · 置信度 · 验证状态
```

## 核心能力

### 意图驱动的智能源选择

不是所有查询都需要所有源。平台识别查询意图后自动裁剪：

| 查询 | 自动启用 | 自动排除 |
|---|---|---|
| `Rust vs Go 微服务` | GitHub · V2EX · 知乎 · YouTube · B站 | 雪球、东方财富、CoinGecko |
| `HBM 价格走势` | 雪球 · 东方财富 · Yahoo Finance · 巨潮 | HN、GitHub Trending、视频源 |
| `Kubernetes 和 Docker 的区别` | 全源并行 | — |
| 非技术类查询 | — | 视频源 + 字幕提取（省 40-70 秒） |

### 跨源交叉验证引擎

8 类正则模式提取结构化事实（价格变动、产能利用率、市占率、营收、增长率、时间节点），多源比对：

- **3 源以上一致** → `High` 置信度 + `Verified`
- **2 源一致** → `Medium` + `Verified`
- **单源** → `Low` + `Unverified`
- **数据冲突** → 按源权重排序，标注优先级

每条结果带 `published_date`，支持 `--freshness 7d` 时效过滤——"最新"不再靠运气。

### 批量并行模式（行业调研利器）

单进程跑几十个查询，共享登录态缓存与浏览器 tab 预算，自动断点续跑：

```bash
python search.py --batch tasks.json --concurrency 6 --tab-budget 12
# 已有的 output 自动跳过 → 中断后直接重跑即可续传
```

### 视频不只是标题

YouTube 自动提取字幕全文（优先中文），B站 通过浏览器提取。45 分钟的系统设计深潜 = 结果里的完整文本，可被交叉验证和引用。

### Agent 原生

CLI 直出 JSON，Agent 直接读取，零 HTTP 开销。自带 `skill.md`，Claude Code 等 Agent 框架开箱即用。

## 信息源

| 类型 | 源 | 它告诉你什么 |
|---|---|---|
| 搜索引擎 | **Google** · **Bing** | 全网覆盖基线 |
| 中文社区 | **知乎** · **雪球** · **V2EX** · 搜狗微信 | 深度分析 · 投资者判断 · 开发者讨论 |
| 财经 | 东方财富 · 巨潮 · 新浪 · Yahoo Finance | A股公告 · 法定披露 · 全球行情 |
| 加密/宏观 | CoinGecko · Binance · Fear&Greed · 世界银行 | 实时行情 · 恐惧贪婪指数 · 宏观数据 |
| 社交媒体 | **Twitter (X)** · Reddit · HN | 第一反应 · 热帖评论 · 开发者共识 |
| 代码 | **GitHub** · GitHub Trending | Issue 脉搏 · 本周最热项目 |
| 视频 | **YouTube** · **Bilibili** | 完整字幕 · 架构解析 · 项目实战 |
| 专业/官方 | TrendForce · SEC EDGAR · 国家统计局 · RSSHub | 产业价格 · 法定文件 · 官方统计 |

> 当前约 20/25 源可用；yandex（CAPTCHA）、sec_edgar（区域封锁）、stats_gov、rsshub（Cloudflare）、trendforce 在代码中已标记为 broken，失败源自动跳过、不阻塞整体搜索。

## 快速开始

**前置条件**（总共 3 步）：

```bash
# 1. 装依赖
pip install -r requirements.txt

# 2. 以 CDP 模式启动浏览器（Edge 或 Chrome，需提前登录知乎/雪球/Twitter 等）
msedge.exe --remote-debugging-port=9222      # Windows
# mac / linux:
# google-chrome --remote-debugging-port=9222

# 3. 验证 CDP 连通
curl -s http://localhost:9222/json/version
```

**搜索**：

```bash
# 全源搜索（智能选源）
python search.py "Kubernetes 和 Docker 的区别" --all --max 50 --json

# 指定源
python search.py "HBM 价格走势" --sources xueqiu,eastmoney,cninfo --max 20 --json

# 时效过滤（只保留 7 天内）
python search.py "最新 AI 大模型" --all --max 30 --json --freshness 7d

# 人类可读输出
python search.py "最新 AI 大模型" --all --max 20
```

**可选配置**（环境变量）：

| 变量 | 说明 | 默认 |
|---|---|---|
| `LLM_URL` | OpenAI 兼容 LLM 地址（仅 `--judge` 重排序需要） | `http://localhost:8080` |
| `LLM_MODEL` | LLM 模型名 | `Qwen3.6-27B` |
| `YT_DLP_PYTHON` | 装有 yt-dlp 的解释器 | 当前解释器 |

> LLM 不可用/未配置时自动降级为确定性评分（实体匹配 + 源质量 + 内容长度），搜索永远可用。

## 输出长什么样

```
搜索结果（共 30 条，来自 14 个源，耗时 68,412ms）

1. [xueqiu] [2026-08-12] 存储三巨头：HBM4 产能被预订到 2027
   观点... (likes=4521, comments=389)
   https://xueqiu.com/...
2. [eastmoney] [2026-08-11] 某存储公司公告：HBM 产线扩产 40%
   公告摘要...
   ...

关键发现：
  - [High][Verified] DRAM 合约价环比上涨 3-5%   (来源: trendforce, yahoo_finance, xueqiu)
  - [Medium][Verified] HBM 产能利用率 2026Q3 达 95% (来源: xueqiu, eastmoney)
  - [Low][Unverified] 某厂 HBM4 良率突破 80%    (来源: zhihu)

数据冲突：
  - DRAM 涨幅: TrendForce 报 3-5%，雪球帖称 8-10% → 优先采信 TrendForce（源权重高）
```

## 项目结构

```
├── search.py                    # CLI 入口（单查询 + 批量模式）
├── run_server.py                # 可选：API 服务器（端口 8085）
├── skill.md                     # Agent skill 定义（Claude Code 等开箱即用）
├── config/
│   ├── sources.yml              # 25 源注册表：类型/优先级/分类/搜索模板
│   └── extraction_rules.yml     # 各源数据提取规则
├── app/
│   ├── api/search.py            # 核心搜索管线
│   ├── sources/
│   │   ├── api_source.py        # 全部直接 API 源（YouTube/GitHub/HN/Reddit/财经）
│   │   ├── cdp_client.py        # CDP 浏览器直连（临时 tab，不抢占浏览）
│   │   ├── edge_mcp_source.py   # 浏览器源基类（登录检测/自动恢复）
│   │   ├── cookie_manager.py    # 登录态管理与自动恢复
│   │   └── video_transcript.py  # YouTube/B站 字幕提取
│   ├── config.py                # 意图识别 · 源能力标签 · 分类排除
│   ├── fusion.py                # RRF 融合 + 五阶段去重
│   ├── judge.py                 # LLM 评分 + 参与度归一化（可降级）
│   ├── validator.py             # 交叉验证引擎
│   ├── cluster.py               # 主题聚类 + MMR
│   ├── llm_client.py            # 本地 LLM 客户端
│   ├── cache.py                 # SQLite 缓存（每源独立 TTL）
│   └── storage/                 # SQLite 存储层
└── requirements.txt
```

## 技术栈

- **CDP (Chrome DevTools Protocol)** — WebSocket 直连浏览器；搜索在**临时 tab** 进行，不抢占你正在浏览的页面，完事自动清理
- **yt-dlp** — YouTube 搜索 + 字幕，无需浏览器
- **Asyncio** — 全源并行，单查询 30-120 秒
- **SQLite** — 每源独立缓存 TTL，登录态持久化

## 隐私

- **无遥测，无追踪，无云端。** 所有数据（缓存、cookie、搜索历史）只存在于你机器的 `data/` 目录
- 搜索使用的是你自己的浏览器登录态，K3 不存储、不传输任何密码
- 删除 `data/` 目录即完成完全清除

## 许可

[MIT](LICENSE) — 无追踪。你的搜索数据留在你的机器上。
