"""探测形状与方法白名单测试（不发真实出网流量，用 respx 拦 HTTP）。

验收要点：
- 出参字段集固定（两次探测字段集完全一致）——1.2
- 写语义方法在发出前被拒，且不产生任何请求 —— 1.3
- 重定向到内网在第二跳被拒 —— 1.4
- 代理不可用如实报错，不降级直连 —— 1.5
"""

from __future__ import annotations

import httpx
import pytest
import respx

from fd_source_probe import probe as probe_mod
from fd_source_probe.config import Settings

FIXED_KEYS = {
    "schema",
    "ok",
    "url",
    "final_url",
    "method",
    "status",
    "elapsed_ms",
    "headers",
    "body_prefix",
    "body_bytes_read",
    "body_truncated",
    "proxy",
    "exit",
    "tls",
    "hops",
    "verdict",
    "error",
}


def _settings(**kw) -> Settings:
    base = dict(
        proxy_forwarder="",
        proxy_control="",
        exit_echo_enabled=False,
        audit_log="",
        max_body_bytes=64,
        max_redirects=3,
        max_requests_per_minute=0,
        timeout_s=5.0,
    )
    base.update(kw)
    return Settings(**base)


@pytest.fixture(autouse=True)
def _public_dns(monkeypatch):
    """让非字面量主机名解析到公网地址，避免依赖真实 DNS。

    只替换 ``getaddrinfo``，**不替换 ``resolve``**：后者对 IP 字面量有短路，
    整体替换会让 127.0.0.1 这类目标被"解析"成公网地址，等于把被测的防线拆掉
    （第一次跑就踩了：私网负例假绿）。
    """
    import socket as _socket

    real_getaddrinfo = _socket.getaddrinfo

    def fake(host, port, *args, **kwargs):
        try:
            import ipaddress

            ipaddress.ip_address(host)
        except ValueError:
            return [
                (
                    _socket.AF_INET,
                    _socket.SOCK_STREAM,
                    6,
                    "",
                    ("93.184.216.34", port or 443),
                )
            ]
        return real_getaddrinfo(host, port, *args, **kwargs)

    monkeypatch.setattr(probe_mod.guard.socket, "getaddrinfo", fake)


@respx.mock
async def test_result_shape_is_fixed_across_calls():
    respx.get("https://example.com/data").mock(
        return_value=httpx.Response(200, json={"values": [1, 2, 3]})
    )
    a = await probe_mod.probe("https://example.com/data", settings=_settings())
    b = await probe_mod.probe("https://example.com/data", settings=_settings())
    assert set(a) == FIXED_KEYS
    assert set(a) == set(b)
    assert a["status"] == 200
    assert a["verdict"]["kind"] == "ok"
    assert a["body_bytes_read"] > 0


@respx.mock
async def test_body_is_capped_and_marked_truncated():
    respx.get("https://example.com/big").mock(
        return_value=httpx.Response(200, content=b"x" * 500)
    )
    r = await probe_mod.probe(
        "https://example.com/big", settings=_settings(max_body_bytes=64)
    )
    assert r["body_bytes_read"] == 64
    assert r["body_truncated"] is True


@pytest.mark.parametrize("method", ["PUT", "DELETE", "PATCH", "TRACE", "OPTIONS"])
async def test_write_methods_rejected_without_network(method):
    # 路由必须注册在本次 mock 上下文内：在装饰器之外注册会泄漏到后续用例
    # （第一次写就踩了这条——一条通配路由把后面两个用例的响应都变成 200）
    # assert_all_called=False：本用例的正常结局就是这条路由一次都没被调用
    with respx.mock(assert_all_called=False) as mock:
        route = mock.route(host="example.com").mock(
            return_value=httpx.Response(200, text="should not be reached")
        )
        r = await probe_mod.probe(
            "https://example.com/x", method=method, settings=_settings()
        )
    assert r["ok"] is False
    assert r["error"]["type"] == "method_not_allowed"
    assert r["status"] is None
    assert route.call_count == 0  # 关键：没有发出任何请求


@respx.mock
async def test_redirect_to_internal_rejected_on_second_hop():
    respx.get("https://example.com/go").mock(
        return_value=httpx.Response(302, headers={"location": "http://127.0.0.1/admin"})
    )
    r = await probe_mod.probe("https://example.com/go", settings=_settings())
    assert r["ok"] is False
    assert "重定向目标被拒" in r["verdict"]["reason"]
    assert r["error"]["type"] == "guard_rejected"


@respx.mock
async def test_redirect_chain_followed_when_public():
    respx.get("https://example.com/a").mock(
        return_value=httpx.Response(302, headers={"location": "/b"})
    )
    respx.get("https://example.com/b").mock(
        return_value=httpx.Response(200, json={"ok": True})
    )
    r = await probe_mod.probe("https://example.com/a", settings=_settings())
    assert r["ok"] is True
    assert len(r["hops"]) == 2
    assert r["final_url"] == "https://example.com/b"


async def test_proxy_unavailable_does_not_fall_back_to_direct():
    # 要求经代理但未配置任何代理通路
    r = await probe_mod.probe(
        "https://example.com/x", via_proxy=True, settings=_settings()
    )
    assert r["ok"] is False
    assert r["error"]["type"] == "proxy_unavailable"
    assert r["proxy"]["used"] is False
    assert "不以降级直连冒充" in r["verdict"]["note"]


@respx.mock
async def test_proxy_control_acquire_failure_reported_honestly():
    respx.post("https://control.example.com/acquire").mock(
        return_value=httpx.Response(503, json={"error": "no upstream"})
    )
    s = _settings(proxy_control="https://control.example.com")
    r = await probe_mod.probe("https://example.com/x", via_proxy=True, settings=s)
    assert r["ok"] is False
    assert r["error"]["type"] == "proxy_unavailable"
    assert r["proxy"]["kind"] == "none"


@respx.mock
async def test_proxy_forwarder_is_used_and_labelled():
    route = respx.get("https://example.com/x").mock(
        return_value=httpx.Response(200, json={"ok": True})
    )
    s = _settings(proxy_forwarder="http://forwarder:8080")
    r = await probe_mod.probe("https://example.com/x", via_proxy=True, settings=s)
    assert r["ok"] is True
    assert route.called
    assert r["proxy"] == {
        "requested": True,
        "used": True,
        "kind": "forwarder",
        "id": "http://forwarder:8080",
    }


@respx.mock
async def test_audit_line_written_with_caller_and_kind(tmp_path):
    log = tmp_path / "audit.jsonl"
    respx.get("https://example.com/x").mock(return_value=httpx.Response(200, text="ok"))
    await probe_mod.probe(
        "https://example.com/x",
        caller="fleet/recon",
        label="hotel-replay/E1",
        settings=_settings(audit_log=str(log)),
    )
    lines = log.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 1
    import json

    row = json.loads(lines[0])
    assert row["caller"] == "fleet/recon"
    assert row["label"] == "hotel-replay/E1"
    assert row["kind"] == "ok"
    assert row["url"] == "https://example.com/x"


async def test_audit_failure_does_not_break_probe(tmp_path):
    # 把审计路径指向一个已存在的目录，写入必失败；探测仍须完成
    with respx.mock() as mock:
        mock.get("https://example.com/x").mock(
            return_value=httpx.Response(200, text="ok")
        )
        r = await probe_mod.probe(
            "https://example.com/x", settings=_settings(audit_log=str(tmp_path))
        )
    assert r["ok"] is True


@respx.mock
async def test_unknown_tls_profile_rejected_without_network():
    route = respx.get("https://example.com/x").mock(
        return_value=httpx.Response(200, text="should not be reached")
    )
    r = await probe_mod.probe(
        "https://example.com/x", tls_profile="nonexistent", settings=_settings()
    )
    assert r["ok"] is False
    assert r["error"]["type"] == "tls_profile_not_allowed"
    assert route.call_count == 0


@respx.mock
async def test_tls_profile_is_echoed_in_result():
    respx.get("https://example.com/x").mock(return_value=httpx.Response(200, text="ok"))
    r = await probe_mod.probe(
        "https://example.com/x", tls_profile="legacy", settings=_settings()
    )
    assert r["tls"]["profile"] == "legacy"
    assert "SECLEVEL=1" in r["tls"]["note"]
