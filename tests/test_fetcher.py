"""fetcher 测试：MockTransport 模拟订阅响应（含 userinfo 头）、快照缓存与读回。

纪律：数据目录一律用 conftest 的 config fixture（tmp_path 隔离），不碰仓库根 data/。
"""
from __future__ import annotations

import logging

import httpx
import pytest

from app.fetcher import (
    fetch_subscription,
    latest_cache_path,
    load_latest_cache,
    parse_userinfo_header,
    save_cache,
)
from app.models import FetchStatus, Subscription

SUB_URL = "https://sub.example.com:8443/token-path/clash.yaml"


def _sub(url: str = SUB_URL, name: str = "kuai", sub_id: int = 1) -> Subscription:
    return Subscription(id=sub_id, name=name, url=url)


def _mock(handler) -> httpx.MockTransport:
    return httpx.MockTransport(handler)


# ---------------------------------------------------------------- 抓取主路径

def test_fetch_ok_with_userinfo_header(config, sub_a_path):
    """200 + subscription-userinfo 头 → OK；Clash UA 生效；快照落盘可读回。"""
    body = sub_a_path.read_bytes()
    seen: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["ua"] = request.headers.get("user-agent", "")
        seen["accept"] = request.headers.get("accept", "")
        return httpx.Response(
            200,
            content=body,
            headers={
                "content-type": "text/yaml",
                "subscription-userinfo":
                    "upload=1024; download=2048; total=107374182400; expire=1790000000",
            },
        )

    result = fetch_subscription(_sub(), config=config, transport=_mock(handler))

    assert result.status is FetchStatus.OK
    assert result.content == body
    assert result.userinfo == {
        "upload": 1024, "download": 2048, "total": 107374182400, "expire": 1790000000,
    }
    assert result.error is None
    assert result.sub_id == 1
    assert result.sub_name == "kuai"
    assert result.fetched_at  # now_iso()
    # 固定 Clash UA（docs/01：拿 YAML 直出）
    assert seen["ua"] == config.fetch_user_agent
    # 快照落盘 data/cache/<订阅名>-<ts>.yaml
    snapshots = sorted(config.cache_dir.glob("kuai-*.yaml"))
    assert len(snapshots) == 1
    assert snapshots[0].read_bytes() == body
    # 读回最近一份快照
    assert latest_cache_path(config, "kuai") == snapshots[0]
    assert load_latest_cache(config, "kuai") == body


def test_fetch_ok_without_userinfo_header(config):
    """无 userinfo 头 → userinfo=None，不影响 OK 状态。"""
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"proxies: []\n")

    result = fetch_subscription(_sub(), config=config, transport=_mock(handler))
    assert result.status is FetchStatus.OK
    assert result.userinfo is None


def test_fetch_follows_redirect(config):
    """302 → 自动跟随，最终 200 记为 OK。"""
    state = {"redirected": False}

    def handler(request: httpx.Request) -> httpx.Response:
        if not state["redirected"]:
            state["redirected"] = True
            return httpx.Response(302, headers={"location": SUB_URL})
        return httpx.Response(200, content=b"proxies: []\n")

    result = fetch_subscription(_sub(), config=config, transport=_mock(handler))
    assert result.status is FetchStatus.OK
    assert result.content == b"proxies: []\n"


# ---------------------------------------------------------------- 失败路径（不抛异常）

@pytest.mark.parametrize("code", [401, 403])
def test_fetch_auth_failed_is_invalid(config, code):
    """401/403 → INVALID；错误信息只含 host，不泄露路径与 token。"""
    url = "https://private.example.com/very/secret/tokenabcdef"
    result = fetch_subscription(
        _sub(url=url), config=config, transport=_mock(lambda r: httpx.Response(code))
    )
    assert result.status is FetchStatus.INVALID
    assert result.content is None
    assert result.error
    assert "private.example.com" in result.error  # 打码到 host
    assert "tokenabcdef" not in result.error      # 不出现完整 URL
    assert "/very/secret" not in result.error


def test_fetch_http_500_is_failed(config):
    """非 401/403 的 HTTP 错误 → FAILED（可重试类故障）。"""
    result = fetch_subscription(
        _sub(), config=config, transport=_mock(lambda r: httpx.Response(500))
    )
    assert result.status is FetchStatus.FAILED
    assert result.content is None
    assert "500" in result.error


def test_fetch_timeout_is_timeout_status(config):
    """超时不抛异常 → TIMEOUT。"""
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectTimeout("模拟连接超时", request=request)

    result = fetch_subscription(_sub(), config=config, transport=_mock(handler))
    assert result.status is FetchStatus.TIMEOUT
    assert result.content is None
    assert result.error
    assert "sub.example.com" in result.error  # 打码到 host
    assert "/token-path" not in result.error


def test_fetch_network_error_is_failed(config):
    """连接被拒等网络错误 → FAILED，不抛异常。"""
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("模拟连接被拒绝", request=request)

    result = fetch_subscription(_sub(), config=config, transport=_mock(handler))
    assert result.status is FetchStatus.FAILED
    assert result.content is None


def test_fetch_empty_body_is_failed(config):
    """200 但响应体为空 → FAILED（避免把空快照当成功）。"""
    result = fetch_subscription(
        _sub(), config=config, transport=_mock(lambda r: httpx.Response(200, content=b"   \n"))
    )
    assert result.status is FetchStatus.FAILED
    assert result.content is None


def test_fetch_empty_url_is_failed(config):
    result = fetch_subscription(_sub(url="   "), config=config,
                                transport=_mock(lambda r: httpx.Response(200)))
    assert result.status is FetchStatus.FAILED
    assert result.error


def test_fetch_failure_does_not_touch_cache(config, sub_a_path):
    """失败抓取不写快照，旧快照保持原样（沿用旧快照的前提）。"""
    body = sub_a_path.read_bytes()
    save_cache(config, "kuai", body)
    before = latest_cache_path(config, "kuai")

    result = fetch_subscription(
        _sub(), config=config, transport=_mock(lambda r: httpx.Response(403))
    )
    assert result.status is FetchStatus.INVALID
    assert latest_cache_path(config, "kuai") == before
    assert load_latest_cache(config, "kuai") == body


def test_fetch_logs_never_contain_full_url(config, sub_a_path, caplog):
    """日志纪律：抓取全程（含 httpx 库自身日志）只出现 host 打码，不出现路径与 token。"""
    url = "https://sub.example.com:8443/secrettoken/clash.yaml"
    body = sub_a_path.read_bytes()

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=body,
                              headers={"subscription-userinfo": "total=100"})

    with caplog.at_level(logging.INFO):
        result = fetch_subscription(_sub(url=url), config=config, transport=_mock(handler))

    assert result.status is FetchStatus.OK
    assert "secrettoken" not in caplog.text        # 完整 URL 不进日志
    assert "clash.yaml" not in caplog.text
    assert "sub.example.com" in caplog.text        # 打码到 host 仍可排查


# ---------------------------------------------------------------- userinfo 头解析

def test_parse_userinfo_header_full():
    value = "upload=123; download=456; total=789; expire=1790000000"
    assert parse_userinfo_header(value) == {
        "upload": 123, "download": 456, "total": 789, "expire": 1790000000,
    }


def test_parse_userinfo_header_partial_and_garbage():
    assert parse_userinfo_header("upload=1; 垃圾片段; total=2") == {"upload": 1, "total": 2}
    assert parse_userinfo_header("upload=abc; total=3") == {"total": 3}
    assert parse_userinfo_header("Upload=5; DOWNLOAD=6") == {"upload": 5, "download": 6}
    assert parse_userinfo_header("expire=1790000000.0") == {"expire": 1790000000}


def test_parse_userinfo_header_empty_returns_none():
    assert parse_userinfo_header(None) is None
    assert parse_userinfo_header("") is None
    assert parse_userinfo_header("   ") is None
    assert parse_userinfo_header(" nonsense ") is None


# ---------------------------------------------------------------- 快照缓存

def test_save_cache_keeps_latest_three(config):
    """每订阅只保留最近 3 份快照（docs/01 流程 1）。"""
    for i in range(5):
        save_cache(config, "kuai", f"content-{i}".encode("utf-8"))
    snapshots = sorted(config.cache_dir.glob("kuai-*.yaml"))
    assert len(snapshots) == 3
    assert load_latest_cache(config, "kuai") == b"content-4"


def test_save_cache_isolated_between_subs(config):
    """两个订阅各自独立保留，互不清理。"""
    for i in range(4):
        save_cache(config, "a", f"a-{i}".encode("utf-8"))
    for i in range(2):
        save_cache(config, "b", f"b-{i}".encode("utf-8"))
    assert len(list(config.cache_dir.glob("a-*.yaml"))) == 3
    assert len(list(config.cache_dir.glob("b-*.yaml"))) == 2
    assert load_latest_cache(config, "a") == b"a-3"
    assert load_latest_cache(config, "b") == b"b-1"


def test_cache_chinese_sub_name_roundtrip(config):
    """中文订阅名可直接作文件名片段，保存/读回一致。"""
    save_cache(config, "机场A", b"payload")
    assert load_latest_cache(config, "机场A") == b"payload"


def test_load_latest_cache_empty_returns_none(config):
    assert latest_cache_path(config, "never-fetched") is None
    assert load_latest_cache(config, "never-fetched") is None
