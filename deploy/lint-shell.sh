#!/usr/bin/env bash
# Shell 闸门：语法 + 花括号闸。CI 与本地预检共用同一份，避免"本机绿 CI 红"。
#
# 一、语法：`bash -n` 逐个过（deploy/*.sh）
# 二、花括号闸：`${VAR}` 写成裸 `$VAR` 且紧跟非 ASCII 字符时，bash 会把该字符首字节**吞进变量名**
#     （实测两次：`${NODEPORT}（全集群）`、`${BUILD_HOST}）`；`set -u` 下直接
#     `BUILD_HOST�: unbound variable` 报错，不在 set -u 下更糟——静默取到别的值）。
#     修法一律写 `${VAR}`。本闸把这条固化，禁止回归。
#
# 退出码：0=全过；1=有违规（逐条点名 文件:行）
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
FAIL=0

echo "[1/2] bash -n 语法"
shopt -s nullglob
shs=("$HERE"/*.sh)
if [ "${#shs[@]}" -eq 0 ]; then
  echo "  (无 .sh)"
else
  for f in "${shs[@]}"; do
    if bash -n "$f" 2>/tmp/syn.err; then
      echo "  ✓ $(basename "$f")"
    else
      echo "  ✗ $(basename "$f")：$(tr '\n' ' ' < /tmp/syn.err | cut -c1-160)"
      FAIL=1
    fi
  done
fi

echo "[2/2] 花括号闸（\$VAR 后接非 ASCII）"
if python3 "$HERE/_lint_brace.py"; then
  echo "  ✓ 无违规"
else
  FAIL=1
fi

if [ "$FAIL" = "1" ]; then
  echo "✗ shell 闸门未过"
  exit 1
fi
echo "✓ shell 闸门通过"
