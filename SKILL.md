---
name: cn-housing-search
description: |
  使用 cn-housing-mcp 搜索房源并抓取详情。每次工具调用都必须回显原始 JSON 片段，便于排障。
version: 0.1.0
emoji: "🏠"
tags: ["housing", "mcp", "china"]
requirements:
  mcp_servers:
    - name: cn-housing
      command: python
      args: ["server.py"]
---

# cn-housing 搜房 Skill（可观测版）

## 可用工具
- search_listings(req)
- get_listing_detail(url)

## 规则：必须回显工具输出
每次调用工具后：
1) 先打印“返回条数/是否为空”
2) 再把工具的原始 JSON（至少前 80 行或前 4000 字符）用 ```json 代码块回显
3) 如果为空，降低筛选条件再试一次（最多 2 次）

## ✅可直接运行示例

### 示例 1：连通性测试
@cn-housing-search
仅调用一次 search_listings，参数：{"city":"北京","keywords":"整租 近地铁","purpose":"rent","limit":5}
把工具原始 JSON 直接打印出来，不要总结。

### 示例 2：先搜再抓 3 个详情
@cn-housing-search
1) 搜索上海 浦东 租房，关键词“一居 近地铁”，预算 5000-7000，返回 10 条；
2) 从结果里挑 3 条看起来最靠谱的链接，分别调用 get_listing_detail；
3) 输出对比表 + 风险清单 + 看房 checklist；
并在每次工具调用后回显原始 JSON 片段。
