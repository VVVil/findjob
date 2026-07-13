# FindJob Agent

LangGraph 多智能体求职助手——简历上传 → 自然对话收集偏好 → Playwright 爬智联招聘 → 三专家辩论式评估 → Markdown 报告输出。

## 架构

```
run.py (CLI / argparse)
  └─ src/graph.py (LangGraph 状态图编排)
       ├─ resume_parser.py    → DeepSeek 解析 PDF/DOCX 简历为结构化画像
       ├─ preference_chat.py  → 多轮自然对话收集用户偏好（自动检测结束信号）
       ├─ job_crawler.py      → Playwright 持久化浏览器爬取智联推荐页
       ├─ evaluators.py       → 多智能体辩论式评估（generate → critique → revise）
       └─ report_generator.py → 生成 Markdown 推荐报告
```

### 流程

```
简历解析 → 偏好对话(最多4轮) → 爬取岗位 → 三专家辩论评估 → 翻页循环 → 报告
                                       ↑                          │
                                       └── 不够阈值且未到上限 ────┘
```

### 多智能体评估（核心）

每个岗位经过三阶段辩论，三位专家（技术面试官 / 猎头顾问 / 职场导师）互相质疑、各自修正：

```
Round 1  初评（3 parallel）  各自独立打分
Round 2  互评（3 parallel）  每个专家对另外两人提出质疑
Round 3  修正（3 parallel）  每个专家看到别人对自己的质疑后给出最终分
```

- **否决**：任一专家给出 ≤3 分则直接拒绝
- **翻页**：本页凑不够 top_k 个 ≥threshold 分的岗位则自动翻下一页，默认最多 5 页
- **降级**：翻完上限仍不够则取最高分补足

## 项目结构

```
findjob/
├── run.py                 # CLI 入口
├── src/
│   ├── graph.py           # LangGraph 编排（7 节点状态图）
│   ├── state.py           # AgentState 定义
│   ├── llm_client.py      # DeepSeek 异步客户端
│   ├── resume_parser.py   # PDF/DOCX 简历解析
│   ├── preference_chat.py # 偏好对话 + 结束信号检测
│   ├── job_crawler.py     # Playwright 爬虫（智联推荐页 + 翻页）
│   ├── evaluators.py      # 三视角评估 + Judge 汇总
│   └── report_generator.py # Markdown 报告生成
├── data/
│   ├── jobs/              # 爬虫 JSON 产出
│   └── reports/           # 输出报告 .md
├── playwright_profile/    # 浏览器持久化登录（需扫码一次）
├── requirements.txt
├── .env.example
└── .gitignore
```

## 快速开始

```powershell
# 1. 环境
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
playwright install chromium

# 2. 配置 API Key
cp .env.example .env
# 编辑 .env，填入 DEEPSEEK_API_KEY

# 3. 运行
python run.py
```

首次运行会打开 Chrome 窗口，扫码登录智联招聘后回到终端按 Enter，后续自动保持登录。

## 参数

```powershell
python run.py                                   # 默认：阈值 8.0，Top 3，最多 5 页
python run.py --threshold 7.0                   # 放宽到 7 分
python run.py --threshold 7.5 --top-k 5         # 7.5 分 + Top 5
python run.py --threshold 9.0 --max-pages 10    # 挑剔模式 + 更多翻页
```

| 参数 | 默认 | 说明 |
|------|------|------|
| `--threshold` | 8.0 | 达标分数线 |
| `--top-k` | 3 | 报告取 Top K 个 |
| `--max-pages` | 5 | 最大翻页数 |

## 技术栈

- **编排**: LangGraph（MemorySaver checkpoint，interrupt 多轮对话）
- **LLM**: DeepSeek Chat（OpenAI 兼容 API）
- **爬虫**: Playwright（persistent context，session 级 Cookie 持久化）
- **终端**: Rich（Markdown 渲染、表格、Panel）
- **简历解析**: pypdf / lxml + DeepSeek 结构化提取
