#!/usr/bin/env python3
"""
findjob Web UI — FastAPI 后端
启动: python app.py  → 访问 http://localhost:8000
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import threading
import time
from datetime import datetime
from pathlib import Path

# Windows 后台运行时 stdout 为 GBK 编码，rich 打印 emoji（📄 ✓ ✗ ⏭）会抛
# UnicodeEncodeError 并中断爬虫线程。强制 stdout/stderr 用 UTF-8，兜底 replace。
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

import yaml
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from logbridge import ScrapeAborted, set_web_log_hook

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from ai import load_api_client, score_jobs, generate_greeting
from boss.scraper import scrape as scrape_boss
from boss.sender import send_greeting as boss_send
from zhaopin.scraper import scrape as scrape_zhaopin
from zhaopin.sender import apply_job as zhilian_apply
from browser import BrowserSession, check_chrome_connection, ensure_chrome, close_chrome

app = FastAPI(title="findjob")

# ── 全局状态 ───────────────────────────────────
_jobs: list[dict] = []
_boss_browser: BrowserSession | None = None
_zhilian_browser: BrowserSession | None = None
_api_client = None
_api_model = ""
_resume = ""

# ── WebSocket 管理器 ────────────────────────────

class WSManager:
    def __init__(self):
        self.connections: list[WebSocket] = []
        self._loop = None

    def set_loop(self, loop):
        self._loop = loop

    async def connect(self, ws: WebSocket):
        await ws.accept()
        self.connections.append(ws)

    def disconnect(self, ws: WebSocket):
        if ws in self.connections:
            self.connections.remove(ws)

    def broadcast(self, event_type: str, data: dict):
        """线程安全广播"""
        msg = json.dumps({"type": event_type, "data": data}, ensure_ascii=False)
        dead = []
        for ws in self.connections:
            try:
                if self._loop:
                    asyncio.run_coroutine_threadsafe(self._send(ws, msg), self._loop)
            except Exception:
                dead.append(ws)
        for ws in dead:
            self.connections.remove(ws)

    async def _send(self, ws: WebSocket, msg: str):
        try:
            await ws.send_text(msg)
        except Exception:
            self.disconnect(ws)

ws_manager = WSManager()


# ── 配置加载 ────────────────────────────────────

def load_cfg():
    cfg_path = HERE / "config.yaml"
    if cfg_path.exists():
        return yaml.safe_load(cfg_path.read_text(encoding="utf-8")) or {}
    return {}

_cfg = load_cfg()


def _load_resume():
    global _resume
    if _resume:
        return _resume
    rp = _cfg.get("resume_path", "../resume/resume.md")
    rp = Path(rp)
    if not rp.is_absolute():
        rp = HERE / rp
    if rp.exists():
        _resume = rp.read_text(encoding="utf-8")
    return _resume


# ── 进度回调（供 scraper 用） ─────────────────────

def _scraper_progress(event_type: str, data: dict):
    ws_manager.broadcast(event_type, data)


# 让 scraper 里的 console.print 日志也转发到 Web 前端
set_web_log_hook(_scraper_progress)


# ═══════════════════════════════════════════════
#  WebSocket
# ═══════════════════════════════════════════════

@app.websocket("/ws")
async def ws_endpoint(ws: WebSocket):
    await ws_manager.connect(ws)
    try:
        while True:
            await ws.receive_text()
    except WebSocketDisconnect:
        ws_manager.disconnect(ws)


# ═══════════════════════════════════════════════
#  REST API
# ═══════════════════════════════════════════════

@app.get("/api/jobs")
async def get_jobs():
    return {"jobs": _jobs, "total": len(_jobs)}


@app.get("/api/config")
async def get_config():
    cfg = load_cfg()
    return {"deal_breakers": cfg.get("deal_breakers", [])}


@app.post("/api/scrape")
async def start_scrape(params: dict):
    global _jobs, _browser
    _cfg = load_cfg()

    platforms = params.get("platforms", ["boss"])
    if not platforms:
        return JSONResponse({"error": "请选择至少一个平台"}, status_code=400)

    # 检查所需浏览器
    boss_need = "boss" in platforms
    zhilian_need = "zhaopin" in platforms
    boss_port = _cfg.get("browser", {}).get("boss_port", 9222)
    zhilian_port = _cfg.get("browser", {}).get("zhilian_port", 9223)

    if boss_need:
        if not check_chrome_connection(boss_port):
            ws_manager.broadcast("log", {"msg": f"BOSS Chrome 未运行，自动启动中 (port {boss_port})..."})
        if not await asyncio.to_thread(ensure_chrome, boss_port, "chrome_boss", "https://www.zhipin.com"):
            return JSONResponse({"error": f"BOSS Chrome 启动失败 (port {boss_port})"}, status_code=400)
    if zhilian_need:
        if not check_chrome_connection(zhilian_port):
            ws_manager.broadcast("log", {"msg": f"智联 Chrome 未运行，自动启动中 (port {zhilian_port})..."})
        if not await asyncio.to_thread(ensure_chrome, zhilian_port, "chrome_zhilian", "https://www.zhaopin.com"):
            return JSONResponse({"error": f"智联 Chrome 启动失败 (port {zhilian_port})"}, status_code=400)

    keywords = [k.strip() for k in params.get("keywords", "").split() if k.strip()]
    cities = [c.strip() for c in params.get("cities", "").split() if c.strip()]
    if not keywords or not cities:
        return JSONResponse({"error": "请填写关键词和城市"}, status_code=400)

    pages = params.get("pages", 2)
    max_exp = params.get("max_exp")

    run_cfg = dict(_cfg)
    for k in ("salary_min", "salary_max"):
        if params.get(k) is not None:
            run_cfg[k] = params[k]

    scale_min = params.get("scale_min")
    scale_max = params.get("scale_max")
    if (scale_min is not None or scale_max is not None) and boss_need:
        from boss.scraper import _build_scale_param
        run_cfg["boss_scale"] = _build_scale_param(scale_min, scale_max)

    if "deal_breakers" in params:
        run_cfg["deal_breakers"] = params.get("deal_breakers", "").split()

    def _run_scrape():
        global _jobs, _boss_browser, _zhilian_browser
        threads: list[tuple] = []
        results_boss: list[dict] = []
        results_zhaopin: list[dict] = []
        aborted = threading.Event()

        if boss_need:
            _boss_browser = BrowserSession(port=boss_port)
            def _do_boss():
                ws_manager.broadcast("log", {"msg": "BOSS 爬虫启动"})
                try:
                    results_boss.extend(scrape_boss(
                        _boss_browser, run_cfg, keywords, cities,
                        per_combo_pages=pages, max_exp=max_exp,
                        on_progress=_scraper_progress
                    ))
                except ScrapeAborted:
                    aborted.set()
                except Exception as e:
                    import traceback
                    ws_manager.broadcast("error", {"stage": "boss_scrape", "message": str(e), "trace": traceback.format_exc()})
            threads.append((threading.Thread(target=_do_boss, daemon=True), "BOSS"))

        if zhilian_need:
            _zhilian_browser = BrowserSession(port=zhilian_port)
            def _do_zhaopin():
                ws_manager.broadcast("log", {"msg": "智联 爬虫启动"})
                try:
                    results_zhaopin.extend(scrape_zhaopin(
                        _zhilian_browser, run_cfg, keywords, cities,
                        per_combo_pages=pages, max_exp=max_exp,
                        on_progress=_scraper_progress
                    ))
                except ScrapeAborted:
                    aborted.set()
                except Exception as e:
                    import traceback
                    ws_manager.broadcast("error", {"stage": "zhilian_scrape", "message": str(e), "trace": traceback.format_exc()})
            threads.append((threading.Thread(target=_do_zhaopin, daemon=True), "智联"))

        for t, _ in threads:
            t.start()
        for t, _ in threads:
            t.join()

        if aborted.is_set():
            ws_manager.broadcast("log", {"msg": "已停止，本次爬取数据未保存"})
            return

        _jobs = results_boss + results_zhaopin
        ws_manager.broadcast("scrape_done", {"total": len(_jobs), "boss": len(results_boss), "zhaopin": len(results_zhaopin)})

    threading.Thread(target=_run_scrape, daemon=True).start()
    return {"status": "ok"}


@app.post("/api/stop")
async def stop_all():
    """中断爬取：关闭 BOSS + 智联两个 debug 浏览器进程，不保存已爬数据"""
    cfg = load_cfg()
    boss_port = cfg.get("browser", {}).get("boss_port", 9222)
    zhilian_port = cfg.get("browser", {}).get("zhilian_port", 9223)

    # 先通知爬虫线程退出（触发 ScrapeAborted，跳过结果保存）
    for b in (_boss_browser, _zhilian_browser):
        if b is not None:
            b.shutdown()

    # 关闭两个浏览器进程（放线程池，避免阻塞事件循环）
    await asyncio.to_thread(close_chrome, boss_port)
    await asyncio.to_thread(close_chrome, zhilian_port)

    ws_manager.broadcast("log", {"msg": "已停止，BOSS/智联 浏览器已关闭"})
    return {"status": "ok"}


@app.post("/api/score")
async def start_score(params: dict):
    global _jobs, _api_client, _api_model
    threshold = params.get("score_threshold", 60)
    resume_text = _load_resume()

    if not resume_text:
        return JSONResponse({"error": "简历未加载，请检查 resume_path"}, status_code=400)
    if not _jobs:
        return JSONResponse({"error": "没有岗位，请先爬取"}, status_code=400)

    if not _api_client:
        _api_client = load_api_client(_cfg)
        _api_model = _cfg.get("ai", {}).get("model", "deepseek-chat")

    def _run_score():
        global _jobs
        try:
            total = len(_jobs)
            # 进度推送
            for i, j in enumerate(_jobs):
                ws_manager.broadcast("scoring", {
                    "progress": f"{i+1}/{total}",
                    "company": j.get("company", ""),
                    "title": j.get("title", ""),
                })

            scored = score_jobs(_api_client, _api_model, resume_text, list(_jobs), threshold)
            scored_map = {j["url"]: j for j in scored}
            scored_urls = set(scored_map.keys())

            for j in _jobs:
                if j["url"] in scored_urls:
                    j["_scored"] = True
                    j["_score"] = scored_map[j["url"]].get("score", 0)
                else:
                    j["_scored"] = False
                    j["_score"] = 0

            _jobs_kept = [j for j in _jobs if j.get("_scored")]
            _jobs_removed = [j for j in _jobs if not j.get("_scored")]

            ws_manager.broadcast("scoring_done", {
                "kept": len(_jobs_kept),
                "removed": len(_jobs_removed),
            })
        except Exception as e:
            ws_manager.broadcast("error", {"stage": "score", "message": str(e)})

    threading.Thread(target=_run_score, daemon=True).start()
    return {"status": "ok"}


@app.post("/api/generate")
async def start_generate(params: dict):
    global _jobs, _api_client, _api_model
    resume_text = _load_resume()
    if not resume_text:
        return JSONResponse({"error": "简历未加载"}, status_code=400)

    if not _api_client:
        _api_client = load_api_client(_cfg)
        _api_model = _cfg.get("ai", {}).get("model", "deepseek-chat")

    # 只生成用户勾选的 + 还没招呼语的
    selected_urls = set(params.get("urls", []) or [])
    if selected_urls:
        targets = [j for j in _jobs if j.get("url") in selected_urls and not j.get("_greeting")]
    else:
        targets = [j for j in _jobs if j.get("_scored", True) and not j.get("_greeting")]

    if not targets:
        return JSONResponse({"error": "没有需要生成招呼语的岗位"}, status_code=400)

    def _run_generate():
        total = len(targets)
        for i, job in enumerate(targets):
            ws_manager.broadcast("generating", {
                "progress": f"{i+1}/{total}",
                "company": job.get("company", ""),
                "title": job.get("title", ""),
            })
            try:
                greeting = generate_greeting(_api_client, _api_model, resume_text, job)
                job["_greeting"] = greeting or ""
                ws_manager.broadcast("greeting_result", {
                    "url": job.get("url", ""),
                    "ok": bool(greeting),
                    "greeting": greeting or "",
                })
            except Exception as e:
                job["_greeting"] = ""
                ws_manager.broadcast("greeting_result", {
                    "url": job.get("url", ""),
                    "ok": False,
                    "error": str(e),
                })
        ws_manager.broadcast("generate_done", {"total": total})

    threading.Thread(target=_run_generate, daemon=True).start()
    return {"status": "ok"}


@app.post("/api/update-greeting")
async def update_greeting(params: dict):
    """编辑招呼语"""
    url = params.get("url", "")
    greeting = params.get("greeting", "")
    for j in _jobs:
        if j.get("url") == url:
            j["_greeting"] = greeting
            return {"status": "ok"}
    return JSONResponse({"error": "not found"}, status_code=404)


@app.post("/api/send")
async def start_send(params: dict):
    global _boss_browser, _zhilian_browser

    # 只发送前端勾选的岗位（勾选 url 由前端传过来，后端 _jobs 不存 _checked）
    selected_urls = set(params.get("urls", []) or [])
    if not selected_urls:
        return JSONResponse({"error": "没有勾选岗位"}, status_code=400)

    # 确定需要哪些平台的浏览器，并确保在运行（停止可能已关闭它们）
    cfg = load_cfg()
    boss_port = cfg.get("browser", {}).get("boss_port", 9222)
    zhilian_port = cfg.get("browser", {}).get("zhilian_port", 9223)

    need_boss = any(j.get("url") in selected_urls and j.get("platform") != "zhaopin" for j in _jobs)
    need_zhilian = any(j.get("url") in selected_urls and j.get("platform") == "zhaopin" for j in _jobs)

    if need_boss:
        if not check_chrome_connection(boss_port):
            ws_manager.broadcast("log", {"msg": f"BOSS Chrome 未运行，自动启动中 (port {boss_port})..."})
        if not await asyncio.to_thread(ensure_chrome, boss_port, "chrome_boss", "https://www.zhipin.com"):
            return JSONResponse({"error": f"BOSS Chrome 启动失败 (port {boss_port})"}, status_code=400)
        _boss_browser = BrowserSession(port=boss_port)
    if need_zhilian:
        if not check_chrome_connection(zhilian_port):
            ws_manager.broadcast("log", {"msg": f"智联 Chrome 未运行，自动启动中 (port {zhilian_port})..."})
        if not await asyncio.to_thread(ensure_chrome, zhilian_port, "chrome_zhilian", "https://www.zhaopin.com"):
            return JSONResponse({"error": f"智联 Chrome 启动失败 (port {zhilian_port})"}, status_code=400)
        _zhilian_browser = BrowserSession(port=zhilian_port)

    targets: list[tuple] = []
    for j in _jobs:
        if j.get("url") not in selected_urls:
            continue
        if j.get("_send_status") == "ok":
            continue
        if j.get("platform") == "zhaopin":
            if _zhilian_browser:
                targets.append((j, _zhilian_browser, None))
        else:
            if j.get("_greeting") and _boss_browser:
                targets.append((j, _boss_browser, j.get("_greeting", "")))

    if not targets:
        return JSONResponse({"error": "没有可发送的岗位"}, status_code=400)

    def _run_send():
        total = len(targets)
        for i, (job, browser, greeting) in enumerate(targets):
            ws_manager.broadcast("sending", {
                "progress": f"{i+1}/{total}",
                "company": job.get("company", ""),
                "title": job.get("title", ""),
                "platform": job.get("platform", "boss"),
            })
            try:
                if job.get("platform") == "zhaopin":
                    ok = zhilian_apply(browser, job)
                    err = "" if ok else "投递失败"
                else:
                    ok, err = boss_send(browser, job, greeting, fast=True)
                job["_send_status"] = "ok" if ok else "failed"
                job["_send_error"] = "" if ok else err
                ws_manager.broadcast("send_result", {
                    "url": job.get("url", ""),
                    "ok": ok,
                    "error": err,
                })
            except Exception as e:
                job["_send_status"] = "failed"
                job["_send_error"] = str(e)
                ws_manager.broadcast("send_result", {
                    "url": job.get("url", ""),
                    "ok": False,
                    "error": str(e),
                })
            if i < total - 1:
                time.sleep(15)
        ws_manager.broadcast("send_done", {"total": total})

    threading.Thread(target=_run_send, daemon=True).start()
    return {"status": "ok"}


@app.on_event("startup")
async def startup():
    ws_manager.set_loop(asyncio.get_running_loop())


# ── 静态文件 ────────────────────────────────────
static_dir = HERE / "static"
static_dir.mkdir(exist_ok=True)

if (static_dir / "index.html").exists():
    app.mount("/", StaticFiles(directory=str(static_dir), html=True), name="static")
