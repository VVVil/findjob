"""
智联投递模块 — 点击"立即投递" → 弹窗点"投递简历" → 关闭
（智联不能发消息，只能投简历，默认选在线简历）
"""

import json
import time

from rich.console import Console

console = Console()

# ── JS 脚本 ──────────────────────────────────────────

JS_CLICK_SUBMIT = """
(() => {
    const btn = document.querySelector('.summary-planes__action .a-button.a--bordered.a--filled');
    if (!btn) {
        // 可能已经投过了
        const applied = document.querySelector('.summary-planes__action .a-button[disabled]');
        if (applied && applied.textContent.includes('已投递')) {
            return JSON.stringify({success: false, error: 'already_applied'});
        }
        return JSON.stringify({success: false, error: 'no_submit_button'});
    }
    if (btn.disabled) {
        return JSON.stringify({success: false, error: 'button_disabled'});
    }
    btn.click();
    return JSON.stringify({success: true});
})()
"""

JS_CLICK_DELIVERY = """
(() => {
    // 等弹窗出现
    const dialog = document.querySelector('.a-dialog');
    if (!dialog) return JSON.stringify({success: false, error: 'no_dialog'});

    // 默认已选中在线简历（带 resume-select class），直接点投递
    const deliveryBtn = dialog.querySelector('.a-attachment-select__action-btn__delivery');
    if (!deliveryBtn) return JSON.stringify({success: false, error: 'no_delivery_button'});

    deliveryBtn.click();
    return JSON.stringify({success: true});
})()
"""


# ── 投递 ─────────────────────────────────────────────

def apply_job(browser, job: dict) -> bool:
    """打开智联岗位详情页 → 点"立即投递" → 弹窗点"投递简历" → 关闭。
    返回 True 表示投递成功。
    """
    url = job.get("url", "")
    if not url:
        console.print("  [red]✗ 无 URL[/red]")
        return False

    company = job.get("company", "?")[:15]
    title = job.get("title", "?")[:20]
    console.print(f"  [dim]📨 {company} - {title}[/dim]")

    target_id = browser.new_tab(url)
    if not target_id:
        console.print(f"  [red]✗ 打不开页面[/red]")
        return False

    time.sleep(2)
    browser.wait_for_load(target_id, timeout=15)
    time.sleep(1)

    # ── 第1步：点击"立即投递" ──
    result = browser.evaluate(target_id, JS_CLICK_SUBMIT, timeout=10)
    if not result:
        browser.close_tab(target_id)
        console.print(f"  [red]✗ 点击投递按钮无返回[/red]")
        return False

    try:
        r = json.loads(result) if isinstance(result, str) else result
    except (json.JSONDecodeError, TypeError):
        browser.close_tab(target_id)
        console.print(f"  [red]✗ step1 解析失败[/red]")
        return False

    if not r.get("success"):
        err = r.get("error", "unknown")
        browser.close_tab(target_id)
        if err == "already_applied":
            console.print(f"  [yellow]⊘ 已投递过[/yellow]")
        else:
            console.print(f"  [yellow]⊘ {err}[/yellow]")
        return False

    # 等弹窗出现
    time.sleep(2)

    # ── 第2步：弹窗中点击"投递简历" ──
    result2 = browser.evaluate(target_id, JS_CLICK_DELIVERY, timeout=10)

    # 解析 step2 结果
    step2_ok = False
    step2_err = "unknown"
    if result2:
        try:
            r2 = json.loads(result2) if isinstance(result2, str) else result2
            if r2.get("success"):
                step2_ok = True
            else:
                step2_err = r2.get("error", "unknown")
        except (json.JSONDecodeError, TypeError):
            step2_err = "parse_error"

    if not step2_ok:
        # 新版智联可能没有弹窗——点"立即投递"后直接投了。
        # Step 1 已成功点击，检测按钮是否变成"已投递"确认实际结果
        confirm = browser.evaluate(target_id, """
        (() => {
            const btn = document.querySelector('.summary-planes__action .a-button[disabled]');
            if (btn && btn.textContent.includes('已投递')) return 'applied';
            const btn2 = document.querySelector('.summary-planes__action .a-button');
            if (btn2 && btn2.textContent.includes('已投递')) return 'applied';
            return 'unknown';
        })()
        """, timeout=5)
        if confirm and "applied" in str(confirm):
            step2_ok = True  # 实际已投递，没有弹窗而已
        else:
            browser.close_tab(target_id)
            console.print(f"  [yellow]⊘ 弹窗: {step2_err}[/yellow]")
            return False

    # 等投递完成
    time.sleep(2)
    browser.close_tab(target_id)
    console.print(f"  [green]✓ 已投递[/green]")
    return True
