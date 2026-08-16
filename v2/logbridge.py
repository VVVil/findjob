"""
日志桥接 — 让 rich 的 console.print 同时写 stdout 和转发到 Web 前端。

背景：
1. Web UI 模式下，scraper 里的 console.print 只输出到后台进程 stdout，前端看不到完整日志。
2. Windows 后台运行时 stdout 是 GBK 编码，打印 emoji 会抛 UnicodeEncodeError 中断爬虫线程。

做法：Console 输出到一个 tee 流 —— 写 stdout 保留颜色，同时剥掉 ANSI 码转发纯文本给 Web 端。
"""
from __future__ import annotations

import re
import sys

from rich.console import Console

_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")
_web_log_hook = None  # 由 app.py 注入，签名 (event_type, data)


class ScrapeAborted(Exception):
    """爬取被用户主动中断。scraper 检测到 is_shutdown 时抛出，app.py 捕获后跳过结果保存。"""


def set_web_log_hook(fn):
    """app.py 在启动爬虫前调用，注入日志转发回调。传 None 可解除。"""
    global _web_log_hook
    _web_log_hook = fn


class _TeeFile:
    """写 stdout（保留 rich 颜色）+ 按行转发纯文本给 Web 端。"""

    def __init__(self):
        self._buf = ""

    def write(self, s: str):
        try:
            sys.stdout.write(s)
        except Exception:
            pass

        if not _web_log_hook:
            return

        self._buf += s
        while "\n" in self._buf:
            line, self._buf = self._buf.split("\n", 1)
            self._emit(line)

    def flush(self):
        try:
            sys.stdout.flush()
        except Exception:
            pass
        if self._buf:
            self._emit(self._buf)
            self._buf = ""

    def _emit(self, line: str):
        plain = _ANSI_RE.sub("", line).rstrip()
        if plain.strip() and _web_log_hook:
            _web_log_hook("log", {"msg": plain})


console = Console(file=_TeeFile(), force_terminal=True)
