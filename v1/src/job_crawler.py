"""
Playwright 爬虫：智联招聘推荐页，支持翻页。
Singleton browser context，浏览器全程不关闭。
"""
from __future__ import annotations

import asyncio
import json
from datetime import datetime
from pathlib import Path
from typing import Any

from playwright.async_api import async_playwright, BrowserContext, Page

HERE = Path(__file__).parent.parent
JOB_DETAILS_DIR = HERE / "data" / "jobs"
PROFILE_DIR = HERE / "playwright_profile"

_playwright = None
_context: BrowserContext | None = None
_logged_in = False
_session_json_path: str | None = None  # 本次会话的 JSON 输出路径（多页追加）


# ---------------------------------------------------------------------------
# Singleton context
# ---------------------------------------------------------------------------

async def _ensure_context() -> BrowserContext:
    """获取或创建 persistent browser context（全局单例，浏览器不关）。"""
    global _playwright, _context
    if _context is not None:
        return _context
    JOB_DETAILS_DIR.mkdir(parents=True, exist_ok=True)
    _playwright = await async_playwright().start()
    _context = await _playwright.chromium.launch_persistent_context(
        user_data_dir=str(PROFILE_DIR),
        headless=False,
        args=["--disable-blink-features=AutomationControlled"],
    )
    return _context


async def shutdown():
    """关闭浏览器和 Playwright（仅在程序退出时调用一次）。"""
    global _playwright, _context, _logged_in, _session_json_path
    try:
        if _context:
            await _context.close()
    except Exception:
        pass
    try:
        if _playwright:
            await _playwright.stop()
    except Exception:
        pass
    _context = None
    _playwright = None
    _logged_in = False
    _session_json_path = None


# ---------------------------------------------------------------------------
# 列表页提取
# ---------------------------------------------------------------------------

async def _extract_list(page: Page) -> list[dict]:
    """从推荐页 DOM 提取所有岗位基础信息。"""
    await page.wait_for_selector(
        ".positionlist__list .joblist-box__item",
        timeout=30_000,
    )
    await asyncio.sleep(2)

    jobs = await page.evaluate("""() => {
        const cards = document.querySelectorAll('.positionlist__list .joblist-box__item');
        return Array.from(cards).map(card => {
            const titleEl = card.querySelector('.jobinfo__name');
            const companyEl = card.querySelector('.companyinfo__name');
            const salaryEl = card.querySelector('.jobinfo__salary');

            const skillTagEls = card.querySelectorAll('.jobinfo__tag .joblist-box__item-tag');
            const companyTagEls = card.querySelectorAll('.companyinfo__tag .joblist-box__item-tag');

            const infoItems = card.querySelectorAll('.jobinfo__other-info-item');
            let location = '', experience = '', education = '';
            infoItems.forEach(item => {
                const hasImg = item.querySelector('img');
                const span = item.querySelector('span');
                if (hasImg && span) {
                    location = span.textContent.trim();
                } else if (!hasImg) {
                    const text = item.textContent.trim();
                    if (!text) return;
                    if (!experience && (text.includes('年') || text.includes('经验') || text.includes('不限'))) {
                        experience = text;
                    } else if (!education) {
                        education = text;
                    }
                }
            });

            return {
                title: titleEl?.textContent?.trim() || '',
                company: companyEl?.textContent?.trim() || '',
                salary: salaryEl?.textContent?.trim() || '',
                location: location,
                experience: experience,
                education: education,
                skill_tags: Array.from(skillTagEls).map(t => t.textContent.trim()).filter(Boolean),
                company_tags: Array.from(companyTagEls).map(t => t.textContent.trim()).filter(Boolean),
                link: titleEl?.href || '',
            };
        }).filter(j => j.title && j.link);
    }""")

    return jobs


# ---------------------------------------------------------------------------
# 详情页提取
# ---------------------------------------------------------------------------

async def _extract_detail(page: Page) -> dict:
    """从详情页 DOM 提取 JD 正文 + 更新时间。"""
    await page.wait_for_selector(".describtion-card__detail-content", timeout=20_000)
    await asyncio.sleep(1)

    result = await page.evaluate("""() => {
        const jdEl = document.querySelector('.describtion-card__detail-content');
        const timeEl = document.querySelector('.summary-planes__time');
        return {
            jd: jdEl?.innerText?.trim() || '',
            update_time: timeEl?.textContent?.replace('更新时间', '').trim() || '',
        };
    }""")

    return result


# ---------------------------------------------------------------------------
# 翻页
# ---------------------------------------------------------------------------

async def _click_next_page(page: Page) -> bool:
    """点击"下一页"按钮。返回 False 如果按钮不可用。"""
    next_btn = page.locator("a.soupager__btn:not(.soupager__btn__before)")
    if await next_btn.count() == 0:
        return False
    # 检查是否 disabled
    is_disabled = await next_btn.get_attribute("disabled")
    classes = await next_btn.get_attribute("class") or ""
    if is_disabled == "disabled" or "soupager__btn--disable" in classes:
        return False
    await next_btn.click()
    await asyncio.sleep(3)  # 等 AJAX 加载新列表
    return True


# ---------------------------------------------------------------------------
# 验证码检测 & 自动点击
# ---------------------------------------------------------------------------

CAPTCHA_SAVE_DIR = HERE / "data" / "captcha_dumps"

# 已知的验证码特征
CAPTCHA_CHECKS = [
    # 腾讯 EdgeOne 验证码（智联实际使用的是这个）
    "#tcaptcha_iframe_eo",
    "iframe[src*='captcha.eo']",
    # 腾讯验证码 iframe（通用）
    "iframe[src*='tcaptcha']",
    "iframe[src*='captcha']",
    # 验证码弹窗
    "#captcha",
]

async def _detect_and_handle_captcha(page: Page, context_label: str = "") -> bool:
    """
    检测页面是否弹出人机验证，尝试自动点击通过。
    返回 True 表示检测到并处理了验证码（调用方需重新等待页面加载）。
    首次遇到未知验证码时，保存 DOM 快照到 data/captcha_dumps/ 供分析。
    """
    try:
        # 1. 先检查是否有验证码 iframe 或弹窗
        captcha_indicators = await page.evaluate("""() => {
            const results = [];
            // 腾讯验证码 iframe
            const tcIframe = document.querySelector('iframe[src*="tcaptcha"]');
            if (tcIframe) results.push({type: 'tcaptcha_iframe', found: true});

            // 其他验证码 iframe
            const captchaIframe = document.querySelector('iframe[src*="captcha"]');
            if (captchaIframe && !tcIframe) results.push({type: 'captcha_iframe', found: true});

            // 验证码弹窗容器
            const verifyBox = document.querySelector(
                '.tx-verify, .captcha-container, [class*="captcha"], [class*="verify"],' +
                '[id*="tcaptcha"], [id*="captcha"]'
            );
            if (verifyBox) results.push({type: 'verify_container', found: true,
                class: verifyBox.className, id: verifyBox.id, tag: verifyBox.tagName});

            // 包含"验证"文字的按钮
            const buttons = document.querySelectorAll('button');
            const verifyBtns = [];
            buttons.forEach((btn, i) => {
                const text = btn.textContent.trim();
                if (text && (text.includes('验证') || text.includes('确认') ||
                    text.includes('verify') || text.includes('完成'))) {
                    verifyBtns.push({index: i, text: text.slice(0, 50)});
                }
            });
            if (verifyBtns.length > 0) results.push({type: 'verify_buttons', buttons: verifyBtns});

            return results;
        }""")

        if not captcha_indicators:
            return False

        # 找到了验证码相关元素 → 先存 DOM 快照
        label = f"[{context_label}]" if context_label else ""
        print(f"  ⚠️ {label}检测到人机验证:", end=" ")

        # 每次检测到验证码都保存 DOM，方便分析规律
        CAPTCHA_SAVE_DIR.mkdir(parents=True, exist_ok=True)
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        dump_path = CAPTCHA_SAVE_DIR / f"captcha_{context_label}_{ts}.html"
        html = await page.content()
        dump_path.write_text(html, encoding="utf-8")
        print(f"DOM已保存 → {dump_path.name}", end=" | ")

        # 打印检测到的类型
        for ind in captcha_indicators:
            t = ind.get('type', '?')
            if t == 'tcaptcha_iframe':
                print("腾讯验证码 iframe", end=" ")
            elif t == 'captcha_iframe':
                print("验证码 iframe", end=" ")
            elif t == 'verify_container':
                print(f"验证容器({ind.get('class', '')[:30]})", end=" ")
            elif t == 'verify_buttons':
                btns = ind.get('buttons', [])
                print(f"验证按钮({', '.join(b['text'][:20] for b in btns[:3])})", end=" ")

        # 2. CAPTCHA 子 Agent（LLM 自主推理 + 缓存）
        handled = False

        # TODO: 以下硬编码策略后续封装为 Agent Tool，子 Agent 可自行调用
        # _try_click_hardcoded_selectors(page) ...

        print("→ 启动 CAPTCHA 子 Agent...")
        try:
            from src.captcha_agent import solve_captcha
            handled = await solve_captcha(page)
        except Exception as e:
            print(f"  [red]CAPTCHA Agent 异常: {e}[/red]")

        # 3. Agent 搞不定 → 保存快照 + 人工介入
        if not handled:
            print("→ 子 Agent 也未成功，保存 DOM 快照...")
            CAPTCHA_SAVE_DIR.mkdir(parents=True, exist_ok=True)
            ts = datetime.now().strftime("%Y%m%d_%H%M%S")
            dump_path = CAPTCHA_SAVE_DIR / f"captcha_{context_label}_{ts}.html"
            html = await page.content()
            dump_path.write_text(html, encoding="utf-8")
            print(f"    快照: {dump_path}")
            print("=" * 50)
            print("请在浏览器中手动完成验证，完成后按 Enter 继续...")
            print("=" * 50)
            input()
            handled = True

        # 点击后等页面反应
        await asyncio.sleep(3)
        return True

    except Exception as e:
        print(f"  [dim]验证码检测异常: {e}[/dim]")
        return False

async def crawl_page(page_num: int) -> tuple[list[dict], str]:
    """
    爬取指定页面（0-indexed）的岗位列表 + 详情。

    page_num=0: 打开推荐页，首次需扫码登录
    page_num>=1: 点击"下一页"翻页

    Returns: (jobs_list, json_path)
    """
    context = await _ensure_context()
    page = context.pages[0] if context.pages else await context.new_page()
    page.set_default_timeout(30_000)

    global _logged_in

    if page_num == 0:
        # ── 首页：打开推荐页 ──
        current_url = page.url
        if "zhaopin.com/recommend" not in current_url:
            await page.goto(
                "https://www.zhaopin.com/recommend",
                wait_until="domcontentloaded",
                timeout=60_000,
            )
            await asyncio.sleep(2)

        # 先检查是否有验证码
        await _detect_and_handle_captcha(page, "首页")

        # 检查是否需要登录
        if not _logged_in:
            # 先短暂等一下，看页面是否已有岗位列表（说明 session cookie 还在）
            try:
                await page.wait_for_selector(
                    ".positionlist__list .joblist-box__item",
                    timeout=5_000,
                )
                # 能加载到 → 已登录
                _logged_in = True
                print("[green]已检测到登录状态，无需扫码[/green]")
            except Exception:
                # 超时 → 需要登录
                print()
                print("=" * 50)
                print("需要登录。请在浏览器中完成扫码。")
                print("登录完成后回到这里按 Enter 继续...")
                print("=" * 50)
                input()
                _logged_in = True
                await page.reload(wait_until="domcontentloaded")
                await asyncio.sleep(2)
    else:
        # ── 翻页 ──
        ok = await _click_next_page(page)
        if not ok:
            print(f"  [yellow]第 {page_num + 1} 页不可用，停止翻页[/yellow]")
            return [], ""

    # ── 等待列表加载（先检查验证码）──
    print(f"\n[dim]等待第 {page_num + 1} 页岗位列表加载...[/dim]")
    list_loaded = False
    for attempt in range(3):
        try:
            await page.wait_for_selector(
                ".positionlist__list .joblist-box__item",
                timeout=10_000,
            )
            list_loaded = True
            break
        except Exception:
            # 超时 → 可能是验证码挡住了
            captcha_found = await _detect_and_handle_captcha(
                page, f"第{page_num + 1}页"
            )
            if not captcha_found:
                # 不是验证码，是真超时
                break
            # 验证码已处理，重试
            if page_num == 0:
                await page.reload(wait_until="domcontentloaded")
                await asyncio.sleep(2)
            continue

    if not list_loaded:
        print(f"  [yellow]第 {page_num + 1} 页加载失败，跳过[/yellow]")
        return [], ""
    await asyncio.sleep(2)

    # ── 提取列表 ──
    jobs_basic = await _extract_list(page)
    print(f"第 {page_num + 1} 页列表提取到 {len(jobs_basic)} 个岗位")

    # ── 逐个访问详情页提取 JD ──
    all_jobs: list[dict] = []
    for i, job in enumerate(jobs_basic):
        if not job["link"]:
            all_jobs.append(job)
            continue

        title_short = job["title"][:40]
        print(f"  [{i + 1}/{len(jobs_basic)}] {title_short}...", end=" ", flush=True)

        detail_page = await context.new_page()
        try:
            for detail_attempt in range(2):
                try:
                    await detail_page.goto(
                        job["link"],
                        wait_until="domcontentloaded",
                        timeout=15_000,
                    )
                    detail = await _extract_detail(detail_page)
                    job["jd_full"] = detail["jd"]
                    job["update_time"] = detail["update_time"]

                    jd_len = len(job["jd_full"])
                    print(f"JD {jd_len} 字符 | {detail['update_time']}")
                    break
                except Exception:
                    # 可能是验证码挡住了
                    captcha_handled = await _detect_and_handle_captcha(
                        detail_page, f"详情{i+1}"
                    )
                    if captcha_handled and detail_attempt == 0:
                        # 验证码已处理，开新页面避免旧 CAPTCHA 状态污染
                        await asyncio.sleep(3)
                        await detail_page.close()
                        detail_page = await context.new_page()
                        continue
                    raise
        except Exception as e:
            print(f"异常: {e}")
        finally:
            await detail_page.close()

        all_jobs.append(job)
        await asyncio.sleep(1.5)

    # ── 保存 JSON（第1页新建，后续页追加） ──
    global _session_json_path
    if page_num == 0 or _session_json_path is None:
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        _session_json_path = str(JOB_DETAILS_DIR / f"jobs_{ts}.json")
        Path(_session_json_path).write_text(
            json.dumps(all_jobs, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        print(f"  已保存 → {_session_json_path}")
    else:
        existing = json.loads(Path(_session_json_path).read_text(encoding="utf-8"))
        existing.extend(all_jobs)
        Path(_session_json_path).write_text(
            json.dumps(existing, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        print(f"  已追加 {len(all_jobs)} 个 → {_session_json_path}（共 {len(existing)} 个）")

    return all_jobs, _session_json_path
