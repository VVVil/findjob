#!/usr/bin/env python3
"""
findjob — 多平台求职助手（BOSS直聘 + 智联招聘）
爬岗位 → 硬过滤 → 逐条批阅 → 生成招呼语/投简历 → 发送

用法:
  python run.py -k "Python 后端" -e 3 -c "深圳" -p 2
  python run.py --json jobs_20260722.json    # 从已有 JSON 直接进入批阅发送

浏览器启动（双 Chrome，一人一个）:
  chrome.exe --remote-debugging-port=9222 --remote-allow-origins=* --user-data-dir="%TEMP%/chrome_boss" https://www.zhipin.com
  chrome.exe --remote-debugging-port=9223 --remote-allow-origins=* --user-data-dir="%TEMP%/chrome_zhilian" https://www.zhaopin.com
"""

import argparse
import json
import os
import random
import signal
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, wait as fut_wait, FIRST_COMPLETED
from datetime import datetime
from pathlib import Path

import yaml
from rich.console import Console
from rich.prompt import Prompt

from ai import load_api_client, score_jobs, generate_greeting
from boss.scraper import scrape as scrape_boss
from boss.sender import send_greeting, touch_job as boss_touch
from zhaopin.scraper import scrape as scrape_zhaopin
from zhaopin.sender import apply_job as zhilian_apply
from browser import BrowserSession, check_chrome_connection, configure

console = Console()
HERE = Path(__file__).resolve().parent

# Ctrl+C flag
_shutdown_requested = threading.Event()
_resume_lock = threading.Lock()
RESUME_FILE = "output/jobs_auto_resume.json"


def _hard_interrupt(signum, frame):
    """硬中断：绕过 Python 异常机制，OS 层直接杀进程。

    当主线程被同步 socket I/O（httpx/OpenAI client）阻塞时，
    Python 的 KeyboardInterrupt 无法投递。此 handler 由 OS 的
    console control handler 直接调用，保证 Ctrl+C 始终有效。
    """
    console.print("\n[yellow]Ctrl+C 已触发，正在退出...[/yellow]")
    _shutdown_browsers()
    os._exit(0)


signal.signal(signal.SIGINT, _hard_interrupt)


def _save_resume(pairs: list, start_idx: int) -> None:
    """将 pairs[start_idx:] 写入断点续跑文件，每个 job 嵌入 _greeting"""
    remaining = []
    for job, greeting in pairs[start_idx:]:
        job_copy = dict(job)
        job_copy["_greeting"] = greeting
        remaining.append(job_copy)
    with _resume_lock:
        try:
            resume_path = os.path.join(HERE, RESUME_FILE)
            with open(resume_path, "w", encoding="utf-8") as f:
                json.dump(remaining, f, ensure_ascii=False, indent=2)
        except Exception:
            pass  # 写文件失败不影响主流程


def _clear_resume() -> None:
    """删除断点续跑文件"""
    resume_path = os.path.join(HERE, RESUME_FILE)
    try:
        if os.path.exists(resume_path):
            os.remove(resume_path)
    except Exception:
        pass

# 各平台浏览器实例（在 main() 中创建）
_boss_browser: BrowserSession | None = None
_zhilian_browser: BrowserSession | None = None


# ══════════════════════════════════════════════════════
#  配置加载
# ══════════════════════════════════════════════════════

def load_config():
    cfg_path = HERE / "config.yaml"
    if not cfg_path.exists():
        default = {
            "resume_path": "../resume/resume.md",
            "deal_breakers": ["外包", "996", "管培", "单休", "实习", "华为", "阿里", "外企德科"],
            "salary_min": 8,
            "salary_max": 20,
            "allowed_edu": ["本科", "大专"],
            "ai": {
                "provider": "openai",
                "model": "deepseek-chat",
                "base_url": "https://api.deepseek.com/v1",
            },
            "output_dir": "./output",
            "browser": {
                "boss_port": 9222,
                "zhilian_port": 9223,
            },
        }
        cfg_path.write_text(yaml.dump(default, allow_unicode=True), encoding="utf-8")
        console.print("[yellow]已生成默认 config.yaml，请检查修改后重新运行[/yellow]")
        sys.exit(0)
    return yaml.safe_load(cfg_path.read_text(encoding="utf-8"))


# ══════════════════════════════════════════════════════
#  平台路由 helper
# ══════════════════════════════════════════════════════

def platform_label(job: dict) -> str:
    """返回平台中文标签"""
    return "智联" if job.get("platform") == "zhaopin" else "BOSS"


def get_browser(job: dict) -> BrowserSession:
    """根据岗位平台返回对应浏览器实例"""
    if job.get("platform") == "zhaopin":
        return _zhilian_browser
    return _boss_browser


def touch_action(job: dict) -> bool:
    """轻触：BOSS=立即沟通, 智联=立即投递"""
    browser = get_browser(job)
    if job.get("platform") == "zhaopin":
        return zhilian_apply(browser, job)
    return boss_touch(browser, job)


def send_action(job: dict, greeting: str = "") -> tuple:
    """发送：BOSS=发招呼语, 智联=投简历"""
    browser = get_browser(job)
    if job.get("platform") == "zhaopin":
        ok = zhilian_apply(browser, job)
        return (ok, "" if ok else "投递失败")
    return send_greeting(browser, job, greeting, fast=True)


# ══════════════════════════════════════════════════════
#  主流程
# ══════════════════════════════════════════════════════

def main():
    global _boss_browser, _zhilian_browser

    parser = argparse.ArgumentParser(description="findjob — 多平台求职助手（BOSS直聘 + 智联招聘）")
    parser.add_argument("-k", "--keywords", default="", help="搜索关键词，空格分隔")
    parser.add_argument("-e", "--exp", type=int, default=None, help="经验上限(年)，0=只要应届，不传=不过滤经验")
    parser.add_argument("-c", "--cities", default="", help="目标城市，空格分隔")
    parser.add_argument("-p", "--pages", type=int, default=None, help="每组合翻几页 (无 -n 时默认2)")
    parser.add_argument("-n", "--target", type=int, default=None, help="每个关键词每平台找多少个岗位")
    parser.add_argument("--max-pages", type=int, default=20, help="单组合最多翻几页，兜底 (默认20)")
    parser.add_argument("-P", "--platform", choices=["all", "boss", "zhaopin"], default="all",
                        help="目标平台：all=双平台, boss=仅BOSS, zhaopin=仅智联 (默认all)")
    parser.add_argument("--salary-min", type=int, default=None, help="最低薪资K")
    parser.add_argument("--salary-max", type=int, default=None, help="最高薪资K")
    parser.add_argument("--scale-min", type=int, default=None, help="BOSS公司最小规模(人数)")
    parser.add_argument("--scale-max", type=int, default=None, help="BOSS公司最大规模(人数)")
    parser.add_argument("-d", "--deal-breakers", default="", help="屏蔽词，空格分隔")
    parser.add_argument("--json", dest="json_file", help="从已有 JSON 文件直接进入批阅发送")
    parser.add_argument("--score-min", type=int, default=None, help="评分阈值，低于此分自动筛掉")
    parser.add_argument("-r", "--resume", default=None, help="简历路径，覆盖 config.yaml")
    parser.add_argument("-a", "--auto", action="store_true", help="全自动：爬→评→生成→发，零确认")
    parser.add_argument("--scrape-only", action="store_true", help="只爬取保存JSON，不评分不发送（适合无人值守）")
    args = parser.parse_args()

    cfg = load_config()

    # ── CLI 参数覆盖 config ──────────────────────────
    if args.salary_min is not None:
        cfg["salary_min"] = args.salary_min
    if args.salary_max is not None:
        cfg["salary_max"] = args.salary_max
    if args.deal_breakers:
        cfg["deal_breakers"] = args.deal_breakers.split()
    if args.scale_min is not None or args.scale_max is not None:
        from boss.scraper import _build_scale_param
        cfg["boss_scale"] = _build_scale_param(args.scale_min, args.scale_max)

    browser_cfg = cfg.get("browser", {})
    boss_port = browser_cfg.get("boss_port", 9222)
    zhilian_port = browser_cfg.get("zhilian_port", 9223)

    # ── 检查浏览器连接（JSON 模式也需要，发送阶段要用） ──
    configure({})

    need_boss = args.platform in ("all", "boss")
    need_zhilian = args.platform in ("all", "zhaopin")

    if need_boss:
        if not check_chrome_connection(boss_port):
            console.print(f"[red]BOSS Chrome 未连接 (port {boss_port})！请先启动:[/red]")
            console.print(f'[dim]chrome.exe --remote-debugging-port={boss_port} --remote-allow-origins=* --user-data-dir="%TEMP%\\chrome_boss" https://www.zhipin.com[/dim]')
            sys.exit(1)
        _boss_browser = BrowserSession(port=boss_port)
        if not args.json_file:
            # JSON 模式不强制要求 zhipin.com tab（可能只要智联）
            boss_tab = _boss_browser.find_tab("zhipin.com")
            if not boss_tab:
                console.print("[red]未发现 BOSS直聘 页面，请先打开并登录 zhipin.com[/red]")
                sys.exit(1)
        console.print(f"[green]✓ BOSS 浏览器就绪 (port {boss_port})[/green]")

    if need_zhilian:
        if not check_chrome_connection(zhilian_port):
            console.print(f"[red]智联 Chrome 未连接 (port {zhilian_port})！请先启动:[/red]")
            console.print(f'[dim]chrome.exe --remote-debugging-port={zhilian_port} --remote-allow-origins=* --user-data-dir="%TEMP%\\chrome_zhilian" https://www.zhaopin.com[/dim]')
            sys.exit(1)
        _zhilian_browser = BrowserSession(port=zhilian_port)
        if not args.json_file:
            zhilian_tab = _zhilian_browser.find_tab("zhaopin.com")
            if not zhilian_tab:
                console.print("[red]未发现 智联招聘 页面，请先打开并登录 zhaopin.com[/red]")
                sys.exit(1)
        console.print(f"[green]✓ 智联 浏览器就绪 (port {zhilian_port})[/green]")

    # ── 加载简历 ─────────────────────────────────
    resume_cfg = args.resume or cfg.get("resume_path", "../resume/resume.md")
    resume_path = Path(resume_cfg)
    if not resume_path.is_absolute():
        resume_path = HERE / resume_path
    if not resume_path.exists():
        console.print(f"[yellow]简历不存在: {resume_path}，招呼语生成将受限[/yellow]")
        resume = ""
    else:
        resume = resume_path.read_text(encoding="utf-8")
        console.print(f"[green]✓ 简历已加载 ({len(resume)} 字符) → {resume_path}[/green]")

    # ── 获取岗位列表 ──────────────────────────────
    jobs = []

    if args.json_file:
        json_path = Path(args.json_file)
        if not json_path.exists():
            console.print(f"[red]文件不存在: {json_path}[/red]")
            sys.exit(1)
        jobs = json.loads(json_path.read_text(encoding="utf-8"))
        console.print(f"[green]✓ 从 JSON 加载 {len(jobs)} 个岗位[/green]")
    else:
        # 解析参数
        kw_str = args.keywords or " ".join(cfg.get("search", {}).get("keywords", ["Python"]))
        keywords = kw_str.split()
        cities_str = args.cities or " ".join(cfg.get("search", {}).get("cities", cfg.get("profile", {}).get("target_cities", ["深圳"])))
        cities = cities_str.split()

        max_exp = args.exp
        if max_exp is None:
            max_exp = cfg.get("profile", {}).get("max_experience_years")

        # 翻页策略：有 -n 而无 -p → 不限页（靠 max_pages 兜底）；否则用 -p 或默认2
        per_combo_pages = args.pages
        if per_combo_pages is None:
            per_combo_pages = 0 if args.target else 2  # 0 表示不限
        target_per_combo = args.target  # 每个城市×关键词组合的目标数
        max_pages = args.max_pages

        mode_parts = []
        if target_per_combo:
            mode_parts.append(f"每组合{target_per_combo}个")
        else:
            mode_parts.append(f"每组合{per_combo_pages}页")
        mode_str = " | ".join(mode_parts)
        console.print(f"[bold]搜索:[/bold] {keywords} | 城市: {cities} | {mode_str} | 经验≤{max_exp}年 | 平台: {args.platform}")
        console.print()

        jobs = []

        # 两个 scraper 并行跑，各用各的浏览器实例（真正并行，互不干扰）
        futures = {}
        with ThreadPoolExecutor(max_workers=2) as executor:
            if need_boss:
                futures[executor.submit(
                    scrape_boss, _boss_browser, cfg, keywords, cities,
                    per_combo_pages, args.exp, target_per_combo, max_pages
                )] = "BOSS"
            if need_zhilian:
                futures[executor.submit(
                    scrape_zhaopin, _zhilian_browser, cfg, keywords, cities,
                    per_combo_pages, args.exp, target_per_combo, max_pages
                )] = "智联"

            # 用 wait() + timeout 轮询，让 Ctrl+C 有机会投递 KeyboardInterrupt
            pending = set(futures.keys())
            try:
                while pending:
                    if _shutdown_requested.is_set():
                        console.print("\n[yellow]已取消爬取[/yellow]")
                        _shutdown_browsers()
                        executor.shutdown(wait=False, cancel_futures=True)
                        os._exit(0)
                    done, pending = fut_wait(pending, timeout=0.5, return_when=FIRST_COMPLETED)
                    for fut in done:
                        name = futures[fut]
                        try:
                            jobs_list = fut.result(timeout=0)
                            jobs.extend(jobs_list)
                            console.print(f"[green]{name}: {len(jobs_list)} 个岗位[/green]")
                        except Exception as exc:
                            console.print(f"[red]{name} 爬取出错: {exc}[/red]")
            except KeyboardInterrupt:
                _shutdown_requested.set()
                _shutdown_browsers()
                console.print("\n[yellow]已中断，正在清理...[/yellow]")
                executor.shutdown(wait=False, cancel_futures=True)
                os._exit(0)

        if not jobs:
            console.print("[yellow]没有匹配的岗位！[/yellow]")
            sys.exit(0)

        # 保存 JSON
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        output_dir = HERE / cfg.get("output_dir", "./output")
        output_dir.mkdir(parents=True, exist_ok=True)
        json_path = output_dir / f"jobs_{ts}.json"
        json_path.write_text(json.dumps(jobs, ensure_ascii=False, indent=2), encoding="utf-8")
        console.print(f"\n[green]✓ 已保存 {len(jobs)} 个岗位 → {json_path}[/green]")

        if args.scrape_only:
            console.print(f"[green] --scrape-only，退出（文件: {json_path}）[/green]")
            sys.exit(0)

    # ── a/s/t/q 批阅 ────────────────────────────────
    if not jobs:
        sys.exit(0)

    # Load API client
    client = load_api_client(cfg)
    model = cfg.get("ai", {}).get("model", "deepseek-chat")

    # ── 评分（CLI --score-min 覆盖 config） ──────────
    score_threshold = args.score_min if args.score_min is not None else cfg.get("scoring", {}).get("threshold")
    if score_threshold is not None and resume:
        if args.auto:
            console.print(f"\n[bold]自动评分中 (阈值={score_threshold}分)...[/bold]")
            jobs = score_jobs(client, model, resume, jobs, score_threshold)
            if not jobs:
                console.print("[yellow]评分后无剩余岗位[/yellow]")
                sys.exit(0)
        else:
            do_score = Prompt.ask(f"\n[bold]是否 AI 评分排序？[/bold] (阈值={score_threshold}分)", choices=["y", "n"], default="y")
            if do_score == "y":
                jobs = score_jobs(client, model, resume, jobs, score_threshold)
                if not jobs:
                    console.print("[yellow]评分后无剩余岗位[/yellow]")
                    sys.exit(0)

    console.print()
    console.print(f"[bold cyan]═══ 批阅 {len(jobs)} 个岗位 ═══[/bold cyan]")

    if args.auto:
        choice = "1"
        console.print("[bold green]  🤖 全自动模式：爬→评→投递/发招呼语，零确认[/bold green]")
    else:
        console.print("  1 = 全投（批量投递/发招呼语） | 2 = 逐个审 | 3 = 轻触（只点沟通/投递） | 4 = 退出")
        choice = Prompt.ask("[bold]操作[/bold]", choices=["1", "2", "3", "4"], default="2")

    if choice == "4":
        console.print("[yellow]已退出[/yellow]")
        sys.exit(0)

    if choice == "3":
        # 轻触模式：BOSS=立即沟通, 智联=立即投递，不用大模型
        console.print(f"\n[bold]t 模式：轻触 {len(jobs)} 个岗位[/bold]\n")
        touched = 0
        for i, job in enumerate(jobs, 1):
            p = platform_label(job)
            console.print(f"[dim]  [{i}/{len(jobs)}] [{p}] {job['company']} - {job['title']}...[/dim]", end="\r")
            if touch_action(job):
                touched += 1
            else:
                console.print(f"[yellow]  [{i}/{len(jobs)}] [{p}] {job['company']} - 操作失败[/yellow]")
            if i < len(jobs):
                wait = random.uniform(5, 10)
                time.sleep(wait)
        console.print(f"\n[green]✓ 轻触完成！已操作 {touched}/{len(jobs)}[/green]")
        sys.exit(0)

    # ═══════════════════════════════════════════
    #  Phase 1: 审岗 → 构建待发队列
    # ═══════════════════════════════════════════
    pending = []

    if choice == "1":
        # 1 模式：BOSS=生成招呼语, 智联=直接投递 → 批量
        console.print(f"\n[bold]全投模式：{len(jobs)} 个岗位[/bold]\n")
        for i, job in enumerate(jobs, 1):
            p = platform_label(job)
            if job.get("platform") == "zhaopin":
                pending.append((job, ""))
                console.print(f"[dim]  [{i}/{len(jobs)}] [{p}] {job['company']} - {job['title']} → 待投递[/dim]")
            else:
                # 断点续跑：如果 job 自带招呼语则直接使用
                if "_greeting" in job:
                    greeting = job.pop("_greeting")
                    console.print(f"[dim]  [{i}/{len(jobs)}] [{p}] {job['company']} - {job['title']} → 断点续跑[/dim]")
                else:
                    console.print(f"[dim]  [{i}/{len(jobs)}] [{p}] {job['company']} - {job['title']} 生成中...[/dim]", end="\r")
                    greeting = generate_greeting(client, model, resume, job)
                    if not greeting:
                        console.print(f"[yellow]  [{i}/{len(jobs)}] [{p}] {job['company']} - 生成失败，跳过[/yellow]")
                        continue
                pending.append((job, greeting))
        console.print(f"\n[green]✓ 待处理队列: {len(pending)} 个（招呼语+投递）[/green]\n")
    else:
        # 2 模式：逐条审 → 平台感知
        for i, job in enumerate(jobs, 1):
            p = platform_label(job)
            console.print()
            console.print(f"[bold cyan]── #{i}/{len(jobs)} [{p}] ──[/bold cyan]")
            console.print(f"  [bold]{job['title']}[/bold]")
            console.print(f"  公司: {job['company']} | {job.get('company_size', '')} | {job.get('company_industry', '')}")
            console.print(f"  薪资: {job['salary']} | 经验: {job['experience']} | 学历: {job['education']}")
            console.print(f"  HR: {job.get('hr_name', '?')} | {job.get('hr_title', '')} | {job.get('hr_active', '')}")
            console.print(f"  JD: {job.get('jd', '')}")
            console.print(f"  [dim]{job['url']}[/dim]")

            if job.get("platform") == "zhaopin":
                # 智联：直接投递，不用招呼语
                action = Prompt.ask("  [bold]y=投递  n=跳过  f=审完投[/bold]", choices=["y", "n", "f"], default="y")
                if action == "f":
                    break
                elif action == "n":
                    continue
                pending.append((job, ""))
                console.print(f"[dim]  ✓ 已入队 (队列: {len(pending)} 条)[/dim]")
            else:
                # BOSS：生成招呼语（断点续跑时直接使用已生成的）
                action = Prompt.ask("  [bold]y=生成招呼语  n=跳过  f=审完发[/bold]", choices=["y", "n", "f"], default="y")
                if action == "f":
                    break
                elif action == "n":
                    continue

                if "_greeting" in job:
                    greeting = job.pop("_greeting")
                    console.print(f"[green]  招呼语(续跑):[/green] {greeting}")
                    act = Prompt.ask("  [bold]入队/编辑/跳过？[/bold]", choices=["y", "e", "n"], default="y")
                else:
                    console.print("[dim]  生成招呼语...[/dim]")
                    greeting = generate_greeting(client, model, resume, job)
                    if not greeting:
                        console.print("[yellow]  生成失败，跳过[/yellow]")
                        continue
                    console.print(f"[green]  招呼语:[/green] {greeting}")
                    act = Prompt.ask("  [bold]入队/编辑/跳过？[/bold]", choices=["y", "e", "n"], default="y")
                if act == "n":
                    continue
                elif act == "e":
                    greeting = Prompt.ask("  修改招呼语", default=greeting)

                pending.append((job, greeting))
                console.print(f"[dim]  ✓ 已入队 (队列: {len(pending)} 条)[/dim]")

    # ═══════════════════════════════════════════
    #  Phase 2: 批量发送（双平台并行，各用各的浏览器）
    # ═══════════════════════════════════════════
    if not pending:
        console.print("\n[yellow]待发队列为空，退出[/yellow]")
        sys.exit(0)

    console.print(f"\n[bold cyan]═══ 审岗完成，待处理 {len(pending)} 条 ═══[/bold cyan]")
    boss_count = sum(1 for j, _ in pending if j.get("platform") != "zhaopin")
    zhilian_count = sum(1 for j, _ in pending if j.get("platform") == "zhaopin")
    for i, (job, greeting) in enumerate(pending, 1):
        p = platform_label(job)
        if job.get("platform") == "zhaopin":
            console.print(f"  {i}. [{p}] [bold]{job['company']}[/bold] - {job['title']} (直接投递)")
        else:
            console.print(f"  {i}. [{p}] [bold]{job['company']}[/bold] - {job['title']}")
    if boss_count > 0 and zhilian_count > 0:
        console.print(f"\n[dim]BOSS {boss_count} 条 + 智联 {zhilian_count} 条，两平台并行发送，互不等待[/dim]")

    if not args.auto:
        Prompt.ask("\n[bold]按回车开始批量处理...[/bold]", default="")

    # 拆成两个平台的队列，各自用独立浏览器并行发送
    boss_pairs = [(j, g) for j, g in pending if j.get("platform") != "zhaopin"]
    zhilian_pairs = [(j, g) for j, g in pending if j.get("platform") == "zhaopin"]

    def _send_platform(browser, pairs, label, is_zhilian):
        """在一个浏览器上串行发送一批岗位（运行在 worker 线程中）"""
        sent = 0
        total = len(pairs)
        for i, (job, greeting) in enumerate(pairs, 1):
            p = label
            console.print(f"\n[bold cyan]{i}/{total} [{p}]: {job['company']} - {job['title']}[/bold cyan]")
            if is_zhilian:
                ok = zhilian_apply(browser, job)
                ok, err = ok, "" if ok else "投递失败"
            else:
                ok, err = send_greeting(browser, job, greeting, fast=True)
            if ok:
                console.print(f"[green]  ✓ 已完成！[/green]")
                sent += 1
            else:
                console.print(f"[red]  ✗ 失败: {err}[/red]")
            # 断点续跑：保存剩余未发的（含已生成招呼语）
            _save_resume(pairs, i)
            if i < total:
                wait = random.uniform(2, 5) if is_zhilian else random.uniform(15, 25)
                console.print(f"[dim]  等待 {wait:.0f}s...[/dim]")
                time.sleep(wait)
        return sent

    sent = 0
    with ThreadPoolExecutor(max_workers=2) as send_executor:
        send_futures = {}
        if boss_pairs:
            send_futures[send_executor.submit(_send_platform, _boss_browser, boss_pairs, "BOSS", False)] = "BOSS"
        if zhilian_pairs:
            send_futures[send_executor.submit(_send_platform, _zhilian_browser, zhilian_pairs, "智联", True)] = "智联"

        # 轮询等待，保证 Ctrl+C 可中断
        pending_sends = set(send_futures.keys())
        try:
            while pending_sends:
                if _shutdown_requested.is_set():
                    console.print("\n[yellow]已取消发送[/yellow]")
                    send_executor.shutdown(wait=False, cancel_futures=True)
                    os._exit(0)
                done, pending_sends = fut_wait(pending_sends, timeout=0.5, return_when=FIRST_COMPLETED)
                for fut in done:
                    try:
                        n = fut.result(timeout=0)
                        sent += n
                        console.print(f"[dim]({send_futures[fut]} 完成: {n} 条)[/dim]")
                    except Exception as exc:
                        console.print(f"[red]{send_futures[fut]} 发送出错: {exc}[/red]")
        except KeyboardInterrupt:
            _shutdown_requested.set()
            _shutdown_browsers()
            console.print("\n[yellow]已中断，正在清理...[/yellow]")
            send_executor.shutdown(wait=False, cancel_futures=True)
            os._exit(0)

    total = len(pending)
    _clear_resume()  # 全部完成，清除断点文件
    console.print(f"\n[bold green]═══ 批量完成！成功 {sent}/{total} ═══[/bold green]")


def _shutdown_browsers():
    """通知所有浏览器退出"""
    if _boss_browser:
        _boss_browser.shutdown()
    if _zhilian_browser:
        _zhilian_browser.shutdown()


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        _shutdown_browsers()
        if _shutdown_requested.is_set():
            console.print("\n[red]强制退出[/red]")
        else:
            console.print("\n[yellow]已取消[/yellow]")
        os._exit(0)
