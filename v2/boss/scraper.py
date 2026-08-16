"""
爬虫模块 — JS 提取脚本 + scrape() 主函数
"""

from __future__ import annotations

import json
import random
import time
from urllib.parse import quote

from browser import TabPool
from filters import filter_job
from logbridge import ScrapeAborted, console, set_web_log_hook

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

JS_ACTIVATE_DETAIL = """
(async () => {
    // 轮询等待 BOSS Vue SPA 真正渲染出内容，而不是只等 readyState
    // BOSS 详情页即使 readyState=complete，JD 区域也可能还在转圈/未渲染
    const start = Date.now();
    const timeout = 12000;  // 最多等 12 秒

    while (Date.now() - start < timeout) {
        // 检查 JD 内容是否已出现
        const jd = document.querySelector('.job-sec-text, .job-detail, .detail-content');
        if (jd && jd.textContent.trim().length > 30) return 'ok';

        // 检查标题 + 薪资是否同时就绪（部分页面无 JD 但基本信息应该有）
        const title = document.querySelector('.info-primary .name h1, .name h1');
        const salary = document.querySelector('.salary');
        if (title && salary && title.textContent.trim() && salary.textContent.trim()) {
            // 基本信息 OK，再等一下 JD
            await new Promise(r => setTimeout(r, 300));
            const jd2 = document.querySelector('.job-sec-text, .job-detail, .detail-content');
            if (jd2 && jd2.textContent.trim().length > 30) return 'ok';
            // 基本信息有了但 JD 为空，尝试点击 tab 触发渲染
        }

        // 策略1: 关掉可能的 loading overlay / 弹窗
        const overlays = document.querySelectorAll('.loading, .skeleton, [class*="loading"], [class*="spin"]');
        // 不关 overlay（可能不是遮罩），转而尝试点掉干扰元素
        const closeBtn = document.querySelector('.boss-popup__close, .icon-close, .dialog-close');
        if (closeBtn) closeBtn.click();

        // 策略2: 点击"职位描述"/"职位详情" tab 触发 Vue 渲染
        const tabSelectors = [
            '.job-tab', '.detail-tab', '.tab-item', '[class*="tab"]',
            '.job-menu li', '.detail-nav li', '.tab-bar li',
            '.info-tab', '.job-info-tab',
        ];
        for (const sel of tabSelectors) {
            const tabs = document.querySelectorAll(sel);
            for (const tab of tabs) {
                const text = tab.textContent || '';
                if (text.includes('职位描述') || text.includes('职位详情') || text.includes('工作内容') || text.includes('岗位职责')) {
                    tab.click();
                    break;
                }
            }
        }

        // 策略3: 滚动到可见区域，触发懒加载
        const jdArea = document.querySelector('.job-sec-text, .job-detail, .detail-content');
        if (jdArea) jdArea.scrollIntoView({behavior: 'instant', block: 'center'});
        window.scrollBy(0, 300);

        await new Promise(r => setTimeout(r, 800));
    }

    // 超时后最后检查一次
    const jd = document.querySelector('.job-sec-text, .job-detail, .detail-content');
    if (jd && jd.textContent.trim().length > 10) return 'ok_partial';
    const title = document.querySelector('.info-primary .name h1, .name h1');
    if (title && title.textContent.trim()) return 'basic_only';  // 至少有标题，让提取器自己尽力
    return 'timeout';
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

    // JD — 多个 fallback selector，适应 BOSS 不同版本的 DOM
    info.jd = document.querySelector('.job-sec-text')?.textContent?.trim()
        || document.querySelector('.job-detail')?.textContent?.trim()
        || document.querySelector('.detail-content')?.textContent?.trim()
        || document.querySelector('.describe-text')?.textContent?.trim()
        || document.querySelector('[class*="job-sec"]')?.textContent?.trim()
        || document.querySelector('[class*="detail-text"]')?.textContent?.trim()
        || '';

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

    // 检测是否已沟通过：按钮是"继续沟通"而非"立即沟通"
    const chatBtn = document.querySelector('.btn-startchat, .op-btn-chat, [ka*="chat"]');
    info.already_contacted = chatBtn ? chatBtn.textContent.includes('继续') : false;

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

        max_scrolls = per_combo_pages if per_combo_pages > 0 else max_pages
        combo_count = 0

        # ── 阶段1：持续滚动搜素 tab，收集卡片 URL ──
        search_url = SEARCH_URL.format(keyword=quote(keyword), city_code=city_code)
        if exp_param:
            search_url += f"&experience={exp_param}"
        if scale_param:
            search_url += f"&scale={scale_param}"

        search_tab = browser.new_tab(search_url)
        if not search_tab:
            console.print(f"[red]  无法打开搜索页[/red]")
            continue

        time.sleep(2)
        browser.wait_for_load(search_tab, timeout=10)

        # 切到搜索 tab（仅 Chrome 内切换，不弹窗），确保 BOSS 懒加载生效
        browser._send_cdp("Target.activateTarget", {"targetId": search_tab})

        collected_cards = []
        scroll_round = 0
        dry_rounds = 0

        while scroll_round < max_scrolls:
            if browser.is_shutdown:
                raise ScrapeAborted()

            scroll_round += 1

            # CDP mouseWheel：熄屏/后台也能触发 BOSS Vue 懒加载
            # JS scrollBy 在 rAF 暂停时无效（Chrome 熄屏/后台 tab）
            viewport = browser.evaluate(search_tab, "window.innerHeight") or 800
            total_scroll = int(viewport * 1.2 * 4)
            browser.scroll_wheel(search_tab, delta_y=total_scroll,
                                 repeat=4, interval=random.uniform(0.1, 0.25))
            time.sleep(random.uniform(2.0, 3.0))

            result = browser.evaluate(search_tab, JS_EXTRACT_LIST)
            if not result:
                break

            try:
                cards = json.loads(result) if isinstance(result, str) else result
            except (json.JSONDecodeError, TypeError):
                break

            if not cards:
                break

            round_new = 0
            for card in cards:
                url = card.get("url", "")
                if not url or url in seen_urls:
                    continue
                seen_urls.add(url)
                collected_cards.append(card)
                round_new += 1

            console.print(f"  [dim]第{scroll_round}轮滚动: {len(cards)} 张卡片, +{round_new} 新[/dim]")

            if round_new == 0:
                dry_rounds += 1
                if dry_rounds >= 3:
                    console.print("  [dim]连续3轮无新卡片，停止滚动[/dim]")
                    break
            else:
                dry_rounds = 0

        console.print(f"  [bold]滚动阶段结束: 共收集 {len(collected_cards)} 个不重复卡片[/bold]")

        # ── 阶段2：TabPool 并发开详情、提取、dwell ──
        filtered_count = 0

        # TabPool：最多 4 个并发 tab，每个停留 20-35s 模拟人类阅读，
        # 开 tab 间隔 4-8s，逐个 stagger
        pool = TabPool(browser, max_concurrent=4,
                       dwell_range=(20, 35), stagger_range=(4, 8))

        for job_data in collected_cards:
            if browser.is_shutdown:
                raise ScrapeAborted()

            # 先用列表数据做初步过滤
            keep, reason = filter_job({**job_data}, cfg, max_exp)
            if not keep:
                filtered_count += 1
                continue

            company_preview = job_data.get("company", "?")[:12]
            title_preview = job_data.get("title", "?")[:20]
            console.print(f"  [dim]  📄 {company_preview} - {title_preview} ... (池中 {pool.active_count} 个 tab)[/dim]")

            detail_url = f"https://www.zhipin.com{job_data.get('url', '')}"

            # TabPool.submit 自动处理: stagger → 开 tab → 激活 → 提取 → dwell
            detail_result = pool.submit(
                detail_url,
                activate_js=JS_ACTIVATE_DETAIL,
                extract_js=JS_EXTRACT_DETAIL,
                activate_timeout=15,
                extract_timeout=15,
            )

            if not detail_result:
                console.print(f"    [red]✗ 提取失败[/red]")
                continue

            try:
                detail = json.loads(detail_result) if isinstance(detail_result, str) else detail_result
            except (json.JSONDecodeError, TypeError):
                continue

            # 跳过已沟通过的（按钮是"继续沟通"而非"立即沟通"）
            if detail.get("already_contacted"):
                filtered_count += 1
                console.print(f"    [dim]⏭ 已沟通过，跳过[/dim]")
                continue

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

            keep, reason = filter_job(job, cfg, max_exp)
            if not keep:
                filtered_count += 1
                console.print(f"    [yellow]✗ 过滤({reason}): {job.get('experience','')} | {job.get('education','')} | {job.get('salary','')}[/yellow]")
                if on_progress:
                    on_progress("job_result", {"status": "filtered", "company": job.get("company",""),
                               "title": job.get("title",""), "salary": job.get("salary",""),
                               "experience": job.get("experience",""), "education": job.get("education","")})
                continue

            all_jobs.append(job)
            combo_count += 1
            console.print(f"    [green]✓ [{len(all_jobs)}] {job['salary']} | {job['experience']} | {job['education']}[/green]")
            if on_progress:
                on_progress("job_result", {"status": "kept", "company": job.get("company",""),
                           "title": job.get("title",""), "salary": job.get("salary",""),
                           "experience": job.get("experience",""), "education": job.get("education","")})

            if target_per_combo and combo_count >= target_per_combo:
                console.print(f"  [green]✓ 已满{target_per_combo}个 ({combo_count}/{target_per_combo})[/green]")
                break

        # 等所有剩余 tab dwell 结束
        pool.drain()

        console.print(f"  [bold]组合完成: +{combo_count} 保留[/bold] (过滤 {filtered_count}) [累计 {len(all_jobs)}]")
        if on_progress:
            on_progress("page_result", {"page": 1, "page_jobs": len(collected_cards), "kept": combo_count,
                       "filtered": filtered_count, "dup": 0, "total_kept": len(all_jobs),
                       "city": city, "keyword": keyword})

        browser.close_tab(search_tab)

    return all_jobs
