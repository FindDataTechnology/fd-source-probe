"""失败四分类（spec `source-probe`：通道故障 / 凭据无效 / 源未发布 / 口径不符）。

纪律：**一次失败不判死源**。分类只说明这一次为什么没拿到东西，不构成源死亡的
结论；源死亡是行业盯梢产出的「疑似源死」差异条目，由人工裁决（design D10）。

分类是穷尽的四类 + ok，不含「反爬」「被封」这类猜测类别：429/WAF 归入通道故障，
在 ``note`` 里写清疑似形态，但不升级为类别——类别一旦掺入猜测，台账就不再可复算。
"""

from __future__ import annotations

from dataclasses import dataclass

OK = "ok"
CHANNEL_FAILURE = "channel_failure"
CREDENTIAL_INVALID = "credential_invalid"
NOT_PUBLISHED = "not_published"
CALIBER_MISMATCH = "caliber_mismatch"

ALL_KINDS = (OK, CHANNEL_FAILURE, CREDENTIAL_INVALID, NOT_PUBLISHED, CALIBER_MISMATCH)

# 供台账与档案直接使用的中文名（spec 用中文定名，代码与输出同时携带两套）
KIND_LABELS = {
    OK: "可得",
    CHANNEL_FAILURE: "通道故障",
    CREDENTIAL_INVALID: "凭据无效",
    NOT_PUBLISHED: "源未发布",
    CALIBER_MISMATCH: "口径不符",
}

CREDENTIAL_STATUSES = frozenset({401, 407})
# 403 是两面派：可以是凭据无效，也可以是网关/WAF 按出口拦截。
# 实测依据（2026-11-10，本机直连 data.stats.gov.cn）：带浏览器 UA 仍返 403，
# 响应体是 `<title>403 Forbidden</title>` + `Client IP: <出口IP>` 的拦截页——
# 那是按出口 IP 拦截，不是凭据问题。两类混判会让台账把「换出口能解」误记成
# 「要申请 key」，所以按响应形态分。
BLOCK_PAGE_STATUSES = frozenset({403})
BLOCK_PAGE_MARKERS = (
    "client ip",
    "access denied",
    "forbidden",
    "request blocked",
    "captcha",
    "拦截",
    "禁止访问",
    "人机验证",
)
NOT_PUBLISHED_STATUSES = frozenset({404, 410, 204})
RATE_LIMIT_STATUSES = frozenset({429, 503})


@dataclass(frozen=True)
class Verdict:
    kind: str
    reason: str
    note: str = ""

    def to_dict(self) -> dict:
        return {
            "kind": self.kind,
            "label": KIND_LABELS[self.kind],
            "reason": self.reason,
            "note": self.note,
        }


def from_transport_error(exc: BaseException) -> Verdict:
    return Verdict(
        CHANNEL_FAILURE,
        f"连接层失败：{type(exc).__name__}: {exc}",
        "通道故障不代表源死亡；海外源按既有代理复测路径处置",
    )


def _looks_like_block_page(body_text: str, content_type: str | None) -> bool:
    """403 的响应形态是不是"网关拦了"，而不是"凭据不对"。

    判据只用可观测特征：HTML 形态、或已知拦截页标记。都不满足时按凭据无效处理
    （多数 API 用 403 JSON 表达 key 无效）。分不清就落到凭据无效并在 note 里
    要求复查——两个方向都不许猜成"源死亡"。
    """
    if content_type and "html" in content_type.lower():
        return True
    lowered = body_text[:4096].lower()
    return any(m in lowered for m in BLOCK_PAGE_MARKERS)


def classify(
    *,
    status: int,
    body_text: str,
    body_truncated: bool,
    expect_contains: str | None,
    expect_content_type: str | None,
    content_type: str | None,
) -> Verdict:
    """按一次已完成的响应给出四分类之一。"""
    if status in CREDENTIAL_STATUSES:
        return Verdict(
            CREDENTIAL_INVALID,
            f"HTTP {status}",
            "需凭据或凭据无效；注册 key 走统一密钥轨道后复查",
        )
    if status in BLOCK_PAGE_STATUSES:
        if _looks_like_block_page(body_text, content_type):
            return Verdict(
                CHANNEL_FAILURE,
                f"HTTP {status}（拦截页形态）",
                "疑似网关/WAF 按出口拦截，非凭据问题；换出口复测而不是申请 key",
            )
        return Verdict(
            CREDENTIAL_INVALID,
            f"HTTP {status}",
            "按凭据无效处理；若换出口后仍 403 再核 key 与权限",
        )
    if status in NOT_PUBLISHED_STATUSES:
        return Verdict(
            NOT_PUBLISHED,
            f"HTTP {status}",
            "该期/该端点当前无内容；不等于源不存在",
        )
    if status in RATE_LIMIT_STATUSES:
        return Verdict(
            CHANNEL_FAILURE,
            f"HTTP {status}",
            "疑似限流或网关拦截（WAF）；不改判为源死亡，也不尝试绕过",
        )
    if status >= 500:
        return Verdict(CHANNEL_FAILURE, f"HTTP {status}", "上游服务端错误")
    if status >= 400:
        return Verdict(CHANNEL_FAILURE, f"HTTP {status}", "请求被拒或路由不符")
    if status < 200 or status >= 300:
        # 3xx 走到这里是重定向未被跟随（超过上限或缺失 Location）
        return Verdict(
            CHANNEL_FAILURE if 300 <= status < 400 else NOT_PUBLISHED,
            f"HTTP {status}",
            "重定向未闭合",
        )

    # 2xx 及以下按内容判
    if expect_content_type and content_type:
        if expect_content_type.lower() not in content_type.lower():
            return Verdict(
                CALIBER_MISMATCH,
                f"内容类型 {content_type} 不含期望 {expect_content_type}",
                "上游返回体的形状与声明不一致",
            )
    if expect_contains and expect_contains not in body_text:
        return Verdict(
            CALIBER_MISMATCH,
            f"响应体不含期望片段 {expect_contains!r}",
            "形状/口径与声明不符；截断读取下也可能是前缀未覆盖",
        )
    if not body_text.strip() and not body_truncated:
        return Verdict(
            NOT_PUBLISHED,
            "HTTP 200 但响应体为空",
            "空响应按未发布处理，不按可得出数",
        )
    return Verdict(OK, "HTTP 200 且内容可读")
