"""SSRF 防线：目标解析 + 拒绝集 + 逐跳复判。

判据只有一条：**解析出来的每个地址都必须是全局可路由地址**（``ip.is_global``）。
这一条同时覆盖私网、回环、链路本地、CGNAT/tailnet（100.64/10 不是 ``is_private``
但一定不是 ``is_global``）、文档段与保留段——用 ``is_global`` 而不是列黑名单，
是因为漏一个网段就等于没防线。

宿主名侧另有一道：单标签主机名（集群内服务名形态）与已知内网后缀一律拒。
域名白名单（``FD_PROBE_ALLOWED_HOSTS``）非空时退化为白名单，但仍先过拒绝集。

**已知残余风险**：解析与连接之间可能发生 DNS rebinding。本服务的定位是内部
侦察工具、调用方是自家舰队，残余风险按「进程隔离 + 域名/地址判据 + 审计」接受，
不在此实现连地址固定（那会牵动 TLS SNI 与连接池，代价与收益不成比例）。
"""

from __future__ import annotations

import ipaddress
import socket
from dataclasses import dataclass
from urllib.parse import urlsplit

from .config import Settings


class GuardError(Exception):
    """目标被拒。``reason`` 是可读原因，直接进探测结果。"""

    def __init__(self, reason: str, *, host: str = "", ip: str = "") -> None:
        super().__init__(reason)
        self.reason = reason
        self.host = host
        self.ip = ip

    def to_dict(self) -> dict:
        return {"reason": self.reason, "host": self.host, "ip": self.ip}


@dataclass(frozen=True)
class Target:
    url: str
    scheme: str
    host: str
    port: int
    addresses: tuple[str, ...]


ALLOWED_SCHEMES = ("http", "https")


def _host_denied(host: str, settings: Settings) -> str | None:
    h = host.strip().lower().rstrip(".")
    if not h:
        return "空主机名"
    if h == "localhost" or h.endswith(".localhost"):
        return "回环主机名"
    for suffix in settings.denied_host_suffixes:
        s = suffix.strip().lower()
        if s and (h == s.lstrip(".") or h.endswith(s)):
            return f"命中内网后缀 {s}"
    # 单标签主机名：公网不可能解析，通常是集群内服务名
    if "." not in h and ":" not in h:
        return "单标签主机名（集群内服务形态）"
    if settings.allowed_hosts:
        ok = any(
            h == a.lower() or h.endswith("." + a.lower()) for a in settings.allowed_hosts
        )
        if not ok:
            return "不在探测白名单内"
    return None


def _ip_denied(raw: str) -> str | None:
    try:
        ip = ipaddress.ip_address(raw)
    except ValueError:
        return "非法的地址字面量"
    mapped = getattr(ip, "ipv4_mapped", None)
    if mapped is not None:
        ip = mapped
    if ip.is_unspecified:
        return "未指定地址"
    if ip.is_multicast:
        return "组播地址"
    if not ip.is_global:
        if getattr(ip, "is_loopback", False):
            return "回环地址"
        if getattr(ip, "is_link_local", False):
            return "链路本地地址"
        if getattr(ip, "is_private", False):
            return "私网地址"
        return "非全局可路由地址（保留/共享地址段）"
    return None


def resolve(host: str, port: int, *, timeout: float = 5.0) -> tuple[str, ...]:
    """解析主机名到全部地址。解析失败抛 GuardError（不是网络错误）。"""
    try:
        # 字面量直接过，避免无意义解析
        ipaddress.ip_address(host)
        return (host,)
    except ValueError:
        pass
    try:
        infos = socket.getaddrinfo(host, port, proto=socket.IPPROTO_TCP)
    except socket.gaierror as exc:
        raise GuardError(f"主机名无法解析：{exc.strerror or exc}", host=host) from exc
    addrs: list[str] = []
    for info in infos:
        addr = info[4][0]
        if addr not in addrs:
            addrs.append(addr)
    if not addrs:
        raise GuardError("主机名未解析到任何地址", host=host)
    return tuple(addrs)


def check_url(url: str, settings: Settings) -> Target:
    """校验一个 URL 是否可探测。任何一条不满足即抛 GuardError。"""
    parts = urlsplit(url)
    scheme = (parts.scheme or "").lower()
    if scheme not in ALLOWED_SCHEMES:
        raise GuardError(f"不支持的协议 {scheme or '(空)'}（仅 http/https）")
    host = parts.hostname or ""
    denied = _host_denied(host, settings)
    if denied:
        raise GuardError(denied, host=host)
    port = parts.port or (443 if scheme == "https" else 80)
    addresses = resolve(host, port)
    for addr in addresses:
        why = _ip_denied(addr)
        if why:
            # 任一解析结果落在禁止段即整体拒绝：调用方无法只取安全的那一条
            raise GuardError(f"目标解析到{why}", host=host, ip=addr)
    return Target(url=url, scheme=scheme, host=host, port=port, addresses=addresses)
