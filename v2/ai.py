"""
AI 模块 — DeepSeek API 客户端 + 评分 + 招呼语生成
"""

import json
import os
import time

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

    # 先找 v2/.env，再找上一级 findjob_new/.env
    env_path = _ENV_DIR / ".env"
    if not env_path.exists():
        env_path = _ENV_DIR.parent / ".env"
    load_dotenv(env_path)
    ai_cfg = cfg.get("ai", {})
    api_key = os.getenv("DEEPSEEK_API_KEY") or os.getenv("OPENAI_API_KEY") or ai_cfg.get("api_key", "")
    base_url = os.getenv("OPENAI_BASE_URL") or ai_cfg.get("base_url", "https://api.deepseek.com/v1")
    if not api_key:
        console.print("[red]缺少 API key！在 .env 里设 DEEPSEEK_API_KEY=xxx[/red]")
        import sys
        sys.exit(1)
    return OpenAI(api_key=api_key, base_url=base_url, timeout=60.0)


# ── 评分 ──────────────────────────────────────────────

SCORING_PROMPT = """你是一位专业的求职顾问。请根据以下简历和岗位JD，评估候选人与该岗位的匹配度。

## 候选人简历
{resume}

## 岗位信息
- 职位：{title}
- 公司：{company}
- 薪资：{salary}
- 要求：{experience}
- 公司规模：{company_size}
- 行业：{company_industry}
- JD：{jd}

## 评估要求
请从以下维度评估匹配度，给出0-100的综合评分：

1. 技能匹配度（最重要）：候选人的核心技能是否覆盖岗位要求
2. 工作年限匹配度：工作年限是否符合要求
3. 薪资合理性：期望薪资与岗位薪资是否匹配
4. 行业背景相关性（加分项）：有相关行业经验加分，跨行业不扣分——职能能力可以跨行业迁移
5. 公司信誉与薪资质量（新增）：
   - 金融/贷款/助贷/担保/催收 → -10到-15分（高风险行业，岗位本质与AI无关）
   - 薪资下限过低（<8K）且写着"提成""奖金为主" → -5到-10分（靠纯提成堆数字，实际到手很少）
   - 公司规模 0-20人或规模未知 → -5分（小公司风险）
   - 公司规模 500人以上或上市/融资 → +3分

## 特殊角色评估规则

如果岗位属于以下类型，应从"技术背景赋能商务"的角度评估，但**必须有区分度**，不要一概给高分：
- 真正的 AI 技术产品销售（SaaS/PaaS、AI 中台、RPA 等企业级产品）
- 售前工程师、解决方案顾问、技术顾问
- 开发者关系（DevRel）、技术社区运营
- 技术交付经理、客户成功经理（需要跟技术团队对接）

**真正的 AI 产品销售 vs 蹭 AI 热度的伪销售 — 必须区分：**

真 AI 销售（75-85分）：
- 卖的是 AI SaaS、大模型 API、RPA、智能客服等企业级技术产品
- 需要跟客户技术团队对接、能看代码/接口、有 POC 流程
- 公司是正规科技公司（有官网、有产品、有研发团队）
- 候选人的技术背景在这里是核心武器

伪 AI 销售（45-65分）：
- 金融贷款/助贷/信贷，只是用 AI 获客渠道包装了一下 → job 本质仍然是金融销售
- 门店零售、数码产品销售 → 技术背景几乎没用
- 职位写着"AI销售"但 JD 没有任何具体 AI 产品描述，公司也没有技术基因
- "接受小白""无经验""无考核"这类标题 → 通常是电销/地推团队批量招人
- 美业、房产、保险、保健品 → 跟 AI 技术无关，按传统销售正常评估

全凭一股"降维打击"给所有带 AI 字样的销售打 85 分是错误的。请逐个岗位审慎评估。

## 评分参考
- 90-100：技能完美匹配，行业对口，年限薪资都合理（很少出现）
- 75-89：核心技能匹配，有少量缺失但可快速补上
- 60-74：部分匹配，需要一定学习成本但方向正确
- 40-59：匹配度偏低，核心技能差距较大
- 0-39：严重不匹配，不建议投递

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
        label = f"[{i}/{len(jobs)}] {job['company']} - {job['title']}"
        t0 = time.time()
        try:
            prompt = SCORING_PROMPT.format(
                resume=resume[:3000],
                title=job.get("title", ""),
                company=job.get("company", ""),
                salary=job.get("salary", ""),
                experience=job.get("experience", ""),
                company_size=job.get("company_size", ""),
                company_industry=job.get("company_industry", ""),
                jd=job.get("jd", "")[:2000],
            )
            resp = client.chat.completions.create(
                model=model,
                messages=[{"role": "user", "content": prompt}],
                temperature=0.3,
                max_tokens=200,
                timeout=30.0,
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

            # 完成后打印结果 + 耗时
            elapsed = time.time() - t0
            s = job.get("score", 0)
            color = "green" if s >= 80 else ("yellow" if s >= 60 else "red")
            reason = job.get("score_reason", "")[:40]
            console.print(f"  [{color}]{s:3d}[/{color}] {label}  [{elapsed:.1f}s]  [dim]{reason}[/dim]")

        except Exception as e:
            elapsed = time.time() - t0
            job["score"] = 0
            failed += 1
            err_msg = str(e)[:60]
            console.print(f"  [red]ERR[/red] {label}  [{elapsed:.1f}s] — {err_msg}")

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
    prompt = f"""你是一个求职者，在BOSS直聘上给HR发第一条消息。根据简历和JD，写一句自然的招呼语。

## 语气要求（重要）

**不要这样写：**
- "看到这个岗位我特别兴奋/非常感兴趣/很激动"
- "贵公司是行业翘楚/成就斐然/令人向往"
- "我对XX充满热情/热爱"
- "希望能有机会加入贵公司"
- 任何感叹号、表情符号
- 长篇大论的自我介绍（HR没时间看）

**应该这样写：**
- "您好"开头，一句寒暄，然后直接说做过什么、跟岗位有什么关系
- 有事说事，自信但不傲慢
- 例："您好，我做过2年Agent开发，FastAPI和LangChain都用过，跟这个岗挺对口的，方便聊聊吗"
- 例："您好，我有RAG和多Agent的落地经验，独立搭建过整套系统，可以发简历您看看"
- 例："您好，我Python后端3年，Docker和PostgreSQL熟，看过JD感觉方向匹配"

## 格式要求
1. 30-80字，以"您好"开头，一两句话
2. 从简历里挑1-2个跟JD最相关的真实经验
3. 不捏造、不把JD写成自己的经历
4. 不夸公司、不表热情、不用感叹号
5. 只输出招呼语文本，不要引号包裹

## 简历
{resume[:2000]}

## 岗位
- 职位：{job.get('title', '')}
- 公司：{job.get('company', '')}
- JD：{job.get('jd', '')[:1500]}

直接输出招呼语："""

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
