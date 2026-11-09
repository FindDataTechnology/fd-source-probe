"""探针 FastMCP 服务器（internal-only，一个工具面）。

对外只有一个工具 `probe_endpoint`：只读探测一个端点并返回固定形状的实测结果。
工具面刻意保持极小——探针是安全敏感件，多一个工具就多一条出网路径。
"""

from __future__ import annotations

from typing import Any, Optional

from fastmcp import FastMCP

from .config import Settings
from .probe import ALLOWED_METHODS, probe

SERVER_NAME = "fd-source-probe"

INSTRUCTIONS = (
    "源侦察探针（内部专用，只读）。用途：对候选数据源做一次可复现的实测，"
    "拿到状态码、耗时、响应头、出口身份与响应体前缀，供源解决方案舰队产出"
    "带证据的生成单。纪律：①只读——仅 GET/HEAD/POST，写语义方法在发出前被拒；"
    "②私网与保留地址、集群内服务名一律拒绝，重定向逐跳复判；"
    "③要经代理时必须显式 via_proxy=true，代理不可用如实报错，绝不以直连冒充；"
    "④失败按四分类（通道故障/凭据无效/源未发布/口径不符）记录，一次失败不判死源。"
)


async def probe_endpoint(
    url: str,
    method: str = "GET",
    headers: Optional[dict[str, str]] = None,
    body: Optional[str] = None,
    via_proxy: bool = False,
    expect_contains: Optional[str] = None,
    expect_content_type: Optional[str] = None,
    tls_profile: str = "default",
    caller: str = "",
    label: str = "",
) -> dict[str, Any]:
    """对 url 做一次只读出网探测，返回固定形状的实测结果。

    Args:
        url: 目标地址（http/https）。私网、保留地址与集群内服务名会被拒绝。
        method: GET / HEAD / POST；其他方法在发出请求前即被拒。
        headers: 追加请求头（user-agent 有默认值，可覆盖）。
        body: POST 的请求体（表单或 JSON 字符串）。GET/HEAD 忽略。
        via_proxy: 是否经既有代理通路出网。代理不可用时返回 proxy_unavailable，
            不会降级为直连。
        expect_contains: 期望响应体含该片段；不含则判口径不符（取前 N 字节内匹配）。
        expect_content_type: 期望的 content-type 子串。
        tls_profile: default / legacy。legacy 放宽到 SECLEVEL=1，用于上游仍在用
            旧套件的情形（实测 data.mofcom.gov.cn 默认档握手失败、legacy 档 200）。
            档位会写进结果，使"可达"连同客户端配置一起可复现。
        caller: 调用者标识（进审计，便于追责）。
        label: 本次探测的业务标签（如"酒店线回放/E1"）。

    Returns:
        固定字段集：schema/ok/url/final_url/method/status/elapsed_ms/headers/
        body_prefix/body_bytes_read/body_truncated/proxy/exit/tls/hops/verdict/error。
    """
    return await probe(
        url,
        method=method,
        headers=headers,
        body=body,
        via_proxy=via_proxy,
        expect_contains=expect_contains,
        expect_content_type=expect_content_type,
        tls_profile=tls_profile,
        caller=caller,
        label=label,
    )


def create_server(settings: Settings | None = None) -> FastMCP:
    mcp: FastMCP = FastMCP(SERVER_NAME, instructions=INSTRUCTIONS)
    mcp.tool(probe_endpoint)
    return mcp


def build_http_app(settings: Settings | None = None):
    from starlette.requests import Request
    from starlette.responses import JSONResponse
    from starlette.routing import Route

    from . import __version__

    app = create_server(settings or Settings.from_env()).http_app(path="/mcp")

    async def healthz(_: Request) -> JSONResponse:
        return JSONResponse(
            {"status": "ok", "service": SERVER_NAME, "version": __version__}
        )

    app.routes.append(Route("/healthz", healthz))
    return app
