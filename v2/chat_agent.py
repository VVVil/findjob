#!/usr/bin/env python3
"""
chat_agent — BOSS 直聘聊天守护进程
轮询未读 → 逐个审核 → AI 生成回复/发简历 → 发送

用法:
  python chat_agent.py                # 默认 3 分钟轮询
  python chat_agent.py -i 5           # 5 分钟轮询
  python chat_agent.py --once         # 只跑一轮
"""

import argparse
import hashlib
import json
import sys
import threading
import time
from datetime import datetime
from pathlib import Path

import yaml
from dotenv import load_dotenv
from openai import OpenAI
from rich.console import Console
from rich.prompt import Prompt

from browser import check_chrome_connection, find_boss_tab, _attach, _send_cdp
from boss.chat import detect_unread, read_messages, generate_reply, send_resume

console = Console()
HERE = Path(__file__).resolve().parent
STATE_FILE = HERE / "output" / "chat_state.json"


# ══════════════════════════════════════════════════════
#  状态管理
# ══════════════════════════════════════════════════════

def load_state() -> dict:
    """加载处理状态"""
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text(encoding="utf-8"))
        except Exception:
            return {}
    return {}


def save_state(state: dict) -> None:
    """保存处理状态"""
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


def conv_key(name: str, company: str) -> str:
    """会话唯一键"""
    return f"{name}@{company}"


def last_hr_msg_hash(messages: list[dict]) -> str:
    """取最后一条 HR 消息的 hash，用于判断是否有新消息"""
    hr_msgs = [m for m in messages if m["role"] == "hr"]
    if not hr_msgs:
        return ""
    return hashlib.md5(hr_msgs[-1]["text"].encode()).hexdigest()[:12]


def is_new_message(state: dict, name: str, company: str, messages: list[dict]) -> bool:
    """判断是否有新消息（跟上次处理过的 hash 比较）"""
    key = conv_key(name, company)
    current_hash = last_hr_msg_hash(messages)
    last_hash = state.get(key, {}).get("last_hr_msg_hash", "")
    return current_hash != last_hash or not last_hash


def mark_processed(state: dict, name: str, company: str, messages: list[dict]) -> dict:
    """标记会话已处理"""
    key = conv_key(name, company)
    state[key] = {
        "last_hr_msg_hash": last_hr_msg_hash(messages),
        "last_processed_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }
    return state


# ══════════════════════════════════════════════════════
#  简历是否已发判断
# ══════════════════════════════════════════════════════

def has_sent_resume(messages: list[dict]) -> bool:
    """从消息历史判断是否已发过附件简历"""
    sent_keywords = ["已发送给Boss", "对方已查看了您的附件简历", "您的附件简历"]
    for m in messages:
        text = m.get("text", "")
        if any(kw in text for kw in sent_keywords):
            return True
    return False


# ══════════════════════════════════════════════════════
#  加载配置和 AI 客户端
# ══════════════════════════════════════════════════════

def load_config():
    cfg_path = HERE / "config.yaml"
    if cfg_path.exists():
        return yaml.safe_load(cfg_path.read_text(encoding="utf-8"))
    return {}


def load_resume(cfg: dict) -> str:
    resume_cfg = cfg.get("resume_path", "../resume/resume.md")
    resume_path = Path(resume_cfg)
    if not resume_path.is_absolute():
        resume_path = HERE / resume_path
    if resume_path.exists():
        return resume_path.read_text(encoding="utf-8")
    return ""


def init_client(cfg: dict) -> OpenAI:
    import os
    env_paths = [HERE / ".env", HERE.parent / ".env"]
    for p in env_paths:
        if p.exists():
            load_dotenv(p)
    ai_cfg = cfg.get("ai", {})
    api_key = os.getenv("DEEPSEEK_API_KEY") or os.getenv("OPENAI_API_KEY") or ai_cfg.get("api_key", "")
    base_url = os.getenv("OPENAI_BASE_URL") or ai_cfg.get("base_url", "https://api.deepseek.com/v1")
    if not api_key:
        console.print("[red]Missing API key! Set DEEPSEEK_API_KEY in .env[/red]")
        sys.exit(1)
    return OpenAI(api_key=api_key, base_url=base_url)


# ══════════════════════════════════════════════════════
#  交互：会话选择
# ══════════════════════════════════════════════════════

def select_conversations(unread: list[dict], timeout: float | None = None) -> list[dict]:
    """展示未读列表，让用户选择要处理哪些。
    若 timeout 秒内无输入 → 自动跳过，进入下一轮轮询。
    """
    console.print(f"\n[bold cyan]═══ {len(unread)} 个未读会话 ═══[/bold cyan]\n")

    for i, c in enumerate(unread, 1):
        console.print(f"  [{i}] [bold]{c['name']}[/bold] | {c['company']}")
        console.print(f"      {c['last_msg'][:80]}")
        console.print(f"      [dim]{c['time']}[/dim]")
    console.print()

    console.print(f"[dim]a=全部 / 1,3=选第1和第3 / q=跳过本轮 / 不输入={timeout:.0f}s后自动跳过[/dim]")

    # 用 daemon 线程读输入，主线程等 timeout 秒后放弃
    choice_holder = ["__timeout__"]
    def _read_input():
        try:
            choice_holder[0] = Prompt.ask("[bold]处理哪些?[/bold]", default="a")
        except (EOFError, KeyboardInterrupt):
            choice_holder[0] = "q"

    t = threading.Thread(target=_read_input, daemon=True)
    t.start()
    t.join(timeout=timeout)

    if t.is_alive():
        # 用户没反应，自动跳过。daemon 线程留在后台等不到输入自然死
        console.print(f"\n[yellow]  ({timeout:.0f}s无输入，自动跳过本轮)[/yellow]")
        return []

    choice = choice_holder[0].strip().lower()
    if choice == "q":
        return []
    if choice == "a":
        return unread

    # 解析数字
    try:
        indices = [int(x.strip()) for x in choice.split(",")]
        return [unread[i - 1] for i in indices if 1 <= i <= len(unread)]
    except (ValueError, IndexError):
        console.print("[yellow]输入无效，跳过本轮[/yellow]")
        return []


# ══════════════════════════════════════════════════════
#  交互：审核单条回复
# ══════════════════════════════════════════════════════

def review_one(target_id: str, conv: dict, messages: list[dict],
               reply: str, resume_already_sent: bool,
               total: int, current: int) -> str | None:
    """审核一条回复。返回: 'send' | 'send+resume' | 'skip' | None(异常)"""

    console.print(f"\n[bold cyan]── [{current}/{total}] {conv['name']} | {conv['company']} ──[/bold cyan]")

    # 对话历史
    console.print("[dim]── 对话历史 ──[/dim]")
    for m in messages[-8:]:
        tag = "[bold blue]我[/bold blue]" if m["role"] == "me" else ("[bold green]HR[/bold green]" if m["role"] == "hr" else "[dim]系统[/dim]")
        console.print(f"  {tag} {m['text'][:120]}")

    # AI 回复
    console.print(f"\n[bold]── 建议回复 ──[/bold]")
    console.print(f"[green]{reply}[/green]")

    # 建议动作
    console.print(f"\n[bold]── 建议动作 ──[/bold]")
    if resume_already_sent:
        console.print("[dim]  [发简历] 已发送过[/dim]")
    else:
        console.print("  [bold yellow][发简历][/bold yellow] 尚未发送附件简历")

    # 让用户选
    name = conv["name"]

    if resume_already_sent:
        action = Prompt.ask(
            "  [bold]y=发送回复  n=跳过  e=编辑[/bold]",
            choices=["y", "n", "e"],
            default="y",
        )
        if action == "n":
            return "skip"
        if action == "e":
            reply = Prompt.ask("  修改回复", default=reply)
        # 发送回复
        ok = _send_message(target_id, reply, expected_name=name)
        if ok:
            console.print("[green]  [OK] 已发送[/green]")
            return "send"
        else:
            console.print("[red]  [FAIL] 发送失败[/red]")
            return None
    else:
        action = Prompt.ask(
            "  [bold]y=发送回复  n=跳过  e=编辑  r=回复+发简历[/bold]",
            choices=["y", "n", "e", "r"],
            default="y",
        )
        if action == "n":
            return "skip"
        if action == "e":
            reply = Prompt.ask("  修改回复", default=reply)
            # fall through to send
        if action in ("y", "e"):
            ok = _send_message(target_id, reply, expected_name=name)
            if ok:
                console.print("[green]  [OK] 已发送[/green]")
                return "send"
            else:
                console.print("[red]  [FAIL] 发送失败[/red]")
                return None
        if action == "r":
            # 先发回复
            ok = _send_message(target_id, reply, expected_name=name)
            if ok:
                console.print("[green]  [OK] 回复已发送[/green]")
            else:
                console.print("[red]  [FAIL] 回复发送失败[/red]")
                return None
            # 再发简历
            time.sleep(1)
            console.print("[dim]  正在发送简历...[/dim]")
            if send_resume(target_id):
                console.print("[green]  [OK] 简历已发送[/green]")
                return "send+resume"
            else:
                console.print("[yellow]  [WARN] 简历发送失败（回复已发）[/yellow]")
                return "send"


def _send_message(target_id: str, text: str, expected_name: str = "") -> bool:
    """在当前会话的聊天输入框填入文本并发送。
    expected_name 不为空时会验证当前会话是否正确。"""
    import json as _json
    from browser import evaluate as _evaluate

    # 安全验证: 确认当前会话标题包含 expected_name
    if expected_name:
        verify = _evaluate(target_id, f"""
        (() => {{
            // 找聊天头部的 .name-text（排除侧边栏列表里的）
            const headers = document.querySelectorAll('.name-text');
            const expected = {_json.dumps(expected_name)};
            for (const h of headers) {{
                const text = h.textContent.trim();
                // 排除侧边栏 li 里的，只留当前聊天窗口顶部
                if (text === expected && !h.closest('[role="listitem"]')) return true;
                if (text === expected && h.offsetParent !== null && h.closest('.chat-window, .im-window')) return true;
            }}
            // 宽松匹配：任意 .name-text 内容匹配即可（新 tab 可能还没带侧边栏）
            for (const h of headers) {{
                if (h.textContent.trim() === expected) return true;
            }}
            return false;
        }})()
        """, timeout=5)
        if not verify:
            console.print(f"[red]  [ABORT] 当前会话不是 {expected_name}，发送取消[/red]")
            return False

    greeting_escaped = _json.dumps(text)
    js = f"""
    (async () => {{
        const input = document.querySelector('#chat-input');
        if (!input) return JSON.stringify({{success: false, error: 'no_input'}});

        input.focus();
        document.execCommand('selectAll', false);
        document.execCommand('insertText', false, {greeting_escaped});

        await new Promise(r => setTimeout(r, 600));

        // Vue handleSubmit
        let el = input;
        for (let i = 0; i < 15 && el; i++) {{
            if (el.__vue__) {{
                const vue = el.__vue__;
                vue._data.enableSubmit = true;
                vue.handleSubmit();
                await new Promise(r => setTimeout(r, 1500));
                return JSON.stringify({{success: true, method: 'vue'}});
            }}
            el = el.parentElement;
        }}

        // fallback: click send btn
        const sendBtn = document.querySelector('.btn-send');
        if (sendBtn && !sendBtn.classList.contains('disabled')) {{
            sendBtn.click();
            await new Promise(r => setTimeout(r, 1500));
            return JSON.stringify({{success: true, method: 'btn'}});
        }}

        return JSON.stringify({{success: false, error: 'no_send_method'}});
    }})()
    """
    result = _evaluate(target_id, js, timeout=15)
    if not result:
        return False
    try:
        return _json.loads(result).get("success", False)
    except Exception:
        return False


# ══════════════════════════════════════════════════════
#  主循环
# ══════════════════════════════════════════════════════

def main():
    parser = argparse.ArgumentParser(description="chat_agent — BOSS 聊天守护进程")
    parser.add_argument("-i", "--interval", type=int, default=3, help="轮询间隔（分钟），默认 3")
    parser.add_argument("--once", action="store_true", help="只跑一轮，不循环")
    args = parser.parse_args()

    cfg = load_config()
    resume = load_resume(cfg)
    client = init_client(cfg)
    model = cfg.get("ai", {}).get("model", "deepseek-chat")

    console.print(f"[green]Resume: {len(resume)} chars[/green]")

    if not check_chrome_connection():
        console.print("[red]Chrome not connected![/red]")
        sys.exit(1)

    target_id = find_boss_tab()
    if not target_id:
        console.print("[red]No BOSS tab found![/red]")
        sys.exit(1)

    # 激活 tab（确保 getBoundingClientRect 正常）
    from browser import _attach as _att, _send_cdp as _scdp
    sid = _att(target_id)
    _scdp("Target.activateTarget", {"targetId": target_id}, session_id=sid, timeout=5)

    state = load_state()
    interval_sec = args.interval * 60

    console.print(f"[bold cyan]chat_agent started[/bold cyan] (interval={args.interval}m, model={model})")
    console.print("[dim]Ctrl+C to exit[/dim]")

    try:
        while True:
            console.print(f"\n[dim]--- Polling at {datetime.now().strftime('%H:%M:%S')} ---[/dim]")

            # 1. 检测未读
            result = detect_unread(target_id)
            if not result.get("success"):
                console.print(f"[yellow]Detection failed: {result.get('error', '?')}[/yellow]")
                if args.once:
                    break
                time.sleep(interval_sec)
                continue

            unread = [c for c in result.get("conversations", []) if c.get("unread")]
            if not unread:
                console.print("[dim]No unread messages[/dim]")
                if args.once:
                    break
                time.sleep(interval_sec)
                continue

            # 2. 用户选择
            selected = select_conversations(unread, timeout=interval_sec)
            if not selected:
                if args.once:
                    break
                time.sleep(interval_sec)
                continue

            # 3. 逐个处理
            for i, conv in enumerate(selected, 1):
                name = conv["name"]
                company = conv["company"]

                # 读消息
                console.print(f"[dim]  Reading {name}...[/dim]")
                messages = read_messages(target_id, name, company)
                if not messages:
                    console.print(f"[yellow]  Failed to read messages for {name}[/yellow]")
                    continue

                # 去重检查
                if not is_new_message(state, name, company, messages):
                    console.print(f"[dim]  {name} | {company} — 无新消息，跳过[/dim]")
                    continue

                # 生成回复
                console.print("[dim]  Generating reply...[/dim]")
                reply = generate_reply(client, model, resume, messages)
                if not reply:
                    console.print(f"[yellow]  Reply generation failed for {name}[/yellow]")
                    continue

                # 是否已发简历
                resume_sent = has_sent_resume(messages)

                # 审核
                action = review_one(target_id, conv, messages, reply, resume_sent, len(selected), i)

                if action:
                    state = mark_processed(state, name, company, messages)
                    save_state(state)

            if args.once:
                break
            time.sleep(interval_sec)

    except KeyboardInterrupt:
        console.print("\n[yellow]chat_agent stopped.[/yellow]")


if __name__ == "__main__":
    main()
