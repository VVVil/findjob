"""
简历解析模块：支持 PDF / DOCX，用 DeepSeek 做结构化提取。
"""

from __future__ import annotations

import json
from pathlib import Path

from pypdf import PdfReader

from src.llm_client import client, DEFAULT_MODEL

RESUME_SYSTEM_PROMPT = """你是一个专业的简历解析器。从简历文本中提取以下字段，以严格 JSON 格式返回。

不要猜测，只提取简历中明确写出的内容。如果某个字段在简历中找不到，将其值设为 null。

{
  "name": "姓名(string|null)",
  "email": "邮箱(string|null)",
  "phone": "电话(string|null)",
  "skills": ["技能列表(string[])"],
  "expected_salary": "期望薪资(string|null，如 15-20K、面议)",
  "expected_position": "期望岗位(string|null，如 AI算法工程师)",
  "years_of_experience": "工作年限(number|null，精确到一位小数，如 0.5=半年、1=一年、3.5=三年半。根据简历中的工作经历时间段计算总月数÷12)",
  "education": {
    "level": "最高学历(string|null，如 本科/硕士/博士)",
    "school": "毕业院校(string|null)",
    "major": "专业(string|null)"
  },
  "current_location": "现居城市(string|null，如 深圳)",
  "last_company": "最近一家公司名称(string|null)",
  "last_position": "最近一个岗位(string|null)",
  "summary": "一句话总结候选人背景(string|null)"
}

返回格式必须是合法的 JSON，不要有 markdown 代码块包裹。"""


def extract_text_from_pdf(file_path: str) -> str:
    """从 PDF 提取纯文本"""
    reader = PdfReader(file_path)
    parts: list[str] = []
    for page in reader.pages:
        text = page.extract_text()
        if text:
            parts.append(text)
    return "\n\n".join(parts)


def extract_text_from_docx(file_path: str) -> str:
    """从 DOCX 提取纯文本——直接用 lxml 解析 XML，兼容性更好"""
    import zipfile
    from lxml import etree

    with zipfile.ZipFile(file_path) as z:
        doc_xml = z.read("word/document.xml")

    tree = etree.fromstring(doc_xml)
    ns = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"

    parts: list[str] = []
    # 按段落 (<w:p>) 分组，每个段落内的 <w:t> 拼接起来
    for p in tree.iter(f"{{{ns}}}p"):
        para_texts = []
        for t in p.iter(f"{{{ns}}}t"):
            if t.text:
                para_texts.append(t.text)
        line = "".join(para_texts).strip()
        if line:
            parts.append(line)

    return "\n".join(parts)


def extract_text(file_path: str) -> str:
    """根据文件扩展名自动选提取器"""
    ext = Path(file_path).suffix.lower()
    if ext == ".pdf":
        return extract_text_from_pdf(file_path)
    if ext in (".docx", ".doc"):
        return extract_text_from_docx(file_path)
    if ext == ".txt":
        return Path(file_path).read_text(encoding="utf-8")
    raise ValueError(f"不支持的文件格式: {ext}")


async def parse_resume_text(resume_text: str) -> dict:
    """用 DeepSeek 从简历文本中提取结构化画像 + 缺失字段列表"""
    response = await client.chat.completions.create(
        model=DEFAULT_MODEL,
        messages=[
            {"role": "system", "content": RESUME_SYSTEM_PROMPT},
            {"role": "user", "content": resume_text},
        ],
        temperature=0.1,
        max_tokens=2000,
    )

    raw = response.choices[0].message.content or "{}"

    # DeepSeek 偶尔会在 JSON 外包 markdown fence
    raw = raw.strip()
    if raw.startswith("```"):
        raw = raw.split("\n", 1)[1]
        if raw.endswith("```"):
            raw = raw[:-3]
        raw = raw.strip()
        # 有些会写 ```json
        if raw.startswith("json"):
            raw = raw[4:].strip()

    try:
        profile = json.loads(raw)
    except json.JSONDecodeError:
        profile = {"raw_response": raw}

    # 检查缺失字段
    missing: list[str] = []
    if not profile.get("expected_salary"):
        missing.append("expected_salary")
    if not profile.get("expected_position"):
        missing.append("expected_position")
    if not profile.get("current_location"):
        missing.append("current_location")
    if not profile.get("skills"):
        missing.append("skills")
    if not profile.get("years_of_experience"):
        missing.append("years_of_experience")

    return {"profile": profile, "missing_from_resume": missing}
