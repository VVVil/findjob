# hunter — BOSS直聘轻量海投工具

## 首次安装

```powershell
cd D:\findjob\hunter
python -m venv venv
.\venv\Scripts\activate
pip install -r requirements.txt
```

在 `.env` 里设 API key：

```
DEEPSEEK_API_KEY=sk-xxx
```

## 启动 Chrome（每次开机一次）

```powershell
taskkill /F /IM chrome.exe
& "C:\Program Files\Google\Chrome\Application\chrome.exe" --remote-debugging-port=9222 --remote-allow-origins=* --user-data-dir="$env:TEMP\chrome_debug_hunter" "https://www.zhipin.com"
```

开好后扫码登录。

---

## 运行

```powershell
cd D:\findjob\hunter
.\venv\Scripts\activate

# 爬 + 发
python hunter.py -k "Python Agent" -e 3 -c "深圳" -p 2

# 从已有 JSON 进入
python hunter.py --json output\jobs_xxx.json

# 评分阈值覆盖
python hunter.py --json output\jobs_xxx.json --score-min 70
```

---

## 参数说明

| 参数 | 说明 | 示例 |
|------|------|------|
| `-k` | 搜索关键词，空格分多个词 | `-k "Python AI Agent"` |
| `-e` | 经验上限（年），0=只要应届 | `-e 3` |
| `-c` | 城市，空格分多个 | `-c "深圳 广州"` |
| `-p` | 每个关键词爬几页 | `-p 2` |
| `--salary-min` | 最低薪资 K | `--salary-min 8` |
| `--salary-max` | 最高薪资 K | `--salary-max 20` |
| `-d` | 屏蔽词，空格分隔 | `-d "外包 996"` |
| `--json` | 跳过爬虫，从已有 JSON 进入 | `--json output/jobs_xxx.json` |
| `--score-min` | AI 评分阈值，低于此分自动筛掉 | `--score-min 70` |

不传参数则读 `config.yaml`。

---

## 流程

1. **爬岗位** → 搜索列表 + 详情页提取 → `output/jobs_时间戳.json`
2. **硬过滤** → 经验/学历/薪资/屏蔽词（本地，不费 token）
3. **AI 评分**（可选）→ DeepSeek 打分排序，低于阈值自动筛掉
4. **选择模式**：

| 模式 | 说明 | 用 AI？ | 速度 |
|------|------|---------|------|
| `t` 轻触 | 只点"立即沟通"发默认招呼语 | 否 | ~8s/条 |
| `s` 逐个审 | 逐条生成招呼语 → 确认入队 → 批量发 | 评分+生成 | 人工节奏 |
| `a` 全投 | 自动生成全部招呼语 → 直接批量发 | 评分+生成 | ~30s/条 |

---

## 配置

编辑 `config.yaml`：

```yaml
resume_path: "../resume/resume.md"

# 过滤
salary_min: 8
salary_max: 20
allowed_edu: ["本科", "大专"]
deal_breakers: ["外包", "996", "管培", "单休", "实习"]

# 默认搜索
search:
  keywords: ["Python", "AI", "Agent"]
  cities: ["深圳", "广州"]

# DeepSeek 评分
scoring:
  threshold: 60          # 低于此分自动筛掉
```

---

## 项目结构

```
hunter/
  hunter.py          # CLI 入口 + main() + 主流程
  browser.py         # CDP 直连 Chrome
  scraper.py         # 爬虫：JS 提取脚本 + scrape()
  filters.py         # 初筛：经验/学历/薪资/deal_breaker
  ai.py              # AI：load_client、score_jobs、generate_greeting
  sender.py          # 发送：send_greeting + touch_job
  config.yaml        # 配置文件
  .env               # DEEPSEEK_API_KEY
  .gitignore
  requirements.txt
  output/            # 爬虫 JSON 输出
```

---

## 常见问题

**Q: 0 个结果？** → 检查 Chrome 是否已登录、放宽 `-e` 参数

**Q: 发送显示成功但消息没出现？** → 已修，2026-07-23 改用 `execCommand('insertText')` + 聊天页统一发送

**Q: 太慢？** → 用 `t` 模式零 token 海投，或评完分只发高分岗
