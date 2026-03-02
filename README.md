# cn-housing-mcp

一个面向国内租房/买房场景的 MCP Server（Model Context Protocol），为智能体（OpenClaw/Cursor/Claude Desktop 等）提供统一的“房源搜索 + 房源详情结构化”工具接口。它的目标不是“暴力爬遍全网”，而是用可控、可观测、可迭代的方式把房源信息变成可分析的数据：对比表、预算测算、风险清单、看房 checklist 等都可以交给上层 skill/workflow 生成。

## 你能用它做什么

cn-housing-mcp 对外提供两类核心能力（两个 MCP tools）：

### 1) search_listings

按条件生成搜索 query，通过轻量级网页搜索拿到候选房源链接，并返回统一结构：

- 支持：城市、区域、关键词（整租/近地铁/小区名等）、租/买、价格区间、几室、limit

- 输出：候选房源列表（title/price/city/district/rooms/area/url/source/warnings…）

适合做“初筛”：先拿到一批链接，再交由 get_listing_detail 深度解析 TOP N。

### 2) get_listing_detail

给定房源 URL，抓取页面并进行结构化抽取，优先从以下位置提取信息：

- JSON-LD（application/ld+json）

- Next.js 数据（__NEXT_DATA__）

- 常见 meta（og:title、description 等）

输出会包含：

- 标准化字段（title/price/address/area/rooms…）

- raw_snippet（截断的原始结构化片段，便于调试）

- warnings（识别到验证码/登录要求/拒绝访问等风险提示）

## 为什么要做成 MCP Server？

* 解耦：MCP 只负责“取数/抽取”，上层 skill 负责“对比/测算/建议”。这样你可以更换前端（OpenClaw、Cursor、Claude Desktop）而不改后端。

* 可观测：每次返回都带 warnings 和 raw_snippet，方便快速定位“是平台拦截还是解析失败”。

* 可扩展：你可以逐步加入站点适配器（贝壳/链家/安居客/58 等），而不破坏 tool 接口。

适用范围与限制（很重要）
✅ 适合

* 半自动找房：你用平台正常浏览，mcp 负责解析/汇总/去重/结构化

* 自动化初筛：低频、低并发地从公开页面拿到候选链接 + 抓少量详情

* 以“分析与决策”为主的工作流：预算测算、风险提示、看房清单、谈判要点

❌ 不适合/不承诺

* 大规模抓取、全站爬虫、绕过验证码/反爬（不做，也不建议）

* 保证某个平台长期稳定（国内房产平台风控强、页面变动频繁）

* 替代官方 API（如果你有合规数据源，建议走 API 或导出清单）

## 运行方式
依赖

Python 3.10+（建议）

mcp、httpx、beautifulsoup4、lxml、pydantic

## 安装
pip install -r requirements.txt
启动（stdio 模式）
python server.py

然后在你的 MCP 客户端（OpenClaw/Cursor/Claude Desktop）里把它配置为一个 server（command 指向 python server.py）。

可选环境变量

### 用于调试与稳定性调参：

HTTP_TIMEOUT：抓取超时（默认 25s）

RATE_LIMIT_SECONDS：请求间隔（默认 1.2s，建议不要太小）

USER_AGENT：自定义 UA（用于降低误伤概率）

## 示例：

HTTP_TIMEOUT=60 RATE_LIMIT_SECONDS=2.0 python server.py
风控与合规提示

国内房产平台普遍有严格的反自动化机制。为了避免账号/网络环境被风控：

低频请求、少翻页、少并发

遇到验证码/登录提示：请停止自动化，改为“贴链接/导出清单 → 分析”

不要在云服务器/数据中心 IP 上跑高频抓取（更容易被拦截）

cn-housing-mcp 会尽量在 warnings 中提示你可能遇到的拦截类型（captcha/login/access denied 等），帮助你快速判断失败原因。

## 典型工作流（推荐）

search_listings 拿 10~20 条候选链接

选 TOP 3~5 调 get_listing_detail 深度抽取

上层 skill 生成：

房源对比表（统一字段）

总成本/月供测算

风险清单 + 看房 checklist

推荐排序与理由
