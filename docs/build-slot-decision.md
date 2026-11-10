# 待裁决一页 · 覆盖 `testaction003/build-pool` 出片

> 2026-11-10 · 配套 `deploy/onboard.sh --step 2`、`deploy/rehearse-slot.sh`、`docs/artifacts/rollback-slot.sh`
> **只等你一句授权 + 一枚注册令牌**，其余全部就绪。

## 一、风险面（全部实测，无估计）

| 问题 | 答案 | 依据 |
|---|---|---|
| 槽位现在是谁 | `fd-wanxing.git @ 53eeb8957` 的快照，**413** 个文件 | 池仓 HEAD `abea607`、`BUILD-INFO.json` |
| 它现在健康吗 | **不健康**：最近 10 次 run **全 failure**，每次 **9–14 秒**即挂 | 下表 |
| 挂的根因 | 池里的 `image.yml` 是 **fd-zhengxin 的**（`working-directory: server` / `context: web`），快照树里没有 `server/`、`web/` → bash 起不来 | 日志原文见下 |
| 覆盖会删什么 | **408 项** | `docs/artifacts/slot-would-delete.txt` |
| 删除构成 | `openspec/changes` 239 · `wanxing-web/src` 33 · `services/wanxing-facade` 20 · `openspec/specs` 13 · 其余 103 | 同上 |
| 会不会害到在产线 | **不会**：池已连续失败两天，无健康构建依赖它 | 下表 |
| 真风险是什么 | **同一时刻别的会话也在推这个池** → 互相覆盖（历史事故：`yizuo/fleet-observer` 曾被推成 `fd-legal-search-mcp`） | 池仓 62 提交，最近 5 个占位分属 fd-wanxing / law-bench / lex-ops-mcp / paas-ship / fd-memory |

### 「9–14 秒即挂」的证据（8 次，run id + 耗时 + 占位）

| run id | 耗时 | 占位 sha | 内容 |
|---|---|---|---|
| 42808513391 | **11s** | abea607 | fd-wanxing.git @ 53eeb8957 — snapshot |
| 42686263646 | **12s** | acaaa57 | law-bench @ 807ae5b05 — snapshot |
| 42648317902 | **10s** | a64b029 | lex-ops-mcp @ bae84c91c — snapshot |
| 42635337380 | **12s** | 2d809a0 | law-bench @ 0d2198940 — snapshot |
| 42547293501 | **14s** | 00ad1fa | paas-ship @ 1d983f871 — snapshot |
| 42481708538 | **10s** | 333f9e3 | image（手动 dispatch） |
| 42481406521 | **9s** | 333f9e3 | fd-memory @ 21ea49706 — snapshot |
| 42459488227 | **10s** | fd9e8ba | fd-proxy-service @ a42705046 — snapshot |

抽查 3 条日志原文（同一根因，非偶发）：

```
run 42808513391  ##[error]An error occurred trying to start process '/usr/bin/bash'
                 with working directory '/home/runner/work/build-pool/build-pool/server'. No such file or directory
run 42648317902  ##[error]An error occurred trying to start process '/usr/bin/bash'
                 with working directory '/home/runner/work/build-pool/build-pool/server'. No such file or directory
run 42459488227  ##[error]An error occurred trying to start process '/usr/bin/bash'
                 with working directory '/home/runner/work/build-pool/build-pool/server'. No such file or directory
```

> 判读：这个池从 **2026-11-08** 起就没出过成功镜像；每换一个项目占位，就把上一个项目的
> workflow 一起冲掉，新 workflow 又期望别的目录结构 → 周而复始 10 秒挂。
> **覆盖它 = 清掉一个已经坏了两天的快照，不是打断在产线。**

## 二、覆盖会做什么（顺序与失败点）

1. 设 4 个仓变量：`BUILD_NAMESPACE=finddata`、`BUILD_IMAGE=fd-source-probe`、`BUILD_DOCKERFILE=Dockerfile`、`BUILD_CONTEXT=.`
   —— **四个必须给齐**：池变量是全局的，只给两个会拿别人的 DF/CONTEXT 起构建
2. 登记 relay：`finddata/fd-source-probe` → cheap-3 `/etc/tcr-relay/repos.conf`（cron 下轮生效，hkccr→ccr 回灌）
3. `git push --force` 快照 → **408 删、5 改、21 增**
4. push 触发 `image.yml`：Shell lint + 测试门（52 条）→ 构建 → 推 `hkccr…/finddata-business/fd-source-probe:main` + 铸 `sha-<7>`
5. relay 把 hkccr 同步到 ccr（≤5 分钟）；k8s 清单 pin 的是 **ccr**

## 三、回滚（已演练）

```bash
bash docs/artifacts/rollback-slot.sh --dry-run                    # 只看差异，不推
bash docs/artifacts/rollback-slot.sh --simulate-overwrite --dry-run   # 证明覆盖后能复原
bash docs/artifacts/rollback-slot.sh                              # 真回滚：reset --hard + force push
```

锚点 `abea607c8a2ff58806f0977403ad6e736db48334`，远端 **62 提交**可列，`git cat-file -e` 实测可达。
镜像侧：已推 tag 不删（探针无消费者，留着无害）；ArgoCD 侧删 Application 即停同步。

## 四、回滚演练结果（`--simulate-overwrite --dry-run`，未推送）

```
== 回滚 testaction003/build-pool → abea607 · dry-run ==
  当前 HEAD : abea607c  fd-wanxing.git @ 53eeb8957 — snapshot
  目标锚点  : abea607  fd-wanxing.git @ 53eeb8957 — snapshot
  [模拟覆盖] 用本仓快照**完整替换**本地 clone 并提交（不推）
    覆盖后：26 个文件，HEAD=c5c192b7
    相对锚点的差异：434 项            ← 408 删 + 5 改 + 21 增
  已 reset 到锚点，工作树文件数: 413   ← 全数恢复
  [核对] 锚点内容与回滚目标是否一致：一致
  $ git push --force --dry-run origin main
    Everything up-to-date
✓ 演练完成（未推送）
```

**判读**：覆盖后 26 个文件 → 回滚后 **413 个文件、内容与锚点逐字节一致**。回滚可靠。

> 演练第一版把「模拟覆盖」写成 `tar -x`（只加不删），得到「434 个文件」的假覆盖、
> 差异只剩 26 项 —— 被自己的输出抓出来后改成「先清空再解包」，才与真实 rsync 的删除语义一致。
> 教训：**演练必须复刻真实工具的删除语义**，否则回滚演练是自欺。

## 五、需要你决定的两件

| # | 事项 | 选项 | 建议 |
|---|---|---|---|
| D1 | 覆盖 `testaction003/build-pool` 出片 | ① 授权覆盖（`--allow-snapshot-overwrite`）② 换 `testaction004/build-pool`（1609 min）③ 换公开线（免费无限，但 `platform-image-verify` 的 workflow 写死 law-bench/platform，需改他人工作流） | **①**：槽位已坏两天、无健康产线依赖；② 同样覆盖别人的快照且份额更少；③ 改他人工作流风险更高 |
| D2 | 注册处条目写入 | 给 `REGISTRY_TOKEN` → 脚本自动 POST；不给 → 打印 curl 人工执行 | 给令牌，一次跑完 |

## 六、授权后 10 分钟内可完成

```bash
bash deploy/onboard.sh --apply --allow-snapshot-overwrite --step 2   # 变量/relay/推快照/等 run/skopeo 查 digest
bash deploy/onboard.sh --apply --step 3                              # ArgoCD apply + rollout
bash deploy/onboard.sh --apply --step 4                              # 注册条目（D2 给令牌则自动写）
bash deploy/ssrf-neg.sh                                              # 集群内 8 条 SSRF 负例
```
