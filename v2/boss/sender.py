"""
发送模块 — 招呼语发送 + 轻触模式
"""

import json
import random
import time

from rich.console import Console

console = Console()


# ── 发送招呼语 ────────────────────────────────────────

def send_greeting(browser, job: dict, greeting: str, fast: bool = False) -> tuple[bool, str]:
    """统一流程：点按钮 → 导航到聊天页 → 匹配会话 → 发自定义招呼语。

    兼容两种模式：
    - "立即沟通"（新联系人）：弹窗自动发默认招呼语
    - "继续沟通"（老联系人）：页面可能跳转
    无论哪种，最终都在 https://www.zhipin.com/web/geek/chat 完成自定义发送。

    fast=True 时跳过模拟浏览，用于批量发送模式。
    """
    target_id = browser.new_tab(job["url"])
    if not target_id:
        return False, "无法打开岗位页"

    time.sleep(3)
    browser.wait_for_load(target_id, timeout=10)
    time.sleep(1)

    # 模拟浏览（批量发送时缩短）
    browse_time = random.uniform(2, 5) if fast else random.uniform(8, 15)
    console.print(f"[dim]  浏览中 ({browse_time:.0f}s)...[/dim]")
    time.sleep(browse_time)

    # ── 第1步：点击"立即沟通"/"继续沟通" ──
    click_js = """
    (() => {
        const btn = document.querySelector('.btn-startchat, .op-btn-chat, [ka*="chat"]');
        if (!btn) return JSON.stringify({success: false, error: 'no_chat_button'});
        btn.click();
        return JSON.stringify({success: true});
    })()
    """
    step1 = browser.evaluate(target_id, click_js, timeout=10)
    if not step1:
        browser.close_tab(target_id)
        return False, "点击按钮无返回"
    try:
        r1 = json.loads(step1) if isinstance(step1, str) else step1
        if not r1.get("success"):
            browser.close_tab(target_id)
            return False, r1.get("error", "没找到沟通按钮")
    except (json.JSONDecodeError, TypeError):
        browser.close_tab(target_id)
        return False, f"step1 解析失败: {str(step1)[:80]}"

    # 等默认招呼语发出（弹窗出现 → 自动发送 → 弹窗关闭）
    time.sleep(2)

    # ── 第1.5步：关闭"今天已联系过 N 位"的限频弹窗 ──
    # 弹窗是 position:fixed，offsetParent 为 null，所以用 getBoundingClientRect 判断可见性
    dismiss_js = """
    (() => {
        const isVisible = (el) => {
            const r = el.getBoundingClientRect();
            return r.width > 0 && r.height > 0;
        };
        // 先找弹窗容器
        for (const popSel of ['.boss-popup__wrapper', '[class*="popup"]', '[class*="dialog"]', '[class*="modal"]', '[class*="overlay"]', '.van-popup', '.van-dialog']) {
            const popup = document.querySelector(popSel);
            if (!popup || !isVisible(popup)) continue;
            const text = popup.textContent;
            if (!text.includes('联系过')) continue;
            // 在弹窗里找确认按钮
            const btns = popup.querySelectorAll('span, button, a, div');
            for (const btn of btns) {
                const t = btn.textContent.trim();
                if ((t === '好' || t === '好的' || t === '确定' || t === '确认' || t === '我知道了') && isVisible(btn)) {
                    btn.click();
                    return JSON.stringify({dismissed: true, text: t, selector: popSel});
                }
            }
            // 兜底：点弹窗里最后一个可见按钮
            for (let i = btns.length - 1; i >= 0; i--) {
                if (isVisible(btns[i]) && btns[i].textContent.trim().length <= 10) {
                    btns[i].click();
                    return JSON.stringify({dismissed: true, text: btns[i].textContent.trim(), fallback: true});
                }
            }
        }
        return JSON.stringify({dismissed: false});
    })()
    """
    dismiss_result = browser.evaluate(target_id, dismiss_js, timeout=5)
    try:
        dr = json.loads(dismiss_result) if isinstance(dismiss_result, str) else dismiss_result
        if dr and dr.get("dismissed"):
            console.print(f"[dim]  已关闭限频弹窗 ({dr.get('text', '')})[/dim]")
            time.sleep(1.5)
        elif dr:
            console.print(f"[yellow]  限频弹窗未关闭[/yellow]")
    except (json.JSONDecodeError, TypeError):
        pass

    # 等聊天弹窗/页面就绪
    time.sleep(1)

    # ── 第2步：导航到聊天页 ──
    browser.navigate(target_id, "https://www.zhipin.com/web/geek/chat")
    time.sleep(3)
    browser.wait_for_load(target_id, timeout=10)
    time.sleep(2)

    # ── 第3步：匹配会话 → 点击进入 ──
    hr_name_escaped = json.dumps(job.get("hr_name", ""))
    company_escaped = json.dumps(job.get("company", ""))
    match_js = f"""
    (() => {{
        const hrName = {hr_name_escaped};
        const company = {company_escaped};
        const items = document.querySelectorAll('li[role="listitem"]');
        if (items.length === 0) return JSON.stringify({{success: false, error: 'no_items'}});

        const allItems = [];
        for (const item of items) {{
            const nameEl = item.querySelector('.name-text');
            const nameText = nameEl?.textContent?.trim() || '';
            const spans = item.querySelectorAll('.name-box > span');
            const companyText = spans.length > 1 ? spans[1].textContent.trim() : '';
            allItems.push(nameText + '|' + companyText);
            if (nameText === hrName && companyText === company) {{
                const target = item.querySelector('.friend-content');
                if (target) {{
                    target.click();
                    return JSON.stringify({{success: true, matched: nameText + ' / ' + companyText}});
                }}
            }}
        }}
        return JSON.stringify({{success: false, error: 'no_match', hr_name: hrName, company: company, all: allItems}});
    }})()
    """
    match_result = browser.evaluate(target_id, match_js, timeout=10)
    try:
        mr = json.loads(match_result) if isinstance(match_result, str) else match_result
        if not mr or not mr.get("success"):
            all_convos = mr.get("all", []) if mr else []
            err_detail = mr.get("error", "unknown") if mr else "none"
            browser.close_tab(target_id)
            return False, f"匹配失败({err_detail}) hr={job.get('hr_name','?')} company={job.get('company','?')} convos={all_convos[:5]}"
    except (json.JSONDecodeError, TypeError):
        pass  # 非关键步骤，继续

    # 等会话内容加载
    time.sleep(2)
    # 确认 #chat-input 就绪
    for _ in range(15):
        ready = browser.evaluate(target_id, "!!document.querySelector('#chat-input')")
        if ready:
            break
        time.sleep(0.5)

    # ── 第4步：填消息 → 发送 ──
    greeting_escaped = json.dumps(greeting)
    send_js = f"""
    (async () => {{
        const input = document.querySelector('#chat-input');
        if (!input) return JSON.stringify({{success: false, error: 'no_chat_input'}});

        input.focus();
        document.execCommand('selectAll', false);
        document.execCommand('insertText', false, {greeting_escaped});

        await new Promise(r => setTimeout(r, 800));

        // 方式1：Vue handleSubmit
        let el = input;
        for (let i = 0; i < 15 && el; i++) {{
            if (el.__vue__) {{
                const vue = el.__vue__;
                vue._data.enableSubmit = true;
                vue.handleSubmit();
                await new Promise(r => setTimeout(r, 2000));
                return JSON.stringify({{success: true, method: 'vue'}});
            }}
            el = el.parentElement;
        }}

        // 方式2：点击发送按钮
        const sendBtn = document.querySelector('.btn-send');
        if (sendBtn && !sendBtn.classList.contains('disabled')) {{
            sendBtn.click();
            await new Promise(r => setTimeout(r, 2000));
            return JSON.stringify({{success: true, method: 'btn'}});
        }}

        return JSON.stringify({{success: false, error: 'no_send_method'}});
    }})()
    """
    result = browser.evaluate(target_id, send_js, timeout=15)
    browser.close_tab(target_id)

    if not result:
        return False, "发送 JS 无返回"
    try:
        data = json.loads(result) if isinstance(result, str) else result
        if data.get("success"):
            return True, ""
        return False, data.get("error", "未知错误")
    except (json.JSONDecodeError, TypeError):
        return False, f"解析失败: {str(result)[:100]}"


# ── 轻触模式 ──────────────────────────────────────────

def touch_job(browser, job: dict) -> bool:
    """打开岗位页 → 点立即沟通（发默认招呼语）→ 关闭。不导航、不填消息。"""
    target_id = browser.new_tab(job["url"])
    if not target_id:
        return False

    time.sleep(2)
    browser.wait_for_load(target_id, timeout=10)

    click_js = """
    (() => {
        const btn = document.querySelector('.btn-startchat, .op-btn-chat, [ka*="chat"]');
        if (!btn) return JSON.stringify({success: false, error: 'no_button'});
        btn.click();
        return JSON.stringify({success: true});
    })()
    """
    result = browser.evaluate(target_id, click_js, timeout=10)
    time.sleep(2)

    # 关闭"今天已联系过 N 位"的限频弹窗（弹窗是 position:fixed，offsetParent 不可用）
    dismiss_js = """
    (() => {
        const isVisible = (el) => {
            const r = el.getBoundingClientRect();
            return r.width > 0 && r.height > 0;
        };
        for (const popSel of ['.boss-popup__wrapper', '[class*="popup"]', '[class*="dialog"]', '[class*="modal"]', '[class*="overlay"]', '.van-popup', '.van-dialog']) {
            const popup = document.querySelector(popSel);
            if (!popup || !isVisible(popup)) continue;
            const text = popup.textContent;
            if (!text.includes('联系过')) continue;
            const btns = popup.querySelectorAll('span, button, a, div');
            for (const btn of btns) {
                const t = btn.textContent.trim();
                if ((t === '好' || t === '好的' || t === '确定' || t === '确认' || t === '我知道了') && isVisible(btn)) {
                    btn.click();
                    return JSON.stringify({dismissed: true, text: t, selector: popSel});
                }
            }
            for (let i = btns.length - 1; i >= 0; i--) {
                if (isVisible(btns[i]) && btns[i].textContent.trim().length <= 10) {
                    btns[i].click();
                    return JSON.stringify({dismissed: true, text: btns[i].textContent.trim(), fallback: true});
                }
            }
        }
        return JSON.stringify({dismissed: false});
    })()
    """
    browser.evaluate(target_id, dismiss_js, timeout=5)

    time.sleep(2)  # 等默认招呼语发出 + 弹窗关闭
    browser.close_tab(target_id)

    if not result:
        return False
    try:
        data = json.loads(result) if isinstance(result, str) else result
        return data.get("success", False)
    except (json.JSONDecodeError, TypeError):
        return False
