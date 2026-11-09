"""工具面测试：恰好一个工具，且名字与入参契约稳定（1.1）。"""

from __future__ import annotations

import asyncio

from fd_source_probe.config import Settings
from fd_source_probe.server import SERVER_NAME, build_http_app, create_server


def _tools():
    mcp = create_server(Settings(audit_log=""))
    return {t.name: t for t in asyncio.run(mcp.list_tools())}


def test_server_name_and_single_tool():
    assert SERVER_NAME == "fd-source-probe"
    assert list(_tools()) == ["probe_endpoint"]


def test_tool_schema_exposes_guard_and_proxy_switches():
    schema = _tools()["probe_endpoint"].parameters
    props = schema.get("properties", {})
    for key in (
        "url",
        "method",
        "headers",
        "body",
        "via_proxy",
        "expect_contains",
        "expect_content_type",
        "tls_profile",
        "caller",
        "label",
    ):
        assert key in props, key
    assert schema.get("required") == ["url"]


def test_http_app_mounts_mcp_and_healthz():
    app = build_http_app(Settings(audit_log=""))
    paths = {getattr(r, "path", None) for r in app.routes}
    assert "/mcp" in paths
    assert "/healthz" in paths
