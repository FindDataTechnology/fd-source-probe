# 上牌校验清单（fd-source-probe）

> 配套 `deploy/onboard.sh`。每条都是**可判绿判红**的判据，不是「看起来正常」。
> 顺序即依赖顺序：前一条红，后一条的绿没有意义。

## 0. 前置（人工一次）

| # | 判据 | 怎么验 | 期望 |
|---|---|---|---|
| 0.1 | gitee 空仓存在 | `git ls-remote --exit-code gitee HEAD` | 退出码 0（空仓也返回 0） |
| 0.2 | 仓上两个 secret 已配 | `gh secret list -R <仓>` 或在 gitee 仓设置里看 | `TCR_USERNAME`、`TCR_PASSWORD` 都在 |
| 0.3 | 上下文正确 | `kubectl --context cheap-ts get ns fd-prod` | 有 `fd-prod`（ArgoCD 与 fd-prod 同在 cheap 集群） |

## 1. 首推

| # | 判据 | 怎么验 | 期望 |
|---|---|---|---|
| 1.1 | 远端与本地同提交 | `git -C fd-source-probe rev-parse HEAD` vs `git ls-remote gitee refs/heads/main` | 两个 sha 相同 |

## 2. 镜像

| # | 判据 | 怎么验 | 期望 |
|---|---|---|---|
| 2.1 | 测试门绿 | `gh run list -R <仓> --workflow image.yml` | 最新一次 `success` |
| 2.2 | ccr 里有 `:main` | `skopeo inspect --no-tags docker://ccr.ccs.tencentyun.com/finddata-business/fd-source-probe:main` | 返回 manifest（**清单 pin 的是 ccr，不是 hkccr——中间有 relay**） |
| 2.3 | 镜像能起来 | `kubectl --context cheap-ts -n fd-prod get pod -l app.kubernetes.io/name=fd-source-probe` | `1/1 Running`，无 `ImagePullBackOff` |
| 2.4 | 探针代码在镜像里 | `kubectl exec deploy/fd-source-probe -- fd-source-probe --version` | 打印 `0.1.0`（不是 `command not found`） |

## 3. ArgoCD

| # | 判据 | 怎么验 | 期望 |
|---|---|---|---|
| 3.1 | Application 存在且 Synced | `kubectl --context cheap-ts -n argocd get app fd-source-probe -o jsonpath='{.status.sync.status}{" "}{.status.health.status}'` | `Synced Healthy` |
| 3.2 | Service 端口就位 | `kubectl --context cheap-ts -n fd-prod get svc fd-source-probe -o jsonpath='{.spec.ports[0].nodePort}'` | `31911` |
| 3.3 | mesh 可达 | 从集群内 `curl -s -o /dev/null -w '%{http_code}' http://cheap6.mesh.finddata.cn:31911/healthz` | `200` |

> **NodePort 是集群级资源**：只看 `-n fd-prod` 会漏掉别的 namespace 占用（2026-11-10 实测
> 31904 被 `chatwoot/chatwoot-web` 占着，fd-prod 里看不见；server dry-run 当场报
> `provided port is already allocated`）。查法：`kubectl get svc -A`。

## 4. 注册处条目

| # | 判据 | 怎么验 | 期望 |
|---|---|---|---|
| 4.1 | 条目在册 | `curl -s -H "Authorization: Bearer <tok>" https://mcp.finddatatech.cloud/api/servers \| jq '.[] \| select(.path=="/fd-source-probe")'` | 一条，`auth_scheme=none` |
| 4.2 | 工具面 = 1 | 同上，看 `num_tools` / `tool_list` | `1` / `["probe_endpoint"]` |
| 4.3 | **商业面为空** | `metadata.surface.commercial_tools` | `[]`（探针不得对外，这是设计不是遗漏） |
| 4.4 | 台账已落行 | `grep -n "fd-source-probe" MCP-REGISTRY.md` | 有行（按 `/fd-zhengxin-reference` 形状） |

## 5. 上线后实证（全部在集群内跑，不经网关）

> **需要单独跑的那部分已脚本化**：`bash deploy/ssrf-neg.sh`
> （`--dry-run` 占用自检+报错路径自检；`--local` 本机预演；无参=集群内真跑；退出码 0 全过 / 1 有 FAIL / 2 前置不满足）。
> 条目 **5.3 / 5.4** 由它覆盖，且多了一条 `hop-guard-offline`（离线钉住「重定向目标与首跳同一套拒绝集」，
> 防止有人给重定向开后门）。

| # | 判据 | 怎么验（`kubectl exec` 进 pod） | 期望 |
|---|---|---|---|
| 5.1 | 健康面 | `python -c "import urllib.request;print(urllib.request.urlopen('http://127.0.0.1:8000/healthz',timeout=5).read())"` | `{"status": "ok", ...}` |
| 5.2 | 工具面恰好一枚举 | `python -c "from fd_source_probe.server import create_server;import asyncio;print([t.name for t in asyncio.run(create_server().list_tools())])"` | `['probe_endpoint']` |
| 5.3 | **SSRF 负例在真实出网环境同样被拒** | `python -c "import asyncio;from fd_source_probe.probe import probe;r=asyncio.run(probe('http://127.0.0.1:8000/x'));print(r['error'])"` | `guard_rejected`（本机绿不算数，集群网段不同） |
| 5.4 | 重定向复判 | `bash deploy/ssrf-neg.sh`（脚本内 `redirect-to-private` 条） | 第二跳 `guard_rejected`；**首跳跳转器不可达时为 SKIP 而非 FAIL**（本机 httpbin 长年被出口拦） |
| 5.5 | 真实探测可用 | `fd-source-probe probe https://ec.europa.eu/...tour_occ_nim... --expect-contains '"value"'` | `200 kind=ok`，`exit` 有值 |
| 5.6 | 审计落盘 | `ls -l /var/log/fd-source-probe/audit.jsonl` | 存在，每条含 caller/kind |

## 6. 舰队接线（探针上牌后才能做）

| # | 判据 | 怎么验 | 期望 |
|---|---|---|---|
| 6.1 | registryName 门转绿 | `python3 ~/.agents/skills/facet-pack-builder/scripts/cast_fleet_template.py fleet-packs/blueprints/industry-source-scouting` | registryName check OK（4 个名字全在册） |
| 6.2 | packId 台账回填 | `grep industry-source-scouting fleet-packs/README.md` | 有 packId 行 |
| 6.3 | 重铸后 manifest 与铸出物一致 | `cast_fleet_template.py <dir> --check` | `CHECK OK` |

## 回滚

```bash
kubectl --context cheap-ts -n argocd delete app fd-source-probe          # 停同步
kubectl --context cheap-ts -n fd-prod delete deploy,svc fd-source-probe   # 清工作负载
# 注册条目：DELETE /api/servers/fd-source-probe（有令牌时）
```
无消费者：唯一引用方是行业源侦察舰队 pack，尚未铸造 → 删了不影响任何在产链路。
