"""
聊天模块 — 检测未读消息 + 读取对话 + 发消息

BOSS 聊天页 DOM 结构（已探明）:
  li[role="listitem"]                — 会话条目
    .name-text                       — HR 名字
    .name-box > span:nth-child(2)    — 公司名
    .notice-badge                    — 未读红泡泡 (span, 17x17, #FE574A)
    .friend-content                  — 可点击进入会话
    #chat-input                      — 消息输入框
"""

import json
import sys
import time

from browser import evaluate, navigate, wait_for_load

# Rich 在 Windows GBK 终端下会炸非 ASCII 字符，API 层不打印，CLI 用 print
try:
    from rich.console import Console
    console = Console()
except Exception:
    console = None


# ── 检测未读消息（精简版，直接用已知 selector）────────────

DETECT_UNREAD_JS = """
(() => {
    const items = document.querySelectorAll('li[role="listitem"]');
    if (items.length === 0) return JSON.stringify({success: false, error: 'no_items'});

    const results = [];
    for (const item of items) {
        const nameEl = item.querySelector('.name-text');
        const name = nameEl?.textContent?.trim() || '';

        const spans = item.querySelectorAll('.name-box > span');
        const company = spans.length > 1 ? spans[1].textContent.trim() : '';

        const msgEl = item.querySelector('.msg-text, .last-msg, .chat-preview, .message-preview');
        const lastMsg = msgEl?.textContent?.trim() || '';

        const timeEl = item.querySelector('.time, .msg-time, .chat-time');
        const time = timeEl?.textContent?.trim() || '';

        // 核心: .notice-badge = 未读红泡泡
        const badge = item.querySelector('.notice-badge');
        const unreadCount = badge ? parseInt(badge.textContent.trim(), 10) || 1 : 0;

        results.push({
            name,
            company,
            last_msg: lastMsg,
            time,
            unread: unreadCount > 0,
            unread_count: unreadCount,
        });
    }

    const unread = results.filter(r => r.unread);
    return JSON.stringify({
        success: true,
        total: results.length,
        unread_count: unread.length,
        conversations: results,
    });
})()
"""


# ── 公开 API ──────────────────────────────────────────

def detect_unread(target_id: str | None = None, timeout: int = 15) -> dict:
    """检测未读会话。target_id=None 则自动找 BOSS tab。

    Returns:
        {"success": True, "total": 40, "unread_count": 5,
         "conversations": [{"name": "张HR", "company": "XX科技",
                            "last_msg": "...", "time": "10:30",
                            "unread": True, "unread_count": 1}, ...]}
    """
    from browser import find_boss_tab

    if not target_id:
        target_id = find_boss_tab()
        if not target_id:
            return {"success": False, "error": "no_boss_tab"}

    # 确保在聊天页
    page_url = evaluate(target_id, "window.location.href", timeout=5)
    if not page_url or "web/geek/chat" not in str(page_url):
        navigate(target_id, "https://www.zhipin.com/web/geek/chat")
        time.sleep(3)
        wait_for_load(target_id, timeout=10)
        time.sleep(2)

    # 等 Vue 渲染聊天列表
    for _ in range(10):
        has = evaluate(target_id, "!!document.querySelector('li[role=\"listitem\"]')", timeout=5)
        if has:
            break
        time.sleep(1)

    raw = evaluate(target_id, DETECT_UNREAD_JS, timeout=timeout)
    if not raw:
        return {"success": False, "error": "js_no_result"}

    try:
        return json.loads(raw) if isinstance(raw, str) else raw
    except (json.JSONDecodeError, TypeError):
        return {"success": False, "error": f"json_parse_error: {str(raw)[:100]}"}


def list_unread(target_id: str | None = None) -> list[dict]:
    """只返回未读会话列表"""
    r = detect_unread(target_id)
    if not r.get("success"):
        return []
    return [c for c in r.get("conversations", []) if c.get("unread")]


# ── 读取消息 ──────────────────────────────────────────

READ_MESSAGES_JS = """
(() => {
    const items = document.querySelectorAll('.message-item');
    if (items.length === 0) return JSON.stringify({success: false, error: 'no_messages'});

    const messages = [];
    for (const item of items) {
        const isMe = item.classList.contains('item-myself');
        const isFriend = item.classList.contains('item-friend');

        const textEl = item.querySelector('.text-content');
        const text = textEl?.textContent?.trim() || '';

        const timeEl = item.querySelector('.time');
        const time = timeEl?.textContent?.trim() || '';

        // 跳过系统卡片（PK竞争、简历查看通知等）
        const hasCard = item.querySelector('.card-btn');
        if (hasCard && !text) continue;

        // 跳过纯"已读"标记（没有实际内容）
        if (!text || text === '已读') continue;

        let role = 'system';
        if (isMe) role = 'me';
        else if (isFriend) role = 'hr';

        messages.push({role, text, time});
    }
    return JSON.stringify({success: true, count: messages.length, messages});
})()
"""


def read_messages(target_id: str, name: str, company: str = "") -> list[dict]:
    """点击进入指定会话，读取最近消息。返回 [{role, text, time}, ...]

    role: 'me' | 'hr' | 'system'
    """
    # 点击进入会话
    name_escaped = json.dumps(name)
    company_escaped = json.dumps(company)
    click_js = f"""
    (() => {{
        const name = {name_escaped};
        const company = {company_escaped};
        const items = document.querySelectorAll('li[role="listitem"]');
        for (const item of items) {{
            const nameText = item.querySelector('.name-text')?.textContent?.trim() || '';
            const spans = item.querySelectorAll('.name-box > span');
            const companyText = spans.length > 1 ? spans[1].textContent.trim() : '';
            if (nameText === name && (!company || companyText === company)) {{
                const target = item.querySelector('.friend-content');
                if (target) {{
                    target.click();
                    return JSON.stringify({{success: true}});
                }}
            }}
        }}
        return JSON.stringify({{success: false, error: 'not_found'}});
    }})()
    """
    r = evaluate(target_id, click_js, timeout=10)
    try:
        if not json.loads(r).get("success"):
            return []
    except Exception:
        return []

    time.sleep(2)

    # 验证会话确实切换了——检查消息面板是否包含预期内容
    verify = evaluate(target_id, """
    (() => {
        // 等消息加载
        const items = document.querySelectorAll('.message-item');
        if (items.length === 0) return false;
        // 消息列表有内容且不是加载中
        return items[0].textContent.trim().length > 0;
    })()
    """, timeout=5)
    if not verify:
        # 可能没切过去，再等一会
        time.sleep(2)

    # 等消息加载
    for _ in range(10):
        has = evaluate(target_id, "!!document.querySelector('.message-item')", timeout=5)
        if has:
            break
        time.sleep(0.5)

    raw = evaluate(target_id, READ_MESSAGES_JS, timeout=10)
    if not raw:
        return []
    try:
        data = json.loads(raw)
        return data.get("messages", [])
    except Exception:
        return []


# ── AI 回复生成 ────────────────────────────────────────

REPLY_PROMPT = """你是一位求职者，正在BOSS直聘上和HR聊天。根据上下文生成一条自然的回复。

## 你的简历
{resume}

## 会话历史（按时间顺序）
{history}

## 语气要求（重要）
- 不要用"特别兴奋""非常感兴趣""很激动""太好了"等情绪词
- 不要夸公司、不要客套话
- 像给同事回微信，有事说事
- 不主动提"我热爱""我对XX充满热情"
- 不用感叹号

## 格式要求
1. 口语化，30-120字
2. 根据HR最新消息直接回应
3. 从简历里找最相关的真实经验来回应，不捏造
4. 如果是加微信/约面试：简单接受即可，不用过度配合（如"好的，我的微信是xxx"）
5. 只输出回复文本，不要解释"""


def generate_reply(client, model: str, resume: str, messages: list[dict]) -> str | None:
    """根据会话历史生成回复"""
    if not messages:
        return None

    # 格式化历史
    lines = []
    for m in messages[-10:]:  # 最近10条
        tag = "我" if m["role"] == "me" else ("HR" if m["role"] == "hr" else "系统")
        lines.append(f"[{tag}] {m['text']}")
    history = "\n".join(lines)

    prompt = REPLY_PROMPT.format(resume=resume[:2000], history=history)

    try:
        resp = client.chat.completions.create(
            model=model,
            messages=[{"role": "user", "content": prompt}],
            temperature=0.7,
            max_tokens=400,
        )
        return resp.choices[0].message.content.strip()
    except Exception as e:
        return f"[生成失败: {e}]"


# ── 发简历（CDP 鼠标事件，isTrusted=true）──────────────

def send_resume(target_id: str) -> bool:
    """在当前会话中发送附件简历（选最新的那个）。

    使用 CDP Input.dispatchMouseEvent 因为 Vue 组件只响应真实鼠标事件。
    """
    from browser import _attach, _send_cdp

    sid = _attach(target_id)

    # Step 1: 点"发简历"按钮
    pos = evaluate(target_id, """
    (() => {
        const ctrls = document.querySelector(".chat-controls");
        if (!ctrls) return JSON.stringify({error: "no controls"});
        const btn = [...ctrls.children].find(c => c.textContent.trim().startsWith("发简历"));
        if (!btn) return JSON.stringify({error: "no resume btn"});
        const child = btn.querySelector(".toolbar-btn") || btn;
        const r = child.getBoundingClientRect();
        return JSON.stringify({x: Math.round(r.left + r.width/2), y: Math.round(r.top + r.height/2)});
    })()
    """, timeout=5)

    try:
        btn_pos = json.loads(pos)
    except Exception:
        return False
    if "error" in btn_pos:
        return False

    _send_cdp("Input.dispatchMouseEvent", {
        "type": "mousePressed", "x": btn_pos["x"], "y": btn_pos["y"],
        "button": "left", "clickCount": 1,
    }, session_id=sid, timeout=5)
    _send_cdp("Input.dispatchMouseEvent", {
        "type": "mouseReleased", "x": btn_pos["x"], "y": btn_pos["y"],
        "button": "left", "clickCount": 1,
    }, session_id=sid, timeout=5)
    time.sleep(1.5)

    # Step 2: 选第一个简历
    pos2 = evaluate(target_id, """
    (() => {
        const first = document.querySelector(".resume-list .list-item");
        if (!first) return JSON.stringify({error: "no resume items"});
        const r = first.getBoundingClientRect();
        return JSON.stringify({x: Math.round(r.left + r.width/2), y: Math.round(r.top + r.height/2)});
    })()
    """, timeout=5)

    try:
        item_pos = json.loads(pos2)
    except Exception:
        return False
    if "error" in item_pos:
        return False

    _send_cdp("Input.dispatchMouseEvent", {
        "type": "mousePressed", "x": item_pos["x"], "y": item_pos["y"],
        "button": "left", "clickCount": 1,
    }, session_id=sid, timeout=5)
    _send_cdp("Input.dispatchMouseEvent", {
        "type": "mouseReleased", "x": item_pos["x"], "y": item_pos["y"],
        "button": "left", "clickCount": 1,
    }, session_id=sid, timeout=5)
    time.sleep(0.5)

    # Step 3: 点"发送"
    pos3 = evaluate(target_id, """
    (() => {
        const btn = document.querySelector(".boss-popup__wrapper .btn-confirm, .dialog-wrap.active .btn-confirm");
        if (!btn) return JSON.stringify({error: "no send btn"});
        const r = btn.getBoundingClientRect();
        return JSON.stringify({x: Math.round(r.left + r.width/2), y: Math.round(r.top + r.height/2), disabled: btn.classList.contains("disabled")});
    })()
    """, timeout=5)

    try:
        send_pos = json.loads(pos3)
    except Exception:
        return False
    if send_pos.get("disabled"):
        return False

    _send_cdp("Input.dispatchMouseEvent", {
        "type": "mousePressed", "x": send_pos["x"], "y": send_pos["y"],
        "button": "left", "clickCount": 1,
    }, session_id=sid, timeout=5)
    _send_cdp("Input.dispatchMouseEvent", {
        "type": "mouseReleased", "x": send_pos["x"], "y": send_pos["y"],
        "button": "left", "clickCount": 1,
    }, session_id=sid, timeout=5)
    time.sleep(2)

    # Step 4: 不管弹窗是否关了，先关掉残留弹窗，然后导航回聊天页确保状态干净
    # 防止 CDP 操作搞乱页面导致后续消息发错会话
    evaluate(target_id, """
    (() => {
        // 点关闭按钮（如果有的话）
        const closeBtn = document.querySelector(".boss-popup__close, .dialog-wrap.active .icon-close");
        if (closeBtn) closeBtn.click();
        // 也试试 ESC
        document.dispatchEvent(new KeyboardEvent("keydown", {key: "Escape", bubbles: true}));
    })()
    """, timeout=5)
    time.sleep(0.5)

    # 导航回聊天页重置状态
    from browser import navigate as _nav, wait_for_load as _wfl
    _nav(target_id, "https://www.zhipin.com/web/geek/chat")
    time.sleep(2)
    _wfl(target_id, timeout=10)
    time.sleep(1)

    # 验证：看聊天记录里是否有简历发送确认（从之前会话的last-msg-text判断）
    confirmed = evaluate(target_id, """
    (() => {
        const items = document.querySelectorAll("li[role='listitem']");
        for (const item of items) {
            const text = item.textContent || "";
            if (text.includes("已发送给Boss") || text.includes("附件简历")) return true;
        }
        return false;
    })()
    """, timeout=5)

    return bool(confirmed)


# ── CLI: python boss/chat.py ────────────────────────────

def _print(s: str = "") -> None:
    """print that won't die on Windows GBK"""
    try:
        print(s)
    except UnicodeEncodeError:
        print(s.encode("ascii", "replace").decode("ascii"))


if __name__ == "__main__":
    from browser import check_chrome_connection, find_boss_tab

    _print("=== BOSS unread detection ===")

    if not check_chrome_connection():
        _print("[ERROR] Chrome not connected. Start Chrome with --remote-debugging-port=9222")
        sys.exit(1)

    target_id = find_boss_tab()
    if not target_id:
        _print("[ERROR] No BOSS tab found. Open zhipin.com in Chrome first.")
        sys.exit(1)

    _print("[OK] Browser ready, scanning...")

    result = detect_unread(target_id)

    if not result.get("success"):
        _print(f"[ERROR] {result.get('error', '?')}")
        sys.exit(1)

    total = result.get("total", 0)
    unread_count = result.get("unread_count", 0)

    _print(f"\nTotal: {total} conversations, {unread_count} unread")

    unread = [c for c in result.get("conversations", []) if c["unread"]]
    if unread:
        _print(f"\n--- Unread ({unread_count}) ---")
        for i, c in enumerate(unread, 1):
            _print(f"  {i}. {c['name']} | {c['company']}")
            _print(f"     {c['last_msg'][:80]}")
            _print(f"     unread: {c['unread_count']} | time: {c['time']}")
    else:
        _print("\nNo unread messages.")
        # show first few for debug
        all_convos = result.get("conversations", [])
        if all_convos:
            _print(f"\nFirst {min(3, len(all_convos))} conversations:")
            for c in all_convos[:3]:
                _print(f"  {c['name']} | {c['company']} | {c['last_msg'][:40]}")
