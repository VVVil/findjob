# hunter — 多平台轻量海投工具

BOSS直聘 + 智联招聘，爬岗位 → 硬过滤 → AI 评分 → 批量投递/发招呼语。

## 首次安装

```powershell
cd D:\findjob\findjob_new
python -m venv venv
.\venv\Scripts\activate
pip install -r requirements.txt
```

在 `.env` 里设 API key：

```
DEEPSEEK_API_KEY=sk-xxx
```

---

## 启动 Chrome（每次开机一次）

启动 Chrome 调试模式，同时打开两个平台扫码登录：

```powershell
taskkill /F /IM chrome.exe
& "C:\Program Files\Google\Chrome\Application\chrome.exe" --remote-debugging-port=9222 --remote-allow-origins=* --user-data-dir="$env:TEMP\chrome_debug_hunter" "https://www.zhipin.com" "https://www.zhaopin.com"
```

开好后在浏览器里扫码登录两个平台。

---

## 运行

```powershell
cd D:\findjob\findjob_new
.\venv\Scripts\activate

# 双平台爬 + 发（默认）
python hunter.py -k "Python Agent" -e 3 -c "深圳" -p 2

# 只爬智联招聘
python hunter.py -k "Java" -c "北京" -P zhaopin

# 只爬 BOSS 直聘
python hunter.py -k "前端" -c "上海" -P boss

# 全自动模式：爬 → 评 → 投，零确认
python hunter.py -k "Python" -c "深圳" -p 2 -a

# 从已有 JSON 进入（跳过爬虫）
python hunter.py --json output\jobs_20260725_120000.json

# 覆盖评分阈值
python hunter.py --json output\jobs_20260725_120000.json --score-min 70
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
resume_path: "../resume/resume.md"       # 简历路径，-r 可覆盖

# 岗位过滤
salary_min: 8                            # 最低薪资 K
salary_max: 20                           # 最高薪资 K
allowed_edu: ["本科", "大专"]             # 接受的学历，"学历不限"自动保留
deal_breakers:                           # 屏蔽词：命中标题或公司名即过滤
  - "外包"
  - "996"
  - "管培"
  - "单休"
  - "实习"
  - "华为"
  - "阿里"

# 默认搜索（命令行参数会覆盖）
search:
  keywords: ["Python", "AI", "Agent"]
  cities: ["深圳", "广州"]

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

## 项目结构

```
findjob_new/
  hunter.py           # CLI 入口 + main() + 批阅主流程
  browser.py          # CDP 直连 Chrome（WebSocket）
  filters.py          # 硬过滤：经验/学历/薪资/屏蔽词
  ai.py               # DeepSeek：评分 + 招呼语生成
  config.yaml         # 配置文件
  .env                # DEEPSEEK_API_KEY
  requirements.txt
  boss/               # BOSS 直聘
    __init__.py
    scraper.py         #   列表+详情 JS 提取 + scrape()
    sender.py          #   发招呼语 + 轻触模式
  zhaopin/            # 智联招聘
    __init__.py
    scraper.py         #   列表+详情 JS 提取 + scrape()
    sender.py          #   投递简历（在线简历）
  output/             # 爬虫 JSON 输出
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
