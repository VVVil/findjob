"""
CDP 直连 Chrome — BrowserSession

每个 BrowserSession 绑定一个独立的 Chrome debug 端口，
多平台时可以各自拥有一套实例，真正并行且会话隔离。

用法:
    boss_browser = BrowserSession(port=9222)
    zhilian_browser = BrowserSession(port=9223)
    boss_browser.new_tab("https://www.zhipin.com")
"""

import json
import threading
import time

import httpx
import websocket


# ── 模块级 helper（不依赖具体实例） ────────────────

def check_chrome_connection(port: int = 9222) -> bool:
    """检查指定端口的 Chrome 是否以 debug 模式运行（仅 HTTP，不建 WS）"""
    try:
        resp = httpx.get(f"http://127.0.0.1:{port}/json/version", timeout=5)
        return resp.status_code == 200
    except Exception:
        return False


def configure(config: dict | None = None) -> None:
    """空壳 — 保持接口兼容"""
    pass


# ══════════════════════════════════════════════════════

class BrowserSession:
    """一个浏览器实例 = 一个 debug 端口 + 独立 WebSocket"""

    def __init__(self, port: int = 9222):
        self.port = port
        self._base_url = f"http://127.0.0.1:{port}"
        self._ws = None
        self._sessions: dict[str, str] = {}   # targetId → sessionId
        self._shutdown = threading.Event()

    # ── 生命周期 ──────────────────────────────────

    def shutdown(self):
        """请求所有 CDP 操作尽快退出"""
        self._shutdown.set()

    @property
    def is_shutdown(self) -> bool:
        return self._shutdown.is_set()

    # ── 内部 ──────────────────────────────────────

    def _get_ws(self):
        """获取或创建浏览器级 WebSocket 连接"""
        if self._ws is not None and getattr(self._ws, 'connected', False):
            return self._ws
        try:
            resp = httpx.get(f"{self._base_url}/json/version", timeout=5)
            ws_url = resp.json()["webSocketDebuggerUrl"]
            self._ws = websocket.create_connection(ws_url, timeout=10, suppress_origin=True)
            return self._ws
        except Exception:
            return None

    def _send_cdp(self, method: str, params: dict | None = None,
                  session_id: str | None = None, timeout: int = 15) -> dict | None:
        """发送 CDP 命令，等待匹配 id 的响应。无需锁——一个 session 独占一条连接。"""
        ws = self._get_ws()
        if not ws:
            return None

        msg_id = int(time.time() * 1000) & 0xFFFF
        msg = {"id": msg_id, "method": method, "params": params or {}}
        if session_id:
            msg["sessionId"] = session_id

        try:
            ws.send(json.dumps(msg))
        except Exception:
            self._ws = None
            self._sessions = {}
            return None

        deadline = time.time() + timeout
        while time.time() < deadline:
            if self._shutdown.is_set():
                return None
            remaining = min(deadline - time.time(), 2.0)
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

    def _attach(self, target_id: str) -> str | None:
        if target_id in self._sessions:
            return self._sessions[target_id]
        result = self._send_cdp("Target.attachToTarget", {
            "targetId": target_id,
            "flatten": True,
        })
        if result and result.get("sessionId"):
            self._sessions[target_id] = result["sessionId"]
            return result["sessionId"]
        return None

    def _detach(self, target_id: str) -> None:
        sid = self._sessions.pop(target_id, None)
        if sid:
            self._send_cdp("Target.detachFromTarget", {"sessionId": sid})

    def _wait_for_load(self, target_id: str, timeout: int = 15) -> None:
        """内部：attach 后等待页面加载完成"""
        sid = self._attach(target_id)
        if not sid:
            return
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self._shutdown.is_set():
                return
            result = self._send_cdp("Runtime.evaluate", {
                "expression": "document.readyState",
                "returnByValue": True,
            }, session_id=sid, timeout=5)
            if result and result.get("result", {}).get("value") == "complete":
                return
            time.sleep(0.5)

    # ── Tab 管理 ──────────────────────────────────

    def find_tab(self, domain: str) -> str | None:
        """找到第一个包含 domain 的 tab，返回 targetId"""
        result = self._send_cdp("Target.getTargets")
        if not result:
            return None
        for t in result.get("targetInfos", []):
            if t.get("type") == "page" and domain in t.get("url", ""):
                return t["targetId"]
        return None

    def new_tab(self, url: str) -> str | None:
        """打开新 tab，返回 targetId"""
        result = self._send_cdp("Target.createTarget", {"url": url, "background": True})
        if not result:
            return None
        target_id = result.get("targetId")
        if target_id:
            self._attach(target_id)
            self._wait_for_load(target_id)
        return target_id

    def close_tab(self, target_id: str) -> None:
        """关闭指定 tab"""
        self._detach(target_id)
        self._send_cdp("Target.closeTarget", {"targetId": target_id})

    def navigate(self, target_id: str, url: str) -> dict | None:
        """导航 tab 到指定 URL"""
        sid = self._attach(target_id)
        if not sid:
            return None
        result = self._send_cdp("Page.navigate", {"url": url}, session_id=sid)
        self._wait_for_load(target_id)
        return result

    # ── JS 交互 ──────────────────────────────────

    def evaluate(self, target_id: str, expression: str, timeout: int = 15):
        """在 tab 中执行 JS 表达式，返回 JS 值"""
        sid = self._attach(target_id)
        if not sid:
            return None
        result = self._send_cdp("Runtime.evaluate", {
            "expression": expression,
            "returnByValue": True,
            "awaitPromise": True,
        }, session_id=sid, timeout=timeout)
        if result and "result" in result:
            return result["result"].get("value")
        return None

    def scroll(self, target_id: str, y: int = 2000) -> None:
        """滚动页面"""
        self.evaluate(target_id, f"window.scrollTo(0, {y})")

    def wait_for_load(self, target_id: str, timeout: int = 10) -> bool:
        """等待页面加载完成（readyState == 'complete'）"""
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self._shutdown.is_set():
                return False
            result = self.evaluate(target_id, "document.readyState", timeout=5)
            if result in ("complete", '"complete"'):
                return True
            time.sleep(0.5)
        return False


# ══════════════════════════════════════════════════════
#  向后兼容：模块级函数 → 默认 BrowserSession(9222)
#  chat_agent.py / boss/chat.py 等旧调用方无需改动
# ══════════════════════════════════════════════════════

_default_session: BrowserSession | None = None


def _get_default() -> BrowserSession:
    global _default_session
    if _default_session is None:
        _default_session = BrowserSession(port=9222)
    return _default_session


def _attach(target_id: str) -> str | None:
    return _get_default()._attach(target_id)


def _send_cdp(method: str, params: dict | None = None,
              session_id: str | None = None, timeout: int = 15) -> dict | None:
    return _get_default()._send_cdp(method, params, session_id, timeout)


def find_boss_tab() -> str | None:
    return _get_default().find_tab("zhipin.com")


def evaluate(target_id: str, expression: str, timeout: int = 15):
    return _get_default().evaluate(target_id, expression, timeout)


def navigate(target_id: str, url: str) -> dict | None:
    return _get_default().navigate(target_id, url)


def wait_for_load(target_id: str, timeout: int = 10) -> bool:
    return _get_default().wait_for_load(target_id, timeout)


def new_tab(url: str) -> str | None:
    return _get_default().new_tab(url)


def close_tab(target_id: str) -> None:
    _get_default().close_tab(target_id)


def scroll(target_id: str, y: int = 2000) -> None:
    _get_default().scroll(target_id, y)


def request_shutdown():
    if _default_session:
        _default_session.shutdown()


def is_shutdown() -> bool:
    if _default_session:
        return _default_session.is_shutdown
    return False
