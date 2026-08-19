#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""验证 monitor.py 在 Python 3.6+ 下的语法与常见 API 兼容性。"""

from __future__ import print_function

import ast
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
MONITOR = ROOT / "monitor.py"

FORBIDDEN_PATTERNS = [
    (r"(?<!['\"])\|\s*(None|str|int|float|dict|list|tuple|bool|Exception|datetime)\b", "PEP604 联合类型 X | Y"),
    (r"\bdict\[", "内置泛型 dict[...]（需 3.9+ 或 typing.Dict）"),
    (r"\blist\[", "内置泛型 list[...]"),
    (r"\btuple\[", "内置泛型 tuple[...]"),
    (r"\bset\[", "内置泛型 set[...]"),
    (r"from __future__ import annotations", "future annotations（需 3.7+）"),
    (r"\.unlink\(missing_ok=", "Path.unlink(missing_ok=)（需 3.8+）"),
    (r"capture_output\s*=", "subprocess capture_output（需 3.7+）"),
    (r"\btext\s*=\s*True", "subprocess text=（需 3.7+，应用 universal_newlines）"),
    (r"\bmatch\s+\w+\s*:", "match 语句（需 3.10+）"),
    (r":=", "海象运算符（需 3.8+）"),
]


def check_version() -> None:
    if sys.version_info < (3, 6):
        raise SystemExit("验证脚本本身需要 Python 3.6+")


def check_syntax() -> None:
    source = MONITOR.read_text(encoding="utf-8")
    ast.parse(source, filename=str(MONITOR))
    print("[ok] AST 解析通过")


def check_patterns() -> None:
    source = MONITOR.read_text(encoding="utf-8")
    errors = []
    for pattern, message in FORBIDDEN_PATTERNS:
        for match in re.finditer(pattern, source):
            line = source.count("\n", 0, match.start()) + 1
            errors.append("  行 %d: %s -> %s" % (line, match.group(0), message))
    if errors:
        raise SystemExit("[fail] 发现不兼容写法:\n" + "\n".join(errors))
    print("[ok] 静态模式检查通过")


def check_import() -> None:
    sys.path.insert(0, str(ROOT))
    import monitor  # noqa: F401

    checks = [
        ("shop_page_url", monitor.shop_page_url("ldxp", "test")),
        ("parse_shop_timestamp", monitor.parse_shop_timestamp("2026-06-10 08:00:00")),
        ("_markdown_link", monitor._markdown_link("商品", "https://example.com")),
    ]
    for name, value in checks:
        if value is None and name == "parse_shop_timestamp":
            raise SystemExit("[fail] %s 返回 None" % name)
        print("[ok] %s -> %r" % (name, value))
    print("[ok] import monitor 通过")


def main() -> None:
    check_version()
    print("Python %s" % sys.version.split()[0])
    check_syntax()
    check_patterns()
    check_import()
    print("\n全部检查通过，可在 Linux Python 3.6+ 运行。")


if __name__ == "__main__":
    main()
