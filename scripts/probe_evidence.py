#!/usr/bin/env python3
"""真实探测取证：对一组已判安全的上游端点跑一遍探针，落证据。

用法（从 fd-source-probe/ 跑）：

    .venv/bin/python scripts/probe_evidence.py --label smoke-2026-11-10
    .venv/bin/python scripts/probe_evidence.py --only eurostat --json-out /tmp/x.json

设计要点（对齐 spec `source-probe` 与 change tasks 1.7）：

- 证据写两份：机器可读 JSON（逐条完整出参）+ 人读 Markdown（关键字段对拍表）。
- **不编数**：任一目标失败照原样落盘，不重试到"绿"为止，不替换成"应当可用"。
- 端点集刻意选三类形态：免 key 直连 JSON（Eurostat）、需 POST 表单（商务部）、
  已知按出口拦截（国家统计局）——最后一类是分类判据的实证样本。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]  # finddata 根
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fd_source_probe.config import Settings  # noqa: E402
from fd_source_probe.probe import probe  # noqa: E402

EUROSTAT_HOTEL_NIGHTS = (
    "https://ec.europa.eu/eurostat/api/dissemination/statistics/1.0/data/tour_occ_nim"
    "?format=JSON&lang=EN&c_resid=TOTAL&unit=NR&nace_r2=I551-I553&geo=EU27_2020"
)

TARGETS = [
    {
        "id": "eurostat-hotel-nights",
        "note": "EU27 住宿业月度过夜数（酒店线 E2 同源端点），免 key 直连",
        "url": EUROSTAT_HOTEL_NIGHTS,
        "method": "GET",
        "expect_contains": '"value"',
        "expect_content_type": "application/json",
    },
    {
        "id": "mofcom-totalmonth",
        "note": "商务部数据中心货物进出口月度（侦察簿 W1-K1），免鉴权 POST 表单；需 legacy TLS 档",
        "url": "https://data.mofcom.gov.cn/datamofcom/front/totalmonth/query",
        "method": "POST",
        "headers": {"content-type": "application/x-www-form-urlencoded"},
        "body": "pageNumber=1&type=0",
        "expect_contains": "total_value",
        "expect_content_type": "json",
        "tls_profile": "legacy",
    },
    {
        "id": "nbs-easyquery",
        "note": "国家统计局 easyquery（已知按出口拦截：带浏览器 UA 仍 403，拦截页含 Client IP）",
        "url": (
            "https://data.stats.gov.cn/easyquery.htm"
            "?m=QueryData&dbcode=hgnd&rowcode=zb&colcode=sj&wds=%5B%5D&dfwds=%5B%5D"
        ),
        "method": "GET",
        "headers": {
            "user-agent": (
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"
                " (KHTML, like Gecko) Chrome/126.0 Safari/537.36"
            ),
            "referer": "https://data.stats.gov.cn/",
        },
    },
]


async def run_one(target: dict, settings: Settings, via_proxy: bool) -> dict:
    return await probe(
        target["url"],
        method=target.get("method", "GET"),
        headers=target.get("headers"),
        body=target.get("body"),
        via_proxy=via_proxy,
        expect_contains=target.get("expect_contains"),
        expect_content_type=target.get("expect_content_type"),
        tls_profile=target.get("tls_profile", "default"),
        caller="fd-source-probe/scripts/probe_evidence.py",
        label=target["id"],
        settings=settings,
    )


def _md(records: list[dict], label: str, via_proxy: bool, settings: Settings) -> str:
    lines = [
        f"# 探针取证实录 · {label}",
        "",
        f"- 时间：{datetime.now(timezone.utc).isoformat()}",
        f"- 出口形态：{'经代理' if via_proxy else '直连'}",
        f"- 探针版本：fd-source-probe 0.1.0",
        f"- 审计：`{settings.audit_log}`",
        "",
        "| 目标 | 方法 | 状态码 | 耗时(ms) | 分类 | 出口 | 体前缀(bytes) | 说明 |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for r in records:
        verdict = r["verdict"]
        lines.append(
            "| {id} | {m} | {s} | {ms} | {k}({lab}) | {ex} | {n} | {note} |".format(
                id=r["_target"]["id"],
                m=r["method"],
                s=r["status"] if r["status"] is not None else "—",
                ms=r["elapsed_ms"],
                k=verdict["kind"],
                lab=verdict["label"],
                ex=(r["exit"]["identity"] or "未测"),
                n=f"{r['body_bytes_read']}{'+' if r['body_truncated'] else ''}",
                note=r["_target"]["note"],
            )
        )
    lines += ["", "## 逐条证据（体前缀原样截取，未编辑）", ""]
    for r in records:
        lines += [
            f"### {r['_target']['id']}",
            "",
            f"- URL：`{r['url']}`",
            f"- 分类理由：{r['verdict']['reason']}",
            f"- 分类注记：{r['verdict']['note'] or '—'}",
            f"- 响应头（裁剪）：`content-type={r['headers'].get('content-type', '—')}`",
            f"- 响应体前缀：",
            "",
            "```",
            r["body_prefix"][:600].replace("```", "'''"),
            "```",
            "",
        ]
    return "\n".join(lines)


async def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--label", default=datetime.now(timezone.utc).strftime("%Y-%m-%d"))
    ap.add_argument("--only", action="append", default=[], help="只跑指定 target id（可重复）")
    ap.add_argument("--via-proxy", action="store_true", help="经代理探测（默认直连）")
    ap.add_argument("--json-out", default="")
    ap.add_argument("--md-out", default="")
    args = ap.parse_args()

    settings = Settings.from_env()
    # 审计跟着证据走：否则证据在仓库里、审计在家里，事后无法对账
    outdir = REPO / "data-source-scouting" / "evidence"
    outdir.mkdir(parents=True, exist_ok=True)
    settings.audit_log = str(outdir / f"{args.label}-audit.jsonl")
    targets = [t for t in TARGETS if not args.only or t["id"] in args.only]
    if not targets:
        print("no targets selected", file=sys.stderr)
        return 2

    records = []
    for t in targets:
        r = await run_one(t, settings, args.via_proxy)
        r = {**r, "_target": {"id": t["id"], "note": t["note"]}}
        records.append(r)
        print(
            f"[{t['id']}] status={r['status']} kind={r['verdict']['kind']} "
            f"{r['elapsed_ms']}ms bytes={r['body_bytes_read']}"
        )

    json_path = Path(args.json_out) if args.json_out else outdir / f"{args.label}-probe.json"
    md_path = Path(args.md_out) if args.md_out else outdir / f"{args.label}-probe.md"
    json_path.parent.mkdir(parents=True, exist_ok=True)
    md_path.parent.mkdir(parents=True, exist_ok=True)
    json_path.write_text(
        json.dumps(
            {
                "label": args.label,
                "generated_at": datetime.now(timezone.utc).isoformat(),
                "via_proxy": args.via_proxy,
                "records": records,
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    md_path.write_text(_md(records, args.label, args.via_proxy, settings), encoding="utf-8")
    print(f"\njson -> {json_path}\nmd   -> {md_path}")

    # 退出码照实：只要有目标的分类是通道故障/凭据无效，就非零（取证脚本不粉饰）
    bad = [r["_target"]["id"] for r in records if r["verdict"]["kind"] != "ok"]
    if bad:
        print(f"non-ok targets: {', '.join(bad)}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
