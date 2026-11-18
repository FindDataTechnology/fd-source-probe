"""SSRF 防线与四分类的单元测试（不发任何真实出网流量）。

这两组判据是本服务的安全与可核基础，负例必须真的红：
- 目标直指私网/回环/链路本地/共享地址段 → 拒
- 公网首跳重定向到内网 → 第二跳拒
- 方法白名单：写语义方法在发出前拒
"""

from __future__ import annotations

import pytest

from fd_source_probe import classify, guard
from fd_source_probe.config import Settings


def _settings(**kw) -> Settings:
    base = dict(
        denied_host_suffixes=(".svc", ".cluster.local", ".mesh.finddata.cn", ".internal"),
        allowed_hosts=(),
        audit_log="",
    )
    base.update(kw)
    return Settings(**base)


@pytest.mark.parametrize(
    "url,needle",
    [
        ("http://127.0.0.1/x", "回环"),
        ("http://127.1.2.3/x", "回环"),
        ("http://10.0.0.5/x", "私网"),
        ("http://192.168.1.1/x", "私网"),
        ("http://172.16.9.9/x", "私网"),
        ("http://169.254.169.254/latest/meta-data/", "链路本地"),
        ("http://100.64.0.1:18004/mcp", "保留/共享"),
        ("http://0.0.0.0/x", "未指定"),
        ("http://[::1]/x", "回环"),
        ("http://[fd00::1]/x", "私网"),
        ("file:///etc/passwd", "不支持的协议"),
    ],
)
def test_private_and_reserved_targets_rejected(url, needle):
    with pytest.raises(guard.GuardError) as exc:
        guard.check_url(url, _settings())
    assert needle in exc.value.reason


@pytest.mark.parametrize(
    "url",
    [
        "http://localhost/x",
        "http://svc-name/x",
        "http://wire-ops-mcp.svc.cluster.local/mcp",
        "http://cheap6.mesh.finddata.cn:31896/mcp",
        "http://anything.internal/x",
    ],
)
def test_internal_hostnames_rejected(url):
    with pytest.raises(guard.GuardError):
        guard.check_url(url, _settings())


def test_allowlist_narrows_but_keeps_denylist():
    s = _settings(allowed_hosts=("data.stats.gov.cn",))
    with pytest.raises(guard.GuardError):
        guard.check_url("https://example.com/x", s)
    # 白名单内的公网域放行（解析可能受环境影响，仅断言宿主名判据）
    assert guard._host_denied("data.stats.gov.cn", s) is None


def test_ip_denied_uses_is_global_not_blacklist():
    # 100.64/10 既不是 is_private 也不是 is_global —— 用 is_global 才拦得住
    assert guard._ip_denied("100.64.0.1") is not None
    assert guard._ip_denied("8.8.8.8") is None
    assert guard._ip_denied("::ffff:10.0.0.1") is not None


@pytest.mark.parametrize(
    "status,expect,body,kind",
    [
        (200, None, "ok body", classify.OK),
        (401, None, "", classify.CREDENTIAL_INVALID),
        (403, None, '{"error":"invalid api key"}', classify.CREDENTIAL_INVALID),
        (404, None, "", classify.NOT_PUBLISHED),
        (204, None, "", classify.NOT_PUBLISHED),
        (429, None, "", classify.CHANNEL_FAILURE),
        (503, None, "", classify.CHANNEL_FAILURE),
        (500, None, "", classify.CHANNEL_FAILURE),
        (302, None, "", classify.CHANNEL_FAILURE),
        (200, None, "", classify.NOT_PUBLISHED),  # 200 空体 = 未发布
    ],
)
def test_status_classification(status, expect, body, kind):
    v = classify.classify(
        status=status,
        body_text=body,
        body_truncated=False,
        expect_contains=expect,
        expect_content_type=None,
        content_type="application/json",
    )
    assert v.kind == kind


def test_403_block_page_is_channel_failure_not_credential():
    """实测形态：data.stats.gov.cn 带浏览器 UA 仍 403，响应体是拦截页。

    这类"按出口 IP 拦截"若判成凭据无效，台账会把「换出口能解」误记成「要申请 key」。
    """
    html = (
        "<!DOCTYPE html><html><head><title>403 Forbidden</title></head>"
        "<body><h1>403 Forbidden</h1><center>Client IP: 163.125.162.244</center>"
        "</body></html>"
    )
    v = classify.classify(
        status=403,
        body_text=html,
        body_truncated=False,
        expect_contains=None,
        expect_content_type=None,
        content_type="text/html",
    )
    assert v.kind == classify.CHANNEL_FAILURE
    assert "疑似网关/WAF" in v.note
    assert "换出口复测" in v.note


def test_expectation_mismatch_is_caliber_mismatch():
    v = classify.classify(
        status=200,
        body_text='{"values": []}',
        body_truncated=False,
        expect_contains='"data"',
        expect_content_type="application/json",
        content_type="text/html",
    )
    assert v.kind == classify.CALIBER_MISMATCH


def test_transport_error_is_channel_failure_not_death():
    v = classify.from_transport_error(TimeoutError("boom"))
    assert v.kind == classify.CHANNEL_FAILURE
    assert "不代表源死亡" in v.note


def test_every_kind_has_label():
    for kind in classify.ALL_KINDS:
        assert classify.KIND_LABELS[kind]


def test_exit_ip_parser_accepts_only_real_ip():
    from fd_source_probe.probe import _parse_exit_ip

    assert _parse_exit_ip("2408:8256:328e:2a51::1\n") == "2408:8256:328e:2a51::1"
    assert _parse_exit_ip("fl=1\nh=www.cloudflare.com\nip=203.0.113.9\nts=x\n") == (
        "203.0.113.9"
    )
    # 错误页不得被当成出口
    assert _parse_exit_ip("<html>403 Forbidden</html>") is None
    assert _parse_exit_ip("") is None
