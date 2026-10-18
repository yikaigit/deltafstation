#!/usr/bin/env python3
"""DeltaFStation 启动脚本。

用法：
    python run.py                 # 开发模式（默认端口 5000）
    DELTAF_PORT=8000 python run.py
    DELTAF_DEBUG=0 python run.py  # 关闭调试与自动重载
    DELTAF_SELFTEST=0 python run.py  # 跳过启动自检
"""
from __future__ import annotations

import os

from backend.app import create_app
from backend.core.config import get_config

#: 启动自检的提示语。**只提示，不自动补数据** ——
#: 拉数据要几分钟且依赖外网，启动时静默跑一堆网络请求会让人以为程序卡住了。
#: 用户看到提示后自己去首页点「补齐并打分」，这样他能控制时机。
_SELFTEST_HINT = "启动自检"


def startup_selftest() -> None:
    """启动时检查「今天的数据在不在」，只报告不执行。

    为什么值得做：P6 之前没有任何地方会主动告诉你「已经三天没更新了」。
    打开页面看到的是三天前的分数，很容易误以为是今天的。
    """
    try:
        from backend.core import catchup as catchup_lib

        report = catchup_lib.gap_report(lookback_days=10)
    except Exception as exc:  # 自检失败绝不能阻止启动
        print(f"[{_SELFTEST_HINT}] 跳过（{exc}）")
        return

    if report.complete:
        print(f"[{_SELFTEST_HINT}] 数据齐全，最新交易日 {report.target}")
        return

    days = report.days_to_fill
    preview = ", ".join(days[:5]) + (" …" if len(days) > 5 else "")
    print(f"[{_SELFTEST_HINT}] ⚠ 缺少 {len(days)} 个交易日的数据：{preview}")
    print(f"[{_SELFTEST_HINT}]   打开首页点「补齐并打分」即可补齐（不会自动执行）")


def main() -> None:
    conf = get_config()
    app = create_app()

    print("=" * 56)
    print(f"DeltaFStation v{conf.version}")
    print("=" * 56)
    print(f"访问地址: http://127.0.0.1:{conf.port}")
    print(f"调试模式: {'开启' if conf.debug else '关闭'}")
    print(f"数据目录: {conf.paths.data}")
    if os.environ.get("DELTAF_SELFTEST", "1").lower() not in ("0", "false", "no"):
        startup_selftest()
    print("=" * 56)

    use_reloader = conf.debug and os.environ.get("DELTAF_RELOADER", "1").lower() not in (
        "0",
        "false",
        "no",
    )
    app.run(host=conf.host, port=conf.port, debug=conf.debug, use_reloader=use_reloader)


if __name__ == "__main__":
    main()
