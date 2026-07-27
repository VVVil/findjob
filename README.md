# FindJob — AI 求职助手

多平台岗位爬取 + 智能投递 + 简历定制，从 Agent 架构探索到工程化落地的完整演进。

## 项目结构

```
├── v1/   ← LangGraph 多 Agent 原型（7 Agent 协作 + 验证码自适应 + 三视角评估）
├── v2/   ← hunter 确定性 pipeline（CDP 直连 + 双平台 + 规则过滤 + AI 招呼语）
```

## v1 — 多 Agent 协作原型

基于 LangGraph StateGraph + SqliteSaver 的 7 Agent 架构，探索 LLM 自主决策的边界。

- 7 Agent 分工：推理型（Profile / Evaluate Panel / Summarizer）+ 工具型（Search / Company Research / Apply）
- 验证码自适应中间件：LLM 推理选择器 → 代码执行 → 域名缓存
- 三视角结构化评估：技能 / 薪资 / 文化专家辩论 + Judge 裁决
- AI 驱动简历定制 + PDF 生成

## v2 — 确定性工程化版本

从 v1 的实战反馈中认识到：Agent 自主性带来延迟和 token 消耗，确定性的 pipeline 更适合批量海投场景。

- CDP 直连 Chrome，不依赖 Playwright/browser-use
- BOSS直聘 + 智联招聘双平台支持
- 规则过滤（薪资/学历/经验/屏蔽词）+ AI 仅用于招呼语生成
- 交互 / 全自动 / 轻触三种模式
- `-P all|boss|zhaopin` 灵活切换

## 架构演进

| | v1 | v2 |
|---|---|---|
| 架构 | LangGraph 多 Agent | 确定性 pipeline |
| LLM 调用 | 每个决策都过 LLM | 仅招呼语 + 评分 |
| 速度 | 慢（分钟级） | 快（秒级） |
| 平台 | 智联（实验性） | BOSS + 智联双平台 |
| 定位 | Agent 工程探索 | 日常可用工具 |
