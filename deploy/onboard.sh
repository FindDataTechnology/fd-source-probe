#!/usr/bin/env bash
# fd-source-probe 上牌四步 · 一键执行（仓建好之后用）
#
# 前置（人工一次）：在 gitee 建空仓 `FindDataTechnology/fd-source-probe`（私有）
#                  并在仓上配 `TCR_USERNAME` / `TCR_PASSWORD` 两个 secret。
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
while [ $# -gt 0 ]; do
  case "$1" in
    --apply) APPLY=1 ;;
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
  echo "  ✗ gitee 仓不存在（或无权访问）→ 人工建空仓后重跑"
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
  [ "$GITEE_OK" = "1" ] || fail "step 1 前置不满足：gitee 仓不存在"
  run "push gitee" git push -u "$GITEE_REMOTE" main
fi

# ── 2. 出片 / 校验镜像 ────────────────────────────────────────────────────
if step_wanted 2; then
  echo "[2] 镜像"
  echo "  通道：仓内 .github/workflows/image.yml（测试门 → hkccr → 内容闸门铸 sha-<7>）"
  echo "  注意：hkccr → ccr 需 relay（清单里 pin 的是 ccr）。"
  command -v skopeo >/dev/null || fail "缺 skopeo（镜像 digest 硬门用它）"
  run "查 ccr digest" skopeo inspect --no-tags "docker://$IMAGE_CCR" >/dev/null
  echo "  （若上面失败：等仓首推的 image run 绿 + relay 完成，再重跑本步）"
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
tools = [t["name"] for g in surf.get("tool_groups", []) for t in g.get("tools", [])]
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
  echo "  payload:"
  echo "$PAYLOAD" | python3 -m json.tool | sed 's/^/    /' | head -20
  if [ -n "$REGISTRY_TOKEN" ]; then
    run "注册条目" curl -sS -X POST "$REGISTRY_BASE/api/servers" \
      -H "Authorization: Bearer $REGISTRY_TOKEN" -H "Content-Type: application/json" -d "$PAYLOAD"
  else
    echo "  ! 未给 REGISTRY_TOKEN —— 不假装注册成功。人工执行："
    echo "    curl -X POST $REGISTRY_BASE/api/servers \\"
    echo "      -H 'Authorization: Bearer <管理令牌>' -H 'Content-Type: application/json' \\"
    echo "      -d '<上面的 payload>'"
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
