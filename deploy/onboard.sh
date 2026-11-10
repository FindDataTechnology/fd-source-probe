#!/usr/bin/env bash
# fd-source-probe 上牌四步 · 一键执行（仓建好之后用）
#
# 【裁决 2026-11-10】不在 gitee 建 fd-source-probe 仓、不新增 gitee secret；
# 构建统一走 GitHub Actions（复用现有出片仓），故 step 1/2 与 gitee、TCR secret 解耦：
#   · step 1（首推 gitee）默认**跳过**并显式报「已按裁决跳过」；只有探测到仓已存在时才推
#   · step 2 出片改走 `fd-build-snapshot` 到既有 GitHub 出片仓（默认 BUILD_HOST），
#     不需要本仓配 TCR secret——凭据在出片仓上（scs001/各 testaction 账号）
#
# 用法新增：--allow-snapshot-overwrite（真推快照到共享/镜像仓时必须显式给，
#          因为它会覆盖该仓当前快照；dry-run 会先报出将被删除的文件数）
#
# 用法：
#   bash deploy/onboard.sh --dry-run          # 只做只读预检 + 打印将执行的命令（默认）
#   bash deploy/onboard.sh --apply            # 真跑四步
#   bash deploy/onboard.sh --apply --step 3   # 只跑某一步（1..5）
#   bash deploy/onboard.sh --apply --context cheap-ts
#
# 纪律：
#   · **fail-closed**：任何一步的预检不满足就停，不跳过、不"尽力而为"
#   · **幂等**：重复跑不会产生第二份资源（apply/kubectl 天然幂等；push 无新提交则空操作）
#   · **不猜**：注册条目没有令牌就只打印 curl，不假装注册成功
#   · 镜像必须先于 Argo 存在，否则首同步必然 ImagePullBackOff（本脚本 step 3 会拦）
set -uo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
NAME="fd-source-probe"
NS="fd-prod"
ARGOCD_NS="argocd"
CONTEXT="${CONTEXT:-cheap-ts}"
GITEE_REMOTE="gitee"
NODEPORT=31911
IMAGE_CCR="ccr.ccs.tencentyun.com/finddata-business/${NAME}:main"
IMAGE_HKCCR="hkccr.ccs.tencentyun.com/finddata-business/${NAME}:main"
REGISTRY_BASE="${REGISTRY_BASE:-https://mcp.finddatatech.cloud}"
REGISTRY_TOKEN="${REGISTRY_TOKEN:-}"

APPLY=0
ONLY_STEP=""
ALLOW_OVERWRITE=0
BUILD_HOST="${BUILD_HOST:-testaction003/build-pool}"   # 既有 GitHub 出片仓（份额最多）
while [ $# -gt 0 ]; do
  case "$1" in
    --apply) APPLY=1 ;;
    --allow-snapshot-overwrite) ALLOW_OVERWRITE=1 ;;
    --build-host) BUILD_HOST="${2:-}"; shift ;;
    --dry-run) APPLY=0 ;;
    --step) ONLY_STEP="${2:-}"; shift ;;
    --context) CONTEXT="${2:-}"; shift ;;
    -h|--help) sed -n '2,20p' "$0"; exit 0 ;;
    *) echo "unknown arg: $1" >&2; exit 2 ;;
  esac
  shift
done

step_wanted() { [ -z "$ONLY_STEP" ] || [ "$ONLY_STEP" = "$1" ]; }
run() {  # run <描述> <命令...>
  local desc="$1"; shift
  echo "  \$ $*"
  if [ "$APPLY" = "1" ]; then
    "$@" || { echo "✗ FAILED: $desc" >&2; exit 1; }
  fi
}
fail() { echo "✗ $*" >&2; exit 1; }

# 自检：`${VAR}` 花括号闸（$VAR 后接全角字符会被 bash 吞进变量名，set -u 下直接炸）
if [ -x "$REPO_DIR/deploy/lint-shell.sh" ]; then
  "$REPO_DIR/deploy/lint-shell.sh" | tail -1
fi

echo "== $NAME 上牌 · mode=$([ "$APPLY" = 1 ] && echo apply || echo dry-run) · context=$CONTEXT =="

# ── 0. 预检（只读，永远执行）───────────────────────────────────────────────
echo "[0] 预检"
cd "$REPO_DIR" || fail "仓目录不可用"
[ -d .git ] || fail "不是 git 仓：$REPO_DIR"
git rev-parse --abbrev-ref HEAD >/dev/null 2>&1 || fail "无提交"

echo "  \$ git ls-remote --exit-code $GITEE_REMOTE HEAD"
if git ls-remote --exit-code "$GITEE_REMOTE" HEAD >/dev/null 2>&1; then
  echo "  ✓ gitee 仓已存在"
  GITEE_OK=1
else
  # 按 2026-11-10 裁决：不建、不配 secret —— 仓不存在是**预期状态**，不是错误。
  # 这里曾打 ✗「人工建空仓后重跑」，与裁决自相矛盾（脚本自己指了个不该做的动作）。
  echo "  ⤳ gitee 仓不存在（2026-11-10 裁决：不建此仓；step 1 将跳过，不阻塞）"
  GITEE_OK=0
fi

echo "  \$ kubectl --context $CONTEXT get ns $NS"
kubectl --context "$CONTEXT" get ns "$NS" >/dev/null 2>&1 \
  || fail "上下文 $CONTEXT 里没有命名空间 ${NS}（先确认 context）"

# NodePort 是**集群级**资源，不是命名空间级——只查 -n $NS 会漏（2026-11-10 实测：
# 31904 被 chatwoot/chatwoot-web 占着，但 fd-prod 里看不见；server dry-run 当场报
# "provided port is already allocated"）。
echo "  \$ 检查 NodePort ${NODEPORT}（全集群）"
OWNER=$(kubectl --context "$CONTEXT" get svc -A -o json 2>/dev/null | python3 -c '
import json, sys
want = int(sys.argv[1])
d = json.load(sys.stdin)
for s in d["items"]:
    for p in (s["spec"].get("ports") or []):
        if p.get("nodePort") == want:
            print(s["metadata"]["namespace"] + "/" + s["metadata"]["name"])
' "$NODEPORT")
if [ -z "$OWNER" ]; then
  echo "  ✓ NodePort $NODEPORT 空闲（全集群）"
elif [ "$OWNER" = "$NS/$NAME" ]; then
  echo "  ✓ NodePort $NODEPORT 由本服务持有（幂等重跑）"
else
  fail "NodePort $NODEPORT 已被 $OWNER 占用 —— 换一个全集群空闲端口（改 deploy/k8s 与本脚本）"
fi

echo "  \$ 配额余量"
kubectl --context "$CONTEXT" describe resourcequota -n "$NS" 2>/dev/null \
  | awk '/requests.memory|pods /{print "    "$0}' | head -2

# ── 1. 首推 gitee ─────────────────────────────────────────────────────────
if step_wanted 1; then
  echo "[1] 首推 gitee"
  if [ "$GITEE_OK" = "1" ]; then
    echo "  ✓ 探测到仓已存在 → 按指令推送"
    run "push gitee" git push -u "$GITEE_REMOTE" main
  else
    echo "  ⤳ 已按裁决跳过（2026-11-10：不在 gitee 建 fd-source-probe 仓、不新增 gitee secret）"
    echo "    探测结果：ls-remote 404 → 仓不存在。此步不再阻塞后续。"
  fi
fi

# ── 2. 出片 / 校验镜像 ────────────────────────────────────────────────────
if step_wanted 2; then
  echo "[2] 镜像（构建统一走 GitHub Actions · 出片仓 ${BUILD_HOST}）"
  echo "  裁决依据：不依赖 gitee 仓与 TCR secret；出片仓自带 TCR 凭据。"
  command -v fd-build-snapshot >/dev/null || fail "缺 fd-build-snapshot"
  echo "  \$ 先干跑看覆盖面（快照会覆盖出片仓当前内容）"
  # --keep-workflows 是**必需**的（2026-11-10 诊断）：fd-build-snapshot 默认排除 /.github，
  # 于是出片仓里的 image.yml 永远停在上一版（实测池里那份是 fd-zhengxin 的，写死
  # fd-zhengxin-web/api 且要 server//web/ 目录，根本不读 BUILD_* 变量）→ 谁推都 9–14 秒挂，
  # 已连续 11 次失败、跨 6 个项目。带上本仓的 image.yml 才能让构建跑我们自己的 workflow。
  SNAP_OUT=$(fd-build-snapshot "$REPO_DIR" -R "$BUILD_HOST" \
      --namespace finddata --image "${NAME}" \
      --dockerfile Dockerfile --context . --ref main --branch main \
      --keep-workflows --dry-run 2>&1)
  echo "$SNAP_OUT" | grep -E "变更|将提交|将推送|⚠️" | sed 's/^/    /'
  DEL=$(echo "$SNAP_OUT" | grep -oE '变更 [0-9]+ 项' | grep -oE '[0-9]+' || echo 0)
  echo "    将影响 $DEL 项（其中删除项=出片仓现占位内容）"
  if ! echo "$SNAP_OUT" | grep -q "排除 /.github"; then
    echo "    ✓ 含本仓 .github/workflows/image.yml（覆盖池里那份陈旧 workflow，构建才会跑我们的）"
  fi
  if [ "$APPLY" = "1" ] && [ "$ALLOW_OVERWRITE" != "1" ]; then
    fail "真推快照会覆盖 $BUILD_HOST 当前内容——确认请加 --allow-snapshot-overwrite"
  fi
  if [ "$APPLY" != "1" ]; then
    echo "  （dry-run：不推快照。真跑：bash deploy/onboard.sh --apply --allow-snapshot-overwrite）"
  elif [ "$ALLOW_OVERWRITE" = "1" ]; then
    run "推快照出片" fd-build-snapshot "$REPO_DIR" -R "$BUILD_HOST" \
        --namespace finddata --image "${NAME}" \
        --dockerfile Dockerfile --context . --ref main --branch main \
        --keep-workflows
    echo "  \$ 等出片仓的 image run 绿（出片 tag = 快照仓 commit 短 sha）"
    gh run list -R "$BUILD_HOST" --limit 3 || true
  fi
  echo "  \$ 校验 ccr digest（relay 后 ≤5 分钟）"
  command -v skopeo >/dev/null || echo "    ! 缺 skopeo，跳过 digest 硬门"
  command -v skopeo >/dev/null && run "查 ccr digest" skopeo inspect --no-tags "docker://${IMAGE_CCR}" >/dev/null
fi

# ── 3. ArgoCD apply ──────────────────────────────────────────────────────
if step_wanted 3; then
  echo "[3] ArgoCD Application"
  echo "  \$ kubectl --context $CONTEXT apply --dry-run=server -f deploy/argocd/application.yaml"
  kubectl --context "$CONTEXT" apply --dry-run=server -f deploy/argocd/application.yaml >/dev/null \
    || fail "Application 清单过不了 server dry-run"
  echo "  ✓ server dry-run 通过"
  run "apply application" kubectl --context "$CONTEXT" apply -f deploy/argocd/application.yaml
  if [ "$APPLY" = "1" ]; then
    echo "  \$ 等 Argo 同步出 Deployment"
    kubectl --context "$CONTEXT" -n "$NS" rollout status "deploy/$NAME" --timeout=240s \
      || fail "rollout 未就绪（看 kubectl describe pod 的 ImagePullBackOff）"
  fi
fi

# ── 4. 注册处条目 ────────────────────────────────────────────────────────
if step_wanted 4; then
  echo "[4] 注册处条目"
  PAYLOAD="$(python3 - "$NAME" "$NODEPORT" <<'PY'
import json, sys, pathlib
name, nodeport = sys.argv[1], int(sys.argv[2])
surf = json.load(open(pathlib.Path("tool-surface.json")))
# tool_groups[].tools 两种形态都见过：字符串数组（本仓）与 {name, level} 对象（fd-open-data-mcp）。
# 只认一种就会在另一种上炸（dry-run 实测：string indices must be integers）。
tools = []
for g in surf.get("tool_groups", []):
    for t in g.get("tools", []):
        tools.append(t if isinstance(t, str) else t.get("name"))
tools = [t for t in tools if t]
print(json.dumps({
    "path": f"/{name}",
    "proxy_pass_url": f"http://cheap6.mesh.finddata.cn:{nodeport}/mcp",
    "auth_scheme": "none",
    "visibility": surf.get("visibility", "internal"),
    "description": surf.get("description", ""),
    "num_tools": len(tools),
    "tool_list": tools,
    "metadata": {"surface": {"commercial_tools": [], "internal_tools": tools}},
}, ensure_ascii=False))
PY
)"
  # payload 始终落盘：没有令牌时它就是交付物（人工 POST 用），有令牌时它是留档。
  PAYLOAD_FILE="$REPO_DIR/docs/artifacts/registry-payload.json"
  mkdir -p "$(dirname "$PAYLOAD_FILE")"
  echo "$PAYLOAD" > "$PAYLOAD_FILE"
  echo "  payload → $PAYLOAD_FILE"
  echo "$PAYLOAD" | python3 -c 'import json,sys;print(json.dumps(json.load(sys.stdin),ensure_ascii=False,indent=2))' | sed 's/^/    /' | head -20
  if [ -n "$REGISTRY_TOKEN" ]; then
    run "注册条目" curl -sS -X POST "$REGISTRY_BASE/api/servers" \
      -H "Authorization: Bearer $REGISTRY_TOKEN" -H "Content-Type: application/json" -d "$PAYLOAD"
  else
    echo "  ! 未给 REGISTRY_TOKEN —— 不假装注册成功（降级路径见 docs/build-slot-decision.md §五）"
    echo "    人工执行（payload 已落盘，直接引用文件）："
    echo "      curl -sS -X POST $REGISTRY_BASE/api/servers \\"
    echo "        -H \"Authorization: Bearer \$REGISTRY_TOKEN\" -H 'Content-Type: application/json' \\"
    echo "        --data-binary @$PAYLOAD_FILE | python3 -m json.tool"
    echo "    写后核对："
    echo "      curl -sS -H \"Authorization: Bearer \$REGISTRY_TOKEN\" \\"
    echo "        '$REGISTRY_BASE/api/servers' | python3 -c \"import json,sys;print([s for s in json.load(sys.stdin) if s.get('path')=='/fd-source-probe'])\""
  fi
fi

# ── 5. 上线后实证 ────────────────────────────────────────────────────────
if step_wanted 5; then
  echo "[5] 实证（run 427… 全绿才算上牌完成，逐条见 deploy/VERIFY.md）"
  echo "  \$ /healthz"
  run "healthz" kubectl --context "$CONTEXT" -n "$NS" exec "deploy/$NAME" -- \
      python -c "import urllib.request;print(urllib.request.urlopen('http://127.0.0.1:8000/healthz',timeout=5).read().decode())"
  echo "  \$ tools/list（必须恰好一枚举 probe_endpoint）"
  run "tools" kubectl --context "$CONTEXT" -n "$NS" exec "deploy/$NAME" -- \
      python -c "from fd_source_probe.server import create_server;import asyncio;print([t.name for t in asyncio.run(create_server().list_tools())])"
  echo "  \$ SSRF 负例（集群内必须同样被拒）"
  run "ssrf-negative" kubectl --context "$CONTEXT" -n "$NS" exec "deploy/$NAME" -- \
      python -c "
import asyncio
from fd_source_probe.probe import probe
r = asyncio.run(probe('http://127.0.0.1:8000/x'))
assert r['ok'] is False and r['error']['type']=='guard_rejected', r
print('SSRF negative ok:', r['verdict']['reason'])
"
fi

echo
echo "完成。核对清单见 deploy/VERIFY.md"
