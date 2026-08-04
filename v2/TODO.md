# hunter TODO

> 最后更新：2026-07-23

---

## 当前状态

hunter 功能基本完整，跑通了爬→筛→评→审→发的全链路。但整体是一个 900 行的 `hunter.py` + 依赖 BossHunter 的 browser 模块，需要重构解耦。

---

## 下一步：重构解耦 + 上 GitHub

### 目标架构

```
hunter/
  hunter.py          # CLI 入口 + main() + 主流程          (~200 行)
  scraper.py         # 爬虫：JS 提取脚本 + scrape()        (~180 行)
  filters.py         # 初筛：经验/学历/薪资/deal_breaker   (~80 行)
  ai.py              # AI：load_client、score_jobs、generate_greeting (~150 行)
  sender.py          # 发送：send_greeting + 匹配/发送 JS  (~180 行)
  browser.py         # CDP 直连 Chrome（新写，替代 BossHunter）(~120 行)
  config.yaml        # 配置文件
  .env               # DEEPSEEK_API_KEY
  .gitignore         # 忽略 .env / output / __pycache__
  output/            # 爬虫 JSON 输出
  README.md          # 使用文档
```

### 1. 写 `browser.py` — CDP 直连 Chrome

不再依赖 `D:\findjob\BossHunter\src\bosshunter\browser`，直接用 WebSocket 连 Chrome DevTools Protocol。

**需要的函数（9 个）：**

| 函数 | 实现方式 |
|------|---------|
| `check_chrome_connection()` | `httpx.get("http://localhost:9222/json/version")` |
| `find_boss_tab()` | `httpx.get("/json")` 过滤 url 含 zhipin.com |
| `new_tab(url)` | `httpx.put("/json/new?url=" + quote(url))` |
| `close_tab(target_id)` | `httpx.get("/json/close/" + target_id)` |
| `navigate(target_id, url)` | CDP `Page.navigate` over WebSocket |
| `evaluate(target_id, expression)` | CDP `Runtime.evaluate` with `awaitPromise: true` |
| `wait_for_load(target_id)` | 轮询 `Runtime.evaluate("document.readyState")` |
| `click(target_id, selector)` | JS `document.querySelector(sel).click()` via evaluate |
| `configure(config)` | 空壳（保持接口兼容），直接 pass |

**依赖：** `httpx`（已有）+ `websocket-client`

**参考：** BossHunter 的 CDP 代理在 `D:\findjob\BossHunter\src\bosshunter\browser\runtime\cdp-proxy.mjs`

### 2. 拆 `hunter.py` 为 5 个文件

| 新文件 | 从 hunter.py 搬出 |
|--------|------------------|
| `scraper.py` | `JS_EXTRACT_LIST`、`JS_EXTRACT_DETAIL`、`scrape()` |
| `filters.py` | `parse_experience_max`、`parse_salary`、deal_breaker 检查等 |
| `ai.py` | `load_api_client`、`SCORING_PROMPT`、`score_jobs`、`generate_greeting` |
| `sender.py` | `send_greeting()` 全函数 + 内置的 click_js、popup_js、match_js、send_js |
| `hunter.py` | `main()` + import 上面所有模块 |

### 3. 加点工程化

- `.gitignore`：`.env`、`output/`、`__pycache__/`、`venv/`
- `README.md`：Chrome 启动命令、参数说明、使用流程
- 用独立 venv（不再依赖 BossHunter 的 `D:\findjob\BossHunter\venv`）
- `requirements.txt`：`httpx`、`websocket-client`、`openai`、`pyyaml`、`rich`、`python-dotenv`

---

## 已完成

- [x] 招呼语发送修复 —— `execCommand('insertText')` 替代 `innerText`
- [x] 发送流程统一 —— 导航到聊天页 + hr_name+company 精确匹配会话
- [x] 字段抓取修复 —— `icon-scale`/`icon-industry`/`boss-online-tag` 适配新版 DOM
- [x] AI 评分排序 —— `score_jobs()` + `--score-min` + config 阈值
- [x] 先审后发（两阶段）—— Phase 1 审岗入队，Phase 2 批量发送
- [x] a 模式全自动 —— 生成招呼语 → 直接批量发，无需确认
- [x] 发送间隔 20-40s → 15-25s，批浏览 2-5s

---

## Chrome 启动命令

```powershell
taskkill /F /IM chrome.exe
& "C:\Program Files\Google\Chrome\Application\chrome.exe" --remote-debugging-port=9222 --user-data-dir="$env:TEMP\chrome_debug_bh" "https://www.zhipin.com"
```

## 20260804
改排版
ai评分流程前端显示
去ai味
