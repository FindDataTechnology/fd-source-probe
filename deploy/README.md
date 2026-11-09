# 部署与上牌 — fd-source-probe

## 形态

- **internal-only**：不建 Ingress，仅 mesh NodePort 可达（`cheap6.mesh.finddata.cn:31904`）。
  客户凭据面、商业名单表（`tool-surface.json` 的 `commercial_tools=[]`）都不存在，这是设计。
- **零凭据**：无 Secret。代理通路走集群内 forwarder 环境变量（`proxy-fw.scraw:8080`），
  选择/轮换/熔断全在 `proxy-service`，本服务 proxy-ignorant（design D5）。
- **审计落 emptyDir**：pod 重建即清空；取证时先拷（`kubectl cp`，勿用 `exec -i cat`）。
  若要持久，后续波次换 PVC，不在首铸范围。

## NodePort

`31904`（2026-11-10 实查 fd-prod 在用表：31891 与 31904 为仅有的两个空洞，取 31904）。

## 上牌五步（一次性；3-5 为对外动作，需主人一句授权）

| # | 动作 | 性质 |
|---|---|---|
| 1 | gitee 建仓 `FindDataTechnology/fd-source-probe` + 首推（本目录尚无 .git） | 对外（建公开仓） |
| 2 | `docker build` + 推 `ccr.ccs.tencentyun.com/finddata-business/fd-source-probe:main` | 对外（出片） |
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
