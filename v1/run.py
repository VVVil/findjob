r"""
FindJob Agent — CLI 入口
运行: .venv\Scripts\python run.py [--threshold 8.0] [--top-k 3] [--max-pages 5]
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

from langgraph.types import Command
from rich.console import Console
from rich.markdown import Markdown
from rich.panel import Panel
from rich.table import Table

from src.graph import build_graph, create_initial_state
from src.resume_parser import extract_text
from src.job_crawler import shutdown as shutdown_crawler

console = Console()


def show_profile(profile: dict):
    table = Table(title="📋 简历画像", show_header=False, border_style="dim")
    table.add_column("字段", style="bold cyan")
    table.add_column("内容", style="white")
    if profile.get("name"):
        table.add_row("姓名", profile["name"])
    if profile.get("expected_position"):
        table.add_row("期望岗位", profile["expected_position"])
    if profile.get("expected_salary"):
        table.add_row("期望薪资", profile["expected_salary"])
    if profile.get("years_of_experience"):
        table.add_row("工作年限", f"{profile['years_of_experience']} 年")
    if profile.get("current_location"):
        table.add_row("现居城市", profile["current_location"])
    edu = profile.get("education") or {}
    if edu.get("level"):
        table.add_row("学历", f"{edu.get('level', '')} · {edu.get('school', '')} · {edu.get('major', '')}")
    if profile.get("skills"):
        table.add_row("技能", " · ".join(profile["skills"]))
    console.print(table)


async def stream_section(graph, input_data, config):
    """跑一段图，收集所有 node update events"""
    events: list[tuple[str, dict]] = []
    async for event in graph.astream(input_data, config, stream_mode="updates"):
        for node_name, update in event.items():
            if node_name.startswith("__") or not isinstance(update, dict):
                continue
            events.append((node_name, update))
    return events


def handle_events(events: list[tuple[str, dict]]) -> list[dict]:
    """处理事件列表，更新终端显示。返回最新 collected 列表"""
    collected: list[dict] = []
    for node_name, update in events:
        if node_name == "parse_resume":
            show_profile(update.get("profile", {}))
        elif node_name == "generate_question":
            pass
        elif node_name == "start_search":
            console.print(f"\n[bold cyan]🔍 开始搜索[/bold cyan]")
            console.print(f"  关键词: [dim]{update.get('search_keywords', '')}[/dim]")
        elif node_name == "search_page":
            n = len(update.get("jobs_raw", []))
            console.print(f"  [dim]本页提取 {n} 个岗位，正在三视角评估...[/dim]")
        elif node_name == "evaluate_all_jobs":
            c = update.get("collected", [])
            collected = c
            for i, job in enumerate(c, 1):
                console.print(
                    f"  [green]✅ #{i}[/green] "
                    f"{job.get('title', '?')[:45]} "
                    f"[cyan]({job.get('final_score', '?')}/10)[/cyan]"
                )
            console.print(f"  [bold]累计匹配: {len(c)} 个[/bold]")
        elif node_name == "generate_report":
            report = update.get("report_markdown", "")
            console.print("\n" + "=" * 60)
            try:
                console.print(Markdown(report))
            except Exception:
                console.print(report)
    return collected


async def main():
    # ── 参数解析 ──
    parser = argparse.ArgumentParser(
        description="FindJob Agent — LangGraph 多智能体求职助手",
    )
    parser.add_argument(
        "--threshold", type=float, default=8.0,
        help="达标分数线，≥这个分才算合格（默认 8.0）",
    )
    parser.add_argument(
        "--top-k", type=int, default=3,
        help="报告取 Top K 个岗位（默认 3）",
    )
    parser.add_argument(
        "--max-pages", type=int, default=5,
        help="最大翻页数（默认 5）",
    )
    args = parser.parse_args()

    console.print(Panel.fit(
        "[bold cyan]FindJob Agent[/bold cyan] — LangGraph 多智能体求职助手\n"
        f"[dim]阈值 {args.threshold} / Top {args.top_k} / 最多 {args.max_pages} 页[/dim]",
        border_style="cyan",
    ))

    # ── 简历路径 ──
    console.print("\n[bold]📎 简历路径[/bold]")
    while True:
        resume_path = console.input("[dim]  拖拽或输入文件路径: [/dim]").strip().strip('"')
        if not resume_path:
            continue
        p = Path(resume_path)
        if not p.exists():
            console.print(f"  [red]文件不存在，请重试[/red]")
            continue
        break

    console.print("[dim]  提取文本...[/dim]", end="\r")
    resume_text = extract_text(str(p))
    console.print(f"[green]  ✓ 已提取 {len(resume_text):,} 字符[/green]")

    # ── 初始化 ──
    graph = build_graph()
    initial_state = create_initial_state(
        resume_text, str(p),
        threshold=args.threshold,
        top_k=args.top_k,
        max_pages=args.max_pages,
    )
    config = {"configurable": {"thread_id": "cli-session"}}

    # 第一段: START → 第一个 interrupt
    console.print("\n[dim]分析简历...[/dim]")
    events = await stream_section(graph, initial_state, config)
    handle_events(events)

    # ── 对话循环 ──
    while True:
        snapshot = graph.get_state(config)
        if not snapshot.next:
            break

        if "wait_user_input" in snapshot.next:
            question = snapshot.values.get("questions_to_ask", "")
            chat_round = snapshot.values.get("chat_round", 0)

            console.print(f"\n[bold yellow]💬 第 {chat_round + 1}/4 轮[/bold yellow]")
            console.print(Panel(question, border_style="yellow"))

            user_answer = console.input("[bold green]你: [/bold green]").strip()

            if user_answer.lower() in ("跳过", "开始搜", "skip", "go"):
                console.print("[dim]跳过剩余对话，开始搜索...[/dim]")
                # 强制跳到 start_search
                await graph.aupdate_state(
                    config,
                    {
                        "chat_round": 4,
                        "chat_history": snapshot.values.get("chat_history", []),
                        "preferences": snapshot.values.get("preferences", {}),
                        "missing_from_resume": [],
                    },
                )
                # 用空 Command 继续
                events = await stream_section(graph, Command(resume="跳过"), config)
                handle_events(events)
                continue

            # 正常 resume
            events = await stream_section(graph, Command(resume=user_answer), config)
            handle_events(events)
            continue

        # 图已离开对话阶段（search/evaluate 循环中），但还没结束
        # 这时不需要用户输入，图自己会跑
        events = await stream_section(graph, None, config)
        collected = handle_events(events)
        if collected:
            snapshot = graph.get_state(config)
            if snapshot.values.get("phase") == "done":
                break

    # ── 收尾 ──
    final = graph.get_state(config)
    final_values = final.values if final.values else {}
    collected = final_values.get("collected", [])
    report_md = final_values.get("report_markdown", "")

    threshold = args.threshold
    top_k = args.top_k

    if collected:
        # 选 top_k 展示
        high = [j for j in collected if j.get("final_score", 0) >= threshold]
        high.sort(key=lambda j: j.get("final_score", 0), reverse=True)
        if len(high) >= top_k:
            top = high[:top_k]
        else:
            rest = [j for j in collected if j.get("final_score", 0) < threshold]
            rest.sort(key=lambda j: j.get("final_score", 0), reverse=True)
            top = high + rest[:top_k - len(high)]

        table = Table(title=f"🏆 Top {top_k} 推荐岗位（阈值 {threshold}）", border_style="green")
        table.add_column("#", style="bold")
        table.add_column("岗位", style="cyan")
        table.add_column("公司")
        table.add_column("薪资")
        table.add_column("评分", style="bold green")

        for i, j in enumerate(top, 1):
            table.add_row(
                str(i),
                j.get("title", "?")[:30],
                j.get("company", "?")[:20],
                j.get("salary", "?"),
                f"{j.get('final_score', '?')}/10",
            )
        console.print(table)

        if report_md:
            console.print("\n" + "=" * 60)
            console.print(Markdown(report_md))
    else:
        console.print("\n[yellow]⚠️ 未找到匹配岗位，请调整条件后重试[/yellow]")

    await shutdown_crawler()


if __name__ == "__main__":
    asyncio.run(main())
