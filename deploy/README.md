# 部署与上牌 — fd-source-probe

## 形态

- **internal-only**：不建 Ingress，仅 mesh NodePort 可达（`cheap6.mesh.finddata.cn:31904`）。
  客户凭据面、商业名单表（`tool-surface.json` 的 `commercial_tools=[]`）都不存在，这是设计。
- **零凭据**：无 Secret。代理通路走集群内 forwarder 环境变量（`proxy-fw.scraw:8080`），
  选择/轮换/熔断全在 `proxy-service`，本服务 proxy-ignorant（design D5）。
- **审计落 emptyDir**：pod 重建即清空；取证时先拷（`kubectl cp`，勿用 `exec -i cat`）。
  若要持久，后续波次换 PVC，不在首铸范围。

## 出片前置（2026-11-10 勘验）

- **不要用共享构建池出这片**：`fd-build-snapshot fd-source-probe -R testaction003/build-pool` 的 dry-run 显示它会**删掉当前占位项目的 1742 个文件**（池是共享槽位，一项目一占位），且此刻镜像无人可部署（仓与 Argo 都还没有）——白占槽位还挤掉并发会话。
- 正解：仓内 `image.yml` 走 GitHub → hkccr。仓建好后 push 即出片，不占共享池。

## NodePort

`31911`（2026-11-10 实查**全集群**空闲）。

> ⚠️ 首版写的 31904 是错的：当时只查了 `-n fd-prod`，而 **NodePort 是集群级资源**——
> 31904 其实被 `chatwoot/chatwoot-web` 占着（fd-prod 里看不见）。server dry-run 当场报
> `provided port is already allocated`。查法必须是 `kubectl get svc -A`。

## 一键脚本与校验清单

- `deploy/onboard.sh` — 上牌四步一键执行（`--dry-run` 默认只预检；`--apply` 真跑；`--step N` 单步）
- `deploy/VERIFY.md` — 逐条可判绿判红的校验清单（0 前置 / 1 首推 / 2 镜像 / 3 ArgoCD / 4 注册 / 5 实证 / 6 舰队接线）
- `deploy/ssrf-neg.sh` — §5.3/§5.4 的集群内 SSRF 负例（`--dry-run` 占用+报错路径自检 / `--local` 本机预演 / 无参集群真跑）

脚本纪律：fail-closed（预检不满足就停）、幂等（重复跑不产生第二份资源）、不猜（注册条目无令牌只打印 curl）。

## 上牌五步（一次性；3-5 为对外动作，需主人一句授权）

| # | 动作 | 性质 |
|---|---|---|
| 1 | gitee 建仓 `FindDataTechnology/fd-source-probe`（私有即可）→ 本地仓已就绪（commit `0029cb3`，remote `gitee` 已配）：建仓后一条命令 `git -C fd-source-probe push -u gitee main`。**2026-11-10 勘验**：本机无 GITEE_TOKEN；Argo 的 5 个 repo secret 全是 SSH deploy key（org 级只读拉取，实测 ssh -T 认证为寻数科技），建仓用不上——必须主人网页建仓或给 API 令牌 | 对外（建私有仓） |
| 2 | 出片：**仓内已有 `.github/workflows/image.yml`**（照 zhengxin 配方：测试门 → hkccr → 内容闸门铸 `sha-<7>`），仓建好首次 push 即自动出片；前置=仓上配 `TCR_USERNAME`/`TCR_PASSWORD` | 对外（出片） |
| 3 | argocd ns 建 repo 凭据 secret `repo-fd-source-probe` | 集群内 |
| 4 | `kubectl apply -f deploy/argocd/application.yaml`（automated+selfHeal） | 集群内 |
| 5 | 注册处条目 `POST /api/servers`：path=/fd-source-probe、auth_scheme=none、endpoint=mesh:31904/mcp、tool-surface 随仓；surface 名单表 commercial=0/internal=1 | 对外（供给面） |

## 上牌后收尾

1. 真 e2e：直连 `initialize→tools/list` 枚举恰好 `probe_endpoint`（从 cheap-6 节点测，
   不经 mcp 网关——网关剥 auth 的旧结论与本服务无关，auth_scheme=none）。
2. SSRF 负例复测跑在**集群内**（`kubectl exec` 进 pod 打两条负例），本机绿的防线
   要在真实出网环境再绿一次。
3. 回填 `fleet-packs/README` §packId 台账 → 重跑 `cast_fleet_template.py`（自引用占位解析）。
4. `MCP-REGISTRY.md` §1.1 落行（按 `/fd-zhengxin-reference` 行形状，visibility=internal）。

## 回滚

删除 Application + Service + Deployment + 注册条目即净退；无消费者（唯一的引用方
是行业源侦察舰队 pack，尚未铸造）。
