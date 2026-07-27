"""
三个评估 Agent（Skill Match / Compensation / Culture）+ Judge 汇总。
"""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from src.llm_client import client, DEFAULT_MODEL

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# 三个评估 System Prompt
# ---------------------------------------------------------------------------

SKILL_MATCH_PROMPT = """你是一个严格的技术面试官。
你的唯一职责是对照 JD 的技能要求与候选人的技能列表，给出技术匹配度打分。
**不要**考虑薪资、公司文化、工作地点——那些不归你管。

评分标准：
- 9-10: 技能几乎全部匹配，年限要求也满足
- 7-8: 核心技能匹配，1-2 个次要技能欠缺
- 5-6: 部分匹配，有较大差距但可培养
- 3-4: 核心技能多处不匹配
- 1-2: 几乎完全不匹配

以严格 JSON 格式返回（不要 markdown 代码块）:
{
  "score": number(1-10),
  "matched_skills": ["匹配的技能"],
  "missing_skills": ["JD 要求但候选人缺乏的技能"],
  "reason": "一句话评价"
}"""

COMPENSATION_PROMPT = """你是一个猎头顾问。
你的唯一职责是评估岗位的薪资竞争力和职业发展空间。
**不要**考虑技术匹配度——那是技术面试官的事。

评估维度：
- 薪资是否在候选人的期望范围内？
- 薪资在行业中是否具有竞争力（比如 15 薪 > 13 薪）？
- 公司规模/职级是否提供向上空间？

评分标准：
- 9-10: 薪资超预期 + 明显成长空间
- 7-8: 薪资在期望范围 + 有成长空间
- 5-6: 薪资勉强及格，成长空间有限
- 3-4: 薪资明显低于期望
- 1-2: 薪资极低或没有明确薪资信息

以严格 JSON 格式返回（不要 markdown 代码块）:
{
  "score": number(1-10),
  "salary_fit": "薪资匹配度一句话",
  "growth": "成长空间一句话",
  "reason": "综合一句话评价"
}"""

CULTURE_PROMPT = """你是一个职场导师。
你的唯一职责是评估公司的"软性条件"：公司类型、文化、稳定性、地点。
**不要**管技术或薪资——那是别人的工作。

重点关注（按优先级）：
1. 公司性质：甲方还是外包？ 如果是外包/派遣，分数不应该超过 5
2. 公司规模与稳定性：大厂/上市公司/独角兽/创业公司？
3. 工作地点是否符合候选人的偏好（比如不想去外地）？
4. 行业前景：金融科技/互联网/传统行业？

评分标准：
- 9-10: 甲方大厂，地点完美，行业前景好
- 7-8: 甲方中小厂或领域不错，地点合适
- 5-6: 外包但外派到大厂，或信息不足存疑
- 3-4: 明显外包岗位，公司信息模糊
- 1-2: 纯外包公司/劳务派遣/地点极不合适

以严格 JSON 格式返回（不要 markdown 代码块）:
{
  "score": number(1-10),
  "red_flags": ["需要警惕的点"],
  "green_flags": ["加分项"],
  "reason": "一句话综合评价"
}"""

# ---------------------------------------------------------------------------
# 评估函数
# ---------------------------------------------------------------------------

EVALUATOR_PROMPTS = {
    "skill": SKILL_MATCH_PROMPT,
    "compensation": COMPENSATION_PROMPT,
    "culture": CULTURE_PROMPT,
}

EVALUATOR_WEIGHTS = {
    "skill": 0.40,
    "compensation": 0.30,
    "culture": 0.30,
}

EXPERT_NAMES = {
    "skill": "技术面试官",
    "compensation": "猎头顾问",
    "culture": "职场导师",
}

# ---------------------------------------------------------------------------
# 多智能体辩论：互评 → 修正
# ---------------------------------------------------------------------------

CRITIQUE_PROMPT = """你是一个{role}，负责评估岗位的{domain}维度。你刚刚给出了你的初步评分。

现在你看到了另外两位专家的评分和理由。请对他们的评估提出质疑或补充。

**重要**：
- 不要改变你自己的评分——你现在只是在对他们的结论发表意见
- 指出他们可能漏掉的信息、过于乐观/悲观的判断、或逻辑矛盾
- 如果他们评估得合理，也可以认可（但要具体说明哪里合理）
- 只输出对另外两位专家的意见，不要评价自己
- 一两句话即可，不要长篇大论

以严格 JSON 格式返回（不要 markdown 代码块）:
{{
  "to_{peer1}": "对{peer1_role}的意见",
  "to_{peer2}": "对{peer2_role}的意见"
}}"""

REVISE_PROMPT = """你是一个{role}，负责评估岗位的{domain}维度。你给了初步评分 {own_score}/10。

现在另外两位专家对你的评估提出了质疑：

{critiques_text}

请认真考虑这些意见。你的职责仍然是{domain}，但好的意见应该被吸收。

以严格 JSON 格式返回（不要 markdown 代码块）:
{{
  "original_score": number,
  "final_score": number(1-10),
  "final_reason": "最终评分理由（提到是否吸收了别人的意见）"
}}"""


async def critique_peers(
    evaluator_type: str,
    job: dict,
    profile: dict,
    preferences: dict,
    own: EvalResult,
    peers: dict[str, EvalResult],
) -> dict[str, str]:
    """互评轮：一个专家看到另外两位的评分后，对每人提出质疑或认可。Returns {peer_type: critique_text}"""
    role = EXPERT_NAMES[evaluator_type]
    domain = {
        "skill": "技术匹配度",
        "compensation": "薪资竞争力与成长空间",
        "culture": "公司类型/文化/稳定性",
    }[evaluator_type]

    # 构建 prompt 中的 peer 占位符
    peer_types = list(peers.keys())
    prompt_mapping = {
        "peer1": peer_types[0],
        "peer1_role": EXPERT_NAMES[peer_types[0]],
        "peer2": peer_types[1],
        "peer2_role": EXPERT_NAMES[peer_types[1]],
    }
    system_prompt = CRITIQUE_PROMPT.format(role=role, domain=domain, **prompt_mapping)

    peer_text = ""
    for ptype, pr in peers.items():
        peer_text += f"\n### {EXPERT_NAMES[ptype]}（{pr.score}/10）\n{pr.detail.get('reason', '无')}"

    user_content = f"""## 你的原始评分
{own.score}/10 — {own.detail.get('reason', '')}

## 其他专家的评分
{peer_text}

## 岗位信息
- 标题: {job.get('title', '无')}
- 公司: {job.get('company', '无')}
- 薪资: {job.get('salary', '未提供')}
- JD 摘要: {job.get('jd_full', '')[:500]}
"""

    try:
        response = await client.chat.completions.create(
            model=DEFAULT_MODEL,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_content},
            ],
            temperature=0.3,
            max_tokens=500,
        )
        raw = _parse_llm_json(response.choices[0].message.content or "{}")
        detail = json.loads(raw)
    except (json.JSONDecodeError, Exception):
        detail = {f"to_{pt}": "无法生成意见" for pt in peer_types}

    return {pt: detail.get(f"to_{pt}", "") for pt in peer_types}


async def revise_final(
    evaluator_type: str,
    job: dict,
    profile: dict,
    preferences: dict,
    own: EvalResult,
    critiques: list[str],
) -> EvalResult:
    """修正轮：一个专家看到别人对自己的质疑后，给出最终评分"""
    role = EXPERT_NAMES[evaluator_type]
    domain = {
        "skill": "技术匹配度",
        "compensation": "薪资竞争力与成长空间",
        "culture": "公司类型/文化/稳定性",
    }[evaluator_type]

    critiques_text = "\n".join(f"- {c}" for c in critiques if c)
    if not critiques_text.strip():
        critiques_text = "（无实质性质疑）"

    system_prompt = REVISE_PROMPT.format(
        role=role, domain=domain,
        own_score=own.score,
        critiques_text=critiques_text,
    )

    user_content = f"""## 你的原始评分
{own.score}/10 — {own.detail.get('reason', '')}

## 岗位信息（回顾）
- 标题: {job.get('title', '无')}
- 公司: {job.get('company', '无')}
- 薪资: {job.get('salary', '未提供')}
- JD 摘要: {job.get('jd_full', '')[:500]}
"""

    try:
        response = await client.chat.completions.create(
            model=DEFAULT_MODEL,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_content},
            ],
            temperature=0.2,
            max_tokens=500,
        )
        raw = _parse_llm_json(response.choices[0].message.content or "{}")
        detail = json.loads(raw)
    except (json.JSONDecodeError, Exception):
        detail = {"final_score": own.score, "final_reason": "解析失败，维持原判"}

    final_score = int(detail.get("final_score", own.score))
    final_score = max(1, min(10, final_score))

    merged_detail = dict(own.detail)
    merged_detail["round1_score"] = own.score
    merged_detail["round3_score"] = final_score
    merged_detail["round3_reason"] = detail.get("final_reason", "")

    return EvalResult(
        evaluator_type=evaluator_type,
        score=final_score,
        detail=merged_detail,
    )


def _parse_llm_json(raw: str) -> str:
    """清理 LLM 返回的 JSON 字符串（去掉 markdown fence 等）"""
    raw = raw.strip()
    if raw.startswith("```"):
        raw = raw.split("\n", 1)[1]
        if raw.endswith("```"):
            raw = raw[:-3]
        raw = raw.strip()
        if raw.lower().startswith("json"):
            raw = raw[4:].strip()
    return raw


@dataclass
class EvalResult:
    evaluator_type: str
    score: int
    detail: dict


async def run_evaluator(
    evaluator_type: str,
    job: dict,
    profile: dict,
    preferences: dict,
) -> EvalResult:
    """单个评估器：调用 DeepSeek 给一个维度打分"""
    system_prompt = EVALUATOR_PROMPTS[evaluator_type]

    user_content = f"""
## 候选人画像
- 技能: {json.dumps(profile.get('skills', []), ensure_ascii=False)}
- 工作经验: {profile.get('years_of_experience', '未知')} 年
- 期望岗位: {profile.get('expected_position', '未指定')}
- 期望薪资: {profile.get('expected_salary', '未指定')}
- 当前地点: {profile.get('current_location', '未指定')}

## 候选人偏好
{json.dumps(preferences, ensure_ascii=False)}

## 岗位信息
- 标题: {job.get('title', '无')}
- 公司: {job.get('company', '无')}
- 薪资: {job.get('salary', '未提供')}
- 地点: {job.get('location', '未知')}

## 完整 JD
{job.get('jd_full', '无 JD 文本')}
"""

    response = await client.chat.completions.create(
        model=DEFAULT_MODEL,
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_content},
        ],
        temperature=0.2,
        max_tokens=1000,
    )

    raw = (response.choices[0].message.content or "{}").strip()
    if raw.startswith("```"):
        raw = raw.split("\n", 1)[1]
        if raw.endswith("```"):
            raw = raw[:-3]
        raw = raw.strip()
        if raw.lower().startswith("json"):
            raw = raw[4:].strip()

    try:
        detail = json.loads(raw)
    except json.JSONDecodeError:
        detail = {"raw": raw, "score": 5}

    score = int(detail.get("score", 5))
    score = max(1, min(10, score))

    return EvalResult(evaluator_type=evaluator_type, score=score, detail=detail)


def judge_results(
    job: dict,
    results: list[EvalResult],
    collected_so_far: int,
    target_count: int = 3,
) -> dict | None:
    """
    汇总 3 个评估结果，给出综合判断。

    只有任一维度 ≤ 3 分才直接拒绝。分数不管高低都返回，
    由上层 orchestrate 决定是否纳入推荐。
    """
    score_map = {r.evaluator_type: r.score for r in results}
    detail_map = {r.evaluator_type: r.detail for r in results}

    # 否决逻辑：任一维度 ≤ 3 分，直接拒绝
    for etype, score in score_map.items():
        if score <= 3:
            return None

    # 加权计算
    weighted = sum(
        score_map[t] * EVALUATOR_WEIGHTS[t]
        for t in ["skill", "compensation", "culture"]
    )

    # 生成综合 summary（含链接）
    link = job.get("link", "")
    reasons_str = (
        f"技能匹配: {detail_map['skill'].get('reason', 'N/A')} (分: {score_map['skill']})\n"
        f"薪资发展: {detail_map['compensation'].get('reason', 'N/A')} (分: {score_map['compensation']})\n"
        f"公司文化: {detail_map['culture'].get('reason', 'N/A')} (分: {score_map['culture']})"
    )

    return {
        "title": job.get("title", "未知岗位"),
        "company": job.get("company", "未知公司"),
        "salary": job.get("salary", "未知"),
        "location": job.get("location", "未知"),
        "link": link,
        "final_score": round(weighted, 1),
        "skill_score": score_map["skill"],
        "compensation_score": score_map["compensation"],
        "culture_score": score_map["culture"],
        "skill_reason": detail_map["skill"].get("reason", ""),
        "compensation_reason": detail_map["compensation"].get("reason", ""),
        "culture_reason": detail_map["culture"].get("reason", ""),
        "summary": reasons_str,
    }


# ---------------------------------------------------------------------------
# 编排函数：读取 raw JSON → 并行评估 → 写 scored JSON → 返回 top_k
# ---------------------------------------------------------------------------

async def orchestrate_evaluation(
    json_path: str,
    profile: dict,
    preferences: dict,
    top_k: int = 5,
) -> tuple[list[dict], str]:
    """从 raw JSON 文件读取岗位 → 三视角并行评估 → 排序 → 写 scored JSON → 返回 (top_k, scored_path)"""

    # 1. 读取 raw JSON
    raw_path = Path(json_path)
    if not raw_path.exists():
        logger.error(f"[orchestrate] JSON 文件不存在: {json_path}")
        return [], ""

    try:
        jobs = json.loads(raw_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as e:
        logger.error(f"[orchestrate] 读取 JSON 失败: {e}")
        return [], ""

    if not jobs:
        logger.warning("[orchestrate] 无岗位数据，跳过评估")
        return [], ""

    logger.info(f"[orchestrate] 开始评估 {len(jobs)} 个岗位（逐岗串行，凑够 {top_k} 即停）...")

    # 2. 逐岗评估：每个岗位内 3 维并行，岗位之间串行
    THRESHOLD = 8.0  # ≥ 8 分才算真正匹配
    all_scored: list[dict] = []

    for i, job in enumerate(jobs):
        # 已有 3 个 ≥ 8 分的，可以停了
        high_count = sum(1 for s in all_scored if s["final_score"] >= THRESHOLD)
        if high_count >= top_k:
            logger.info(f"[orchestrate] 已有 {top_k} 个 ≥{THRESHOLD} 分岗位，停止评估（共评估 {i}/{len(jobs)} 岗）")
            break

        try:
            results = await asyncio.gather(
                run_evaluator("skill", job, profile, preferences),
                run_evaluator("compensation", job, profile, preferences),
                run_evaluator("culture", job, profile, preferences),
            )
            judged = judge_results(job, list(results), collected_so_far=len(all_scored), target_count=top_k)
            if judged:
                all_scored.append(judged)
                status = "✓" if judged["final_score"] >= THRESHOLD else "△"
                logger.info(
                    f"[orchestrate] [{i+1}/{len(jobs)}] {status} {job.get('title','?')[:30]} "
                    f"→ {judged['final_score']}/10"
                )
            else:
                logger.info(
                    f"[orchestrate] [{i+1}/{len(jobs)}] ✗ {job.get('title','?')[:30]} 某维度≤3 直接拒绝"
                )
        except Exception as e:
            logger.warning(f"[orchestrate] 评估失败: {job.get('title', '?')} - {e}")

        await asyncio.sleep(0.3)

    # 3. 选优：优先 ≥ THRESHOLD 的，不够则降级取最高分
    high = [j for j in all_scored if j["final_score"] >= THRESHOLD]
    high.sort(key=lambda j: j["final_score"], reverse=True)

    if len(high) >= top_k:
        scored = high[:top_k]
        logger.info(f"[orchestrate] ≥{THRESHOLD} 分 {len(high)} 个 → 取 Top {top_k}")
    else:
        # 不够，先把高分全收了，其余按分数降级补足
        rest = [j for j in all_scored if j["final_score"] < THRESHOLD]
        rest.sort(key=lambda j: j["final_score"], reverse=True)
        scored = high + rest[:top_k - len(high)]
        logger.info(
            f"[orchestrate] ≥{THRESHOLD} 分仅 {len(high)} 个，降级补足 {len(scored) - len(high)} 个 "
            f"(最低 {scored[-1]['final_score']}/10)"
        )

    # 4. 写 scored JSON
    scored_dir = Path(__file__).parent.parent / "data" / "scored"
    scored_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    scored_path = scored_dir / f"jobs_scored_{ts}.json"
    scored_path.write_text(
        json.dumps(scored, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    logger.info(f"[orchestrate] 已保存 {len(scored)} 个评分岗位 → {scored_path}")

    # 5. 取 top_k
    top = scored[:top_k]
    logger.info(f"[orchestrate] 取 Top {len(top)} 个岗位")

    return top, str(scored_path)
