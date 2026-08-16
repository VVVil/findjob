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
import os
import random
import subprocess
import tempfile
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


# ── Chrome 自动启动 ───────────────────────────────

CHROME_PATH = r"C:\Program Files\Google\Chrome\Application\chrome.exe"


def launch_chrome(port: int, user_data_dir_name: str, start_url: str) -> bool:
    """启动一个独立 user-data-dir 的 debug Chrome。

    熄屏/后台也能触发懒加载：--disable-renderer-backgrounding 等三个 flag，
    配合 scroll_wheel 的 CDP 滚轮，让 BOSS/智联的 Vue 列表在后台继续加载。
    """
    user_data_dir = os.path.join(tempfile.gettempdir(), user_data_dir_name)
    cmd = [
        CHROME_PATH,
        f"--remote-debugging-port={port}",
        "--remote-allow-origins=*",
        f"--user-data-dir={user_data_dir}",
        start_url,
        "--disable-frame-rate-limit",
        "--disable-renderer-backgrounding",
        "--disable-features=CalculateNativeWinOcclusion",
    ]
    try:
        subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return True
    except Exception:
        return False


def ensure_chrome(port: int, user_data_dir_name: str, start_url: str,
                  wait: float = 15.0) -> bool:
    """确保 debug Chrome 在指定端口运行；不在则自动启动并等待就绪。"""
    if check_chrome_connection(port):
        return True
    if not launch_chrome(port, user_data_dir_name, start_url):
        return False
    deadline = time.time() + wait
    while time.time() < deadline:
        if check_chrome_connection(port):
            time.sleep(2)  # 给首页 + 登录态恢复留缓冲
            return True
        time.sleep(0.5)
    return False


def close_chrome(port: int) -> None:
    """关闭指定 debug 端口的 Chrome 浏览器进程。

    先发 CDP 的 Browser.close 优雅关闭；连接失败/无效再按命令行匹配
    --remote-debugging-port 用 PowerShell 强杀进程（不依赖 WS 是否还活着）。
    """
    # 1) CDP Browser.close —— fire-and-forget，浏览器收到即关闭，不读响应
    try:
        resp = httpx.get(f"http://127.0.0.1:{port}/json/version", timeout=3)
        ws = websocket.create_connection(resp.json()["webSocketDebuggerUrl"],
                                         timeout=3, suppress_origin=True)
        ws.send(json.dumps({"id": 1, "method": "Browser.close", "params": {}}))
        ws.close()
        return
    except Exception:
        pass

    # 2) 兜底：按命令行匹配 --remote-debugging-port 强杀
    try:
        ps = (
            "Get-CimInstance Win32_Process -Filter \"Name='chrome.exe'\" | "
            f"Where-Object {{ $_.CommandLine -match 'remote-debugging-port={port}' }} | "
            "ForEach-Object { Stop-Process -Id $_.ProcessId -Force }"
        )
        subprocess.run(["powershell", "-NoProfile", "-Command", ps],
                       capture_output=True, timeout=20)
    except Exception:
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
        self._created_tabs: set[str] = set()  # 本 session 通过 new_tab 打开的 tab

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
            self._created_tabs.add(target_id)
            self._attach(target_id)
            self._wait_for_load(target_id)
        return target_id

    def close_tab(self, target_id: str) -> None:
        """关闭指定 tab"""
        self._detach(target_id)
        self._created_tabs.discard(target_id)
        self._send_cdp("Target.closeTarget", {"targetId": target_id})

    def close_all_tabs(self) -> None:
        """关闭本 session 打开的所有 tab —— 用于立即中断爬取（打断正在 dwell/提取的 tab）。
        只关爬虫自己 new_tab 创建的标签，不影响用户手动打开的标签页，也不关浏览器进程。"""
        for target_id in list(self._created_tabs):
            self._detach(target_id)
            try:
                self._send_cdp("Target.closeTarget", {"targetId": target_id})
            except Exception:
                pass
        self._created_tabs.clear()

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

    def scroll_wheel(self, target_id: str, delta_y: int = 300,
                     repeat: int = 4, interval: float = 0.15) -> None:
        """CDP 鼠标滚轮事件 — 熄屏/后台也能触发 Vue 懒加载。

        JS 的 scrollBy 在 Chrome 熄屏后 rAF 被暂停，Vue 的 scroll
        handler 不会触发。CDP Input.dispatchMouseEvent 是协议层事件，
        不受 rAF/页面可见性影响。
        """
        sid = self._attach(target_id)
        if not sid:
            return
        x = 500  # 页面中间位置
        for i in range(repeat):
            # 每次滚一点点，模拟真实用户滚轮
            y = 400 + i * 50
            dy = delta_y // repeat
            self._send_cdp("Input.dispatchMouseEvent", {
                "type": "mouseWheel",
                "x": x, "y": y,
                "deltaX": 0, "deltaY": dy,
                "modifiers": 0,
            }, session_id=sid, timeout=5)
            time.sleep(interval)

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
#  TabHandle — 每个 tab 独立一条 WebSocket，不阻塞其他 tab
# ══════════════════════════════════════════════════════

class TabHandle:
    """一个独立 tab，拥有自己的 WebSocket 连接。

    CDP 的 /json 端点给每个 page target 分配了独立的
    webSocketDebuggerUrl，连接后可以直接发 Runtime.evaluate
    等命令，不需要 sessionId，也不会跟其他 tab 的通信排队。
    """

    def __init__(self, browser_url: str, target_id: str):
        self.browser_url = browser_url
        self.target_id = target_id
        self._ws = None
        self.created_at = time.time()
        self.dwell_until: float = 0          # 到期时间戳，TabPool 管理

    # ── 连接 ──────────────────────────────────────

    def connect(self) -> bool:
        """获取这个 tab 的 webSocketDebuggerUrl 并建立独立 WS"""
        try:
            resp = httpx.get(f"{self.browser_url}/json", timeout=5)
            for t in resp.json():
                if t.get("id") == self.target_id:
                    ws_url = t["webSocketDebuggerUrl"]
                    self._ws = websocket.create_connection(
                        ws_url, timeout=10, suppress_origin=True)
                    return True
        except Exception:
            pass
        return False

    # ── CDP ───────────────────────────────────────

    def _send(self, method: str, params: dict | None = None,
              timeout: int = 15) -> dict | None:
        """在 tab 自己的 WS 上发送 CDP 命令"""
        ws = self._ws
        if not ws:
            return None
        msg_id = int(time.time() * 1000) & 0xFFFF
        msg = {"id": msg_id, "method": method, "params": params or {}}
        try:
            ws.send(json.dumps(msg))
        except Exception:
            self._ws = None
            return None

        deadline = time.time() + timeout
        while time.time() < deadline:
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

    def evaluate(self, expression: str, timeout: int = 15):
        """执行 JS 并返回值"""
        result = self._send("Runtime.evaluate", {
            "expression": expression,
            "returnByValue": True,
            "awaitPromise": True,
        }, timeout=timeout)
        if result and "result" in result:
            return result["result"].get("value")
        return None

    def close(self):
        """断开 WS（不关闭 tab，tab 由 BrowserSession 关闭）"""
        if self._ws:
            try:
                self._ws.close()
            except Exception:
                pass
            self._ws = None

    @property
    def dwell_expired(self) -> bool:
        return time.time() >= self.dwell_until


# ══════════════════════════════════════════════════════
#  TabPool — 并发 tab 池，stagger + dwell 管理
# ══════════════════════════════════════════════════════

class TabPool:
    """管理一个并发 tab 池，控制最大并发、自动 stagger 和 dwell。

    用法:
        pool = TabPool(browser, max_concurrent=4, dwell_range=(20, 35))
        for url in urls:
            result = pool.submit(url, extract_js=JS_EXTRACT)
            # result 是提取到的数据，tab 继续在后台 dwell
        pool.drain()  # 等所有 tab dwell 结束并关闭
    """

    def __init__(self, browser_session: BrowserSession,
                 max_concurrent: int = 4,
                 dwell_range: tuple[float, float] = (20, 35),
                 stagger_range: tuple[float, float] = (4, 8)):
        self._browser = browser_session
        self._browser_url = f"http://127.0.0.1:{browser_session.port}"
        self.max_concurrent = max_concurrent
        self.dwell_range = dwell_range
        self.stagger_range = stagger_range
        self._handles: dict[str, TabHandle] = {}  # targetId → handle
        self._last_submit: float = 0
        self._errors: list[str] = []

    # ── 内部 ──────────────────────────────────────

    def _reap_expired(self):
        """关闭 dwell 到期的 tab"""
        expired = [tid for tid, h in self._handles.items() if h.dwell_expired]
        for tid in expired:
            h = self._handles.pop(tid)
            h.close()
            self._browser.close_tab(tid)

    def _wait_for_slot(self):
        """等到池里有空位"""
        while len(self._handles) >= self.max_concurrent:
            oldest_tid = min(self._handles,
                             key=lambda k: self._handles[k].dwell_until)
            wait = self._handles[oldest_tid].dwell_until - time.time()
            if wait > 0:
                time.sleep(min(wait, 0.5))
            self._reap_expired()

    def _stagger(self):
        """确保距上次 submit 有最小间隔"""
        since_last = time.time() - self._last_submit
        min_gap = self.stagger_range[0]
        if since_last < min_gap:
            time.sleep(min_gap - since_last)

    # ── 公开 API ──────────────────────────────────

    def submit(self, url: str, activate_js: str | None = None,
               extract_js: str | None = None,
               activate_timeout: int = 15,
               extract_timeout: int = 15) -> str | None:
        """提交一个 URL 到池里。

        1. stagger 间隔保证
        2. 清理到期 tab，池满则等
        3. 开新 tab → 独立 WS → 等渲染 → 提取
        4. 设定 dwell 倒计时，tab 留在池里继续"阅读"
        5. 返回 extract 结果字符串

        返回 None = 某步失败（tab 立即关闭，不 dwell）。
        """
        self._stagger()

        self._reap_expired()
        self._wait_for_slot()

        # 3. 创建 tab
        target_id = self._browser.new_tab(url)
        if not target_id:
            self._errors.append(f"new_tab: {url}")
            return None

        # 4. 独立 WS
        handle = TabHandle(self._browser_url, target_id)
        if not handle.connect():
            self._browser.close_tab(target_id)
            self._errors.append(f"connect: {url}")
            return None

        # 5. 等页面基础加载
        self._browser.wait_for_load(target_id, timeout=10)

        # 激活 tab（避免 Chrome 节流后台 timer）
        self._browser._send_cdp("Target.activateTarget",
                                {"targetId": target_id})

        # 6. 激活页面（等 Vue 渲染，点 tab，滚动）
        if activate_js:
            handle.evaluate(activate_js, timeout=activate_timeout)

        # 7. 提取
        result = None
        if extract_js:
            result = handle.evaluate(extract_js, timeout=extract_timeout)

        if result is None:
            handle.close()
            self._browser.close_tab(target_id)
            self._errors.append(f"extract: {url}")
            return None

        # 8. 设定 dwell 倒计时
        dwell = random.uniform(*self.dwell_range)
        handle.dwell_until = time.time() + dwell
        self._handles[target_id] = handle

        self._last_submit = time.time()
        return result

    def drain(self):
        """等待所有 tab dwell 结束并关闭"""
        while self._handles:
            oldest_tid = min(self._handles,
                             key=lambda k: self._handles[k].dwell_until)
            wait = self._handles[oldest_tid].dwell_until - time.time()
            if wait > 0:
                time.sleep(min(wait, 0.5))
            self._reap_expired()

    @property
    def active_count(self) -> int:
        return len(self._handles)

    @property
    def errors(self) -> list[str]:
        return list(self._errors)


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


def scroll_wheel(target_id: str, delta_y: int = 300,
                 repeat: int = 4, interval: float = 0.15) -> None:
    _get_default().scroll_wheel(target_id, delta_y, repeat, interval)


def request_shutdown():
    if _default_session:
        _default_session.shutdown()


def is_shutdown() -> bool:
    if _default_session:
        return _default_session.is_shutdown
    return False
