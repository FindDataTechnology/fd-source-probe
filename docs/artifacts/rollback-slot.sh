#!/usr/bin/env bash
# 回滚 testaction003/build-pool 到本仓覆盖前的占位。
#
# 锚点（默认 abea607 = "fd-wanxing.git @ 53eeb8957 — snapshot"）
#   ⚠️ 池是共享槽位、随时被别的会话推走（2026-11-10 实测：abea607 → 09bcdd4 在 1 小时内发生）。
#   **覆盖前必须重跑 `bash deploy/rehearse-slot.sh` 核对锚点**，不一致就用它给的当前 HEAD：
#       ANCHOR=<当前 HEAD> bash docs/artifacts/rollback-slot.sh --apply
#   远端 60+ 提交可列；`git cat-file -e <anchor>^{commit}` 会先验可达，不可达直接 exit 2。
#
# 安全设计（2026-11-10 教训）：默认**只演练，不推送**。
#   bash docs/artifacts/rollback-slot.sh                     # 演练（默认）
#   bash docs/artifacts/rollback-slot.sh --simulate-overwrite # 演练：先模拟覆盖再回滚，验证能复原
#   bash docs/artifacts/rollback-slot.sh --apply             # 真回滚（force push）
#
# 为什么默认不推：初版模板「无参数即推」，与下面这份手写版并存时被生成器覆盖过一次，
# 一次 `--dry-run` 调用实际执行了 push（所幸当时 HEAD 已等于锚点，是 no-op）。
# 现在**默认演练、要推必须显式 --apply**，且不再由 rehearse-slot.sh 生成（单源）。
set -euo pipefail

POOL="${POOL:-testaction003/build-pool}"
ANCHOR="${ANCHOR:-abea607c8a2ff58806f0977403ad6e736db48334}"
SRC="${SRC:-$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)}"

MODE="dry"
SIMULATE=0
for a in "$@"; do
  case "$a" in
    --apply) MODE="apply" ;;
    --simulate-overwrite) SIMULATE=1 ;;
    --dry-run) MODE="dry" ;;
    -h|--help) sed -n '2,18p' "$0"; exit 0 ;;
    *) echo "unknown arg: $a" >&2; exit 2 ;;
  esac
done

echo "== 回滚 ${POOL} → ${ANCHOR:0:7} · mode=${MODE} · simulate=${SIMULATE} =="
cd /tmp && rm -rf slot-restore
git clone -q "git@github.com:${POOL}.git" slot-restore
cd slot-restore

git cat-file -e "${ANCHOR}^{commit}" 2>/dev/null || { echo "✗ 锚点不可达：${ANCHOR}" >&2; exit 2; }
echo "  当前 HEAD : $(git rev-parse --short HEAD)  $(git log -1 --format=%s)"
echo "  目标锚点  : ${ANCHOR:0:7}  $(git log -1 --format=%s "$ANCHOR")"
echo "  差异项数  : $(git diff --name-status "$ANCHOR" HEAD | wc -l | tr -d ' ')"

if [ "$SIMULATE" = 1 ]; then
  echo "  [模拟覆盖] 用本仓快照**完整替换**本地 clone 并提交（不推）"
  # 必须「先清空再解包」：fd-build-snapshot 用带 delete 的 rsync（会删 408 项）。
  # 只 tar -x = 只加不删的假覆盖（首版就这么错的，结果差异只剩 26 项）。
  find . -mindepth 1 -maxdepth 1 ! -name .git -exec rm -rf {} +
  git -C "$SRC" archive HEAD | tar -x -C .
  git add -A >/dev/null
  git -c user.email=rehearsal@local -c user.name=rehearsal \
      commit -qm "SIMULATED overwrite by fd-source-probe" || true
  echo "    覆盖后：$(git ls-files | wc -l | tr -d ' ') 个文件，HEAD=$(git rev-parse --short HEAD)"
  echo "    相对锚点差异：$(git diff --name-status "$ANCHOR" HEAD | wc -l | tr -d ' ') 项"
fi

git reset --hard "$ANCHOR" >/dev/null
echo "  已 reset 到锚点，工作树文件数: $(git ls-files | wc -l | tr -d ' ')"
if [ "$SIMULATE" = 1 ]; then
  git diff --quiet "$ANCHOR" HEAD && echo "  [核对] 与锚点逐字节一致 ✓" || echo "  [核对] ✗ 与锚点不一致"
fi

if [ "$MODE" = "apply" ]; then
  echo "  \$ git push --force origin main"
  git push --force origin main
  echo "✓ 已回滚 ${POOL} → ${ANCHOR:0:7}"
else
  echo "  \$ git push --force --dry-run origin main   （演练，不推送）"
  git push --force --dry-run origin main 2>&1 | sed 's/^/    /'
  echo "✓ 演练完成。真回滚加 --apply"
fi
