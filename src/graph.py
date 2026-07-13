"""
LangGraph 多智能体编排：7 节点状态图。
简历解析 → 多轮对话 → 浏览器搜索 → 三视角并行评估 → 报告生成
"""

from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
from typing import Any, Literal

from langgraph.graph import StateGraph, END
from langgraph.checkpoint.memory import MemorySaver
from langgraph.types import interrupt

from src.state import AgentState
from src.resume_parser import parse_resume_text
from src.preference_chat import generate_question as gen_q, is_ready_signal
from src.job_crawler import crawl_page
from src.evaluators import run_evaluator, judge_results, critique_peers, revise_final
from src.report_generator import generate_markdown, save_and_get_path
from rich.console import Console
from rich.table import Table

console = Console()
logger = logging.getLogger(__name__)

# 专家中英文映射（终端展示用）
EXPERT_LABEL = {
    "skill":        ("🔧 技能匹配", "cyan"),
    "compensation": ("💵 薪资发展", "yellow"),
    "culture":      ("🏠 公司文化", "magenta"),
}


def _color_score(score: int) -> str:
    if score >= 8:
        return f"[green]{score}/10[/green]"
    elif score >= 5:
        return f"[yellow]{score}/10[/yellow]"
    return f"[red]{score}/10[/red]"


def _format_extra(etype: str, detail: dict) -> str:
    """格式化专家额外信息（匹配技能/薪资/red_flags 等）"""
    extra = ""
    if etype == "skill":
        matched = detail.get("matched_skills", [])
        missing = detail.get("missing_skills", [])
        if matched:
            extra += f"  [green]匹配: {', '.join(matched[:5])}[/green]\n"
        if missing:
            extra += f"  [red]欠缺: {', '.join(missing[:5])}[/red]"
    elif etype == "compensation":
        salary_fit = detail.get("salary_fit", "")
        growth = detail.get("growth", "")
        if salary_fit:
            extra += f"  {salary_fit}\n"
        if growth:
            extra += f"  {growth}"
    elif etype == "culture":
        red_flags = detail.get("red_flags", [])
        green_flags = detail.get("green_flags", [])
        if green_flags:
            extra += f"  [green]加分: {', '.join(green_flags[:4])}[/green]\n"
        if red_flags:
            extra += f"  [red]警惕: {', '.join(red_flags[:4])}[/red]"
    return extra


# ---------------------------------------------------------------------------
# Node 1: 简历解析
# ---------------------------------------------------------------------------

async def parse_resume_node(state: AgentState) -> dict[str, Any]:
    """LLM 提取简历结构化画像"""
    logger.info("[parse_resume] 开始解析简历...")
    result = await parse_resume_text(state["resume_text"])
    return {
        "profile": result["profile"],
        "missing_from_resume": result["missing_from_resume"],
        "phase": "chat",
    }


# ---------------------------------------------------------------------------
# Node 2: 生成问题（自然对话，非结构化）
# ---------------------------------------------------------------------------

async def generate_question_node(state: AgentState) -> dict[str, Any]:
    """LLM 自然对话——根据上下文决定问什么"""
    chat_round = state.get("chat_round", 0)
    logger.info(f"[generate_question] 第 {chat_round + 1} 轮")

    text = await gen_q(
        profile=state.get("profile", {}),
        chat_history=state.get("chat_history", []),
        chat_round=chat_round,
    )

    return {
        "questions_to_ask": text,
        "phase": "chat",
    }


# ---------------------------------------------------------------------------
# Node 3: 等待用户输入 (interrupt)
# ---------------------------------------------------------------------------

async def wait_user_input_node(state: AgentState) -> dict[str, Any]:
    """LangGraph interrupt — 暂停等用户 CLI 输入"""
    question = state.get("questions_to_ask", "")

    user_response = interrupt({"type": "question", "text": question})

    new_history = list(state.get("chat_history", []))
    new_history.append({"role": "assistant", "content": question})
    new_history.append({"role": "user", "content": user_response})

    new_round = state.get("chat_round", 0) + 1

    return {
        "chat_history": new_history,
        "chat_round": new_round,
        "phase": "chat",
    }


# ---------------------------------------------------------------------------
# Node 4: 开始搜索
# ---------------------------------------------------------------------------

async def start_search_node(state: AgentState) -> dict[str, Any]:
    """用 LLM 从整段对话中理解用户意图，生成自然搜索词"""
    profile = state.get("profile", {})
    chat_history = state.get("chat_history", [])

    # 把完整的对话上下文喂给 LLM
    history_text = ""
    for h in chat_history:
        role = "候选人" if h["role"] == "user" else "顾问"
        history_text += f"{role}: {h['content']}\n"

    skills = ", ".join(profile.get("skills", [])[:8])
    location = profile.get("current_location", "")
    last_job = profile.get("last_position", "")

    prompt = f"""你是一个招聘搜索专家。根据候选人的简历和对话，你认为在智联招聘上应该搜什么关键词？

简历摘要：
- 技能：{skills}
- 现居：{location}
- 上份工作：{last_job}

对话记录：
{history_text}

请直接输出一个搜索关键词（5-10个字），就像你会敲进招聘网站的搜索框那样。只输出关键词本身，不要引号、不要解释。"""

    try:
        from src.llm_client import client
        resp = await client.chat.completions.create(
            model="deepseek-chat",
            messages=[{"role": "user", "content": prompt}],
            temperature=0.5,
            max_tokens=40,
        )
        keywords = resp.choices[0].message.content.strip().strip('"').strip("'")
    except Exception:
        keywords = "AI 算法工程师"

    logger.info(f"[start_search] LLM 生成搜索词: {keywords}")

    return {
        "search_keywords": keywords or "Python 开发",
        "current_page": 0,
        "collected": [],
        "already_seen_ids": [],
        "phase": "searching",
    }


# ---------------------------------------------------------------------------
# Node 5: 搜索一页岗位
# ---------------------------------------------------------------------------

async def search_page_node(state: AgentState) -> dict[str, Any]:
    """Playwright 爬虫：爬取一页推荐岗位 + 详情，写 JSON 到 src/job_details/"""
    page_num = state.get("current_page", 0)
    seen = set(state.get("already_seen_ids", []))
    logger.info(f"[search_page] 第 {page_num + 1} 页")

    try:
        jobs, json_path = await crawl_page(page_num)
    except Exception as e:
        logger.error(f"[search_page] 爬取失败: {e}")
        jobs, json_path = [], ""

    new_seen = list(seen) + [
        j.get("title", "") + "|" + j.get("company", "") for j in jobs
    ]

    console.print(f"\n[bold cyan]🔍 第 {page_num + 1} 页[/bold cyan] → {len(jobs)} 个岗位 → [dim]{json_path}[/dim]")

    return {
        "jobs_raw": jobs,
        "jobs_json_path": json_path,
        "current_page": page_num + 1,
        "already_seen_ids": new_seen,
        "phase": "evaluating",
    }


# ---------------------------------------------------------------------------
# Node 6: 并行评估所有岗位
# ---------------------------------------------------------------------------

async def evaluate_all_jobs_node(state: AgentState) -> dict[str, Any]:
    """
    读取当前页 JSON → 逐岗三维度评估 + rich 终端输出 → 累计到 collected。
    岗内 3 专家并行，岗间串行。凑够 top_k 个 ≥threshold 分提前停。
    """
    json_path = state.get("jobs_json_path", "")
    profile = state.get("profile", {})
    preferences = state.get("preferences", {})
    collected = list(state.get("collected", []))
    threshold = state.get("threshold", 8.0)
    top_k = state.get("top_k", 3)

    if not json_path:
        logger.warning("[evaluate_all] 无 jobs_json_path，跳过评估")
        return {"jobs_raw": [], "phase": "searching"}

    # 已有足够高分 → 不用再评估
    high_count = sum(1 for j in collected if j["final_score"] >= threshold)
    if high_count >= top_k:
        logger.info(f"[evaluate_all] 已有 {top_k} 个 ≥{threshold} 分，跳过评估")
        return {"jobs_raw": [], "phase": "done"}

    # 读 JSON
    try:
        jobs = json.loads(Path(json_path).read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as e:
        logger.error(f"[evaluate_all] 读取 JSON 失败: {e}")
        return {"jobs_raw": [], "phase": "searching"}

    if not jobs:
        logger.warning("[evaluate_all] 无岗位数据")
        return {"jobs_raw": [], "phase": "searching"}

    logger.info(f"[evaluate_all] 开始评估 {len(jobs)} 个岗位（已累计 {len(collected)} 个）")

    # 逐岗评估
    new_this_page = 0
    for i, job in enumerate(jobs):
        high_count = sum(1 for j in collected if j["final_score"] >= threshold)
        if high_count >= top_k:
            logger.info(f"[evaluate_all] 已有 {top_k} 个 ≥{threshold} 分，停止评估")
            break

        title = job.get("title", "?")[:40]
        company = job.get("company", "?")[:20]
        ut = job.get("update_time", "?")
        salary = job.get("salary", "?")

        console.print()
        console.print("─" * 60)
        console.print(
            f"[bold white][{i + 1}/{len(jobs)}][/bold white] "
            f"[cyan]{title}[/cyan] [dim]@ {company}[/dim]  "
            f"[yellow]| {salary}[/yellow]  |  {ut}"
        )

        # Round 1: 三专家初评（并行）
        try:
            skill_r, comp_r, culture_r = await asyncio.gather(
                run_evaluator("skill", job, profile, preferences),
                run_evaluator("compensation", job, profile, preferences),
                run_evaluator("culture", job, profile, preferences),
            )

            # 逐个打印初评结论
            console.print("  [dim]── 初评 ──[/dim]")
            for etype, r in {"skill": skill_r, "compensation": comp_r, "culture": culture_r}.items():
                label, color = EXPERT_LABEL[etype]
                detail_data = r.detail
                reason = detail_data.get("reason", "")
                extra = _format_extra(etype, detail_data)
                score_str = _color_score(r.score)
                console.print(f"  [{color}]{label}[/{color}] {score_str} — {reason}")
                if extra:
                    console.print(extra)

            # Round 2: 互评 — 每个专家对另外两人提出质疑（3 parallel）
            console.print("  [dim]── 互评 ──[/dim]")
            skill_c, comp_c, culture_c = await asyncio.gather(
                critique_peers("skill", job, profile, preferences, skill_r,
                               {"compensation": comp_r, "culture": culture_r}),
                critique_peers("compensation", job, profile, preferences, comp_r,
                               {"skill": skill_r, "culture": culture_r}),
                critique_peers("culture", job, profile, preferences, culture_r,
                               {"skill": skill_r, "compensation": comp_r}),
            )

            # 展示互评结果
            critique_map = {
                "skill": (skill_c, "🔧"),
                "compensation": (comp_c, "💵"),
                "culture": (culture_c, "🏠"),
            }
            peer_labels = {
                "skill": ("💵", "🏠"),
                "compensation": ("🔧", "🏠"),
                "culture": ("🔧", "💵"),
            }
            for etype, (critiques, from_icon) in critique_map.items():
                for ptype, label_icon in zip(critiques.keys(), peer_labels[etype]):
                    text = critiques.get(ptype, "")
                    if text:
                        console.print(
                            f"  [dim]{from_icon} → {label_icon}:[/dim] {text[:100]}"
                        )

            # Round 3: 修正 — 每个专家看到别人对自己的质疑后给最终分（3 parallel）
            console.print("  [dim]── 修正 ──[/dim]")
            skill_f, comp_f, culture_f = await asyncio.gather(
                revise_final("skill", job, profile, preferences, skill_r,
                             [comp_c.get("skill", ""), culture_c.get("skill", "")]),
                revise_final("compensation", job, profile, preferences, comp_r,
                             [skill_c.get("compensation", ""), culture_c.get("compensation", "")]),
                revise_final("culture", job, profile, preferences, culture_r,
                             [skill_c.get("culture", ""), comp_c.get("culture", "")]),
            )

            # 展示修正结果
            revised_map = {
                "skill": (skill_r, skill_f),
                "compensation": (comp_r, comp_f),
                "culture": (culture_r, culture_f),
            }
            for etype, (old, new) in revised_map.items():
                label, color = EXPERT_LABEL[etype]
                if new.score != old.score:
                    direction = "↑" if new.score > old.score else "↓"
                    console.print(
                        f"  [{color}]{label}[/{color}] "
                        f"{_color_score(old.score)} {direction} {_color_score(new.score)} "
                        f"— {new.detail.get('round3_reason', '')}"
                    )
                else:
                    console.print(
                        f"  [{color}]{label}[/{color}] "
                        f"{_color_score(old.score)} → 坚持原判"
                    )

            # Judge 综合（用修正后的最终分）
            judged = judge_results(
                job, [skill_f, comp_f, culture_f],
                collected_so_far=len(collected),
                target_count=top_k,
            )

            if judged:
                collected.append(judged)
                new_this_page += 1
                fs = judged["final_score"]
                if fs >= threshold:
                    bar = "█" * max(1, int(fs))
                    console.print(
                        f"  [bold cyan]📊 综合: {fs}/10[/bold cyan] "
                        f"[green]{bar}[/green] [bold green]✓ 达标[/bold green]"
                    )
                else:
                    bar = "░" * max(1, int(fs))
                    console.print(
                        f"  [bold cyan]📊 综合: {fs}/10[/bold cyan] "
                        f"[dim]{bar}[/dim] [yellow]△ 未达 {threshold} 分[/yellow]"
                    )
            else:
                console.print(f"  [red]✗ 某维度 ≤3，直接拒绝[/red]")

        except Exception as e:
            logger.warning(f"[evaluate_all] 评估失败: {job.get('title', '?')} - {e}")
            console.print(f"  [red]评估异常: {e}[/red]")

        await asyncio.sleep(0.3)

    logger.info(
        f"[evaluate_all] 本页通过 {new_this_page} 个，累计 {len(collected)} 个"
    )
    console.print(f"\n[dim]本页通过 {new_this_page} 个，累计 {len(collected)} 个[/dim]")

    return {
        "collected": collected,
        "jobs_raw": [],
        "phase": "searching",
    }


# ---------------------------------------------------------------------------
# Node 7: 生成报告
# ---------------------------------------------------------------------------

async def generate_report_node(state: AgentState) -> dict[str, Any]:
    """生成 Markdown 报告"""
    collected = state.get("collected", [])
    profile = state.get("profile", {})
    threshold = state.get("threshold", 8.0)
    top_k = state.get("top_k", 3)

    logger.info(f"[generate_report] 共 {len(collected)} 个岗位入选")

    # 选 top_k：优先 ≥threshold，不够就降级取最高分
    high = [j for j in collected if j["final_score"] >= threshold]
    high.sort(key=lambda j: j["final_score"], reverse=True)

    if len(high) >= top_k:
        top = high[:top_k]
    else:
        rest = [j for j in collected if j["final_score"] < threshold]
        rest.sort(key=lambda j: j["final_score"], reverse=True)
        top = high + rest[:top_k - len(high)]

    report = generate_markdown(top, profile)
    filepath = save_and_get_path(report)

    logger.info(f"[generate_report] 报告已保存: {filepath}")

    return {
        "report_markdown": report,
        "phase": "done",
    }


# ---------------------------------------------------------------------------
# 条件路由
# ---------------------------------------------------------------------------

def route_after_generate(state: AgentState) -> Literal["wait_user_input", "start_search"]:
    """生成问题后：如果 LLM 发出"准备开始搜"的信号 → 直接跳到搜索，不再等用户输入"""
    question = state.get("questions_to_ask", "")
    if is_ready_signal(question):
        logger.info("[route] 检测到结束信号，自动跳到搜索阶段")
        return "start_search"
    return "wait_user_input"


def route_after_chat(state: AgentState) -> Literal["generate_question", "start_search"]:
    """对话轮结束后：继续聊 还是 开始搜索"""
    chat_round = state.get("chat_round", 0)
    # 最多 4 轮对话
    if chat_round < 4:
        return "generate_question"
    return "start_search"


def route_after_search(state: AgentState) -> Literal["evaluate_all_jobs", "generate_report"]:
    """搜索完一页后：有岗位就评估，没岗位就生成报告"""
    jobs = state.get("jobs_raw", [])
    if jobs:
        return "evaluate_all_jobs"
    return "generate_report"


def route_after_eval(state: AgentState) -> Literal["search_page", "generate_report"]:
    """评估完后：≥top_k 个达标 → 报告；不够 + 未到上限 → 继续翻页；到上限 → 报告（降级）"""
    collected = state.get("collected", [])
    threshold = state.get("threshold", 8.0)
    top_k = state.get("top_k", 3)
    high = sum(1 for j in collected if j["final_score"] >= threshold)
    page = state.get("current_page", 0)
    max_pages = state.get("max_pages", 5)

    if high >= top_k:
        logger.info(f"[route] {high} 个 ≥{threshold} 分 → 生成报告")
        return "generate_report"

    if page < max_pages:
        logger.info(f"[route] 仅 {high} 个达标，翻第 {page + 2} 页")
        return "search_page"

    logger.info(f"[route] 已达 {max_pages} 页上限，仅 {high} 个达标 → 降级取 top_k")
    return "generate_report"


# ---------------------------------------------------------------------------
# 构建图
# ---------------------------------------------------------------------------

def build_graph():
    """构建并编译 LangGraph 状态图"""
    workflow = StateGraph(AgentState)

    # 注册节点
    workflow.add_node("parse_resume", parse_resume_node)
    workflow.add_node("generate_question", generate_question_node)
    workflow.add_node("wait_user_input", wait_user_input_node)
    workflow.add_node("start_search", start_search_node)
    workflow.add_node("search_page", search_page_node)
    workflow.add_node("evaluate_all_jobs", evaluate_all_jobs_node)
    workflow.add_node("generate_report", generate_report_node)

    # 入口
    workflow.set_entry_point("parse_resume")

    # parse → generate_question
    workflow.add_edge("parse_resume", "generate_question")

    # generate_question → 检测结束信号 → wait_user_input 或直接 start_search
    workflow.add_conditional_edges(
        "generate_question",
        route_after_generate,
        {
            "wait_user_input": "wait_user_input",
            "start_search": "start_search",
        },
    )

    # wait_user_input → 条件路由
    workflow.add_conditional_edges(
        "wait_user_input",
        route_after_chat,
        {
            "generate_question": "generate_question",
            "start_search": "start_search",
        },
    )

    # start_search → search_page
    workflow.add_edge("start_search", "search_page")

    # search_page → 条件路由
    workflow.add_conditional_edges(
        "search_page",
        route_after_search,
        {
            "evaluate_all_jobs": "evaluate_all_jobs",
            "generate_report": "generate_report",
        },
    )

    # evaluate_all_jobs → 条件路由
    workflow.add_conditional_edges(
        "evaluate_all_jobs",
        route_after_eval,
        {
            "search_page": "search_page",
            "generate_report": "generate_report",
        },
    )

    # generate_report → END
    workflow.add_edge("generate_report", END)

    return workflow.compile(checkpointer=MemorySaver())


def create_initial_state(
    resume_text: str,
    resume_path: str = "",
    threshold: float = 8.0,
    top_k: int = 3,
    max_pages: int = 5,
) -> dict:
    """创建初始状态"""
    return {
        "resume_text": resume_text,
        "resume_path": resume_path,
        "profile": {},
        "chat_round": 0,
        "chat_history": [],
        "preferences": {},
        "missing_from_resume": [],
        "questions_to_ask": "",
        "search_keywords": "",
        "jobs_raw": [],
        "jobs_json_path": "",
        "current_page": 0,
        "max_pages": max_pages,
        "threshold": threshold,
        "top_k": top_k,
        "evaluations": [],
        "collected": [],
        "already_seen_ids": [],
        "report_markdown": "",
        "phase": "upload",
    }
