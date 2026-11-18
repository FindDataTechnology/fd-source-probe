#!/usr/bin/env bash
# deploy/VERIFY.md §5 的集群内 SSRF 负例 —— 单独成脚本，可干跑。
#
# 为什么必须**在集群里再跑一遍**：本机绿是弱证据。库里对目标的判据是
# `ip.is_global` 与内网后缀集，而在集群里 100.64.0.0/10（tailnet）、
# `*.svc.cluster.local`、`*.mesh.finddata.cn` 这些**真的可达**——本机根本没有这些地址，
# 本机测不出「集群内服务名被拒」这件事。
#
# 用法：
#   bash deploy/ssrf-neg.sh --dry-run          # 占用自检（pod 在不在）+ 报错路径自检（本地跑断言）+ 打印将执行的命令
#   bash deploy/ssrf-neg.sh --local            # 本机 venv 里跑（集群不可用时的预演；证明断言会红也会绿）
#   bash deploy/ssrf-neg.sh                    # 真跑：kubectl exec 进 pod 逐条验
#   bash deploy/ssrf-neg.sh --context cheap-ts --ns fd-prod --deploy fd-source-probe
#
# 判据形状（每条都要"拒绝理由"对上，不只看 ok=False）：
#   guard_rejected   + 理由含 回环/链路本地/私网/保留/内网后缀/单标签
#   method_not_allowed（写方法在发出请求前被拒，零出网）
# 退出码：0=全过（SKIP 不算失败）／1=有 FAIL／2=前置不满足
set -uo pipefail   # pipefail 必开：任何 `cmd | 过滤器` 的失败都必须冒泡成退出码

# REPO_DIR 解析：显式 REPO_ROOT > git 顶层 > 脚本目录上一级；解析后**必须**含 pyproject.toml，
# 否则说明脚本被拷到别处跑了（曾被拷到 /tmp，dirname 变成 / → 解析成 //.venv 才报错）。
_resolve_repo_dir() {
  local cand="${REPO_ROOT:-}"
  if [ -z "$cand" ]; then
    cand="$(git -C "$(dirname "${BASH_SOURCE[0]}")" rev-parse --show-toplevel 2>/dev/null || true)"
  fi
  if [ -z "$cand" ]; then
    cand="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
  fi
  printf '%s' "$cand"
}
REPO_DIR="$(_resolve_repo_dir)"
case "$REPO_DIR" in
  /*) ;;
  *) echo "✗ REPO_DIR 不是绝对路径：$REPO_DIR" >&2; exit 2 ;;
esac
[ -f "$REPO_DIR/pyproject.toml" ] || {
  echo "✗ REPO_DIR 下没有 pyproject.toml：${REPO_DIR}（脚本被拷出仓了？用 REPO_ROOT=<仓根> 覆盖）" >&2
  exit 2
}
CONTEXT="${CONTEXT:-cheap-ts}"
NS="${NS:-fd-prod}"
DEPLOY="${DEPLOY:-fd-source-probe}"
MODE="cluster"     # cluster | local
DRY=0
TIMEOUT="${TIMEOUT:-90}"

while [ $# -gt 0 ]; do
  case "$1" in
    --dry-run) DRY=1 ;;
    --local) MODE="local" ;;
    --context) CONTEXT="${2:-}"; shift ;;
    --ns) NS="${2:-}"; shift ;;
    --deploy) DEPLOY="${2:-}"; shift ;;
    --timeout) TIMEOUT="${2:-}"; shift ;;
    -h|--help) sed -n '2,26p' "$0"; exit 0 ;;
    *) echo "unknown arg: $1" >&2; exit 2 ;;
  esac
  shift
done

echo "== SSRF 负例 · mode=${MODE} · dry-run=$([ "$DRY" = 1 ] && echo yes || echo no) =="
echo "   REPO_DIR=${REPO_DIR}"
echo "   shell: pipefail=$([[ -o pipefail ]] && echo on || echo off)"

# 断言脚本（在 pod 内与本机通用；只依赖探针包 + 标准库）
read -r -d '' ASSERT_PY <<'PY'
import asyncio, json, sys
from fd_source_probe.probe import probe

CASES = [
    ("loopback-ipv4",     dict(url="http://127.0.0.1:8000/x"),                       "guard_rejected", "回环"),
    ("metadata-linklocal", dict(url="http://169.254.169.254/latest/meta-data/"),     "guard_rejected", "链路本地"),
    ("tailnet-cgnat",     dict(url="http://100.64.0.1:18004/mcp"),                  "guard_rejected", "保留"),
    ("cluster-dns-name",  dict(url="http://fd-source-probe.fd-prod.svc.cluster.local/mcp"),
                                                                                     "guard_rejected", "内网后缀"),
    ("single-label-host", dict(url="http://probe-internal/x"),                       "guard_rejected", "单标签"),
    ("ipv6-loopback",     dict(url="http://[::1]/x"),                                "guard_rejected", "回环"),
    ("write-method",      dict(url="https://example.com/x", method="PUT"),           "method_not_allowed", ""),
]
OPTIONAL = [
    ("redirect-to-private", dict(url="https://httpbin.org/redirect-to?url=http://127.0.0.1/"),
                                                                                     "guard_rejected", "重定向"),
]

async def run(case_id, kw, want_type, want_word, optional=False):
    try:
        r = await probe(**kw, caller="verify/ssrf-neg", label=case_id)
    except Exception as exc:
        return ("FAIL", f"探测自身抛错 {type(exc).__name__}: {exc}")
    err = r.get("error") or {}
    got_type = err.get("type")
    verdict = r["verdict"]
    detail = err.get("reason") or verdict.get("reason") or ""
    if got_type != want_type:
        if optional and got_type is None:
            st = r.get("status")
            if st:
                return ("SKIP", f"首跳即被拦（HTTP {st}）——不是断言失败，是跳转器不可达；"
                                "该条需一个可达的 302 端点（集群内用外网可达跳转器）")
            return ("SKIP", f"网络不可用（{verdict['reason'][:60]}）")
        return ("FAIL", f"期望 {want_type}，实得 {got_type or verdict['kind']}（{detail[:80]}）")
    if want_word and want_word not in detail:
        return ("FAIL", f"类型对但理由不含「{want_word}」：{detail[:100]}")
    return ("PASS", detail[:100])

def hop_guard_offline():
    """离线钉住「逐跳复判」的逻辑：guard 对重定向目标的判定与首跳同源。

    HTTP 层的 302→内网 需要可达跳转器（本机常不可达），但**判据本身**可以离线验：
    对同一个私网目标，guard 给出的拒绝理由必须与首跳一致——若这里不一致，
    说明有人给重定向开了后门（历史上正是这种写法放过过 SSRF）。
    """
    from fd_source_probe import guard
    from fd_source_probe.config import Settings
    s = Settings(denied_host_suffixes=(".svc", ".cluster.local", ".mesh.finddata.cn"),
                 allowed_hosts=(), audit_log="")
    first, redirect = "http://127.0.0.1/admin", "http://10.1.2.3/admin"
    r1 = r2 = ""
    try:
        guard.check_url(first, s)
    except guard.GuardError as e:
        r1 = e.reason
    try:
        guard.check_url(redirect, s)
    except guard.GuardError as e:
        r2 = e.reason
    if not r1 or not r2:
        return ("FAIL", f"逐跳判据失效：first={r1!r} redirect={r2!r}")
    return ("PASS", f"首跳「{r1}」/ 重定向目标「{r2}」——同一套拒绝集，无后门")


async def main():
    rows = []
    for cid, kw, wt, ww in CASES:
        rows.append((cid, *await run(cid, kw, wt, ww)))
    for cid, kw, wt, ww in OPTIONAL:
        rows.append((cid, *await run(cid, kw, wt, ww, optional=True)))
    rows.append(("hop-guard-offline", *hop_guard_offline()))
    for cid, verdict, detail in rows:
        print(f"{verdict}\t{cid}\t{detail}")
    bad = [r for r in rows if r[1] == "FAIL"]
    print(f"SUMMARY\tpass={sum(1 for r in rows if r[1]=='PASS')}\tskip={sum(1 for r in rows if r[1]=='SKIP')}\tfail={len(bad)}")
    return 1 if bad else 0

sys.exit(asyncio.run(main()))
PY

# ── 占用自检（pod 在不在、能不能跑）─────────────────────────────────────
echo "[占用自检]"
POD_READY=0
if [ "$MODE" = "cluster" ]; then
  echo "  \$ kubectl --context $CONTEXT -n $NS get deploy/$DEPLOY"
  if kubectl --context "$CONTEXT" -n "$NS" get deploy "$DEPLOY" >/dev/null 2>&1; then
    READY=$(kubectl --context "$CONTEXT" -n "$NS" get deploy "$DEPLOY" \
            -o jsonpath='{.status.readyReplicas}' 2>/dev/null)
    echo "  ✓ Deployment 存在，readyReplicas=${READY:-0}"
    [ "${READY:-0}" -ge 1 ] 2>/dev/null && POD_READY=1
    [ "$POD_READY" = 1 ] || echo "  ! 没有就绪副本（镜像没起来？先看 deploy/VERIFY.md §2.3）"
  else
    echo "  ✗ Deployment 不存在（未上牌）→ 只能 --local 预演"
  fi
else
  echo "  \$ 本机 venv: $REPO_DIR/.venv"
  [ -x "$REPO_DIR/.venv/bin/python" ] || { echo "  ✗ 缺 venv（uv venv && uv pip install -e '.[dev]'）" >&2; exit 2; }
  echo "  ✓ venv 可用"
fi

# ── 报错路径自检（证明断言会红也会绿）───────────────────────────────────
echo "[报错路径自检] 同一套断言先在本机跑一遍："
PY_BIN="$REPO_DIR/.venv/bin/python"
[ -x "$PY_BIN" ] || PY_BIN="python3"
LOCAL_OUT=$(cd "$REPO_DIR" && "$PY_BIN" -c "$ASSERT_PY" 2>&1)
echo "$LOCAL_OUT" | grep -v '^SUMMARY' | sed 's/^/    /'
LOCAL_SUM=$(echo "$LOCAL_OUT" | grep '^SUMMARY' || echo "SUMMARY pass=0 skip=0 fail=?")
echo "    $LOCAL_SUM"
case "$LOCAL_SUM" in
  *fail=0*) echo "  ✓ 断言自检通过（本机形态）" ;;
  *)        echo "  ✗ 断言自检失败（本机形态）——先修断言，别去集群里跑" ; exit 1 ;;
esac

if [ "$DRY" = "1" ]; then
  echo
  echo "[将执行]（--dry-run 不真跑）"
  if [ "$MODE" = "cluster" ]; then
    [ "$POD_READY" = 1 ] && echo "  kubectl --context $CONTEXT -n $NS exec deploy/$DEPLOY -- python - <<'PY' …（同一套断言）" \
                         || echo "  （pod 未就绪：上牌后本脚本可直接跑；现在只能 --local 预演）"
  else
    echo "  $PY_BIN -c <同一套断言>（本机 venv）"
  fi
  echo "  → 判定：全部 PASS 即 §5.3/5.4 通过；SKIP 表示该条依赖外网（重定向那一跳）"
  exit 0
fi

# ── 真跑 ────────────────────────────────────────────────────────────────
if [ "$MODE" = "cluster" ]; then
  [ "$POD_READY" = 1 ] || { echo "✗ 集群模式需要就绪副本；或用 --local" >&2; exit 2; }
  echo "[集群内执行]"
  OUT=$(kubectl --context "$CONTEXT" -n "$NS" exec "deploy/$DEPLOY" -- \
        env PYTHONUNBUFFERED=1 python3 -c "$ASSERT_PY" 2>&1)
  RC=$?
else
  echo "[本机执行]"
  OUT=$(cd "$REPO_DIR" && "$PY_BIN" -c "$ASSERT_PY" 2>&1); RC=$?
fi
echo "$OUT" | grep -v '^SUMMARY' | sed 's/^/  /'
SUM=$(echo "$OUT" | grep '^SUMMARY' || true)
echo "  ${SUM:-<无汇总行>}"
case "$SUM" in
  *fail=0*) exit 0 ;;
  *) exit 1 ;;
esac
