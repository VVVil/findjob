"""
智联招聘爬虫 — JS 提取脚本 + scrape()
"""

import json
import random
import time
from urllib.parse import quote

from rich.console import Console

from browser import new_tab, close_tab, evaluate, scroll, wait_for_load
from filters import filter_job

console = Console()

# ── 搜索 URL ──────────────────────────────────────────
SEARCH_URL = "https://sou.zhaopin.com/?jl={city_code}&kw={keyword}&p={page}"

CITY_CODES = {
    "北京": "530", "上海": "538", "深圳": "765",
    "广州": "763", "杭州": "653", "成都": "801",
    "武汉": "736", "南京": "635", "西安": "854",
    "苏州": "639", "天津": "531", "重庆": "551",
    "郑州": "558", "长沙": "749", "东莞": "769",
    "佛山": "766", "合肥": "577", "厦门": "591",
    "青岛": "672", "大连": "600", "中山": "723",
}

# ── JS 提取器 ─────────────────────────────────────────

JS_EXTRACT_LIST = """
(() => {
    const cards = document.querySelectorAll('.joblist-box__item');
    const jobs = [];
    cards.forEach((card) => {
        const nameEl = card.querySelector('.jobinfo__name');
        const salaryEl = card.querySelector('.jobinfo__salary');
        const companyEl = card.querySelector('.companyinfo__name');
        const href = nameEl ? nameEl.getAttribute('href') : '';

        if (!nameEl || !href) return;

        const otherItems = card.querySelectorAll('.jobinfo__other-info-item');
        const location = otherItems[0]?.textContent?.trim() || '';
        const experience = otherItems[1]?.textContent?.trim() || '';
        const education = otherItems[2]?.textContent?.trim() || '';

        const companyTags = card.querySelectorAll('.companyinfo__tag .joblist-box__item-tag');
        let companyType = '', companySize = '', companyIndustry = '';
        const tagTexts = Array.from(companyTags).map(t => t.textContent.trim());
        for (const t of tagTexts) {
            if (/\\d/.test(t) && /人/.test(t)) {
                companySize = t;
            } else if (['民营','国企','上市公司','外商独资','合资','事业单位','股份制','未融资','A轮','B轮','C轮','D轮','天使轮','已上市','不需要融资'].some(k => t.includes(k))) {
                companyType = t;
            } else {
                companyIndustry = companyIndustry || t;
            }
        }

        const hrNameEl = card.querySelector('.companyinfo__staff-name');
        const hrStatusEl = card.querySelector('.companyinfo__staff-state');
        const jobTags = card.querySelectorAll('.jobinfo__tag .joblist-box__item-tag');
        const tags = Array.from(jobTags).map(t => t.textContent.trim());

        jobs.push({
            title: nameEl.textContent.trim(),
            salary: salaryEl ? salaryEl.textContent.trim() : '',
            experience: experience,
            education: education,
            company: companyEl ? companyEl.textContent.trim() : '',
            location: location,
            company_type: companyType,
            company_size: companySize,
            company_industry: companyIndustry,
            hr_name: hrNameEl ? hrNameEl.textContent.trim() : '',
            hr_active: hrStatusEl ? hrStatusEl.textContent.trim() : '',
            url: href,
            tags: tags
        });
    });
    return JSON.stringify(jobs);
})()
"""

JS_EXTRACT_DETAIL = """
(() => {
    const info = {};

    // ── 基本信息 ──
    info.title = document.querySelector('.summary-planes__title')?.textContent?.trim()
        || document.querySelector('h1')?.textContent?.trim()
        || document.title.split('-')[0]?.trim() || '';
    info.salary = document.querySelector('.summary-planes__salary')?.textContent?.trim() || '';

    // 地点 / 经验 / 学历 / 类型
    const infoItems = document.querySelectorAll('.summary-planes__info li');
    let items = Array.from(infoItems).map(li => li.textContent.trim());
    // 第一个有城市链接，通常会是"深圳南山区"合并
    info.city = items[0] || '';
    info.experience = items[1] || '';
    info.education = items[2] || '';

    // ── JD ──
    const jdEl = document.querySelector('.describtion-card__detail-content')
        || document.querySelector('.responsibility, .job-detail, .describtion__content, .job-sec-text');
    info.jd = jdEl ? jdEl.textContent.trim() : '';

    // ── HR / 发布者 ──
    info.hr_name = document.querySelector('.publisher-seo__name')?.textContent?.trim() || '';
    info.hr_title = document.querySelector('.publisher-seo__job-title')?.textContent?.trim() || '';
    info.hr_active = document.querySelector('.publisher-seo__tag')?.textContent?.trim() || '';

    // ── 公司 ──
    info.company = document.querySelector('.company-summary__name-link')?.textContent?.trim()
        || document.querySelector('.company-summary__name')?.textContent?.trim() || '';

    // 公司详情列表（融资 / 规模 / 行业）
    const companyItems = document.querySelectorAll('.company-summary__item');
    info.company_finance = '';
    info.company_size = '';
    info.company_industry = '';
    companyItems.forEach((item) => {
        const icon = item.querySelector('[class*="icon--"]');
        const text = item.querySelector('span:last-child, .company-summary__text')?.textContent?.trim() || '';
        if (icon && text) {
            const cls = icon.className;
            if (cls.includes('finance')) info.company_finance = text;
            else if (cls.includes('scale')) info.company_size = text;
            else if (cls.includes('industry')) info.company_industry = text;
        }
    });

    info.url = window.location.href;

    return JSON.stringify(info);
})()
"""


# ══════════════════════════════════════════════════════
#  scrape()
# ══════════════════════════════════════════════════════

def scrape(cfg: dict, keywords: list[str], cities: list[str], pages: int, max_exp: int | None) -> list[dict]:
    """爬智联岗位（列表+详情），返回过滤后的列表"""
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

    total_combos = len(combos)

    for idx, (city, city_code, keyword) in enumerate(combos, 1):
        console.print(f"[dim][{idx}/{total_combos}] 智联搜索: {city} / {keyword}[/dim]")

        for page in range(1, pages + 1):
            search_url = SEARCH_URL.format(
                keyword=quote(keyword),
                city_code=city_code,
                page=page,
            )

            target_id = new_tab(search_url)
            if not target_id:
                console.print(f"[red]  无法打开搜索页 (第{page}页)[/red]")
                break

            time.sleep(3)
            wait_for_load(target_id, timeout=15)
            scroll(target_id, y=2000)
            time.sleep(1)

            result = evaluate(target_id, JS_EXTRACT_LIST)
            close_tab(target_id)

            if not result:
                console.print(f"  [red]提取失败[/red]")
                break

            try:
                card_jobs = json.loads(result) if isinstance(result, str) else result
            except (json.JSONDecodeError, TypeError):
                console.print(f"  [red]JSON 解析失败[/red]")
                break

            if not card_jobs:
                console.print(f"  [dim]第{page}页 0 张卡片[/dim]")
                break

            console.print(f"  [dim]第{page}页搜到 {len(card_jobs)} 张卡片[/dim]")
            page_new = 0
            filtered_count = 0

            for job_data in card_jobs:
                job_url = job_data.get("url", "")
                if job_url in seen_urls:
                    continue
                seen_urls.add(job_url)

                # 先构建列表数据做初步过滤
                full_url = job_url if job_url.startswith("http") else f"https:{job_url}"
                job = {
                    "title": job_data.get("title", ""),
                    "company": job_data.get("company", ""),
                    "salary": job_data.get("salary", ""),
                    "city": city,
                    "experience": job_data.get("experience", ""),
                    "education": job_data.get("education", ""),
                    "hr_name": job_data.get("hr_name", ""),
                    "hr_title": "",
                    "hr_active": job_data.get("hr_active", ""),
                    "company_size": job_data.get("company_size", ""),
                    "company_industry": job_data.get("company_industry", ""),
                    "url": full_url,
                    "jd": "",
                    "platform": "zhaopin",
                }

                if not filter_job(job, cfg, max_exp):
                    filtered_count += 1
                    continue

                # ── 开详情页 ──
                company_preview = job_data.get("company", "?")[:12]
                title_preview = job_data.get("title", "?")[:20]
                console.print(f"  [dim]  📄 {company_preview} - {title_preview} ...[/dim]")

                time.sleep(random.uniform(1.5, 3.0))
                detail_target = new_tab(full_url)
                if not detail_target:
                    console.print(f"    [yellow]✗ 打不开详情页，用列表数据[/yellow]")
                    all_jobs.append(job)
                    page_new += 1
                    continue

                time.sleep(2)
                wait_for_load(detail_target, timeout=15)
                detail_result = evaluate(detail_target, JS_EXTRACT_DETAIL)
                close_tab(detail_target)

                if detail_result:
                    try:
                        detail = json.loads(detail_result) if isinstance(detail_result, str) else detail_result

                        # 用详情数据覆盖
                        job["title"] = detail.get("title") or job["title"]
                        job["company"] = detail.get("company") or job["company"]
                        job["salary"] = detail.get("salary") or job["salary"]
                        job["experience"] = detail.get("experience") or job["experience"]
                        job["education"] = detail.get("education") or job["education"]
                        job["hr_name"] = detail.get("hr_name") or job["hr_name"]
                        job["hr_title"] = detail.get("hr_title") or job["hr_title"]
                        job["hr_active"] = detail.get("hr_active") or job["hr_active"]
                        job["company_size"] = detail.get("company_size") or job["company_size"]
                        job["company_industry"] = detail.get("company_industry") or job["company_industry"]
                        job["jd"] = detail.get("jd", "")
                    except (json.JSONDecodeError, TypeError):
                        pass

                # 用详情后的数据再过滤一次
                if not filter_job(job, cfg, max_exp):
                    filtered_count += 1
                    console.print(f"    [yellow]✗ 过滤: {job.get('experience','')} | {job.get('education','')} | {job.get('salary','')}[/yellow]")
                    continue

                all_jobs.append(job)
                page_new += 1
                console.print(f"    [green]✓ [{len(all_jobs)}] {job['salary']} | {job['experience']} | {job['education']}[/green]")

            if page_new > 0:
                console.print(f"  [bold]第{page}页: +{page_new} 保留[/bold] (过滤 {filtered_count}) [累计 {len(all_jobs)}]")

            if page < pages:
                time.sleep(random.uniform(2.0, 4.0))

    return all_jobs
