#!/usr/bin/env python3
"""DeltaFStation 启动脚本。

用法：
    python run.py                 # 开发模式（默认端口 5000）
    DELTAF_PORT=8000 python run.py
    DELTAF_DEBUG=0 python run.py  # 关闭调试与自动重载
"""
from __future__ import annotations

import os

from backend.app import create_app
from backend.core.config import get_config


def main() -> None:
    conf = get_config()
    app = create_app()

    print("=" * 56)
    print(f"DeltaFStation v{conf.version}")
    print("=" * 56)
    print(f"访问地址: http://127.0.0.1:{conf.port}")
    print(f"调试模式: {'开启' if conf.debug else '关闭'}")
    print(f"数据目录: {conf.paths.data}")
    print("=" * 56)

    use_reloader = conf.debug and os.environ.get("DELTAF_RELOADER", "1").lower() not in (
        "0",
        "false",
        "no",
    )
    app.run(host=conf.host, port=conf.port, debug=conf.debug, use_reloader=use_reloader)


if __name__ == "__main__":
    main()
