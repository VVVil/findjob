"""
报告生成模块：把 Top N 岗位转成 Markdown 报告。
纯 Python 拼字符串，不调 LLM。
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path


def generate_markdown(top_jobs: list[dict], profile: dict) -> str:
    """生成 Markdown 格式的岗位推荐报告"""
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    name = profile.get("name", "候选人")
    position = profile.get("expected_position", "未指定")

    lines: list[str] = []
    lines.append(f"# 🎯 岗位推荐报告")
    lines.append(f"")
    lines.append(f"**候选人**: {name}　|　**目标岗位**: {position}　|　**生成时间**: {now}")
    lines.append(f"")
    lines.append(f"---")
    lines.append(f"")

    if not top_jobs:
        lines.append("> ⚠️ 未找到符合条件的岗位，请调整搜索条件后重试。")
        return "\n".join(lines)

    for i, job in enumerate(top_jobs, 1):
        lines.append(f"## {i}. {job['title']}")
        lines.append(f"")
        lines.append(f"| 项目 | 详情 |")
        lines.append(f"|------|------|")
        lines.append(f"| 🏢 公司 | {job.get('company', '未知')} |")
        lines.append(f"| 💰 薪资 | {job.get('salary', '面议')} |")
        lines.append(f"| 📍 地点 | {job.get('location', '未知')} |")
        lines.append(f"| ⭐ 综合评分 | **{job.get('final_score', 'N/A')} / 10** |")
        lines.append(f"| 🔗 链接 | [点击投递]({job.get('link', '#')}) |")
        lines.append(f"")

        # 三维度打分
        lines.append(f"### 📊 评估详情")
        lines.append(f"")
        lines.append(f"- 🔧 **技能匹配** ({job.get('skill_score', '?')}/10): {job.get('skill_reason', '')}")
        lines.append(f"- 💵 **薪资发展** ({job.get('compensation_score', '?')}/10): {job.get('compensation_reason', '')}")
        lines.append(f"- 🏠 **公司文化** ({job.get('culture_score', '?')}/10): {job.get('culture_reason', '')}")
        lines.append(f"")
        lines.append(f"---")
        lines.append(f"")

    lines.append(f"## 📝 搜索说明")
    lines.append(f"")
    lines.append(f"- 搜索平台：智联招聘")
    lines.append(f"- 评估模型：DeepSeek（三视角并行评估：技能匹配 + 薪资发展 + 公司文化）")
    lines.append(f"- 架构：LangGraph 多智能体流水线")
    lines.append(f"")

    return "\n".join(lines)


def save_and_get_path(report_md: str, output_dir: str | None = None) -> str:
    """保存报告到文件，返回路径"""
    if output_dir is None:
        output_dir = str(Path(__file__).parent.parent / "data" / "reports")
    Path(output_dir).mkdir(parents=True, exist_ok=True)

    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    filename = f"job_report_{ts}.md"
    filepath = Path(output_dir) / filename
    filepath.write_text(report_md, encoding="utf-8")
    return str(filepath)
