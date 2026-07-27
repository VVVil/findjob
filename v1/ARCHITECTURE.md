# FindJob Agent — 多智能体求职系统架构

## 设计哲学

```
Agent 负责 观察 → 推理 → 决策
确定性代码负责 执行副作用（点击、写文件、发请求）
零硬编码知识 — 所有经验来自 Agent 推理 + 缓存积累

推理型 Agent（只调 LLM） vs 工具型 Agent（LLM + ToolNode）
LLM 推理选择器，代码执行点击 — 不把副作用交给模型
```

---

## Agent 清单（7 个图节点 + 1 个 Middleware）

```
┌──────────────────────────────────────────────────────────┐
│                 Captcha Middleware                        │
│  所有 Playwright 操作外层 wrapper，对上层 Agent 完全透明     │
│  检测验证码 → DOM交互元素树 → LLM推理CSS选择器               │
│  → 代码执行点击 → 按域名写缓存                               │
│  支持滑块/点击/复选框等多类型验证码自适应                      │
└──────────────────────────────────────────────────────────┘

  START
    │
    ▼
┌──────────────────┐
│ 1. Profile Agent │  纯推理型 · 无 Tool
│                  │
│ 输入：简历原文     │
│ 输出：结构化画像    │
│  • 技能列表 + 等级  │
│  • 工作经历摘要    │
│  • 信息缺口探测    │
└────────┬─────────┘
         │
         ▼
┌──────────────────┐
│ 2. Chat Loop     │  纯推理型 · 无 Tool
│   (Profile Agent │
│    对话阶段)      │
│                  │
│ 输入：画像 + 历史  │
│ 输出：偏好补全    │
│ LLM 自行判断      │
│ 信息足够 → 自动跳 │
└────────┬─────────┘
         │
         ▼
┌──────────────────┐  ┌─────────────────────────────┐
│ 3. Search Agent  │→ │ Tool: crawl_jobs             │
│   工具型          │  │  参数: page_num, site, kw    │
│                  │  │  返回: jobs JSON 路径         │
│                  │  │  内部: Playwright + Captcha   │
│                  │  │        Middleware 透明保护     │
└────────┬─────────┘  └─────────────────────────────┘
         │  每爬完一个岗位 → 立即写 state → 触发下游
         ▼
┌──────────────────┐  ┌─────────────────────────────┐
│ 4. Company       │→ │ Tool: search_company         │
│    Research      │  │  参数: company_name           │
│    Agent 工具型   │  │  返回: 工商信息 JSON          │
│                  │  │  • 注册资本/成立时间/参保人数   │
│                  │  │  • 司法风险/经营异常/融资轮次   │
│                  │  │  同公司命中缓存 → 不重复查询     │
│                  │  │  搜索失败 → 空报告,不阻塞流程   │
└────────┬─────────┘  └─────────────────────────────┘
         │  岗位 JD + 公司报告 齐备 → 异步启动评估
         ▼
┌──────────────────────────────────────────┐
│ 5. Evaluate Panel  纯推理型 · 无 Tool     │
│                                          │
│  输入：JD + Company Report + Profile     │
│                                          │
│  ┌────────┐ ┌────────┐ ┌────────┐       │
│  │🔧技能  │ │💵薪资  │ │🏠文化  │  3并行 │
│  │ 专家   │ │ 专家   │ │ 专家   │       │
│  └───┬────┘ └───┬────┘ └───┬────┘       │
│      └──── 互评（交叉质疑）────┘          │
│      └──── 修正（回应质疑）────┘          │
│                  │                       │
│             ┌────▼────┐                  │
│             │  Judge  │ 40/30/30 加权    │
│             └────┬────┘ 任一≤3 → 否决    │
│                  │                       │
│  评估 + 爬取流水线并行，够 top_k 达标 → 早停│
└────────┬─────────────────────────────────┘
         │
         ▼
  够 top_k 个达标？ ─── 够 → Summarizer
         │ 不够
         └── → Search Agent 翻页继续
         │
         ▼
┌──────────────────────┐
│ 6. Summarizer Agent  │  纯推理型 · 无 Tool
│                      │
│ 输入：Top-K + 9轮辩论 │
│       + 公司报告      │
│                      │
│ 输出：               │
│  • 横向对比排名+理由   │
│  • 三维度优劣势分析    │
│  • 公司风险提示       │
│  • 可操作面试建议     │
└──────────┬───────────┘
           │
           ▼
┌──────────────────┐  ┌─────────────────────────────┐
│ 7. Apply Agent   │→ │ Tool: tailor_resume          │
│   工具型          │  │  JD + 能力清单 → 定制简历JSON  │
│                  │  │  防幻觉：清单锁死边界           │
│                  │  │  HTML模板渲染 → Playwright PDF │
│                  │→ ├─────────────────────────────┤
│                  │  │ Tool: generate_greeting       │
│                  │  │  JD + 定制简历 → 200字招呼语   │
│                  │  │  聚焦1-2个最匹配技能点          │
└──────────────────┘  └─────────────────────────────┘
           │
           ▼
  展示给用户确认 → 辅助投递（高亮提交按钮但不自动点击）
           │
           ▼
          END
```

---

## Agent × Tool 对应矩阵

| Agent | 类型 | 可调用 Tool | 原因 |
|-------|:---:|------------|------|
| Profile Agent | 纯 LLM | 无 | 读文本做结构化提取，不需外部能力 |
| Search Agent | LLM + ToolNode | `crawl_jobs` | 控制浏览器爬招聘网站 |
| Company Research | LLM + ToolNode | `search_company` | 爱企查搜企业工商信息 |
| Evaluate Panel | 纯 LLM | 无 | JD + 公司报告已在 state 中 |
| Summarizer | 纯 LLM | 无 | 前面所有报告已在 state 中 |
| Apply Agent | LLM + ToolNode | `tailor_resume`, `generate_greeting` | PDF 生成 + 文本生成 |
| Captcha Middleware | 非图节点 | 自身即工具层 | 对所有 Playwright 操作透明 |

---

## Tool 层

```
┌─────────────────────────────────────────────────────┐
│                   Web Tools（浏览器操作）             │
├──────────────┬──────────┬───────────────────────────┤
│ crawl_jobs   │ Search   │ 爬招聘网站列表+详情         │
│              │ Agent    │ 含 Captcha Middleware      │
├──────────────┼──────────┼───────────────────────────┤
│ search_      │ Company  │ 爱企查 → 工商信息           │
│ company      │ Research │ 含 Captcha Middleware      │
└──────────────┴──────────┴───────────────────────────┘

┌─────────────────────────────────────────────────────┐
│                Document Tools（文档处理）             │
├──────────────┬──────────┬───────────────────────────┤
│ tailor_      │ Apply    │ JD+能力清单 → 定制简历JSON  │
│ resume       │ Agent    │ 防幻觉：清单边界锁定        │
├──────────────┼──────────┼───────────────────────────┤
│ generate_    │ Apply    │ JD+定制简历 → 招呼语        │
│ greeting     │ Agent    │                           │
└──────────────┴──────────┴───────────────────────────┘
```

---

## State 设计

```python
class AgentState(TypedDict):
    # === 简历 & 对话 ===
    resume_text: str
    profile: dict             # {skills, skill_levels, expected_role, ...}
    chat_history: list[dict]  # [{role, content}, ...]
    preferences: dict         # 对话提取的偏好

    # === 搜索 & 爬取 ===
    search_keywords: str
    current_page: int
    max_pages: int
    jobs_raw: list[dict]      # 边爬边追加

    # === 公司研究 ===
    company_reports: dict[str, dict]  # {公司名: {注册资本, 参保人数, ...}}

    # === 评估 ===
    threshold: float
    top_k: int
    collected: list[dict]                        # 达标岗位
    evaluation_details: dict[str, list[dict]]    # 岗位ID → 9轮辩论完整记录

    # === 定制 ===
    tailored_resumes: dict[str, str]  # 岗位ID → PDF路径
    greetings: dict[str, str]        # 岗位ID → 招呼语

    # === 控制 ===
    phase: str
```

---

## 存储分层

```
data/
├── checkpoints.db         ← SQLite：LangGraph 状态快照 + 评估辩论详情
│                            （需要条件查询：按分数/维度/岗位ID）
├── captcha_cache.json     ← JSON：按域名分组的验证码选择器缓存
│                            （访问模式：按域名读一个列表）
├── site_adapters.json     ← JSON：按域名分组的 DOM 适配配置
│                            （LLM 首次推理 → 缓存 → 后续复用）
├── jobs/                  ← JSON 目录：每次运行的岗位数据
│                            （写一次读一次，文件遍历即可）
├── reports/               ← Markdown：Summarizer 生成的推荐报告
├── resumes/               ← PDF：Apply Agent 生成的定制简历
└── playwright_profile/    ← 浏览器持久化目录（cookies/登录态）
```

**分层原则**：
- SQLite — 需要跨 session 持久化 + 条件查询（checkpoint、评估详情）
- JSON — 简单 KV 读写，按 key 取 value（captcha 缓存、adapter 配置）
- JSON 目录 — 一次性写入后顺序读取，不需要跨文件查询（岗位数据）
- 文件系统 — 二进制产出物（PDF、Markdown 报告、浏览器 profile）

---

## 数据流

```
用户拖入简历 PDF
      │
      ▼
Profile Agent → 结构化画像 + 能力清单加载
      │
      ▼
Chat Loop → 偏好补全（Agent 自行判断何时问够）
      │
      ▼
Search Agent ──(流水线)──→ 爬详情 → Company Research → Evaluate Panel
      ↑                         │              │              │
      │                    爱企查查公司    JD+公司报告   3专家×3轮辩论
      │                         │              │              │
      └──── 不够数,翻页 ────────┘              │              │
                                               ▼              │
                                        够 top_k 达标 ←────────┘
                                               │
                                               ▼
                                       Summarizer Agent
                                         横向对比 Top-K
                                               │
                                               ▼
                                         Apply Agent
                                    定制简历 + 招呼语 + 展示确认
```

---

## 防幻觉方案

1. **能力清单（skills_inventory.txt）**：用户维护，分三级
   - 绝对掌握 → 可写"熟练""主导"
   - 用过但不深入 → 可写"熟悉""参与"
   - 了解原理 → 可写"了解"，不可夸大
2. LLM 只能从清单中选技术栈匹配 JD，不能新增
3. 清单里没有的技术栈 = 用户不会 = 禁止写入
4. 定制内容输出结构化 JSON，通过确定性 HTML 模板渲染
   → 避免 LLM 直接生成 HTML 的排版不稳定

---

## 验证码自适应

```
零硬编码，只靠推理 + 缓存

触发（翻页/开详情/投递时检测到验证码弹窗）
       │
       ▼
 ┌──────────────┐
 │ 缓存命中？     │──是──→ 直接点击,完成
 │ {域名: [选择器]}│
 └──────┬───────┘
        │ 否
        ▼
 ┌──────────────┐
 │ DOM 树提取    │  注入 JS,提取验证码区域所有可交互元素
 │ (含 iframe)   │  借鉴 browser-use 缩进文本树格式
 └──────┬───────┘
        ▼
 ┌──────────────┐
 │ LLM 推理      │  输入: DOM树 → 输出: CSS 选择器字符串
 │ 只输出选择器   │  LLM 不执行任何浏览器操作
 └──────┬───────┘
        ▼
 ┌──────────────┐
 │ 代码执行点击   │  page.locator(selector).click()
 │              │  轮询等待验证码消失(最多20秒)
 └──────┬───────┘
        ▼
 ┌──────────────┐
 │ 写入缓存      │  按域名分组存储
 │ 连续命中→可靠  │  支持新网站首次自适应
 └──────────────┘
```

---

## 关键工程实践

| 实践 | 实现 |
|------|------|
| Agent 编排 | LangGraph StateGraph + SqliteSaver checkpoint |
| 结构化输出 | Pydantic Schema 约束所有 Agent 输出 |
| Tool 层 | LangChain @tool + ToolNode，工具与推理解耦 |
| 并发调度 | asyncio.gather 三专家并行 + create_task 流水线早停 |
| LLM 协议 | OpenAI 兼容协议，provider 可切换 |
| 存储分层 | SQLite(checkpoint+评估) + JSON(cache/adapter) + 文件(产出物) |
| 浏览器自动化 | Playwright persistent context + CDP |
| 验证码 | Middleware 模式，零硬编码，按域名缓存自适应 |
| 防幻觉 | 能力清单 truth source + 结构化输出约束 |

---

## 与 TradingAgents 的对比

| 维度 | TradingAgents | FindJob Agent |
|------|:---:|:---:|
| Agent 数量 | 12 | 7 |
| 带 Tool 的 Agent | 4 | 3 |
| Tool 类型 | HTTP API 调用 | **真实浏览器操作 + 验证码自适应** |
| 辩论机制 | 对话式（牛/熊互怼） | **结构化三轮**（初评→互评→修正） |
| 浏览器自动化 | 无 | Playwright + Captcha Middleware |
| 验证码处理 | 无 | **自有方案** |
| 防幻觉 | 结构化输出（部分agent） | **能力清单 truth source** |
| 包管理 | pyproject.toml | pyproject.toml |
| 测试 | 60+ | 持续补充中 |
| 存储 | SQLite + Redis + JSON | SQLite + JSON（按访问模式分层） |

---

## 技术栈

```
Agent 编排:    LangGraph (StateGraph + SqliteSaver + ToolNode)
LLM:          DeepSeek Chat API (OpenAI 兼容协议, provider 可切换)
Tool 层:      LangChain @tool + Pydantic args_schema
浏览器:        Playwright + CDP (persistent context)
并发:          asyncio (gather + create_task 流水线)
结构化输出:     Pydantic Schema
存储:          SQLite + JSON (按访问模式分层)
语言:          Python 3.11+
```
