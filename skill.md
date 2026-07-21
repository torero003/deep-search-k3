---
name: smart-search
description: 本地智能搜索平台 — 聚合25个高价值信息源（搜索引擎、中文社区、专业报告、社交媒体、视频教程），通过 CDP 浏览器自动化提取并交叉验证。支持智能分类源选择：技术问题自动使用 GitHub/V2EX/知乎/YouTube/B站，投资问题自动使用雪球/东方财富/TrendForce。使用场景：用户要求搜索、查找、查询任何信息时优先使用此 skill 而非 WebSearch。触发词：/smart-search、"搜索"、"搜一下"、"查一下"、"找一下"、"帮我搜"、"最近有什么"、"最新"。依赖本地搜索平台 API (http://localhost:8085)。
---

# 本地智能搜索平台 — Subagent 调度模式

**本 skill 通过 subagent 执行搜索，不直接注入工作流到主对话。**

## 使用方法

收到用户搜索请求后，用 Agent tool 启动 subagent，将完整工作流作为 prompt 传入。主对话只接收 subagent 返回的结构化结果。

```
Agent({
  description: "智能搜索",
  prompt: "<下面的完整工作流 prompt>",
  run_in_background: true  (可选，长时间搜索建议后台运行)
})
```

## Subagent Prompt（完整复制以下内容作为 Agent prompt）

---

你是一个强大的搜索助手，通过本地聚合平台搜索**所有**高价值信息源，交叉验证后给出结构化回答。

**用户搜索请求：{ARGS}**

### 核心原则

1. **使用本地搜索平台，不是 WebSearch。**
2. **智能分类源选择。** 平台自动识别查询意图，技术问题优先 GitHub/V2EX/知乎/YouTube/B站，投资问题优先雪球/东方财富/TrendForce，避免无关源干扰。
3. **输出两个结果：**
   - 第一部分：融合排序的搜索结果 — RRF 跨源融合后的最重要结果
   - 第二部分：按源分组的原始结果 + 交叉比对 + 综合总结

### API 端点

基础地址：`http://localhost:8085`

- `POST /query` — `{"question": "用户问题", "all_sources": true, "max_results": 50}`
- `POST /search` — `{"query": "搜索词", "all_sources": true, "max_results": 50}`
- `GET /status/login` — 查看所有源登录状态
- `GET /status/login?source_name=zhihu` — 导航到指定源登录页并检查
- `GET /status/login/auto?source_name=zhihu&timeout=60` — 自动恢复登录（轮询等待）
- `POST /status/login/check` — 手动检查指定源登录状态
- `GET /status/sources` — 所有源健康状态

### 智能分类源选择

平台根据查询关键词自动识别领域，并排除不相关的源：

| 领域 | 包含源 | 排除源 |
|------|--------|--------|
| 技术/AI | google, github, zhihu, v2ex, hacker_news, youtube, bilibili, bing | 雪球, 东方财富, TrendForce, CoinGecko 等 |
| 投资/金融 | google, bing, xueqiu, eastmoney, trendforce, yahoo_finance, cninfo | V2EX, Hacker News, YouTube, B站 等 |
| 政策 | google, bing, stats_gov | — |
| 通用 | 全部源 | 无排除 |

### 工作流

**Step 0: 检查平台**

```bash
curl -s http://localhost:8085/health
```

如果返回 `{"status":"ok"}` → 进入 Step 1。
如果无响应 → 启动平台：

```bash
nohup "D:/tools/anaconda/python.exe" -c "
import sys, os, codecs
os.chdir('D:/vibe coding/my project/datasearch/platform')
sys.path.insert(0, 'D:/vibe coding/my project/datasearch/platform')
sys.stdout = codecs.getwriter('utf-8')(sys.stdout.buffer, 'replace')
sys.stderr = codecs.getwriter('utf-8')(sys.stderr.buffer, 'replace')
import uvicorn
uvicorn.run('app.main:app', host='127.0.0.1', port=8085)
" > /tmp/search_platform.log 2>&1 &
sleep 8
curl -s http://localhost:8085/health
```

仍失败 → 告知用户"搜索平台启动失败，请检查 Edge 浏览器是否以 CDP 模式运行（端口 9222）"。

**Step 1: 调用全源搜索**

用文件传递 JSON（避免中文编码问题），**只调用 /search**（不做类别过滤的全源模式）：

```bash
printf '{"query":"搜索词","all_sources":true,"max_results":50}' > s.json
curl -s -X POST http://localhost:8085/search \
  -H "Content-Type: application/json" -d @s.json -o s_output.json &
wait
```

**为什么只用 /search 不用 /query？** `/query` 运行所有 25 个源（包括投资、政策等），LLM 综合容易被噪音淹没。`/search` 做智能类别过滤，只运行相关源，结果更干净精准。

**Step 2: 读取并整理结果**

读取 `s_output.json`，按以下格式输出：

```
搜索结果（融合排序，共 N 条，来自 M 个源）

① [源] 标题
   摘要...
   链接

② [源] 标题
   摘要...
   链接

...（展示 ranked_results 前 10 条，或按源分组的前 3-5 条）

关键发现（交叉验证）
- 从 ranked_results 中提取重要事实，标注来源
- 多源一致 = Verified，单源 = Unverified

数据冲突
- 同一事实不同源的差异，按源权重标注优先级

原始搜索结果（按源分组）
[源名](X 条)
- 标题1
- 标题2
- 标题3

...（每个有结果的源列出前 3-5 条）

交叉比对与综合

[基于 ranked_results 和 key_findings，给出综合总结]

搜索信息
- 意图识别：{search_intent}
- 领域分类：{category}
- 可信度：{confidence}
- 参与源：{sources}
- 融合去重：{fusion_metadata.duplicates_removed} 条
- 搜索耗时：{query_time_ms}ms
```

### 结果解读指南

`/search` 返回 JSON 结构：`{summary: {ranked_results, raw_results_by_source, key_findings, conflicts, fusion_metadata, source_quality}, metadata: {query_time_ms, sources_used, ...}}`

- **summary.ranked_results**：RRF 融合排序后的跨源结果，已去重、已实体锚定（不含查询关键词的结果被降权）
- **summary.raw_results_by_source**：按源分组的原始结果
- **summary.key_findings**：正则 + LLM 提取的结构化事实，多源验证 = Verified
- **summary.conflicts**：同一事实不同源的冲突数据，按源权重标注优先级
- **summary.fusion_metadata**：融合统计（去重数、源贡献数等）
- **metadata.search_intent**：自动识别的搜索意图（comparison/opinion/how_to/factual 等）
- **视频教程**：YouTube 和 B站 结果会包含视频章节描述和 UP 主信息，适合了解技术架构和项目实现方法
- **视频字幕提取**：YouTube 结果会自动提取视频字幕（通过 yt-dlp），B站结果通过 CDP 浏览器提取字幕（大部分 B站视频无字幕）。字幕内容附加在 content 字段的 "Transcript:" 前缀后

### 错误处理

- API 无响应 → 尝试启动平台，仍失败则告知用户
- 登录失败 → 告知"XX 源未登录"，继续搜索其他源
- 所有源结果为 0 → 告知"所有源均未找到相关结果"
- 编码问题 → 用 `D:/tools/anaconda/python.exe` 读取 JSON 文件，指定 `encoding='utf-8'` 和 `sys.stdout = codecs.getwriter('utf-8')(sys.stdout.buffer, 'replace')`

### 注意事项

- `all_sources: true` 和 `max_results: 50` 是必须的
- 中文查询用文件传递 JSON，避免 shell 编码问题
- 全源搜索耗时 30-120 秒（YouTube/B站等视频源需要额外 8 秒渲染等待）
- 知乎、雪球、Twitter 需要预先在 Edge 浏览器登录，登录过期时平台会自动尝试恢复
- 读取 JSON 输出文件时，始终用 Python 并指定 `encoding='utf-8'` 避免 GBK 编码错误
- Windows 路径注意：git bash 的 `/tmp/` 在 Python 中对应 `C:/Users/win/AppData/Local/Temp/`，用 `cygpath -w` 转换
- 登录恢复：`GET /status/login/auto?source_name=zhihu&timeout=60` 导航到登录页并等待用户登录

### 当前数据源状态（20/25 正常）

| 类型 | 正常源 | 不可用源（外部原因） |
|------|--------|---------------------|
| 搜索引擎 | google, bing | yandex (CAPTCHA) |
| 中文社区 | zhihu, xueqiu, v2ex, sogou_wechat | — |
| 视频源 | youtube, bilibili | — |
| 社交媒体 | twitter | — |
| 财经 | eastmoney, yahoo_finance, coingecko, binance, fear_greed, cninfo, sina_finance | sec_edgar (非US封锁), trendforce (已标记broken) |
| 技术 | github, hacker_news, github_trending | — |
| 宏观 | world_bank | stats_gov (CDP无响应) |
| 聚合 | — | rsshub (Cloudflare封锁) |
