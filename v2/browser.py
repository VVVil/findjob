"""
CDP 直连 Chrome — 替代 bosshunter.browser

通过浏览器级 WebSocket 连接 + session 路由，
与 BossHunter CDP proxy 架构一致。
Chrome 需以 --remote-debugging-port=9222 启动。
"""

import json
import time

import httpx
import websocket

CDP_BASE = "http://127.0.0.1:9222"

# ── 浏览器级 WebSocket（全局复用） ────────────────────
_browser_ws = None
_sessions: dict[str, str] = {}   # targetId → sessionId


def _get_browser_ws():
    """获取或创建浏览器级 WebSocket 连接"""
    global _browser_ws
    if _browser_ws is not None and getattr(_browser_ws, 'connected', False):
        return _browser_ws
    try:
        resp = httpx.get(f"{CDP_BASE}/json/version", timeout=5)
        ws_url = resp.json()["webSocketDebuggerUrl"]
        _browser_ws = websocket.create_connection(ws_url, timeout=10, suppress_origin=True)
        return _browser_ws
    except Exception:
        return None


def _send_cdp(method: str, params: dict | None = None, session_id: str | None = None, timeout: int = 15) -> dict | None:
    """通过浏览器 WS 发送 CDP 命令，等待结果。可指定 sessionId。"""
    global _browser_ws, _sessions
    ws = _get_browser_ws()
    if not ws:
        return None

    msg_id = int(time.time() * 1000) & 0xFFFF
    msg = {"id": msg_id, "method": method, "params": params or {}}
    if session_id:
        msg["sessionId"] = session_id

    try:
        ws.send(json.dumps(msg))
    except Exception:
        _browser_ws = None  # 标记失效，下次重连
        _sessions = {}       # 连接断了，session 全失效
        return None

    deadline = time.time() + timeout
    while time.time() < deadline:
        remaining = deadline - time.time()
        if remaining <= 0:
            break
        try:
            ws.settimeout(remaining)
            raw = ws.recv()
            resp = json.loads(raw)
            if resp.get("id") == msg_id:
                if "error" in resp:
                    return None
                return resp.get("result", {})
        except websocket.WebSocketTimeoutException:
            continue
        except Exception:
            break
    return None


def _attach(target_id: str) -> str | None:
    """Attach 到 target，缓存并返回 sessionId"""
    if target_id in _sessions:
        return _sessions[target_id]

    result = _send_cdp("Target.attachToTarget", {
        "targetId": target_id,
        "flatten": True,
    })
    if result and result.get("sessionId"):
        _sessions[target_id] = result["sessionId"]
        return result["sessionId"]
    return None


def _detach(target_id: str) -> None:
    """从 target detach"""
    sid = _sessions.pop(target_id, None)
    if sid:
        _send_cdp("Target.detachFromTarget", {"sessionId": sid})


# ══════════════════════════════════════════════════════
#  公开 API（与 bosshunter.browser 接口兼容）
# ══════════════════════════════════════════════════════

def check_chrome_connection() -> bool:
    """检查 Chrome 是否以 debug 模式运行（仅 HTTP，不建 WS）"""
    try:
        resp = httpx.get(f"{CDP_BASE}/json/version", timeout=5)
        return resp.status_code == 200
    except Exception:
        return False


def find_boss_tab() -> str | None:
    """找到第一个 zhipin.com 的 tab，返回 targetId"""
    result = _send_cdp("Target.getTargets")
    if not result:
        return None
    for t in result.get("targetInfos", []):
        if t.get("type") == "page" and "zhipin.com" in t.get("url", ""):
            return t["targetId"]
    return None


def new_tab(url: str) -> str | None:
    """打开新 tab，返回 targetId"""
    result = _send_cdp("Target.createTarget", {"url": url, "background": True})
    if not result:
        return None
    target_id = result.get("targetId")
    if target_id:
        # Attach 并等待加载（与 BossHunter 行为一致）
        _attach(target_id)
        _wait_for_load_via_session(target_id)
    return target_id


def close_tab(target_id: str) -> None:
    """关闭指定 tab"""
    _detach(target_id)
    _send_cdp("Target.closeTarget", {"targetId": target_id})


def navigate(target_id: str, url: str) -> dict | None:
    """导航 tab 到指定 URL"""
    sid = _attach(target_id)
    if not sid:
        return None
    result = _send_cdp("Page.navigate", {"url": url}, session_id=sid)
    _wait_for_load_via_session(target_id)
    return result


def evaluate(target_id: str, expression: str, timeout: int = 15):
    """在 tab 中执行 JS 表达式，返回 JS 值"""
    sid = _attach(target_id)
    if not sid:
        return None
    result = _send_cdp("Runtime.evaluate", {
        "expression": expression,
        "returnByValue": True,
        "awaitPromise": True,
    }, session_id=sid, timeout=timeout)
    if result and "result" in result:
        return result["result"].get("value")
    return None


def scroll(target_id: str, y: int = 2000) -> None:
    """滚动页面"""
    evaluate(target_id, f"window.scrollTo(0, {y})")


def wait_for_load(target_id: str, timeout: int = 10) -> bool:
    """等待页面加载完成（readyState == 'complete'）"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        result = evaluate(target_id, "document.readyState", timeout=5)
        if result in ("complete", '"complete"'):
            return True
        time.sleep(0.5)
    return False


def _wait_for_load_via_session(target_id: str, timeout: int = 15) -> None:
    """内部：attach 后等待页面加载（通过 session 轮询 readyState）"""
    sid = _attach(target_id)
    if not sid:
        return
    deadline = time.time() + timeout
    while time.time() < deadline:
        result = _send_cdp("Runtime.evaluate", {
            "expression": "document.readyState",
            "returnByValue": True,
        }, session_id=sid, timeout=5)
        if result and result.get("result", {}).get("value") == "complete":
            return
        time.sleep(0.5)


def configure(config: dict | None = None) -> None:
    """空壳 — 保持接口兼容"""
    pass
