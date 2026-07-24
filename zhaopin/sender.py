"""
智联投递模块 — 点击"立即投递" → 弹窗点"投递简历" → 关闭
（智联不能发消息，只能投简历，默认选在线简历）
"""

import json
import time

from rich.console import Console

from browser import new_tab, close_tab, evaluate, wait_for_load

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

def apply_job(job: dict) -> bool:
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

    target_id = new_tab(url)
    if not target_id:
        console.print(f"  [red]✗ 打不开页面[/red]")
        return False

    time.sleep(2)
    wait_for_load(target_id, timeout=15)
    time.sleep(1)

    # ── 第1步：点击"立即投递" ──
    result = evaluate(target_id, JS_CLICK_SUBMIT, timeout=10)
    if not result:
        close_tab(target_id)
        console.print(f"  [red]✗ 点击投递按钮无返回[/red]")
        return False

    try:
        r = json.loads(result) if isinstance(result, str) else result
    except (json.JSONDecodeError, TypeError):
        close_tab(target_id)
        console.print(f"  [red]✗ step1 解析失败[/red]")
        return False

    if not r.get("success"):
        err = r.get("error", "unknown")
        close_tab(target_id)
        if err == "already_applied":
            console.print(f"  [yellow]⊘ 已投递过[/yellow]")
        else:
            console.print(f"  [yellow]⊘ {err}[/yellow]")
        return False

    # 等弹窗出现
    time.sleep(2)

    # ── 第2步：弹窗中点击"投递简历" ──
    result2 = evaluate(target_id, JS_CLICK_DELIVERY, timeout=10)
    if not result2:
        close_tab(target_id)
        console.print(f"  [red]✗ 点击投递简历无返回[/red]")
        return False

    try:
        r2 = json.loads(result2) if isinstance(result2, str) else result2
    except (json.JSONDecodeError, TypeError):
        close_tab(target_id)
        console.print(f"  [red]✗ step2 解析失败[/red]")
        return False

    if not r2.get("success"):
        err = r2.get("error", "unknown")
        close_tab(target_id)
        console.print(f"  [yellow]⊘ 弹窗: {err}[/yellow]")
        return False

    # 等投递完成
    time.sleep(2)
    close_tab(target_id)
    console.print(f"  [green]✓ 已投递[/green]")
    return True
