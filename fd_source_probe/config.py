"""探针配置。环境变量前缀 ``FD_PROBE_``。

只放探针自己的形状参数；出口策略不在本文件——那是 ``proxy-service`` 的事。
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

DEFAULT_DENIED_SUFFIXES = (
    ".localhost",
    ".local",
    ".internal",
    ".intranet",
    ".svc",
    ".cluster.local",
    ".mesh.finddata.cn",
    ".in-addr.arpa",
    ".ip6.arpa",
)

DEFAULT_USER_AGENT = (
    "fd-source-probe/0.1 (+source scouting; read-only; contact=ops@finddata.cn)"
)


def _csv(value: str) -> tuple[str, ...]:
    return tuple(x.strip() for x in value.split(",") if x.strip())


@dataclass
class Settings:
    # 出口：既有通路。forwarder 是 HTTP 代理地址；control 是 proxy-service 控制面。
    proxy_forwarder: str = field(
        default_factory=lambda: os.environ.get("FD_PROBE_PROXY_FORWARDER", "")
    )
    proxy_control: str = field(
        default_factory=lambda: os.environ.get("FD_PROBE_PROXY_CONTROL", "")
    )
    proxy_source: str = field(
        default_factory=lambda: os.environ.get("FD_PROBE_PROXY_SOURCE", "source-probe")
    )

    # 出口身份：经同一通路向 echo 端点问一次自己的 IP（沿用 proxy-service 的
    # MIHOMO_EXIT_PROBE_URL 手法——出口必须是观测值，不是假设值）。
    # 默认用 ifconfig.me/ip（纯 IP 回显）：实测 api.ipify.org 在本机不可达（000），
    # 而 cloudflare trace 返回多行；换到可用且形状最简单的一个。
    exit_echo_url: str = field(
        default_factory=lambda: os.environ.get(
            "FD_PROBE_EXIT_ECHO_URL", "https://ifconfig.me/ip"
        )
    )
    exit_echo_enabled: bool = field(
        default_factory=lambda: os.environ.get("FD_PROBE_EXIT_ECHO", "1") != "0"
    )
    exit_echo_timeout_s: float = field(
        default_factory=lambda: float(os.environ.get("FD_PROBE_EXIT_ECHO_TIMEOUT", "8"))
    )

    # SSRF 拒绝集。allowed_hosts 非空时退化为白名单（仍先过拒绝集）。
    denied_host_suffixes: tuple[str, ...] = field(
        default_factory=lambda: _csv(
            os.environ.get("FD_PROBE_DENIED_SUFFIXES", ",".join(DEFAULT_DENIED_SUFFIXES))
        )
    )
    allowed_hosts: tuple[str, ...] = field(
        default_factory=lambda: _csv(os.environ.get("FD_PROBE_ALLOWED_HOSTS", ""))
    )

    # TLS 档位。'default' 走系统默认安全级别；'legacy' 放宽到 SECLEVEL=1，
    # 用于仍在用旧套件/弱 DH 的上游（实测：data.mofcom.gov.cn 在默认档下
    # SSLV3_ALERT_HANDSHAKE_FAILURE，放宽后 200）。档位是一等输出字段，
    # 因为"可达"必须连同"用什么客户端配置可达"一起记——否则接入档案不可复现。
    tls_profiles: tuple[str, ...] = field(
        default_factory=lambda: _csv(os.environ.get("FD_PROBE_TLS_PROFILES", "default,legacy"))
    )

    max_body_bytes: int = field(
        default_factory=lambda: int(os.environ.get("FD_PROBE_MAX_BODY_BYTES", "2048"))
    )
    timeout_s: float = field(
        default_factory=lambda: float(os.environ.get("FD_PROBE_TIMEOUT", "20"))
    )
    max_redirects: int = field(
        default_factory=lambda: int(os.environ.get("FD_PROBE_MAX_REDIRECTS", "5"))
    )
    max_requests_per_minute: int = field(
        default_factory=lambda: int(os.environ.get("FD_PROBE_MAX_RPM", "20"))
    )
    max_header_value_chars: int = field(
        default_factory=lambda: int(os.environ.get("FD_PROBE_MAX_HEADER_CHARS", "512"))
    )
    user_agent: str = field(
        default_factory=lambda: os.environ.get("FD_PROBE_USER_AGENT", DEFAULT_USER_AGENT)
    )

    # 审计（spec：可查到调用者、时间、目标与结果类别）。
    audit_log: str = field(
        default_factory=lambda: os.environ.get(
            "FD_PROBE_AUDIT_LOG", str(Path.home() / ".fd-source-probe" / "audit.jsonl")
        )
    )

    @classmethod
    def from_env(cls) -> "Settings":
        return cls()

    def proxy_configured(self) -> bool:
        return bool(self.proxy_control or self.proxy_forwarder)
