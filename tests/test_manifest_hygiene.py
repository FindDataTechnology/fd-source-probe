"""清单公开投影的内容卫生：每个字符串都可能匿名公开。

判据源=converge-line-identity-and-keys 的 specs/line-manifest（「清单的每个字符串都会
匿名公开，因此只许承载产品语义」那条）；全量体检=`scripts/line_manifest_check.py`。

2026-11-15 本仓曾有 4 段注（顶层 165 / `identity.note` 141 / `auth_tracks[0].note` 30 /
`billing.note` 90 字符）：`identity.note` 写的是 `server.py:76-78`、`server.py:81-88`
的行号与「`FastMCP` 未传 auth、未挂中间件」这类内部接线，`billing.note` 写的是
`hosting=internal-only` 的内部字段名。它们已逐字移入 `docs/line-auth-evidence.md`
（公开载荷清理批次 I）。

本线的暴露路径与其它线**不同**，别按「清单 COPY 进镜像」去推理：镜像里没有清单
（Dockerfile 不 COPY，2026-11-15 线内实测 `/.well-known/line-manifest.json` = 404，
健康面是 `/healthz`），真正把这几个字符串投出去的是 `fd-prod/line-manifests` 这份
ConfigMap——平台 pod 挂在 `/etc/line-manifests/source-probe/`。本测试防的是「投影面
又被写脏」。

**`fallback: false` 不是本测试要修的东西**：本线 `hosting=internal-only`（不进对外
商业工具名单），「无回退轨」是设计选择而非缺陷，所以 `line_manifest_check.py` 为它
报的那条 repo-class FAIL 是**真能力缺口**的如实记载。要消它只能先给本线加线自有钥匙
（并同时改 hosting），不许为了让闸变绿去改声明。
"""

import json
import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SOURCE = REPO_ROOT / "line-manifest.json"
EVIDENCE = REPO_ROOT / "docs" / "line-auth-evidence.md"

#: 规格允许出现字符串的路径（闭集）。键名不在集合里即自由文本——**不按长度判**：
#: 20 个字符的 note 一样能写出 Secret 名。
_SANCTIONED_TEXT_PATHS = {
    "schema", "line", "public_base_url", "internal_base_url",
    "identity.issuer", "identity.jwks_uri", "identity.jit_policy",
    "billing.plane", "billing.evidence_url", "billing.evidence_path",
    "auth_tracks[].name", "auth_tracks[].kind", "auth_tracks[].accepts",
    "auth_tracks[].migration_task", "scopes[]",
}

_INTERNAL_PATTERNS = (
    (r"fd-[a-z0-9][a-z0-9-]*-(?:token|key)\b", "Secret 名"),
    (r"\b[A-Z][A-Z0-9_]{2,}(?:TOKEN|SECRET|PASSWORD|PEM)\b", "凭据类环境变量"),
    (r"(?<![\w/.-])(?:[\w.-]+/)+[\w.-]+\.(?:py|js|ts|sh|sql|ya?ml|json)(?::\d+(?:-\d+)?)?",
     "内部代码路径"),
    # 裸文件名带行号也要抓：被删的注里就是这个形状（`server.py:76-78`），
    # 上面那条要求至少一层目录，对它们恒不命中。
    (r"(?<![\w/.-])[\w.-]+\.(?:py|js|ts|tsx|jsx|sh|sql|ya?ml|json):\d+",
     "裸文件名+行号"),
    (r"\b\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}\b", "IP/主机地址"),
    (r"\b[a-z0-9][a-z0-9-]*\.mesh\.finddata\.cn\b", "集群 mesh 主机名"),
    (r"/api/(?:servers|patch-keys|tenants|ops)\b|X-Internal-Token", "内部管理面调用形状"),
    (r"/(?:health|healthz|metrics)\b", "豁免面探针路径"),
)


def _walk_strings(node, path=""):
    if isinstance(node, dict):
        for k in node:
            yield from _walk_strings(node[k], (path + "." + k) if path else k)
    elif isinstance(node, list):
        for i in range(len(node)):
            yield from _walk_strings(node[i], path + "[]")
    elif isinstance(node, str):
        yield path, node


def test_source_manifest_has_no_free_text():
    """只许产品语义：键在闭集里，且正文不含凭据/代码路径/内部主机/探针路径。"""
    doc = json.loads(SOURCE.read_text(encoding="utf-8"))
    strings = list(_walk_strings(doc))

    stray = [p for p, _ in strings if p not in _SANCTIONED_TEXT_PATHS]
    assert not stray, f"清单里出现自由文本键（会被投影公开）: {stray}；证据请写进 docs/"

    body = json.dumps({k: v for k, v in doc.items() if k != "internal_base_url"},
                      ensure_ascii=False)
    for pattern, what in _INTERNAL_PATTERNS:
        hit = re.search(pattern, body)
        assert hit is None, f"清单里出现{what}: {hit.group(0)!r}"


def test_stripped_notes_are_archived_verbatim():
    """剥掉的自由文本必须留在仓内证据文档里——只删不归档等于丢证据。"""
    assert EVIDENCE.is_file(), (
        f"缺 {EVIDENCE.relative_to(REPO_ROOT)}：清单里的自由文本被删掉了却没有归档，"
        "无法审计当初声明的是什么事实")
    text = EVIDENCE.read_text(encoding="utf-8")
    for pointer in ("`note`", "`identity.note`", "`auth_tracks[0].note`", "`billing.note`"):
        assert pointer in text, f"证据文档缺 {pointer} 这一节"
