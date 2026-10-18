"""P0 冒烟自测：启动应用上下文，遍历 4 个页面 + 全部 API，检查状态码与响应结构。

用法：.venv/Scripts/python.exe scripts/smoke_test.py
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from backend.app import create_app  # noqa: E402

PAGES = ["/", "/data", "/scores", "/backtest"]
APIS = [
    ("GET", "/api/health"),
    ("GET", "/api/universe"),
    ("GET", "/api/universe?industry=银行&limit=5"),
    ("GET", "/api/data/status"),
    ("GET", "/api/data/quality"),
    ("GET", "/api/scores"),
    ("GET", "/api/scores/industries"),
    ("GET", "/api/scores/600519"),
    ("GET", "/api/score-versions"),
    ("GET", "/api/backtests"),
    ("GET", "/api/backtests/not-exist"),
    ("GET", "/api/jobs"),
    ("POST", "/api/jobs"),
    ("GET", "/api/jobs/not-exist"),
    ("POST", "/api/backtests"),
    ("GET", "/api/nonexistent"),
]


def main() -> int:
    app = create_app()
    client = app.test_client()
    failures: list[str] = []

    print("=" * 68)
    print("页面")
    print("=" * 68)
    for path in PAGES:
        res = client.get(path)
        ok = res.status_code == 200 and b"<html" in res.data.lower()
        print(f"  {'PASS' if ok else 'FAIL'}  {path:<14} HTTP {res.status_code}  {len(res.data):>6} bytes")
        if not ok:
            failures.append(f"页面 {path} -> {res.status_code}")

    print()
    print("=" * 68)
    print("接口")
    print("=" * 68)
    for method, path in APIS:
        body = {"kind": "update", "params": {}} if method == "POST" and "jobs" in path else (
            {"top_n": 20} if method == "POST" else None
        )
        res = client.open(path, method=method, json=body)
        try:
            payload = res.get_json()
        except Exception:
            payload = None

        has_shape = isinstance(payload, dict) and "ok" in payload
        code = (payload.get("error") or {}).get("code") if payload else None
        # 无数据时允许的降级返回：接口应报错但结构完整，不 500
        graceful = {"no_version", "no_score", "not_found", "not_implemented"}

        # 预期状态：成功 200/201，未实现 501，不存在 404
        expect = 200
        if "not-exist" in path or "nonexistent" in path:
            expect = 404
        elif method == "POST" and "backtests" in path:
            expect = 501
        elif method == "POST" and "jobs" in path:
            expect = 201

        ok = has_shape and (res.status_code == expect or code in graceful)
        detail = ""
        if payload and payload.get("ok") is False:
            detail = (payload.get("error") or {}).get("code", "")
            if code in graceful and res.status_code != expect:
                detail += "（无数据降级，符合预期）"
        elif payload and isinstance(payload.get("data"), dict):
            d = payload["data"]
            keys = [k for k in ("items", "count", "meta", "datasets", "symbols") if k in d]
            detail = ",".join(keys) or "scalar"
        elif payload and isinstance(payload.get("data"), list):
            detail = f"list[{len(payload['data'])}]"

        print(
            f"  {'PASS' if ok else 'FAIL'}  {method:<4} {path:<34} "
            f"HTTP {res.status_code:<3} {detail}"
        )
        if not ok:
            failures.append(f"{method} {path} -> {res.status_code} (期望 {expect})")

    print()
    print("=" * 68)
    if failures:
        print(f"失败 {len(failures)} 项：")
        for item in failures:
            print("  -", item)
        return 1
    print(f"全部通过：{len(PAGES)} 个页面 + {len(APIS)} 个接口")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
