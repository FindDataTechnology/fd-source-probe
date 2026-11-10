#!/usr/bin/env python3
"""闸门：bash 脚本里 `$VAR` 后紧跟非 ASCII 字符会被 bash 吞进变量名（set -u 下直接报错）。

2026-11-10 连踩两次（`$NODEPORT（`、`$BUILD_HOST）`）：全角括号的首字节被当作变量名字符。
修法一律写 `${VAR}`。本脚本把这条做成发布前闸门。
"""
import pathlib
import re
import sys

BAD = re.compile(r"\$([A-Za-z_][A-Za-z0-9_]*)(?=[^\x00-\x7F])")
bad = []
roots = [pathlib.Path(__file__).resolve().parent,
         pathlib.Path(__file__).resolve().parent.parent / "docs" / "artifacts"]
files = [f for r in roots if r.is_dir() for f in sorted(r.glob("*.sh"))]
for f in files:
    for i, line in enumerate(f.read_text(encoding="utf-8").splitlines(), 1):
        for m in BAD.finditer(line):
            bad.append(f"{f.parent.name}/{f.name}:{i}: ${m.group(1)} 后接非 ASCII → 改 ${{{m.group(1)}}}")
for b in bad:
    print(b)
print(f"checked, violations={len(bad)}")
sys.exit(1 if bad else 0)
