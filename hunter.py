#!/usr/bin/env python3
"""
hunter — 多平台轻量海投工具（BOSS直聘 + 智联招聘）
爬岗位 → 硬过滤 → 逐条批阅 → 生成招呼语/投简历 → 发送

用法:
  python hunter.py -k "Python 后端" -e 3 -c "深圳" -p 2
  python hunter.py --json jobs_20260722.json    # 从已有 JSON 直接进入批阅发送
"""

import argparse
import json
import random
import sys
import time
from datetime import datetime
from pathlib import Path

import yaml
from rich.console import Console
from rich.prompt import Prompt

from ai import load_api_client, score_jobs, generate_greeting
from boss.scraper import scrape as scrape_boss
from boss.sender import send_greeting, touch_job
from zhaopin.scraper import scrape as scrape_zhaopin
from zhaopin.sender import apply_job
from browser import configure, check_chrome_connection, find_boss_tab

console = Console()
HERE = Path(__file__).resolve().parent


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


def touch_action(job: dict) -> bool:
    """轻触：BOSS=立即沟通, 智联=立即投递"""
    if job.get("platform") == "zhaopin":
        return apply_job(job)
    return touch_job(job)


def send_action(job: dict, greeting: str = "") -> tuple:
    """发送：BOSS=发招呼语, 智联=投简历"""
    if job.get("platform") == "zhaopin":
        ok = apply_job(job)
        return (ok, "" if ok else "投递失败")
    return send_greeting(job, greeting, fast=True)


# ══════════════════════════════════════════════════════
#  主流程
# ══════════════════════════════════════════════════════

def main():
    parser = argparse.ArgumentParser(description="hunter — BOSS直聘轻量海投工具")
    parser.add_argument("-k", "--keywords", default="", help="搜索关键词，空格分隔")
    parser.add_argument("-e", "--exp", type=int, default=None, help="经验上限(年)，0=只要应届，不传=不过滤经验")
    parser.add_argument("-c", "--cities", default="", help="目标城市，空格分隔")
    parser.add_argument("-p", "--pages", type=int, default=2, help="每关键词翻几页 (默认2)")
    parser.add_argument("-P", "--platform", choices=["all", "boss", "zhaopin"], default="all",
                        help="目标平台：all=双平台, boss=仅BOSS, zhaopin=仅智联 (默认all)")
    parser.add_argument("--salary-min", type=int, default=None, help="最低薪资K")
    parser.add_argument("--salary-max", type=int, default=None, help="最高薪资K")
    parser.add_argument("-d", "--deal-breakers", default="", help="屏蔽词，空格分隔")
    parser.add_argument("--json", dest="json_file", help="从已有 JSON 文件直接进入批阅发送")
    parser.add_argument("--score-min", type=int, default=None, help="评分阈值，低于此分自动筛掉")
    parser.add_argument("-r", "--resume", default=None, help="简历路径，覆盖 config.yaml")
    parser.add_argument("-a", "--auto", action="store_true", help="全自动：爬→评→生成→发，零确认")
    args = parser.parse_args()

    cfg = load_config()

    # ── CLI 参数覆盖 config ──────────────────────────
    if args.salary_min is not None:
        cfg["salary_min"] = args.salary_min
    if args.salary_max is not None:
        cfg["salary_max"] = args.salary_max
    if args.deal_breakers:
        cfg["deal_breakers"] = args.deal_breakers.split()

    # ── 检查浏览器连接 ────────────────────────────
    configure({})
    if not check_chrome_connection():
        console.print("[red]Chrome 未连接！请先启动:[/red]")
        console.print('[dim]chrome.exe --remote-debugging-port=9222 --remote-allow-origins=* --user-data-dir="%TEMP%\\chrome_debug" https://www.zhipin.com[/dim]')
        sys.exit(1)
    if args.platform in ("all", "boss"):
        boss_tab = find_boss_tab()
        if not boss_tab:
            console.print("[red]未发现 BOSS直聘 页面，请先打开并登录 zhipin.com[/red]")
            sys.exit(1)
    console.print("[green]✓ 浏览器就绪[/green]")

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

        console.print(f"[bold]搜索:[/bold] {keywords} | 城市: {cities} | 页数: {args.pages} | 经验≤{max_exp}年 | 平台: {args.platform}")
        console.print()

        jobs = []

        if args.platform in ("all", "boss"):
            console.print("[bold cyan]═══ BOSS直聘 ═══[/bold cyan]")
            boss_jobs = scrape_boss(cfg, keywords, cities, args.pages, args.exp)
            jobs.extend(boss_jobs)
            console.print(f"[green]BOSS直聘: {len(boss_jobs)} 个岗位[/green]")

        if args.platform in ("all", "zhaopin"):
            if args.platform == "all":
                console.print()
            console.print("[bold cyan]═══ 智联招聘 ═══[/bold cyan]")
            zhilian_jobs = scrape_zhaopin(cfg, keywords, cities, args.pages, args.exp)
            jobs.extend(zhilian_jobs)
            console.print(f"[green]智联招聘: {len(zhilian_jobs)} 个岗位[/green]")

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

    # ── a/s/q 批阅 ────────────────────────────────
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
        choice = "a"
        console.print("[bold green]  🤖 全自动模式：爬→评→投递/发招呼语，零确认[/bold green]")
    else:
        console.print("  a = 全投（批量投递/发招呼语） | s = 逐个审 | t = 轻触（只点沟通/投递） | q = 退出")
        choice = Prompt.ask("[bold]操作[/bold]", choices=["a", "s", "t", "q"], default="s")

    if choice == "q":
        console.print("[yellow]已退出[/yellow]")
        sys.exit(0)

    if choice == "t":
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

    if choice == "a":
        # a 模式：BOSS=生成招呼语, 智联=直接投递 → 批量
        console.print(f"\n[bold]全投模式：{len(jobs)} 个岗位[/bold]\n")
        for i, job in enumerate(jobs, 1):
            p = platform_label(job)
            if job.get("platform") == "zhaopin":
                # 智联：不用招呼语，直接入队
                pending.append((job, ""))
                console.print(f"[dim]  [{i}/{len(jobs)}] [{p}] {job['company']} - {job['title']} → 待投递[/dim]")
            else:
                console.print(f"[dim]  [{i}/{len(jobs)}] [{p}] {job['company']} - {job['title']} 生成中...[/dim]", end="\r")
                greeting = generate_greeting(client, model, resume, job)
                if not greeting:
                    console.print(f"[yellow]  [{i}/{len(jobs)}] [{p}] {job['company']} - 生成失败，跳过[/yellow]")
                    continue
                pending.append((job, greeting))
        console.print(f"\n[green]✓ 待处理队列: {len(pending)} 个（招呼语+投递）[/green]\n")
    else:
        # s 模式：逐条审 → 平台感知
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
                # BOSS：生成招呼语
                action = Prompt.ask("  [bold]y=生成招呼语  n=跳过  f=审完发[/bold]", choices=["y", "n", "f"], default="y")
                if action == "f":
                    break
                elif action == "n":
                    continue

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
    #  Phase 2: 批量发送
    # ═══════════════════════════════════════════
    if not pending:
        console.print("\n[yellow]待发队列为空，退出[/yellow]")
        sys.exit(0)

    console.print(f"\n[bold cyan]═══ 审岗完成，待处理 {len(pending)} 条 ═══[/bold cyan]")
    for i, (job, greeting) in enumerate(pending, 1):
        p = platform_label(job)
        if job.get("platform") == "zhaopin":
            console.print(f"  {i}. [{p}] [bold]{job['company']}[/bold] - {job['title']} (直接投递)")
        else:
            console.print(f"  {i}. [{p}] [bold]{job['company']}[/bold] - {job['title']}")

    if not args.auto:
        Prompt.ask("\n[bold]按回车开始批量处理...[/bold]", default="")

    sent = 0
    for i, (job, greeting) in enumerate(pending, 1):
        p = platform_label(job)
        console.print(f"\n[bold cyan]{i}/{len(pending)} [{p}]: {job['company']} - {job['title']}[/bold cyan]")
        ok, err = send_action(job, greeting)
        if ok:
            console.print(f"[green]  ✓ 已完成！[/green]")
            sent += 1
        else:
            console.print(f"[red]  ✗ 失败: {err}[/red]")
        if i < len(pending):
            wait = random.uniform(15, 25)
            console.print(f"[dim]  等待 {wait:.0f}s...[/dim]")
            time.sleep(wait)

    console.print(f"\n[bold green]═══ 批量完成！成功 {sent}/{len(pending)} ═══[/bold green]")


if __name__ == "__main__":
    main()
