# findjob v2 — 多平台求职助手

BOSS直聘 + 智联招聘，爬岗位 → 硬过滤 → AI 评分 → 批量投递/发招呼语。

**支持两种模式：CLI 命令行 + Web UI 界面。**

## 首次安装

```powershell
cd D:\findjob\findjob_new\v2
.\venv\Scripts\activate
pip install -r requirements.txt
```

在 `.env` 里设 API key：

```
DEEPSEEK_API_KEY=sk-xxx
```

---

## Web UI 模式（推荐）

### 启动

```powershell
# 1. 启动 BOSS Chrome
& "C:\Program Files\Google\Chrome\Application\chrome.exe" --remote-debugging-port=9222 --remote-allow-origins=* --user-data-dir="$env:TEMP\chrome_boss" "https://www.zhipin.com"

# 2.（可选）启动智联 Chrome
& "C:\Program Files\Google\Chrome\Application\chrome.exe" --remote-debugging-port=9223 --remote-allow-origins=* --user-data-dir="$env:TEMP\chrome_zhilian" "https://www.zhaopin.com"

# 3. 启动 Web 服务
cd D:\findjob\findjob_new\v2
.\venv\Scripts\activate
python -m uvicorn app:app --host 0.0.0.0 --port 8000
```

浏览器打开 `http://localhost:8000`。

### 界面操作

```
┌── 配置栏 ───────────────────────────────────────────┐
│ 关键词标签 + 城市标签 + 经验/页数/薪资/规模/平台勾选    │
│                                                      │
│ [1.开始爬取] [2.AI评分] [3.生成招呼语] [4.统一发送]    │
│                                    [⚡全自动] [🗑清空] │
├── 实时进度 ──────────────────────────────────────────┤
│ [████████░░░░] 评分中: 5/23 — 微品致远...            │
│ 日志流...                                            │
├── 岗位卡片列表 ───────────────────────────────────────┤
│ ☐ 全选 (18个)                                       │
│ ┌──────────────────────────────────────┐ 85分 ✓已发送│
│ │ ☑ 微品致远  AI研发  20-30K  1-3年     │ BOSS      │
│ │ [展开JD]  招呼语: ___________ [保存]   │           │
│ └──────────────────────────────────────┘            │
│ ┌──────────────────────────────────────┐ 45分(划掉) │
│ │ ☐ 某外包   Python  8-12K             │ 智联       │
│ └──────────────────────────────────────┘            │
└──────────────────────────────────────────────────────┘
```

### 按钮逻辑

| 阶段 | 可用按钮 |
|------|---------|
| 爬取完成 | AI评分、生成招呼语 都亮（可跳过评分直接生成） |
| 评分完成 | 生成招呼语亮，卡片按分数排序，低分划掉沉底 |
| 招呼语完成 | 统一发送亮，没招呼语的 BOSS 岗位划掉沉底 |
| 发送完成 | 卡片右上角显示 ✓已发送 / ✗失败 |

- **全自动**：一键爬→评→生成→发，等于 CLI 的 `-a`
- **智联岗位**不需要招呼语，评分后直接勾选发送
- **发送**只发勾选的岗位

### 实时进度

WebSocket 推送，前端实时显示：爬取当前页/城市/关键词、评分进度、招呼语生成进度、发送状态。跟 CLI 日志一样的详细程度。

---

## CLI 模式

//best_command
python run.py -k "AI开发 Python Agent" -e 3 -c "深圳 广州" -p 1 -P all  

```powershell
cd D:\findjob\findjob_new
.\venv\Scripts\activate

# 双平台爬 + 发（默认）
python run.py -k "Python Agent" -e 3 -c "深圳" -p 2

# 只爬智联招聘
python run.py -k "Java" -c "北京" -P zhaopin

# 只爬 BOSS 直聘
python run.py -k "前端" -c "上海" -P boss

# 全自动模式：爬 → 评 → 投，零确认
python run.py -k "Python" -c "深圳" -p 2 -a

# 从已有 JSON 进入（跳过爬虫）
python run.py --json output\jobs_20260725_120000.json

# 覆盖评分阈值
python run.py --json output\jobs_20260725_120000.json --score-min 70
```

---

## 参数说明

| 参数 | 说明 | 示例 |
|------|------|------|
| `-k, --keywords` | 搜索关键词，空格分隔多个词 | `-k "Python AI Agent"` |
| `-e, --exp` | 经验上限（年），0=只要应届，不传=不过滤经验 | `-e 3` |
| `-c, --cities` | 目标城市，空格分隔多个 | `-c "深圳 广州"` |
| `-p, --pages` | 每个关键词翻几页 | `-p 2` |
| `-P, --platform` | 目标平台：`all`（默认）、`boss`、`zhaopin` | `-P zhaopin` |
| `--salary-min` | 最低薪资 K | `--salary-min 8` |
| `--salary-max` | 最高薪资 K | `--salary-max 20` |
| `-d, --deal-breakers` | 屏蔽词，空格分隔 | `-d "外包 996"` |
| `-r, --resume` | 简历路径，覆盖 config.yaml | `-r C:\resume\my.md` |
| `--scale-min` | BOSS 公司最小规模（人数） | `--scale-min 20` |
| `--scale-max` | BOSS 公司最大规模（人数） | `--scale-max 999` |
| `-a, --auto` | 全自动模式：爬→评→生成→投递/发招呼语，零确认 | `-a` |
| `--json` | 跳过爬虫，从已有 JSON 进入批阅发送 | `--json output\jobs_xxx.json` |
| `--score-min` | AI 评分阈值，低于此分自动筛掉 | `--score-min 70` |

不传参数则读 `config.yaml` 中的默认值。

---

## 三个模式

| 模式 | 触发 | BOSS 行为 | 智联行为 | 用 AI？ | 速度 |
|------|------|-----------|----------|---------|------|
| `t` 轻触 | 交互选择 `t` | 点"立即沟通"发默认招呼语 | 点"立即投递"投简历 | 否 | ~8s/条 |
| `s` 逐个审 | 交互选择 `s`（默认） | 生成招呼语 → 确认入队 → 批量发 | 逐条确认 → 入队 → 批量投递 | 评分+生成 | 人工节奏 |
| `a` 全投 | 交互选择 `a` 或 CLI `--auto` | 自动生成全部招呼语 → 批量发 | 全部入队 → 批量投递 | 评分+生成 | ~30s/条 |

### 不同平台的区别

| | BOSS直聘 | 智联招聘 |
|---|---|---|
| 搜索方式 | 关键词 + 城市 | 关键词 + 城市 |
| 交互方式 | 发招呼语 + 自定义消息 | 直接投递简历（在线简历） |
| 需招呼语 | ✅ 自定义招呼语 | ❌ 不需要，直接投 |
| 轻触模式 | 点"立即沟通"发默认语 | 点"立即投递"投简历 |

---

## 流程

1. **爬岗位** → 搜索列表页 + 详情页提取 → `output/jobs_时间戳.json`
2. **硬过滤** → 经验/学历/薪资/屏蔽词过滤（本地，不费 token）
3. **AI 评分**（可选）→ DeepSeek 打分排序，低于阈值自动筛掉
4. **批阅发送** → 按平台分别处理（BOSS 发招呼语，智联投简历）

---

## 配置

编辑 `config.yaml`：

```yaml
resume_path: "../../resume/resume.md"       # 简历路径，-r 可覆盖

# 岗位过滤
salary_min: 8                            # 最低薪资 K
salary_max: 30                           # 最高薪资 K
allowed_edu: ["本科", "大专"]             # 接受的学历，"学历不限"自动保留
boss_scale: "302,303,304,305"            # BOSS 公司规模（301=0-20人 306=万人以上），留空=不过滤
deal_breakers:                           # 屏蔽词：命中标题/公司名/JD 即过滤
  - "外包"
  - "管培"
  - "单休"
  - "实习"
  - "培训"

# 默认搜索（命令行参数会覆盖）
search:
  keywords: ["Python", "AI", "Agent"]
  cities: ["深圳", "广州"]

# BOSS 规模参数值: 301=0-20人, 302=20-99人, 303=100-499人, 304=500-999人, 305=1000-9999人, 306=10000人以上

# DeepSeek API
ai:
  provider: "openai"
  model: "deepseek-chat"
  base_url: "https://api.deepseek.com/v1"

# AI 评分
scoring:
  threshold: 60                           # 低于此分自动筛掉

output_dir: "./output"
```

---

## 聊天 Agent（chat_agent.py）

守护进程，定时轮询 BOSS 聊天页的未读消息 → AI 生成回复 → 你在终端逐个审核发送。

### 运行

```powershell
cd D:\findjob\findjob_new\v2
.\venv\Scripts\activate

# 默认每 3 分钟轮询一次
python chat_agent.py

# 自定义轮询间隔（5 分钟）
python chat_agent.py -i 5

# 只跑一轮（不循环）
python chat_agent.py --once
```

前提：Chrome 已以 debug 模式启动，且打开了 BOSS 聊天页 `https://www.zhipin.com/web/geek/chat`。

### 交互流程

```
═══ 3 个未读会话 ═══

  [1] 沈女士 | 乐恋屋
      稍等我加你
      19:11
  [2] 李飞 | 意如图真科技
      你好，我们公司正在招聘初级java开发工程师，请问考虑吗
      21:43
  [3] 付先生 | 上海勤穆网络科技
      您是否接受此工作地点?
      17:43

a=全部 / 1,3=选第1和第3 / q=跳过本轮
处理哪些? (a):
```

选择后会逐个展示：

```
── [1/3] 沈女士 | 乐恋屋 ──

── 对话历史 ──
  我  看到这个岗位感觉挺对口的，我之前...
  HR  我们前期更专注于自动化工作流赋能各职能部门，愿意来做这一块吗
  HR  现在方便吗，加个微信语音沟通一会？
  我  好的
  我  已经加了
  HR  稍等我加你                           ← 未读

── 建议回复 ──
好的，通过后随时喊我，我这边现在方便语音。

── 建议动作 ──
  [发简历] 尚未发送附件简历

  y=发送回复  n=跳过  e=编辑  r=回复+发简历
```

### 操作说明

| 操作 | 行为 |
|------|------|
| `y` | 只发送 AI 回复 |
| `n` | 跳过，不处理这个会话 |
| `e` | 编辑回复文本后再发送 |
| `r` | 发送回复 + 发送附件简历（发送前选最新版本） |

### 会话选择

| 输入 | 行为 |
|------|------|
| `a` | 逐个审核所有未读会话 |
| `1,3` | 只处理第 1 和第 3 个 |
| `q` | 跳过本轮，等下次轮询 |

### 去重机制

处理过的会话会记录在 `output/chat_state.json`。下次轮询时，如果最后一条 HR 消息跟上次一样（hash 对比），自动跳过，不会重复生成回复。

### 简历检测

脚本会自动从对话历史判断是否已经发过附件简历。检测到以下关键词会跳过建议发简历：
- "已发送给Boss"
- "对方已查看了您的附件简历"
- "您的附件简历"

### 参数

| 参数 | 说明 | 默认 |
|------|------|------|
| `-i, --interval` | 轮询间隔（分钟） | `3` |
| `--once` | 只跑一轮，不循环 | — |

`Ctrl+C` 随时退出。

---

## 项目结构

```
findjob_new/v2/
  app.py              # FastAPI 后端 + WebSocket 实时进度
  static/
    index.html        # Web UI 前端（单页，vanilla JS）
  run.py              # CLI 入口 + 批阅主流程（爬→评→发）
  chat_agent.py       # 聊天守护进程（轮询未读→AI回复→审核发送）
  browser.py          # CDP 直连 Chrome（WebSocket）
  filters.py          # 硬过滤：经验/学历/薪资/屏蔽词 + JD 关键词
  ai.py               # DeepSeek：评分 + 招呼语生成
  config.yaml         # 配置文件（技术方向）
  config_1.yaml       # 配置文件（电商/独立站方向）
  .env                # DEEPSEEK_API_KEY
  requirements.txt
  boss/               # BOSS 直聘
    __init__.py
    scraper.py        #   列表+详情 JS 提取 + scrape() + URL经验/规模参数
    sender.py         #   发招呼语 + 轻触模式 + 限频弹窗处理
    chat.py           #   聊天操作：检测未读、读消息、发简历
  zhaopin/            # 智联招聘
    __init__.py
    scraper.py        #   列表+详情 JS 提取 + scrape()
    sender.py         #   投递简历（在线简历）
  output/             # 爬虫 JSON + chat_state.json
```

---

## 常见问题

**Q: 0 个结果？**
→ 检查 Chrome 是否已登录对应平台、放宽 `-e` 参数、检查 `deal_breakers` 是否过滤太严。

**Q: 前端报 Chrome 未连接？**
→ 确保 Chrome 以 `--remote-debugging-port=9222` 启动，且没有其他进程占用 9222 端口。先 `taskkill /F /IM chrome.exe` 再重新启动。

**Q: 智联投递没反应？**
→ 智联用的是在线简历投递，检查智联上是否已创建在线简历。已投递过的岗位会显示"已投递过"并跳过。

**Q: BOSS 发送显示成功但消息没出现？**
→ 已修（2026-07-23），改用 `execCommand('insertText')` + 聊天页统一发送。

**Q: 太慢？**
→ 用 `t` 模式零 token 海投，或评完分只处理高分岗。减少 `-p` 页数也能显著提速。
