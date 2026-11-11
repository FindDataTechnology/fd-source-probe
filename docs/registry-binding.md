# fd-source-probe 的注册语义：spine 有行 ≠ 包能绑定

> 2026-11-11 实测。起因：给探针在 supply-store（spine）里落了注册行之后，
> 要回答一个真问题——**舰队 pack 里 `registryName: fd-source-probe` 到底能不能解析出工具面？**
> 结论是**不能**，而且原因是结构性的，不是配错了。这份文档把两条路径分开写清，免得
> 后来人看到「已注册」就以为装得上。

## 一、已注册的是什么（可复核）

| 事实 | 验证方式 | 结果 |
|---|---|---|
| spine `mcp_servers` 有行 | pod 内 `GET /api/supply-store/projection` | 在册，`visibility='internal'`、`groups=[]`、`num_tools=1`、`tool_list=['probe_endpoint']`、`metadata.surface.commercial_tools=[]` |
| 写操作留痕 | `supply_write_audit` | `actor=zcode/industry-source-solution-fleet`、`source=gate`；3.2s 后 `registry-service`/`facade` 复写但字段逐项保留 |
| 商业目录不暴露 | pod 内 `GET /api/mcp-catalog` | 26 条里 `fd-source-probe` **0 次** |
| **单条目也不可达** | pod 内 `GET /api/mcp-catalog/fd-source-probe` | **404 `{"error":"Server not found"}`** |
| 从 cell 宿主侧真能调 | platform pod 内 MCP `initialize` → `tools/call probe_endpoint` | **200**，固定形状完整，出口 `103.236.94.58` |

## 二、为什么包装不上（代码级判据）

`paas/pack-store.js` 的 `installMcpRef(name = ref.registryName)` 有两条解析路径：

1. **市场条目路径**（`isMarketInstallableEntry(entry)`）——`paas/registry-bridge.js:67`：
   ```js
   const MARKET_ORIGINS = new Set(["registry", "facet-catalog"]);
   export function isMarketInstallableEntry(entry) {
     return Boolean(entry) && MARKET_ORIGINS.has(entry.origin);
   }
   ```
   而这条路径的前置是 `extensionStore.findMarketMcpEntry(name)` 能拿到条目——**目录面对
   `visibility≠public` 的条目是 fail-closed 的**（`visibleToViewer` 只在 `visibility==='public'`
   时放行），实测 `/api/mcp-catalog/fd-source-probe` 直接 404。故这条路走不通。

2. **本地绑定路径**（`extensionStore.getMcpServer(name)`）——`pack-store.js:320`：
   ```js
   // No market entry: a local row of this name is a binding (registry entries
   // take precedence when both exist — the branch above).
   if (extensionStore.getMcpServer(name)) {
     return { name, status: "bound", reason: "bound to the local server of this name (its configuration is untouched)" };
   }
   return { name, status: "awaiting-local-binding", reason: "no market entry and no local server of this name yet …" };
   ```
   这条路才通，但**前提是那个 cell 里已经有一行同名（`fd-source-probe`）的本地 MCP server**。

所以：**spine 的行是台账/组织记录，不是安装来源。** 包引用要成立，必须在目标 cell 里
把探针加成**本地** MCP server，且名字逐字等于 `fd-source-probe`（本地绑定是**按名**相交，
配置不动、不写 extension 行、不过组闸、不套市场凭据）。

## 三、为什么不干脆改成 public（这条是刻意的，不是漏了）

探针能对**任意 URL** 出网探测一次（只读、私网拒连、逐跳复判、方法白名单）。若 `visibility='public'`：

- 会进商业目录（26 条 → 27 条），成为对外可见能力；
- 会落进市场安装路径，任何有 catalog 可见性的人都能把它装进自己的 cell；
- 等于把「从我们的出口探测任意地址」这项能力交出去——即便有 SSRF 守卫，
  它仍然是可被用于侧信道与扫描的基础设施。

**所以它必须停在 internal**，接受「包不能通过市场路径装它」这个代价。

## 四、要给舰队用，正确做法（待做，非本文件范围）

每个需要它的 cell 加一行本地 MCP server：

- `name`: `fd-source-probe`（**必须逐字相同**，本地绑定按名相交）
- `endpoint`: `http://cheap6.mesh.finddata.cn:31911/mcp`（internal，不建 Ingress）
- `auth`: 无（`auth_scheme=none`），不需要任何凭据引用
- 该 cell 内的 agent 才能拿到 `probe_endpoint` 这一个工具

### 可直接执行的配方（2026-11-11 从代码核过字段名）

平台侧新增本地 MCP server 的接口是 `POST /api/extensions/mcp`（`paas/server/routes/extensions.js:96`），
body = `{name, config, enabled}`。运行时按 `config.url` 判 `transport: "streamable-http"`
（`paas/dsh-profile.js:850-852`）；`config.credentialRef` 是**系统托管字段，客户端提交会被 400 拒**
（"credentialRef is system-managed"）——正好，探针零凭据，本来也不需要。

```json
{
  "name": "fd-source-probe",
  "enabled": true,
  "config": {
    "transport": "streamable-http",
    "url": "http://cheap6.mesh.finddata.cn:31911/mcp"
  }
}
```

```bash
# 在目标 cell 内（或对 platform 的 /api/extensions/mcp，需 mcp 管理权限）：
curl -sS -X POST http://127.0.0.1:3100/api/extensions/mcp \
  -H 'Content-Type: application/json' \
  -d '{"name":"fd-source-probe","enabled":true,
       "config":{"transport":"streamable-http","url":"http://cheap6.mesh.finddata.cn:31911/mcp"}}'
```

**判据（装完当场可验，不要靠"应该能行"）**：
1. cell 内 `tools/list` 出现且仅出现 `probe_endpoint`；
2. 包安装报告里该引用 `status` 为 **`bound`**（不是 `awaiting-local-binding`，也不是 `reused`）；
3. 真调一次 `probe_endpoint` 拿到 200 与固定形状出参（而不是只看 tools 列表）。

> **为什么还没建这一行**：目前没有任何 cell/包在用它（舰队 pack 尚未铸造）。
> 先建行等于给一个没有消费者的 cell 加配置 —— 那是"挂载了但没人用"，与本文件批的
> "声明了但没挂载"是同一枚硬币的两面。所以这里只备配方，等真有消费者再落。

## 五、教训（写给后来人）

- **「已注册」与「能装上」是两个判据**：前者看 spine 行，后者看 `findMarketMcpEntry`
  + `isMarketInstallableEntry` 的 origin 闸 + 目录可见性。只验前者会得到一句漂亮的假话。
- 这类"声明了但没挂载"的坑在本仓已出现多次（含 `installMcpRef` 的 origin 闸本身），
  判据应始终落在**消费端实测**（cell 里真调一次），而不是登记端的回执。
