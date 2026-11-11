"""fd-source-probe：源侦察探针（internal-only）。

设计纪律（见 openspec change `industry-source-solution-fleet`，2026-11-11 已归档为
`openspec/changes/archive/2026-11-11-industry-source-solution-fleet/`；纪律本身已并进
`openspec/specs/source-probe/spec.md`，`openspec list` 里不再列出该 change）：

- 只读：只发 GET/HEAD 与受控提交，永不发写语义请求（D6 / spec `source-probe`）。
- 出参固定形状：状态码、耗时、响应头、出口身份、响应体前缀、代理标识；
  字段集不随调用方变化，使同一请求可重复发起并逐字段对拍。
- 出口复用既有代理通路：只持一条 forwarder 配置与 acquire/release 语义，
  代理选择、轮换、熔断全部留在 `proxy-service`（D5）。
- SSRF 防线是逐跳判定 + 拒绝集：私网/回环/链路本地/保留地址/集群服务名一律拒，
  重定向逐跳复判（D6）。
"""

__version__ = "0.1.0"
