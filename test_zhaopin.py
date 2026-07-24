#!/usr/bin/env python3
"""临时测试智联爬虫——只爬不筛不发"""
import json
import yaml
from pathlib import Path
from rich.console import Console

from browser import configure, check_chrome_connection
from zhaopin.scraper import scrape

console = Console()
HERE = Path(__file__).resolve().parent
cfg = yaml.safe_load(HERE.joinpath("config.yaml").read_text(encoding="utf-8"))

configure({})
if not check_chrome_connection():
    console.print("[red]Chrome 未连接！[/red]")
    import sys; sys.exit(1)

console.print("[green]✓ 浏览器就绪[/green]")

# 爬智联：Agent 深圳 1 页
jobs = scrape(cfg, ["Agent"], ["深圳"], pages=1, max_exp=None)

if jobs:
    ts = __import__("datetime").datetime.now().strftime("%Y%m%d_%H%M%S")
    out = HERE / "output" / f"zhaopin_test_{ts}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(jobs, ensure_ascii=False, indent=2), encoding="utf-8")
    console.print(f"\n[green]✓ 已保存 {len(jobs)} 个岗位 → {out}[/green]")
else:
    console.print("\n[yellow]0 个结果[/yellow]")
