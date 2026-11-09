"""探测核心：一次只读请求，出参固定形状。

形状契约（spec `source-probe`）：单次探测返回的字段集**不随调用方变化**——
状态码、耗时、响应头、出口身份、响应体前缀、代理标识，加上逐跳轨迹与四分类
判定。同一端点重复探测的字段集完全一致，才能逐字段对拍（这是「实测」与
「推断」的分界）。
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urljoin

import httpx

from . import audit, classify, guard
from .config import Settings

SCHEMA = "source-probe/v1"

ALLOWED_METHODS = ("GET", "HEAD", "POST")

# TLS 档位 → (是否放宽安全级别, 人读说明)。档位是一等输出字段：
# "可达"必须连同"用什么客户端配置可达"一起记，否则接入档案不可复现。
TLS_PROFILES = {
    "default": (False, "系统默认安全级别"),
    "legacy": (True, "SECLEVEL=1（上游用旧套件/弱 DH；实测 mofcom 需要此档）"),
}

# 出口身份缓存：同一出口 5 分钟内复用观测值，避免每次探测都多打一次 echo。
_EXIT_CACHE: dict[str, tuple[float, str | None]] = {}
_EXIT_TTL_S = 300.0

# 全局速率闸（按进程）。侦察量小，先只做全局；按域名配额按需再加（design Open Questions）。
_RATE_LOCK: dict[str, float] = {}


@dataclass
class Hop:
    url: str
    status: int | None = None
    location: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {"url": self.url, "status": self.status, "location": self.location}


@dataclass
class ProxyInfo:
    requested: bool = False
    used: bool = False
    kind: str = "none"  # none | forwarder | control
    id: str | None = None
    upstream: str | None = field(default=None, repr=False)
    token: str | None = field(default=None, repr=False)

    def to_dict(self) -> dict[str, Any]:
        # upstream/token 不外泄（可能内含出口凭据）
        return {"requested": self.requested, "used": self.used, "kind": self.kind, "id": self.id}


def _rate_limit(settings: Settings) -> None:
    if settings.max_requests_per_minute <= 0:
        return
    interval = 60.0 / settings.max_requests_per_minute
    now = time.monotonic()
    last = _RATE_LOCK.get("global", 0.0)
    wait = interval - (now - last)
    if wait > 0:
        time.sleep(wait)
    _RATE_LOCK["global"] = time.monotonic()


async def _acquire(settings: Settings, client: httpx.AsyncClient) -> ProxyInfo:
    info = ProxyInfo(requested=True)
    if settings.proxy_control:
        try:
            resp = await client.post(
                urljoin(settings.proxy_control.rstrip("/") + "/", "acquire"),
                json={"source": settings.proxy_source},
                timeout=min(settings.timeout_s, 10.0),
            )
            data = resp.json() if resp.headers.get("content-type", "").startswith("application/json") else {}
        except Exception as exc:  # noqa: BLE001 - 代理不可用要如实上报
            raise guard.GuardError(f"代理控制面不可用：{type(exc).__name__}: {exc}") from exc
        upstream = data.get("upstream") or data.get("address") or data.get("url")
        if resp.status_code >= 400 or not upstream:
            raise guard.GuardError(
                f"代理控制面无可用上游（HTTP {resp.status_code}）"
            )
        info.used = True
        info.kind = "control"
        info.upstream = upstream
        info.token = data.get("token")
        info.id = data.get("id") or data.get("addr_id") or upstream
        return info
    if settings.proxy_forwarder:
        info.used = True
        info.kind = "forwarder"
        info.upstream = settings.proxy_forwarder
        info.id = settings.proxy_forwarder
        return info
    raise guard.GuardError("要求经代理探测，但未配置代理通路")


async def _release(settings: Settings, client: httpx.AsyncClient, info: ProxyInfo, outcome: str) -> None:
    if info.kind != "control" or not info.token or not settings.proxy_control:
        return
    try:
        await client.post(
            urljoin(settings.proxy_control.rstrip("/") + "/", "release"),
            json={"token": info.token, "outcome": outcome},
            timeout=5.0,
        )
    except Exception:  # noqa: BLE001 - 释放失败只影响熔断精度，不影响本次结果
        pass


def _parse_exit_ip(text: str) -> str | None:
    """从 echo 响应里取出出口地址。取不到就返回 None——绝不把错误页当出口。

    判据沿用 proxy-service 的 `_is_ip` 纪律：只接受能解析成地址的字面量。
    兼容三种形状：纯 IP（ifconfig.me）、`ip=…` 行（cloudflare trace）、
    多行文本里只有一行是 IP。
    """
    import ipaddress

    candidates: list[str] = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        if line.startswith("ip="):
            candidates.append(line[3:].strip())
        candidates.append(line)
        for token in line.replace(",", " ").split():
            candidates.append(token.strip())
    for cand in candidates:
        try:
            return str(ipaddress.ip_address(cand))
        except ValueError:
            continue
    return None


async def _exit_identity(
    settings: Settings, proxy_upstream: str | None
) -> tuple[str | None, str]:
    """经同一出口向 echo 端点问一次自己的 IP。出口必须是观测值，不是假设值。"""
    if not settings.exit_echo_enabled or not settings.exit_echo_url:
        return None, "disabled"
    key = proxy_upstream or "direct"
    now = time.monotonic()
    cached = _EXIT_CACHE.get(key)
    if cached and now - cached[0] < _EXIT_TTL_S:
        return cached[1], "cached"
    kwargs: dict[str, Any] = {"timeout": settings.exit_echo_timeout_s}
    if proxy_upstream:
        kwargs["proxy"] = proxy_upstream
    value: str | None = None
    try:
        async with httpx.AsyncClient(**kwargs) as client:
            resp = await client.get(settings.exit_echo_url)
            if resp.status_code == 200:
                value = _parse_exit_ip(resp.text)
    except Exception:  # noqa: BLE001 - echo 失败只让出口未知，不影响探测本身
        value = None
    _EXIT_CACHE[key] = (now, value)
    return value, "echo" if value else "unknown"


def _header_map(resp: httpx.Response, settings: Settings) -> dict[str, str]:
    out: dict[str, str] = {}
    for k, v in resp.headers.items():
        key = k.lower()
        out[key] = v[: settings.max_header_value_chars]
    return dict(sorted(out.items()))


def _ssl_context(profile: str):
    """按档位造 SSL 上下文。未知档位在调用入口就被拒，这里不兜底。"""
    import ssl

    relax, _ = TLS_PROFILES[profile]
    ctx = ssl.create_default_context()
    if relax:
        ctx.set_ciphers("DEFAULT@SECLEVEL=1")
    return ctx


async def probe(
    url: str,
    *,
    method: str = "GET",
    headers: dict[str, str] | None = None,
    body: str | None = None,
    via_proxy: bool = False,
    expect_contains: str | None = None,
    expect_content_type: str | None = None,
    tls_profile: str = "default",
    caller: str = "",
    label: str = "",
    settings: Settings | None = None,
) -> dict[str, Any]:
    settings = settings or Settings.from_env()
    started = time.perf_counter()
    method = (method or "GET").upper()
    tls_profile = (tls_profile or "default").lower()
    hops: list[Hop] = []
    proxy = ProxyInfo(requested=via_proxy)

    def finish(verdict: classify.Verdict, **extra: Any) -> dict[str, Any]:
        elapsed_ms = int((time.perf_counter() - started) * 1000)
        result: dict[str, Any] = {
            "schema": SCHEMA,
            "ok": verdict.kind == classify.OK,
            "url": url,
            "final_url": hops[-1].url if hops else url,
            "method": method,
            "status": hops[-1].status if hops else None,
            "elapsed_ms": elapsed_ms,
            "headers": {},
            "body_prefix": "",
            "body_bytes_read": 0,
            "body_truncated": False,
            "proxy": proxy.to_dict(),
            "exit": {"identity": None, "source": "unknown"},
            "tls": {"profile": tls_profile, "note": TLS_PROFILES.get(tls_profile, (False, ""))[1]},
            "hops": [h.to_dict() for h in hops],
            "verdict": verdict.to_dict(),
            "error": None,
        }
        result.update(extra)
        audit.record(
            settings.audit_log,
            {
                "caller": caller,
                "label": label,
                "url": url,
                "final_url": result["final_url"],
                "method": method,
                "status": result["status"],
                "kind": verdict.kind,
                "via_proxy": via_proxy,
                "proxy_kind": proxy.kind,
                "proxy_id": proxy.id,
                "elapsed_ms": elapsed_ms,
            },
        )
        return result

    # 1) 方法白名单（spec：只读；写语义方法一律拒且不产生出网流量）
    if method not in ALLOWED_METHODS:
        return _reject_method(url, method, caller, label, settings, started, proxy)

    # 1b) TLS 档位白名单（未知档位不发请求，避免"静默按默认档跑"）
    if tls_profile not in TLS_PROFILES:
        return _reject_profile(
            url, method, tls_profile, caller, label, settings, started, proxy
        )

    # 2) SSRF 首跳校验
    try:
        guard.check_url(url, settings)
    except guard.GuardError as exc:
        return finish(
            classify.Verdict(classify.CHANNEL_FAILURE, f"目标被拒：{exc.reason}", "SSRF 防线"),
        ) | {"error": {"type": "guard_rejected", **exc.to_dict()}}

    _rate_limit(settings)

    # 3) 出口
    control_client = httpx.AsyncClient(timeout=10.0)
    try:
        if via_proxy:
            try:
                proxy = await _acquire(settings, control_client)
            except guard.GuardError as exc:
                return finish(
                    classify.Verdict(
                        classify.CHANNEL_FAILURE,
                        f"代理不可用：{exc.reason}",
                        "不以降级直连冒充经代理结果",
                    ),
                    proxy=proxy.to_dict(),
                    error={"type": "proxy_unavailable", "message": exc.reason},
                )
    finally:
        pass

    client_kwargs: dict[str, Any] = {
        "timeout": settings.timeout_s,
        "follow_redirects": False,
        "headers": {"user-agent": settings.user_agent},
        "verify": _ssl_context(tls_profile),
    }
    if proxy.used and proxy.upstream:
        client_kwargs["proxy"] = proxy.upstream

    req_headers = {"user-agent": settings.user_agent, **(headers or {})}
    content = body.encode("utf-8") if body is not None else None

    current = url
    verdict: classify.Verdict | None = None
    status_code: int | None = None
    header_map: dict[str, str] = {}
    body_text = ""
    body_read = 0
    truncated = False
    redirect_guard_error: dict[str, Any] | None = None

    try:
        async with httpx.AsyncClient(**client_kwargs) as client:
            for hop_index in range(settings.max_redirects + 1):
                hop = Hop(url=current)
                hops.append(hop)
                try:
                    async with client.stream(
                        method, current, headers=req_headers, content=content
                    ) as resp:
                        hop.status = resp.status_code
                        status_code = resp.status_code
                        header_map = _header_map(resp, settings)
                        location = resp.headers.get("location")
                        if 300 <= resp.status_code < 400 and location:
                            hop.location = location
                            nxt = urljoin(current, location)
                            # 逐跳复判：公网首跳 302 到内网，必须在第二跳被拒
                            try:
                                guard.check_url(nxt, settings)
                            except guard.GuardError as exc:
                                verdict = classify.Verdict(
                                    classify.CHANNEL_FAILURE,
                                    f"重定向目标被拒（第 {hop_index + 2} 跳）：{exc.reason}",
                                    "逐跳 SSRF 复判",
                                )
                                redirect_guard_error = {"type": "guard_rejected", **exc.to_dict()}
                                break
                            current = nxt
                            continue
                        # 读体（带上限）
                        if method != "HEAD":
                            buf: list[bytes] = []
                            async for chunk in resp.aiter_bytes():
                                if body_read + len(chunk) > settings.max_body_bytes:
                                    keep = settings.max_body_bytes - body_read
                                    if keep > 0:
                                        buf.append(chunk[:keep])
                                        body_read += keep
                                    truncated = True
                                    break
                                buf.append(chunk)
                                body_read += len(chunk)
                            raw = b"".join(buf)
                            body_text = raw.decode(
                                resp.encoding or "utf-8", errors="replace"
                            )
                        verdict = classify.classify(
                            status=resp.status_code,
                            body_text=body_text,
                            body_truncated=truncated,
                            expect_contains=expect_contains,
                            expect_content_type=expect_content_type,
                            content_type=header_map.get("content-type"),
                        )
                        break
                except Exception as exc:  # noqa: BLE001 - 一切连接层失败都是通道故障
                    verdict = classify.from_transport_error(exc)
                    break
            if verdict is None:
                verdict = classify.Verdict(
                    classify.CHANNEL_FAILURE,
                    f"重定向超过 {settings.max_redirects} 跳",
                    "重定向未闭合",
                )

            exit_identity, exit_source = await _exit_identity(settings, proxy.upstream if proxy.used else None)
            await _release(
                settings,
                control_client,
                proxy,
                "ok" if verdict.kind == classify.OK else "fail",
            )
    finally:
        await control_client.aclose()

    proxy_dict = proxy.to_dict()
    result = finish(
        verdict,
        status=status_code,
        headers=header_map,
        body_prefix=body_text,
        body_bytes_read=body_read,
        body_truncated=truncated,
        proxy=proxy_dict,
        exit={"identity": exit_identity, "source": exit_source},
        error=redirect_guard_error,
    )
    return result


def _reject_method(
    url: str,
    method: str,
    caller: str,
    label: str,
    settings: Settings,
    started: float,
    proxy: ProxyInfo,
) -> dict[str, Any]:
    elapsed_ms = int((time.perf_counter() - started) * 1000)
    verdict = classify.Verdict(
        classify.CHANNEL_FAILURE,
        f"方法 {method} 不被允许（仅 {'/'.join(ALLOWED_METHODS)}）",
        "探针只读；写语义请求在发出前被拒，不产生出网流量",
    )
    audit.record(
        settings.audit_log,
        {
            "caller": caller,
            "label": label,
            "url": url,
            "method": method,
            "kind": verdict.kind,
            "rejected": "method_not_allowed",
        },
    )
    return {
        "schema": SCHEMA,
        "ok": False,
        "url": url,
        "final_url": url,
        "method": method,
        "status": None,
        "elapsed_ms": elapsed_ms,
        "headers": {},
        "body_prefix": "",
        "body_bytes_read": 0,
        "body_truncated": False,
        "proxy": proxy.to_dict(),
        "exit": {"identity": None, "source": "unknown"},
        "tls": {"profile": "default", "note": ""},
        "hops": [],
        "verdict": verdict.to_dict(),
        "error": {"type": "method_not_allowed", "message": verdict.reason},
    }


def _reject_profile(
    url: str,
    method: str,
    profile: str,
    caller: str,
    label: str,
    settings: Settings,
    started: float,
    proxy: ProxyInfo,
) -> dict[str, Any]:
    """未知 TLS 档位：不发请求，显式拒绝（避免静默按默认档跑出假结论）。"""
    elapsed_ms = int((time.perf_counter() - started) * 1000)
    verdict = classify.Verdict(
        classify.CHANNEL_FAILURE,
        f"TLS 档位 {profile!r} 不被允许（可选：{'/'.join(TLS_PROFILES)}）",
        "档位必须显式且在白名单内；未知档位不静默降级",
    )
    audit.record(
        settings.audit_log,
        {
            "caller": caller,
            "label": label,
            "url": url,
            "method": method,
            "kind": verdict.kind,
            "rejected": "tls_profile_not_allowed",
        },
    )
    return {
        "schema": SCHEMA,
        "ok": False,
        "url": url,
        "final_url": url,
        "method": method,
        "status": None,
        "elapsed_ms": elapsed_ms,
        "headers": {},
        "body_prefix": "",
        "body_bytes_read": 0,
        "body_truncated": False,
        "proxy": proxy.to_dict(),
        "exit": {"identity": None, "source": "unknown"},
        "tls": {"profile": profile, "note": ""},
        "hops": [],
        "verdict": verdict.to_dict(),
        "error": {"type": "tls_profile_not_allowed", "message": verdict.reason},
    }
