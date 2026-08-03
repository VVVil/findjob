"""
爬虫模块 — JS 提取脚本 + scrape() 主函数
"""

from __future__ import annotations

import json
import random
import time
from urllib.parse import quote

from rich.console import Console

from filters import filter_job

console = Console()

# ── BOSS直聘搜索 URL ─────────────────────────────────
SEARCH_URL = "https://www.zhipin.com/web/geek/jobs?query={keyword}&city={city_code}"

CITY_CODES = {
    "北京": "101010100", "上海": "101020100", "深圳": "101280600",
    "广州": "101280100", "杭州": "101210100", "成都": "101270100",
    "武汉": "101200100", "南京": "101190100", "西安": "101110100",
    "苏州": "101190400", "天津": "101030100", "重庆": "101040100",
    "郑州": "101180100", "长沙": "101250100", "东莞": "101281600",
    "佛山": "101280800", "合肥": "101220100", "厦门": "101230200",
    "青岛": "101120200", "大连": "101070200", "中山": "101281700",
}

# BOSS 经验分段（URL 粗筛用），支持逗号拼接
# 101=经验不限, 102=应届, 103=1年以内, 104=1-3年, 105=3-5年, 106=5-10年, 107=10年以上
EXP_BANDS = [
    ("101",  0, 99),   # 经验不限
    ("102",  0,  0),   # 应届
    ("103",  0,  1),   # 1年以内
    ("104",  1,  3),   # 1-3年
    ("105",  3,  5),   # 3-5年
    ("106",  5, 10),   # 5-10年
    ("107", 10, 99),   # 10年以上
]

# 薪资分段是互斥的，无法多选 → 不拼 URL 参数，留在 Python 端精确过滤

# BOSS 公司规模参数，支持逗号拼接
# 301=0-20人, 302=20-99人, 303=100-499人, 304=500-999人, 305=1000-9999人, 306=10000人以上
SCALE_BANDS = [
    ("301",  0,  20),
    ("302",  20,  99),
    ("303", 100, 499),
    ("304", 500, 999),
    ("305", 1000, 9999),
    ("306", 10000, 999999),
]


def _build_scale_param(scale_min: int | None, scale_max: int | None) -> str:
    """将人数范围映射为 BOSS scale 参数值（取有交集的段）"""
    if scale_min is None and scale_max is None:
        return ""
    codes = []
    for code, lo, hi in SCALE_BANDS:
        if scale_min is not None and hi <= scale_min:
            continue
        if scale_max is not None and lo > scale_max:
            continue
        codes.append(code)
    return ",".join(codes) if codes else ""


def _build_exp_param(max_exp: int | None) -> str:
    """将 max_exp 映射为 BOSS experience 参数值（逗号分隔），None = 经验不限"""
    if max_exp is None:
        return "101"                                 # 默认经验不限
    if max_exp == 0:
        return "102"                                 # 只要应届
    codes = ["101"]                                  # 经验不限总是带上
    for code, lo, hi in EXP_BANDS:
        if code in ("101", "102"):
            continue
        if hi <= max_exp:
            codes.append(code)
    return ",".join(codes)


# ── JS 提取器 ─────────────────────────────────────────

JS_EXTRACT_LIST = """
(() => {
    const wraps = document.querySelectorAll('.job-card-wrap');
    const jobs = [];
    wraps.forEach((wrap) => {
        const box = wrap.querySelector('.job-card-box') || wrap;
        const nameEl = box.querySelector('.job-name');
        const salaryEl = box.querySelector('.job-salary');
        const expEl = box.querySelector('.text-experiece');
        const degEl = box.querySelector('.text-degree');
        const companyEl = box.querySelector('.boss-name') || box.querySelector('.company-name');
        const locationEl = box.querySelector('.company-location');
        const href = nameEl ? nameEl.getAttribute('href') : '';

        if (!nameEl || !href) return;

        jobs.push({
            title: nameEl.textContent.trim(),
            salary: salaryEl ? salaryEl.textContent.trim() : '',
            experience: expEl ? expEl.textContent.trim() : '',
            education: degEl ? degEl.textContent.trim() : '',
            company: companyEl ? companyEl.textContent.trim() : '',
            location: locationEl ? locationEl.textContent.trim() : '',
            url: href
        });
    });
    return JSON.stringify(jobs);
})()
"""

JS_EXTRACT_DETAIL = """
(() => {
    const info = {};
    info.title = document.querySelector('.info-primary .name h1')?.textContent?.trim()
        || document.querySelector('.name h1')?.textContent?.trim()
        || document.title.split('-')[0]?.trim();
    info.salary = document.querySelector('.salary')?.textContent?.trim() || '';

    // Experience & education — BOSS uses misspelled class names
    const expEl = document.querySelector('.text-experiece');
    const degEl = document.querySelector('.text-degree');
    info.experience = expEl ? expEl.textContent.trim() : '';
    info.education = degEl ? degEl.textContent.trim() : '';

    // Fallback: try tag-list
    if (!info.experience || !info.education) {
        const tags = document.querySelectorAll('.info-primary .tag-list span');
        if (!info.experience && tags[0]) info.experience = tags[0].textContent.trim();
        if (!info.education && tags[1]) info.education = tags[1].textContent.trim();
    }

    info.jd = document.querySelector('.job-sec-text')?.textContent?.trim() || '';

    // Company
    const companyLinks = document.querySelectorAll('.sider-company .company-info a');
    info.company = '';
    for (const link of companyLinks) {
        const text = link.textContent.trim();
        if (text && text.length > 0 && !text.includes('http')) {
            info.company = text; break;
        }
    }
    if (!info.company) {
        const m = document.title.match(/_(.+?)招聘/);
        info.company = m ? m[1] : '';
    }

    // Company tags（BOSS 新版 DOM: <p><i class="icon-scale"/>100-499人</p>）
    const siderCompany = document.querySelector('.sider-company');
    info.company_size = ''; info.company_industry = '';
    if (siderCompany) {
        const scaleEl = siderCompany.querySelector('.icon-scale');
        if (scaleEl) info.company_size = scaleEl.parentElement?.textContent?.trim() || '';
        const industryEl = siderCompany.querySelector('.icon-industry');
        if (industryEl) info.company_industry = industryEl.parentElement?.textContent?.trim() || '';
    }

    // HR info（BOSS 新版 DOM: .job-boss-info > h2.name + .boss-info-attr）
    info.hr_name = ''; info.hr_title = ''; info.hr_active = '';
    const bossSection = document.querySelector('.job-boss-info');
    if (bossSection) {
        const nameH2 = bossSection.querySelector('h2.name');
        if (nameH2) {
            info.hr_name = nameH2.childNodes[0]?.textContent?.trim() || nameH2.textContent?.split('\\n')[0]?.trim() || '';
        }
        const attrDiv = bossSection.querySelector('.boss-info-attr');
        if (attrDiv) {
            const parts = attrDiv.textContent?.trim()?.split('·') || [];
            info.hr_title = parts.length > 1 ? parts[parts.length - 1].trim() : '';
        }
    }
    info.hr_active = document.querySelector('.boss-online-tag')?.textContent?.trim() || '';
    info.url = window.location.pathname;

    return JSON.stringify(info);
})()
"""


# ══════════════════════════════════════════════════════
#  scrape() — 主爬虫
# ══════════════════════════════════════════════════════

def scrape(browser, cfg: dict, keywords: list[str], cities: list[str],
           per_combo_pages: int, max_exp: int | None,
           target_per_combo: int | None = None, max_pages: int = 20,
           on_progress: callable | None = None) -> list[dict]:
    """爬 BOSS 岗位，返回过滤后的列表。browser 为 BrowserSession 实例。

    per_combo_pages: 每组合翻几页，0=不限（由 max_pages 兜底）
    target_per_combo: 每个城市×关键词组合找多少个，凑够即停。None=不限
    max_pages: 单组合绝对上限，防翻穿
    on_progress: 可选回调，签名 (event_type: str, data: dict)，用于 Web 端实时进度
    """
    all_jobs = []
    seen_urls = set()

    combos = []
    for city in cities:
        city_code = CITY_CODES.get(city.strip())
        if not city_code:
            console.print(f"[yellow]未知城市: {city}[/yellow]")
            continue
        for kw in keywords:
            combos.append((city, city_code, kw.strip()))

    if not combos:
        console.print("[red]没有有效搜索组合[/red]")
        return []

    # ── URL 参数预计算 ──
    exp_param = _build_exp_param(max_exp)
    scale_param = cfg.get("boss_scale", "")  # 如 "302,303,304,305,306"

    total_combos = len(combos)

    for idx, (city, city_code, keyword) in enumerate(combos, 1):
        console.print(f"[dim][{idx}/{total_combos}] 搜索: {city} / {keyword}[/dim]")
        if on_progress:
            on_progress("combo_start", {"idx": idx, "total": total_combos, "city": city, "keyword": keyword})

        page_limit = per_combo_pages if per_combo_pages > 0 else max_pages
        combo_count = 0

        for page in range(1, page_limit + 1):
            if browser.is_shutdown:
                console.print("  [yellow]收到退出信号[/yellow]")
                break
            search_url = SEARCH_URL.format(keyword=quote(keyword), city_code=city_code) + "&sortType=2"
            if exp_param:
                search_url += f"&experience={exp_param}"
            if scale_param:
                search_url += f"&scale={scale_param}"
            if page > 1:
                search_url += f"&page={page}"

            target_id = browser.new_tab(search_url)
            if not target_id:
                console.print(f"[red]  无法打开搜索页 (第{page}页)[/red]")
                break

            time.sleep(2)
            browser.wait_for_load(target_id, timeout=10)
            browser.scroll(target_id, y=2000)
            time.sleep(1)

            result = browser.evaluate(target_id, JS_EXTRACT_LIST)
            browser.close_tab(target_id)

            if not result:
                break

            try:
                jobs = json.loads(result) if isinstance(result, str) else result
            except (json.JSONDecodeError, TypeError):
                break

            if not jobs:
                break

            console.print(f"  [dim]第{page}页搜到 {len(jobs)} 张卡片[/dim]")
            page_new = 0
            filtered_count = 0
            dup_count = 0

            for job_data in jobs:
                if browser.is_shutdown:
                    console.print("  [yellow]收到退出信号[/yellow]")
                    break
                job_url = job_data.get("url", "")
                if job_url in seen_urls:
                    dup_count += 1
                    continue
                seen_urls.add(job_url)

                # 先用列表数据做初步过滤
                combined = {**job_data}
                if not filter_job(combined, cfg, max_exp):
                    filtered_count += 1
                    continue

                # 开详情页
                company_preview = job_data.get("company", "?")[:12]
                title_preview = job_data.get("title", "?")[:20]
                console.print(f"  [dim]  📄 {company_preview} - {title_preview} ...[/dim]")

                time.sleep(random.uniform(1.5, 3.0))
                detail_url = f"https://www.zhipin.com{job_url}"
                detail_target = browser.new_tab(detail_url)
                if not detail_target:
                    console.print(f"    [red]✗ 打不开详情页[/red]")
                    continue

                time.sleep(2)
                browser.wait_for_load(detail_target, timeout=10)
                detail_result = browser.evaluate(detail_target, JS_EXTRACT_DETAIL)
                browser.close_tab(detail_target)

                if not detail_result:
                    console.print(f"    [red]✗ 提取失败[/red]")
                    continue

                try:
                    detail = json.loads(detail_result) if isinstance(detail_result, str) else detail_result
                except (json.JSONDecodeError, TypeError):
                    continue

                # 合并详情数据
                job = {
                    "title": detail.get("title") or job_data.get("title", ""),
                    "company": detail.get("company") or job_data.get("company", ""),
                    "salary": detail.get("salary") or job_data.get("salary", ""),
                    "city": city,
                    "experience": detail.get("experience") or job_data.get("experience", ""),
                    "education": detail.get("education") or job_data.get("education", ""),
                    "hr_name": detail.get("hr_name", ""),
                    "hr_title": detail.get("hr_title", ""),
                    "hr_active": detail.get("hr_active", ""),
                    "company_size": detail.get("company_size", ""),
                    "company_industry": detail.get("company_industry", ""),
                    "url": detail_url,
                    "jd": detail.get("jd", ""),
                    "platform": "boss",
                }

                # 用详情数据再过滤一次
                if not filter_job(job, cfg, max_exp):
                    filtered_count += 1
                    console.print(f"    [yellow]✗ 过滤: {job.get('experience','')} | {job.get('education','')} | {job.get('salary','')}[/yellow]")
                    if on_progress:
                        on_progress("job_result", {"status": "filtered", "company": job.get("company",""),
                                   "title": job.get("title",""), "salary": job.get("salary",""),
                                   "experience": job.get("experience",""), "education": job.get("education","")})
                    continue

                all_jobs.append(job)
                page_new += 1
                console.print(f"    [green]✓ [{len(all_jobs)}] {job['salary']} | {job['experience']} | {job['education']}[/green]")
                if on_progress:
                    on_progress("job_result", {"status": "kept", "company": job.get("company",""),
                               "title": job.get("title",""), "salary": job.get("salary",""),
                               "experience": job.get("experience",""), "education": job.get("education","")})

            extra = ""
            if dup_count > 0:
                extra += f", 重复 {dup_count}"
            console.print(f"  [bold]第{page}页: +{page_new} 保留[/bold] (过滤 {filtered_count}{extra}) [累计 {len(all_jobs)}]")
            if on_progress:
                on_progress("page_result", {"page": page, "page_jobs": len(jobs), "kept": page_new,
                           "filtered": filtered_count, "dup": dup_count, "total_kept": len(all_jobs),
                           "city": city, "keyword": keyword})

            # ── 更新组合计数 ──
            combo_count += page_new

            # ── 枯竭检测已移除：-p 翻到页数停，-n 翻到凑够或 max_pages 停 ──

            # ── 组合目标达成 ──
            if target_per_combo and combo_count >= target_per_combo:
                console.print(f"  [green]✓ 已满{target_per_combo}个 ({combo_count}/{target_per_combo})[/green]")
                break

            if browser.is_shutdown:
                break

            if page < page_limit:
                time.sleep(random.uniform(2.0, 4.0))

    return all_jobs
