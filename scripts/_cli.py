"""CLI 公共工具：进度条、任务记录、参数解析。"""
from __future__ import annotations

import sys
import time
from typing import Optional


class ConsoleProgress:
    """把 (ratio, message) 渲染成单行进度。"""

    def __init__(self, width: int = 28, stream=None):
        self.width = width
        self.stream = stream or sys.stdout
        self.started = time.monotonic()
        self._last_len = 0
        self._last_message = ""

    def __call__(self, ratio: float, message: str) -> None:
        if ratio is None or ratio < 0:
            self._write(message, prefix="  · ")
            return
        filled = int(max(0.0, min(1.0, ratio)) * self.width)
        bar = "█" * filled + "·" * (self.width - filled)
        elapsed = time.monotonic() - self.started
        text = f"  [{bar}] {ratio * 100:5.1f}%  {elapsed:6.1f}s  {message}"
        self._write(text, prefix="", replace=True)

    def _write(self, text: str, prefix: str = "", replace: bool = False) -> None:
        if replace:
            pad = max(0, self._last_len - len(text))
            self.stream.write("\r" + text + " " * pad)
            self._last_len = len(text)
        else:
            if self._last_len:
                self.stream.write("\r" + " " * self._last_len + "\r")
                self._last_len = 0
            self.stream.write(prefix + text + "\n")
        self.stream.flush()

    def done(self, message: str = "") -> None:
        if self._last_len:
            self.stream.write("\r" + " " * self._last_len + "\r")
            self._last_len = 0
        if message:
            elapsed = time.monotonic() - self.started
            self.stream.write(f"  ✓ {message}（{elapsed:.1f}s）\n")
            self.stream.flush()


def record_job(kind: str, params: dict, status: str, message: str, log: Optional[str] = None) -> str:
    """把 CLI 运行也记进 job_runs，让前端能看到。"""
    try:
        from backend.core.meta_db import get_meta_db

        db = get_meta_db()
        job_id = db.create_job(kind=kind, params=params)
        db.update_job(job_id, status=status, progress=1.0 if status == "success" else 0.0,
                      message=message, log=log)
        return job_id
    except Exception as exc:  # 记录失败不影响主流程
        print(f"  ! 写入任务记录失败：{exc}")
        return ""


def human_int(value) -> str:
    try:
        return f"{int(value):,}"
    except (TypeError, ValueError):
        return str(value)
