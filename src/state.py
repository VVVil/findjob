"""
LangGraph AgentState — 贯穿整个多智能体流水线的共享状态。
"""

from __future__ import annotations

import operator
from typing import Annotated, Any, TypedDict


class AgentState(TypedDict):
    # === 简历解析 ===
    resume_text: str
    resume_path: str
    profile: dict[str, Any]  # {name, skills[], expected_salary, expected_position,
    #  years_of_experience, education, current_location}

    # === 对话 ===
    chat_round: int  # 0-3
    chat_history: list[dict[str, str]]  # [{role, content}, ...]
    preferences: dict[str, Any]  # 从对话中提取的用户偏好
    missing_from_resume: list[str]  # 简历缺的字段名
    questions_to_ask: str  # LLM 生成的问题（前端渲染用）

    # === 搜索 ===
    search_keywords: str  # 搜索关键词
    jobs_raw: list[dict[str, Any]]  # 当前页岗位（每次替换）
    jobs_json_path: str  # data/jobs/jobs_{ts}.json 路径
    current_page: int
    max_pages: int  # 翻页上限，默认 5

    # === 评估 ===
    threshold: float  # 达标分数线，默认 8.0
    top_k: int  # 报告取 Top K 个岗位，默认 3
    evaluations: Annotated[list[dict[str, Any]], operator.add]  # 每个岗位×3维度
    collected: list[dict[str, Any]]  # 综合分 >= 阈值的岗位
    already_seen_ids: list[str]  # 已评估岗位 ID，避免重复

    # === 控制 ===
    report_markdown: str
    phase: str  # "upload" | "chat" | "searching" | "evaluating" | "done"
