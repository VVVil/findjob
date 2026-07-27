"""
AI 模块 — DeepSeek API 客户端 + 评分 + 招呼语生成
"""

import json
import os

from dotenv import load_dotenv
from openai import OpenAI
from rich.console import Console

console = Console()

# 配置路径由调用方传入
_ENV_DIR = None


def load_api_client(cfg: dict) -> OpenAI:
    """从 .env 或 config 初始化 DeepSeek client"""
    from pathlib import Path

    global _ENV_DIR
    if _ENV_DIR is None:
        # hunter.py 会先调用 set_env_dir()
        _ENV_DIR = Path(__file__).resolve().parent

    load_dotenv(_ENV_DIR / ".env")
    ai_cfg = cfg.get("ai", {})
    api_key = os.getenv("DEEPSEEK_API_KEY") or os.getenv("OPENAI_API_KEY") or ai_cfg.get("api_key", "")
    base_url = os.getenv("OPENAI_BASE_URL") or ai_cfg.get("base_url", "https://api.deepseek.com/v1")
    if not api_key:
        console.print("[red]缺少 API key！在 .env 里设 DEEPSEEK_API_KEY=xxx[/red]")
        import sys
        sys.exit(1)
    return OpenAI(api_key=api_key, base_url=base_url)


# ── 评分 ──────────────────────────────────────────────

SCORING_PROMPT = """你是一位专业的求职顾问。请根据以下简历和岗位JD，评估候选人与该岗位的匹配度。

## 候选人简历
{resume}

## 岗位信息
- 职位：{title}
- 公司：{company}
- 薪资：{salary}
- 要求：{experience}
- JD：{jd}

## 评估要求
请从以下维度评估匹配度，给出0-100的综合评分：
1. 职能技能匹配度（最重要）：候选人的核心职能技能是否覆盖岗位要求
2. 工作年限匹配度：工作年限是否符合要求
3. 薪资合理性：期望薪资与岗位薪资是否匹配
4. 行业背景相关性（加分项）：有相关行业经验加分，跨行业不扣分——职能能力可以跨行业迁移

请严格按以下JSON格式输出，不要输出其他内容：
{{"score": 75, "reason": "匹配理由简述（50字内）", "missing": "缺失的关键技能或经验（30字内）"}}"""


def score_jobs(client: OpenAI, model: str, resume: str, jobs: list[dict], threshold: int) -> list[dict]:
    """用 AI 给岗位打分排序，返回排序后的列表（附带 score/reason/missing 字段）"""
    if not resume or not jobs:
        return jobs

    console.print(f"\n[bold]AI 评分中 ({len(jobs)} 个岗位)...[/bold]")
    scored = []
    failed = 0

    for i, job in enumerate(jobs, 1):
        console.print(f"[dim]  [{i}/{len(jobs)}] {job['company']} - {job['title']}...[/dim]", end="\r")
        try:
            prompt = SCORING_PROMPT.format(
                resume=resume[:3000],
                title=job.get("title", ""),
                company=job.get("company", ""),
                salary=job.get("salary", ""),
                experience=job.get("experience", ""),
                jd=job.get("jd", "")[:2000],
            )
            resp = client.chat.completions.create(
                model=model,
                messages=[{"role": "user", "content": prompt}],
                temperature=0.3,
                max_tokens=200,
            )
            text = resp.choices[0].message.content.strip()

            # 解析 JSON
            start = text.find("{")
            end = text.rfind("}") + 1
            if start >= 0 and end > start:
                result = json.loads(text[start:end])
                job["score"] = int(result.get("score", 0))
                job["score_reason"] = result.get("reason", "")
                job["score_missing"] = result.get("missing", "")
            else:
                job["score"] = 0
                failed += 1
        except Exception:
            job["score"] = 0
            failed += 1

        scored.append(job)

    scored.sort(key=lambda j: j.get("score", 0), reverse=True)
    console.print(f"[green]✓ 评分完成[/green] (成功 {len(scored) - failed}, 阈值 {threshold})")

    # 打印排序列表
    for i, job in enumerate(scored, 1):
        s = job.get("score", 0)
        color = "green" if s >= 80 else ("yellow" if s >= 60 else "red")
        reason = job.get("score_reason", "")[:60]
        console.print(f"  [{color}]{s:3d}[/{color}] {i}. [bold]{job['company']}[/bold] - {job['title']}")
        if reason:
            console.print(f"       [dim]{reason}[/dim]")

    below = sum(1 for j in scored if j.get("score", 0) < threshold)
    if below > 0:
        console.print(f"\n[yellow]低于阈值({threshold})的有 {below} 个，将自动筛掉[/yellow]")
        scored = [j for j in scored if j.get("score", 0) >= threshold]
        console.print(f"剩余 {len(scored)} 个待审")

    return scored


# ── 招呼语生成 ────────────────────────────────────────

def generate_greeting(client: OpenAI, model: str, resume: str, job: dict) -> str | None:
    """用 DeepSeek 生成招呼语"""
    prompt = f"""你是一位求职者，要在BOSS直聘上给HR发第一条消息。根据简历和JD生成一条自然的招呼语。

## 要求
1. 50-120字，像真人发IM
2. 突出1-2个最匹配的真实经历
3. 不捏造、不把JD写成自己的经历
4. 不用"您好我是xxx"开头
5. 只输出招呼语文本

## 简历
{resume[:2000]}

## 岗位
- 职位：{job.get('title', '')}
- 公司：{job.get('company', '')}
- JD：{job.get('jd', '')[:1500]}

请直接输出招呼语："""

    try:
        resp = client.chat.completions.create(
            model=model,
            messages=[{"role": "user", "content": prompt}],
            temperature=0.7,
            max_tokens=600,
        )
        return resp.choices[0].message.content.strip()
    except Exception as e:
        console.print(f"[red]招呼语生成失败: {e}[/red]")
        return None
