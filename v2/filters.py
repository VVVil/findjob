"""
硬过滤模块 — 经验/学历/薪资/屏蔽词
"""

import re


def parse_experience_max(exp_str: str) -> float | None:
    """解析经验字符串，返回上限年数。
    1年以内→1, 1-3年→3, 3-5年→5, 在校/应届→0, 经验不限→None"""
    if not exp_str:
        return None
    exp_str = exp_str.strip()
    if "不限" in exp_str or not exp_str:
        return None
    if "应届" in exp_str or "在校" in exp_str:
        return 0
    if "以上" in exp_str:
        m = re.search(r"(\d+)", exp_str)
        return float(m.group(1)) if m else 99
    if "以内" in exp_str:
        m = re.search(r"(\d+)", exp_str)
        return float(m.group(1)) if m else 1
    m = re.search(r"(\d+)\s*-\s*(\d+)\s*年?", exp_str)
    if m:
        return float(m.group(2))
    m = re.search(r"(\d+)\s*年", exp_str)
    if m:
        return float(m.group(1))
    return None


def parse_degree(exp_str: str) -> str:
    """从经验/标签字符串中提取学历"""
    for kw in ["博士", "硕士", "MBA", "本科", "大专", "高中", "中专", "学历不限", "不限"]:
        if kw in (exp_str or ""):
            return kw
    return ""


def _parse_salary_range_k(salary: str) -> tuple[float | None, float | None]:
    """解析薪资字符串，返回 (min_K, max_K)。支持：
    - BOSS: "8K-12K", "15K"
    - 智联: "8000-12000元", "1.1-1.8万", "面议"
    """
    if not salary:
        return None, None

    # "面议" — 无法解析
    if "面议" in salary:
        return None, None

    s = salary.strip()

    # 范围格式: 8-12K, 8K-12K, 1.1-1.8万, 8000-12000元
    m = re.search(r"(\d+(?:\.\d+)?)\s*[kK]?\s*-\s*(\d+(?:\.\d+)?)\s*([kK万])?", s)
    if m:
        lo, hi, unit = float(m.group(1)), float(m.group(2)), m.group(3)
        if unit in ("万",):
            lo *= 10
            hi *= 10
        elif unit in ("k", "K"):
            pass  # already in K
        else:
            # 无单位默认"元"，转 K
            lo /= 1000
            hi /= 1000
        return lo, hi

    # 单值格式: 15K, 1.5万, 10000元
    m = re.search(r"(\d+(?:\.\d+)?)\s*[kK]", s)
    if m:
        v = float(m.group(1))
        return v, v

    m = re.search(r"(\d+(?:\.\d+)?)\s*万", s)
    if m:
        v = float(m.group(1)) * 10
        return v, v

    m = re.search(r"(\d+)\s*元", s)
    if m:
        v = float(m.group(1)) / 1000
        return v, v

    return None, None


def _parse_salary_max_k(salary: str) -> float | None:
    """解析薪资上限（K）"""
    _, hi = _parse_salary_range_k(salary)
    return hi


def _parse_salary_min_k(salary: str) -> float | None:
    """解析薪资下限（K）"""
    lo, _ = _parse_salary_range_k(salary)
    return lo


def filter_job(job: dict, cfg: dict, max_exp: int | None) -> tuple[bool, str]:
    """返回 (保留?, 原因)。True = 保留，原因仅在被过滤时有意义"""
    title = job.get("title", "")
    company = job.get("company", "")
    salary = job.get("salary", "")
    experience = job.get("experience", "")
    education = job.get("education", "")

    # Deal breakers — 标题 & 公司名 & JD
    jd = job.get("jd", "")
    for kw in cfg.get("deal_breakers", []):
        target = f"{title} {company} {jd}".lower()
        if kw.lower() in target:
            return False, f"屏蔽词: {kw}"

    # 薪资下限
    smin = cfg.get("salary_min", 0)
    smax_k = _parse_salary_max_k(salary)
    if smin > 0 and smax_k is not None and smax_k < smin:
        return False, f"薪资上限{smax_k}K < 最低要求{smin}K"

    # 薪资上限：只过滤完全无交集的（底薪 > salary_max）
    smax_limit = cfg.get("salary_max", 0)
    if smax_limit > 0 and smax_k is not None:
        smin_k = _parse_salary_min_k(salary)
        if smin_k is not None and smin_k > smax_limit:
            return False, f"薪资下限{smin_k}K > 上限{smax_limit}K"

    # 经验过滤
    exp_max_years = parse_experience_max(experience)
    if max_exp is not None:
        if max_exp == 0:
            # -e 0: 只要应届
            if exp_max_years != 0:
                return False, f"经验要求{experience}(非应届)，只要应届"
        else:
            # -e 3: ≤3年，但排除应届
            if exp_max_years == 0:
                return False, f"经验要求{experience}(应届)，已排除应届"
            if exp_max_years is not None and exp_max_years > max_exp:
                return False, f"经验要求{experience} > 上限{max_exp}年"

    # 学历过滤（从 education 字段提取）
    edu = education or ""
    allowed = cfg.get("allowed_edu", ["本科", "大专"])
    if allowed and edu:
        edu_matched = any(a in edu for a in allowed) or any(a in edu for a in ["学历不限", "不限"])
        if not edu_matched:
            parsed = parse_degree(edu)
            if parsed and parsed not in allowed and parsed not in ["学历不限", "不限"]:
                return False, f"学历{edu}不在允许列表{allowed}"

    return True, ""
