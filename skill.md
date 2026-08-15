---
name: deep-search-k3
description: 本地智能搜索平台 k3 — 聚合 25 个高价值信息源（搜索引擎、中文社区、专业报告、社交媒体、视频教程），通过 CDP 浏览器自动化提取并交叉验证。每条结果带发布日期、支持 --freshness 时效过滤、结构化事实（entity/attribute/value）、批量并行模式。支持智能分类源选择：技术问题自动使用 GitHub/V2EX/知乎/YouTube/B站，投资问题自动使用雪球/东方财富/巨潮/新浪。使用场景：用户要求搜索、查找、查询任何信息时优先使用此 skill。CLI 直接调用，无需启动 API 服务器。
---

# DeepSearch K3 — Subagent 调度模式

**本 skill 通过 subagent 执行搜索，不直接注入工作流到主对话。**
**CLI 架构：直接调用 Python 脚本，无需启动 HTTP 服务器。**

## 配置（首次使用）

| 变量 | 说明 | 默认值 |
|------|------|--------|
| `<PYTHON>` | 装有本项目依赖（见 requirements.txt）的 Python 解释器 | 系统 `python` |
| `<REPO>` | 本仓库根目录（search.py 所在目录） | — |
| `LLM_URL` | 本地 OpenAI 兼容 LLM 服务地址（可选，仅 `--judge` 时需要） | `http://localhost:8080` |
| `LLM_MODEL` | LLM 模型名（可选） | `Qwen3.6-27B` |
| `YT_DLP_PYTHON` | 装有 yt-dlp 的 Python（可选） | 当前解释器 |

## 使用方法

收到用户搜索请求后，用 Agent tool 启动 subagent，将完整工作流作为 prompt 传入。主对话只接收 subagent 返回的结构化结果。

```
Agent({
  description: "智能搜索k3",
  prompt: "<下面的完整工作流 prompt>",
  run_in_background: true  (可选，长时间搜索建议后台运行)
})
```

## Subagent Prompt（完整复制以下内容作为 Agent prompt）

---

你是一个强大的搜索助手，通过本地聚合平台搜索**所有**高价值信息源，交叉验证后给出结构化回答。

**用户搜索请求：{ARGS}**

### 核心原则

1. **使用本地搜索平台 CLI 脚本，不是 WebSearch。**
2. **智能分类源选择。** 平台自动识别查询意图，技术问题优先 GitHub/V2EX/知乎/YouTube/B站，投资问题优先雪球/东方财富/巨潮/新浪，避免无关源干扰。
3. **时效性判断依据 published_date。** 每条结果都带 `published_date`（`YYYY-MM-DD`，未知为空）。**投资/资讯类查询默认加 `--freshness 30d`**（除非用户明确要求别的窗口）；其他涉及"最新/最近"的请求也要用 `--freshness` 过滤，并在整理结果时标注日期。
4. **输出两个结果：**
   - 第一部分：融合排序的搜索结果 — RRF 跨源融合后的最重要结果
   - 第二部分：交叉比对 + 综合总结

### CLI 脚本

入口：`<PYTHON> <REPO>/search.py`

```bash
# 全源搜索（智能分类，只运行相关源）
<PYTHON> <REPO>/search.py "搜索词" --all --max 30 --json

# 指定源搜索
<PYTHON> <REPO>/search.py "搜索词" --sources youtube,github,hacker_news --max 20 --json

# 时效过滤：只保留 7 天内的结果（无日期条目保留）
<PYTHON> <REPO>/search.py "搜索词" --all --max 30 --json --freshness 30d

# 人类可读输出（不加 --json，结果自带 [日期] 标注）
<PYTHON> <REPO>/search.py "搜索词" --all --max 20

# 批量并行（单进程多查询，共享登录态与 tab 预算，适合行业调研）
<PYTHON> <REPO>/search.py --batch tasks.json --concurrency 6 --tab-budget 12
# tasks.json: [{"query": "...", "output": "out/xx.json", "sector": "...", "search_id": "..."}]
```

参数说明：
- `--all`：搜索所有源（平台会根据查询意图自动排除不相关源）
- `--sources`：逗号分隔的源名列表，覆盖自动分类
- `--max`：最大结果数（默认 30）
- `--json`：输出 JSON；不加则输出格式化 markdown
- `--judge`：开启 LLM 相关性重排序（每次搜索 +25-74 秒；单次临时搜索可用，批量不建议）
- `--freshness`：时效过滤，如 `7d`/`30d`/`24h` 或纯数字（按天）。日期可解析且过期的条目被丢弃，无日期条目保留
- `--no-retry` / `--no-entity` / `--no-transcript`：跳过对应增强阶段（各省 15-30 秒）
- `--batch TASKS.json`：批量模式，单进程并发执行多个查询，断点续跑（output 已存在且有效则跳过）

### 智能分类源选择

平台根据查询关键词自动识别领域，并排除不相关的源：

| 领域 | 包含源 | 排除源 |
|------|--------|--------|
| 技术/AI | google, github, zhihu, v2ex, hacker_news, youtube, bilibili, bing | 雪球, 东方财富, CoinGecko 等 |
| 投资/金融 | google, bing, xueqiu, eastmoney, yahoo_finance, cninfo, sina_finance | V2EX, Hacker News, YouTube, B站 等 |
| 政策 | google, bing | YouTube, B站 |
| 通用 | 除视频源外的全部源 | YouTube, B站 |

**视频源规则：** YouTube/B站仅技术/AI 类查询启用；其他类别不搜索视频源、不执行字幕提取（省 40-70 秒）。非技术类查询如需视频内容，用 `--sources youtube,bilibili,...` 显式指定。

### 工作流

**Step 0: 检查环境**

确认浏览器以 CDP 模式运行（默认端口 9222）：

```bash
curl -s http://localhost:9222/json/version
```

无响应 → 告知用户"请检查浏览器是否以 CDP 模式运行（端口 9222）"。
（Windows: `msedge.exe --remote-debugging-port=9222`；Chrome 亦可）

**Step 1: 调用 CLI 搜索**

直接调用，`--json` 输出到文件。涉及"最新/最近"的请求加 `--freshness`（如 7d）：

```bash
<PYTHON> <REPO>/search.py "搜索词" --all --max 30 --json > search_output.json 2>search_err.log
```

**Step 2: 读取并整理结果**

用 Python 读取 JSON 输出：

```bash
<PYTHON> -c "
import json
print(json.dumps(json.load(open('search_output.json', encoding='utf-8')), ensure_ascii=False, indent=2))
"
```

输出格式（从 JSON 结果整理后呈现给用户）：

```
搜索结果（融合排序，共 N 条，来自 M 个源，耗时 Xms）

① [源] [2026-07-20] 标题
   摘要...
   链接

② [源] [2026-07-18] 标题
   摘要...
   链接

...（展示 summary.ranked_results 前 10 条，每条标注 published_date）

关键发现（交叉验证）
- 从 summary.key_findings 中提取，标注 confidence 和 verified 状态
- 多源一致 = Verified，单源 = Unverified

数据冲突
- 从 summary.conflicts 中提取，按源权重标注优先级

搜索信息
- 参与源：{metadata.sources_used}
- 融合去重：{summary.fusion_metadata.duplicates_removed} 条
- 时效过滤：{metadata.freshness}（丢弃 {metadata.freshness_filtered} 条过期结果）
- 搜索耗时：{metadata.query_time_ms}ms
```

### 结果解读指南

CLI `--json` 输出结构：`{query, summary: {ranked_results, raw_results_by_source, key_findings, conflicts, fusion_metadata, source_quality}, metadata: {query_time_ms, sources_used, freshness, freshness_filtered, ...}}`

- **summary.ranked_results**：RRF 融合排序后的跨源结果。每条必含 `title/url/content/source/score/published_date`，judge 后另有 `judge_score`
- **published_date**：发布日期 `YYYY-MM-DD`（部分带时分）。空字符串 = 该源页面无日期
- **summary.raw_results_by_source**：按源分组的原始结果（每源最多 10 条）
- **summary.key_findings**：结构化事实，dict 含 `entity/attribute/value/fact/confidence/verified/sources/urls`，多源验证 = Verified
- **summary.conflicts**：同一事实不同源的冲突数据，按源权重标注优先级
- **summary.fusion_metadata**：融合统计（去重数、源贡献数等）
- **metadata.search_intent**：自动识别的搜索意图（comparison/opinion/how_to/factual 等）
- **视频字幕提取**：YouTube 结果自动带字幕（"Transcript:" 前缀）

### 错误处理

- 脚本报错 → 检查 stderr 日志，确认 CDP 浏览器是否运行（端口 9222）
- 登录失败 → 告知"XX 源未登录"，继续搜索其他源
- 所有源结果为 0 → 告知"所有源均未找到相关结果"

### 注意事项

- 全源搜索标准参数 `--all --max 30 --json`；**投资/资讯类查询默认加 `--freshness 30d`**
- 视频源（youtube/bilibili）仅技术/AI 类查询自动启用
- LLM judge 重排序默认关闭：RRF 融合排序后直接输出；需要语义重排时显式加 `--judge`
- 全源搜索耗时：技术类约 60-120 秒（含视频源与字幕提取），非技术类约 30-60 秒
- 知乎、雪球、Twitter 需要在浏览器中登录过（cookie 在浏览器 profile 内即可）；搜索在临时 tab 中进行，不会抢占你正在浏览的页面；登录过期时平台会自动尝试恢复
- CDP 浏览器是必须的（默认端口 9222），用于 google、bing、雪球、B站、Twitter、东方财富等源的搜索
- 不需要启动 API 服务器，CLI 脚本直接调用搜索管线（服务器模式仅可选：`python run_server.py`）

### 当前数据源状态（约 20/25 正常）

| 类型 | 正常源 | 不可用源（代码已标记 broken） |
|------|--------|---------------------|
| 搜索引擎 | google, bing | yandex (CAPTCHA) |
| 中文社区 | zhihu, xueqiu, v2ex, sogou_wechat | — |
| 视频源 | youtube, bilibili | — |
| 社交媒体 | twitter | — |
| 财经 | eastmoney, yahoo_finance, coingecko, binance, fear_greed, cninfo, sina_finance | sec_edgar (区域封锁), trendforce |
| 技术 | github, hacker_news, github_trending | — |
| 宏观 | world_bank | stats_gov (无响应) |
| 聚合 | — | rsshub (Cloudflare 封锁) |
