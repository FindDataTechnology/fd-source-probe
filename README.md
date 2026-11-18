# fd-source-probe — 源侦察探针（internal-only）

> **Part of the [Wire line (寻数·柏讯)](https://www.finddatatech.cloud/products/wire) of [FindData](https://www.finddatatech.cloud)** — Source probe — checks whether an upstream source is reachable and what it returns.

一个只读探针 MCP：对候选数据源做**一次可复现的实测**，返回状态码、耗时、响应头、
出口身份与响应体前缀，供源解决方案舰队产出带证据的生成单。

它的存在理由：`INDUSTRY-DATA-SOURCES.md` 的每条判断（`直连=200 / 代理=200`、
`T+35 天`、`返回全序列 JSON`、`WAF 两步 cookie`）都必须来自观测。现有工具面拿不到
这些——`eco-mcp-fetch` 只把网页转文本（无状态码、无代理选择、无 POST），
`fd-scraw-harness.analyze_page` 走浏览器（且受 computer-use 禁令约束），
`fetch_log` 没有响应头/body/exit_ip 三列。本服务补的就是这一件。

## 工具面（恰好一个）

| 工具 | 说明 |
|---|---|
| `probe_endpoint` | 只读探测一个端点，返回固定形状的实测结果 |

入参：`url`（必填）、`method`（GET/HEAD/POST）、`headers`、`body`、`via_proxy`、
`expect_contains`、`expect_content_type`、`tls_profile`、`caller`、`label`。

出参字段集固定（不随调用方变化，才能逐字段对拍）：

```
schema ok url final_url method status elapsed_ms headers body_prefix
body_bytes_read body_truncated proxy exit tls hops verdict error
```

`verdict.kind` 是四分类之一：`ok` / `channel_failure`（通道故障）/
`credential_invalid`（凭据无效）/ `not_published`（源未发布）/
`caliber_mismatch`（口径不符）。

## TLS 档位（实施期增补）

`tls_profile` 取 `default` 或 `legacy`（`SECLEVEL=1`）。档位进固定出参，
未知档位在发出请求前被拒（不静默降级）。

**这条是被实测逼出来的**：`data.mofcom.gov.cn` 在 Python 默认安全级别下
`SSLV3_ALERT_HANDSHAKE_FAILURE`，放宽到 `SECLEVEL=1` 后 200 并拿到真数据；
但 `curl`（macOS SecureTransport）直连即可。若探针只有一档，就会把一个**实际可接**
的源判成不可达——那是探针自己制造的假阴性，恰好是本仓最反的「读不到当成不存在」。
档位写进证据，是为了让「可达」连同「用什么客户端配置可达」一起可复现。

## 纪律（不是建议，是判据）

1. **只读**：仅 GET/HEAD/POST。PUT/DELETE/PATCH/TRACE/OPTIONS 在发出请求前被拒，
   不产生任何出网流量（有单测钉住 `call_count == 0`）。
2. **SSRF 防线**：目标是私网/回环/链路本地/保留或共享地址段（含 `100.64/10`
   这类既非 `is_private` 也非 `is_global` 的段）、集群内服务名（单标签主机名、
   `.svc` / `.cluster.local` / `.mesh.finddata.cn` 等后缀）一律拒。判据用
   `ip.is_global` 而不是列黑名单——漏一个网段等于没防线。
3. **逐跳复判**：重定向手工跟随，每一跳重新过防线。公网首跳 302 到 `127.0.0.1`
   是经典绕法，有负例单测。
4. **代理不降级**：`via_proxy=true` 时代理不可用就如实报 `proxy_unavailable`，
   **绝不以直连结果冒充经代理结果**。出口策略全在 `proxy-service`，本服务不做
   选择、不做熔断。
5. **出口是观测值**：经同一出口向 echo 端点问一次自己的 IP（5 分钟缓存），
   沿用 `proxy-service` 的 `probe_exit_ip` 手法。
6. **一次失败不判死源**：四分类只说明这一次为什么没拿到东西；源死亡是行业盯梢
   产出的「疑似源死」差异条目，由人工裁决。
7. **内部专用**：`tool-surface.json` 的 `commercial_tools` 为空是设计，不是遗漏。

## 环境变量

| 变量 | 默认 | 说明 |
|---|---|---|
| `FD_PROBE_PROXY_CONTROL` | 空 | `proxy-service` 控制面地址；设置后 `via_proxy` 走 `acquire/release` |
| `FD_PROBE_PROXY_FORWARDER` | 空 | HTTP 代理地址（控制面未配时的退路） |
| `FD_PROBE_PROXY_SOURCE` | `source-probe` | 向控制面申请上游时使用的 source 名 |
| `FD_PROBE_EXIT_ECHO_URL` | `https://api.ipify.org` | 出口身份回显端点 |
| `FD_PROBE_EXIT_ECHO` | `1` | 设 `0` 关闭出口回显（省流量） |
| `FD_PROBE_DENIED_SUFFIXES` | 内网后缀集 | 逗号分隔的主机名后缀拒绝集 |
| `FD_PROBE_ALLOWED_HOSTS` | 空 | 非空则退化为白名单（仍先过拒绝集） |
| `FD_PROBE_TLS_PROFILES` | `default,legacy` | 允许的 TLS 档位集 |
| `FD_PROBE_MAX_BODY_BYTES` | `2048` | 响应体读取上限 |
| `FD_PROBE_TIMEOUT` | `20` | 单次请求超时（秒） |
| `FD_PROBE_MAX_REDIRECTS` | `5` | 重定向跳数上限 |
| `FD_PROBE_MAX_RPM` | `20` | 进程级速率上限 |
| `FD_PROBE_AUDIT_LOG` | `~/.fd-source-probe/audit.jsonl` | 审计（调用者/时间/目标/结果类别） |

## 跑

```bash
uv venv --python 3.12 && uv pip install -e ".[dev]"
.venv/bin/python -m pytest -q          # 51 条，全离线
.venv/bin/fd-source-probe serve --port 8000
curl -s localhost:8000/healthz
```

真实出网探测与取证（不进常规套件）：

```bash
# 三类形态各一：免 key 直连 JSON / POST 表单（需 legacy TLS）/ 已知按出口拦截
.venv/bin/python scripts/probe_evidence.py --label 2026-11-10-smoke
# 证据落 data-source-scouting/evidence/<label>-probe.{json,md}
```

## 已知残余风险

解析与连接之间可能发生 DNS rebinding。本服务定位为内部侦察工具、调用方是自家舰队，
按「进程隔离 + 地址判据 + 审计」接受该残余风险；不实现连接地址固定（会牵动 TLS SNI
与连接池，代价与收益不成比例）。若探针未来对外或跨信任边界使用，必须先补这一层。
