"""CLI：probe（单次探测）+ serve（HTTP 面 = /mcp + /healthz）。"""

from __future__ import annotations

import asyncio
import json
import sys

import click
import uvicorn

from . import __version__
from .server import build_http_app


@click.group()
@click.version_option(__version__)
def cli() -> None:
    """fd-source-probe：源侦察探针 MCP（internal-only）。"""


@cli.command()
@click.argument("url")
@click.option("--method", default="GET", show_default=True)
@click.option("--header", "headers", multiple=True, help="追加请求头 Key:Value（可重复）")
@click.option("--body", default=None, help="POST 请求体")
@click.option("--via-proxy", is_flag=True, default=False, help="经既有代理通路出网")
@click.option("--expect-contains", default=None, help="期望响应体含该片段（口径对拍）")
@click.option("--tls-profile", default="default", show_default=True, type=click.Choice(["default", "legacy"]))
@click.option("--caller", default="cli", show_default=True)
@click.option("--label", default="", help="业务标签（进审计）")
@click.option("--json-out", default="", help="结果落盘路径（同时打印到 stdout）")
def probe(url: str, method: str, headers: tuple[str, ...], body: str | None,
          via_proxy: bool, expect_contains: str | None, tls_profile: str,
          caller: str, label: str, json_out: str) -> None:
    """对 URL 做一次只读探测，打印固定形状的实测结果。"""
    from .probe import probe as run_probe

    hdrs: dict[str, str] = {}
    for h in headers:
        if ":" not in h:
            raise click.ClickException(f"--header 需要 Key:Value 形态，收到 {h!r}")
        k, _, v = h.partition(":")
        hdrs[k.strip()] = v.strip()

    async def _run():
        return await run_probe(
            url, method=method, headers=hdrs or None, body=body,
            via_proxy=via_proxy, expect_contains=expect_contains,
            tls_profile=tls_profile, caller=caller, label=label,
        )

    result = asyncio.run(_run())
    text = json.dumps(result, ensure_ascii=False, indent=2)
    if json_out:
        from pathlib import Path

        p = Path(json_out)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text + "\n", encoding="utf-8")
        click.echo(f"-> {p}", err=True)
    verdict = result["verdict"]
    click.echo(
        f"{result['status']} {result['elapsed_ms']}ms kind={verdict['kind']}({verdict['label']})"
        f" exit={result['exit']['identity'] or '未测'} tls={result['tls']['profile']}"
    )
    if not result["ok"]:
        click.echo(text, err=True)
        sys.exit(1)
    click.echo(text)


@cli.command()
@click.option("--host", default="0.0.0.0", show_default=True)
@click.option("--port", default=8000, show_default=True)
def serve(host: str, port: int) -> None:
    """启动 HTTP 服务（/mcp + /healthz）。"""
    app = build_http_app()
    config = uvicorn.Config(app, host=host, port=port, log_level="info")
    asyncio.run(uvicorn.Server(config).serve())


if __name__ == "__main__":
    cli()
