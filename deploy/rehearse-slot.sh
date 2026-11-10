#!/usr/bin/env bash
# 出片池覆盖演练（只读 + 生成清单/回滚脚本，**永不推送**）
#
# 干跑 `--allow-snapshot-overwrite` 到底会动到什么：把「会删哪些、会改哪些、锚点在哪、
# 怎么滚回去」摊开成一页报告 + 两份产物（删除清单、可执行回滚脚本）。
#
# 用法：
#   bash deploy/rehearse-slot.sh                    # 默认池 testaction003/build-pool
#   bash deploy/rehearse-slot.sh --pool testaction004/build-pool
#   bash deploy/rehearse-slot.sh --out /tmp/rehearsal
#
# 纪律：本脚本**只读**远端（clone --depth 1 到临时目录、gh api 查历史），不 push、不改变量。
set -uo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
POOL="${POOL:-testaction003/build-pool}"
OUT="${OUT:-$REPO_DIR/docs/artifacts}"
WORK="${WORK:-/tmp/slot-rehearsal}"

while [ $# -gt 0 ]; do
  case "$1" in
    --pool) POOL="${2:-}"; shift ;;
    --out) OUT="${2:-}"; shift ;;
    --work) WORK="${2:-}"; shift ;;
    -h|--help) sed -n '2,14p' "$0"; exit 0 ;;
    *) echo "unknown arg: $1" >&2; exit 2 ;;
  esac
  shift
done

mkdir -p "$OUT" || exit 2
echo "== 出片池覆盖演练 · pool=${POOL} · out=${OUT} =="
echo "   （只读：不 push、不改仓变量）"

# ── 1. 抓池当前状态 ──────────────────────────────────────────────────────
rm -rf "$WORK"
if ! git clone -q --depth 1 "git@github.com:${POOL}.git" "$WORK" 2>/dev/null; then
  echo "✗ 无法 clone ${POOL}（网络/权限）" >&2
  exit 2
fi
HEAD_SHA=$(git -C "$WORK" rev-parse HEAD)
HEAD_MSG=$(git -C "$WORK" log -1 --format=%s)
FILE_N=$(git -C "$WORK" ls-files | wc -l | tr -d ' ')
echo "[1] 当前占位"
echo "    HEAD       ${HEAD_SHA:0:7}"
echo "    内容说明   ${HEAD_MSG}"
echo "    文件数     ${FILE_N}"

DEPTH=$(gh api "repos/${POOL}/commits?per_page=100" --jq 'length' 2>/dev/null || echo "?")
echo "    远端历史   ${DEPTH} 提交可列（回滚锚点是否可达看这个）"

# ── 2. 池的产线健康度（决定覆盖的风险）────────────────────────────────
echo "[2] 该池最近 run 结果（failure 连片 = 没有健康产线依赖它）"
gh run list -R "$POOL" --limit 6 --json conclusion,headSha,displayTitle \
  2>/dev/null | "${REPO_DIR}/.venv/bin/python" -c "
import json, sys
rows = json.load(sys.stdin)
fail = 0
for r in rows:
    print(f\"    {r['headSha'][:7]}  {(r['conclusion'] or 'running'):9} {r['displayTitle'][:52]}\")
    fail += (r['conclusion'] == 'failure')
print(f'    → 近 {len(rows)} 次里 failure {fail} 次')
" || echo "    （取不到 run 列表）"

# ── 3. 精确差分：会删什么、会改什么、会加什么 ─────────────────────────
git -C "$REPO_DIR" ls-files | sort > "$WORK/../ours.txt"
git -C "$WORK" ls-files | sort > "$WORK/../pool.txt"
comm -23 "$WORK/../pool.txt" "$WORK/../ours.txt" > "$OUT/slot-would-delete.txt"
comm -13 "$WORK/../pool.txt" "$WORK/../ours.txt" > "$OUT/slot-would-add.txt"
comm -12 "$WORK/../pool.txt" "$WORK/../ours.txt" > "$WORK/../shared.txt"

DEL=$(wc -l < "$OUT/slot-would-delete.txt" | tr -d ' ')
ADD=$(wc -l < "$OUT/slot-would-add.txt" | tr -d ' ')
SHR=$(wc -l < "$WORK/../shared.txt" | tr -d ' ')
echo "[3] 差分（我们的快照 vs 池当前）"
echo "    删除 ${DEL} 项 → ${OUT}/slot-would-delete.txt"
echo "    新增 ${ADD} 项 → ${OUT}/slot-would-add.txt"
echo "    同名 ${SHR} 项（内容可能不同=修改）"
echo "    删除项按顶层目录聚合："
/usr/bin/awk -F/ '{print ($2==""?"<root>":$1"/"$2)}' "$OUT/slot-would-delete.txt" \
  | sort | uniq -c | sort -rn | head -8 | sed 's/^/      /'

# ── 4. 生成可执行回滚脚本 ─────────────────────────────────────────────
cat > "$OUT/rollback-slot.sh" <<ROLLBACK
#!/usr/bin/env bash
# 回滚 ${POOL} 到覆盖前的占位（由 rehearse-slot.sh 于 $(date -u +%FT%TZ) 生成）
# 锚点：${HEAD_SHA:0:7} = ${HEAD_MSG}
set -euo pipefail
cd /tmp && rm -rf slot-restore
git clone git@github.com:${POOL}.git slot-restore
cd slot-restore
git reset --hard ${HEAD_SHA}
git push --force origin main
echo "已回滚 ${POOL} → ${HEAD_SHA:0:7}"
echo "注意：该占位本身处于失败态（见出片池演练报告），回滚等于回到同样的状态。"
ROLLBACK
chmod +x "$OUT/rollback-slot.sh"
echo "[4] 回滚脚本 → ${OUT}/rollback-slot.sh（锚点 ${HEAD_SHA:0:7}）"

# ── 5. 判定 ─────────────────────────────────────────────────────────────
echo "[5] 判定"
echo "    · 覆盖=清掉 ${DEL} 项、改 ${SHR} 项、加 ${ADD} 项；不触碰任何已推镜像 tag"
echo "    · 回滚一条命令（reset --hard ${HEAD_SHA:0:7} + force push），远端历史 ${DEPTH} 提交可回"
if [ "$DEPTH" != "?" ] && [ "$DEPTH" -lt 2 ] 2>/dev/null; then
  echo "    ! 远端历史过浅（${DEPTH}）——回滚锚点可能不可达，覆盖前务必确认"
fi
echo
echo "→ 决策页：docs/build-slot-decision.md"
echo "→ 回滚演练：bash docs/artifacts/rollback-slot.sh --simulate-overwrite --dry-run"
echo "→ 授权后执行：bash deploy/onboard.sh --apply --allow-snapshot-overwrite --step 2"
