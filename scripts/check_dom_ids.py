"""静态一致性检查：JS 里引用的 DOM id 是否都存在于对应模板（含 base.html）。

用法：.venv/Scripts/python.exe scripts/check_dom_ids.py
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TPL = ROOT / "frontend" / "templates"
JS = ROOT / "frontend" / "static" / "js"

PAIRS = [
    ("index.js", ["index.html"]),
    ("data.js", ["data.html"]),
    ("scores.js", ["scores.html"]),
    ("backtest.js", ["backtest.html"]),
    ("common.js", ["base.html"]),
]

ID_IN_JS = re.compile(r"""(?:getElementById|el)\(\s*['"]([\w-]+)['"]\s*\)""")
ID_IN_HTML = re.compile(r"""id=["']([\w-]+)["']""")

# 由 JS 动态插入到 DOM 的节点，模板里不存在属正常
DYNAMIC_IDS = {"drawer-chart", "drawer-radar", "drawer-kline"}


def ids_of_html(names: list[str]) -> set[str]:
    found: set[str] = set()
    for name in names + ["base.html"]:
        path = TPL / name
        if path.exists():
            found |= set(ID_IN_HTML.findall(path.read_text(encoding="utf-8")))
    return found


def main() -> int:
    problems = 0
    for js_name, html_names in PAIRS:
        js_path = JS / js_name
        if not js_path.exists():
            print(f"MISS  {js_name} 不存在")
            problems += 1
            continue
        used = set(ID_IN_JS.findall(js_path.read_text(encoding="utf-8"))) - DYNAMIC_IDS
        available = ids_of_html(html_names)
        missing = sorted(used - available)
        status = "PASS" if not missing else "FAIL"
        print(f"{status}  {js_name:<14} 引用 {len(used):>2} 个 id，模板提供 {len(available):>2} 个")
        if missing:
            problems += len(missing)
            for name in missing:
                print(f"        缺少 #{name}  （模板 {html_names} 中未找到）")

    # 反向检查：模板里 onclick 调用的函数是否在 JS 中导出
    print()
    for js_name, html_names in PAIRS:
        for name in html_names:
            path = TPL / name
            if not path.exists():
                continue
            calls = set(re.findall(r"""on(?:click|change|input|submit)=["'](\w+)\.(\w+)\(""", path.read_text(encoding="utf-8")))
            if not calls:
                continue
            js_text = (JS / js_name).read_text(encoding="utf-8")
            for obj, fn in sorted(calls):
                ok = re.search(rf"\b{fn}\b", js_text) is not None
                print(f"{'PASS' if ok else 'FAIL'}  {name:<14} {obj}.{fn}()")
                if not ok:
                    problems += 1

    print()
    print("一致" if not problems else f"发现 {problems} 处不一致")
    return 1 if problems else 0


if __name__ == "__main__":
    raise SystemExit(main())
