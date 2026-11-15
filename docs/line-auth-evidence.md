# 源探针线鉴权与计费证据（公开清单之外的逐字记录）

> 本文件是 `line-manifest.json` 自由文本字段的**逐字**归档。清单的每个字符串都可能匿名公开，
> 所以那些字段已从清单里删除（2026-11-15，任务=converge-line-identity-and-keys 的公开载荷清理批次 I）。
> 本线的暴露路径与其它线**不同**：镜像里没有清单（Dockerfile 不 COPY，2026-11-15 线内实测
> `/.well-known/line-manifest.json` = 404、`/healthz` 才是健康面），真正把这几个字符串投出去的
> 是 `fd-prod/line-manifests` 这份 ConfigMap——平台 pod 挂在 `/etc/line-manifests/source-probe/`
> （实测 1606 B）。所以这次删除收窄的是**投影面**，不是某个已经存在的公网正文。
> 本文件不随镜像发布、不进门投影。

## `note` — 顶层 note：为何「无回退轨」是设计选择，不是缺陷

```text
**内部专用线**：tool-surface.json 的 `hosting=internal-only`、描述明写「不进对外商业工具名单」；1 个只读工具，给源解决方案舰队产带证据的生成单（SSRF 防线在工具内）。因此「无回退轨」是设计选择而非缺陷：它本就不该被外部客户直接消费。若将来转对外，必须先加线自有钥匙再谈商业供给。
```

## `identity.note` — identity.note：本仓零入站凭据校验

```text
本仓零入站凭据校验：`FastMCP(SERVER_NAME, ...)` 未传 auth、未挂中间件（server.py:76-78），HTTP 面只有 `/mcp` + 无门 `/healthz`（server.py:81-88）。无断言兑换端点，jit_policy=off。
```

## `auth_tracks[0].note` — auth_tracks[0].note（ciyuan-sk 客户轨）

```text
未接：本仓不解析任何入站凭据，词元钥匙打进来只被当作不存在。
```

## `billing.note` — billing.note：无计费代码且本线不对外供给

```text
本仓无计费代码；且 hosting=internal-only，本线不在对外商业供给面，计量不适用。记 ciyuan 只为与其它线同口径（真扣费只发生在 registry/网关）。
```
