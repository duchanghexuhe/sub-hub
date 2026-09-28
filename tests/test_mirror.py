"""mirror 模块测试：默认关闭、请求形态（mock）、hash 无变化不推、失败不影响主链路。

提供方请求一律用 httpx.MockTransport 捕获断言，不发真实网络请求；
数据目录用 conftest 的 tmp_path 隔离，绝不读写仓库根 data/。
"""
from __future__ import annotations

import base64
import json
from typing import Any
from urllib.parse import unquote

import httpx

from app import mirror
from app.config import AppConfig
from app.utils import atomic_write_text, content_hash

CF_TOKEN = "cf-api-token"
ACCOUNT_ID = "acct123"
NAMESPACE_ID = "nsid456"
URL_TOKEN = "a" * 32
WORKER_BASE = "https://sub-hub-mirror.example.workers.dev"

CLASH_YAML = "mixed-port: 7897\nproxies: []\n"
SR_CONF = "[Proxy]\n[Proxy Group]\n[Rule]\n"


def _enable(config: AppConfig, provider: str = "cf-kv") -> None:
    """写入一份完整启用的镜像设置（凭据仅落在临时 mirror.json）。"""
    settings = mirror.load_mirror_settings(config)
    settings["enabled"] = True
    settings["provider"] = provider
    if provider == "cf-kv":
        settings["cf_kv"].update(
            {
                "api_token": CF_TOKEN,
                "account_id": ACCOUNT_ID,
                "namespace_id": NAMESPACE_ID,
                "url_token": URL_TOKEN,
                "worker_base": WORKER_BASE,
            }
        )
    else:
        settings["github"].update(
            {"api_token": "ghp_test", "owner": "me", "repo": "mirror", "branch": "main"}
        )
    mirror.save_mirror_settings(config, settings)


def _publish_artifacts(config: AppConfig, clash: str = CLASH_YAML, sr: str = SR_CONF) -> None:
    """在临时 out/v0001 放两份产物（模拟 validator.publish 的输出，仅供读取）。"""
    vdir = config.out_dir / "v0001"
    vdir.mkdir(parents=True, exist_ok=True)
    atomic_write_text(vdir / "clash.yaml", clash)
    atomic_write_text(vdir / "shadowrocket.conf", sr)


def _artifact_bytes(config: AppConfig, name: str) -> bytes:
    """磁盘上产物的真实字节（Windows 文本写会把 \\n 转成 \\r\\n，断言以实际为准）。"""
    return (config.out_dir / "v0001" / name).read_bytes()


def _ok_handler(seen: list[httpx.Request]):
    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"success": True, "errors": [], "result": {"id": "x"}})

    return handler


# ------------------------------------------------------------------ 默认状态

def test_default_settings_disabled_and_status_masks_credentials(config):
    settings = mirror.load_mirror_settings(config)
    assert settings["enabled"] is False
    assert settings["provider"] is None

    info = mirror.status(config)
    assert info["enabled"] is False
    assert info["configured"] is False
    assert "api_token" not in json.dumps(info, ensure_ascii=False)


def test_push_disabled_returns_error_without_state_write(config):
    result = mirror.push_current(config)
    assert result.ok is False
    assert result.error == "镜像未启用"
    # 未启用时不应产生任何状态写入
    assert not config.mirror_settings_path.exists()


def test_push_without_artifacts_fails_gracefully(config):
    _enable(config)
    result = mirror.push_current(config)
    assert result.ok is False
    assert "尚无已发布产物" in result.error
    assert mirror.load_mirror_settings(config)["last_result"]["ok"] is False


# ------------------------------------------------------------------ cf-kv 提供方

def test_cf_kv_push_request_shape_then_skip_on_unchanged(config):
    _publish_artifacts(config)
    seen: list[httpx.Request] = []
    _enable(config)

    result = mirror.push_current(config, transport=httpx.MockTransport(_ok_handler(seen)))

    assert result.ok is True
    assert result.provider == "cf-kv"
    assert result.url == f"{WORKER_BASE}/{URL_TOKEN}/clash.yaml"
    assert len(seen) == 2
    bodies: dict[str, bytes] = {}
    for request in seen:
        assert request.method == "PUT"
        assert request.headers["Authorization"] == f"Bearer {CF_TOKEN}"
        tail = str(request.url).split("/values/", 1)[1]
        assert "%2F" in tail  # KV key 中的斜杠必须编码，避免被当成路径
        key = unquote(tail)
        bodies[key.split("/", 1)[1]] = request.content
    assert bodies == {
        "clash.yaml": _artifact_bytes(config, "clash.yaml"),
        "shadowrocket.conf": _artifact_bytes(config, "shadowrocket.conf"),
    }

    # 内容无变化 → 不发请求直接跳过
    result2 = mirror.push_current(config, transport=httpx.MockTransport(_ok_handler(seen)))
    assert result2.ok is True
    assert result2.url == result.url
    assert len(seen) == 2  # 请求数不变
    assert mirror.load_mirror_settings(config)["last_result"]["skipped"] is True

    # 内容变化 → 重新推送并更新 last_push
    atomic_write_text(config.out_dir / "v0001" / "clash.yaml", CLASH_YAML + "# changed\n")
    result3 = mirror.push_current(config, transport=httpx.MockTransport(_ok_handler(seen)))
    assert result3.ok is True
    assert len(seen) == 4
    last_push = mirror.load_mirror_settings(config)["last_push"]
    assert last_push["files"] == ["clash.yaml", "shadowrocket.conf"]
    assert last_push["content_hash"] == content_hash(
        _artifact_bytes(config, "clash.yaml") + _artifact_bytes(config, "shadowrocket.conf")
    )


def test_cf_kv_http_error_recorded_not_raised(config):
    _publish_artifacts(config)
    _enable(config)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(403, json={"success": False, "errors": [{"message": "auth"}]})

    result = mirror.push_current(config, transport=httpx.MockTransport(handler))
    assert result.ok is False
    assert "403" in result.error
    last_result = mirror.load_mirror_settings(config)["last_result"]
    assert last_result["ok"] is False
    assert last_result["error"] == result.error


def test_cf_kv_incomplete_config_fails_before_requests(config):
    _publish_artifacts(config)
    settings = mirror.load_mirror_settings(config)
    settings["enabled"] = True
    settings["provider"] = "cf-kv"  # 凭据留空
    mirror.save_mirror_settings(config, settings)

    def handler(request: httpx.Request) -> httpx.Response:  # pragma: no cover - 不应被调用
        raise AssertionError("配置不完整时不应发起请求")

    result = mirror.push_current(config, transport=httpx.MockTransport(handler))
    assert result.ok is False
    assert "配置不完整" in result.error


# ------------------------------------------------------------------ github 提供方

def test_github_push_request_shape_and_stable_random_path(config):
    _publish_artifacts(config)
    _enable(config, provider="github")
    puts: list[httpx.Request] = []
    bodies: list[dict] = []
    seen_headers: list[httpx.Headers] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen_headers.append(request.headers)
        if request.method == "GET":
            return httpx.Response(404, json={"message": "Not Found"})
        puts.append(request)
        bodies.append(json.loads(request.content))
        return httpx.Response(201, json={"content": {}, "commit": {}})

    result = mirror.push_current(config, transport=httpx.MockTransport(handler))

    assert result.ok is True
    assert result.provider == "github"
    assert len(puts) == 2
    saved_path = mirror.load_mirror_settings(config)["github"]["path"]
    assert len(saved_path) == 64  # 64 位十六进制随机路径（docs/04 §2）
    int(saved_path, 16)
    for headers in seen_headers:
        assert headers["Authorization"] == "Bearer ghp_test"
        assert headers["Accept"] == "application/vnd.github+json"
    for request, body in zip(puts, bodies):
        assert str(request.url).startswith("https://api.github.com/repos/me/mirror/contents/")
        assert f"/{saved_path}/" in str(request.url)
        assert body["branch"] == "main"
        assert body["message"].startswith("sub-hub 镜像更新")
    assert base64.b64decode(bodies[0]["content"]) == _artifact_bytes(config, "clash.yaml")
    assert base64.b64decode(bodies[1]["content"]) == _artifact_bytes(config, "shadowrocket.conf")
    # 公网订阅 URL = raw 链接，随机路径已固化
    assert result.url == f"https://raw.githubusercontent.com/me/mirror/main/{saved_path}/clash.yaml"

    # 内容变化后再推：随机路径保持不变（订阅 URL 稳定）
    atomic_write_text(config.out_dir / "v0001" / "clash.yaml", CLASH_YAML + "# v2\n")
    result2 = mirror.push_current(config, transport=httpx.MockTransport(handler))
    assert result2.ok is True
    assert mirror.load_mirror_settings(config)["github"]["path"] == saved_path
    assert result2.url == result.url


def test_github_update_sends_existing_sha(config):
    _publish_artifacts(config)
    _enable(config, provider="github")
    bodies: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(200, json={"sha": "abc123", "content": ""})
        bodies.append(json.loads(request.content))
        return httpx.Response(200, json={"content": {}, "commit": {}})

    result = mirror.push_current(config, transport=httpx.MockTransport(handler))
    assert result.ok is True
    assert bodies and all(b.get("sha") == "abc123" for b in bodies)


# ------------------------------------------------------------------ 主链路安全网

def test_push_never_raises_on_transport_exception(config):
    _publish_artifacts(config)
    _enable(config)

    class BrokenTransport(httpx.BaseTransport):
        def handle_request(self, request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("网络不可达")

    result = mirror.push_current(config, transport=BrokenTransport())
    assert result.ok is False
    assert "网络不可达" in result.error
    # 本地产物不受镜像失败影响（主链路安全网）
    assert (config.out_dir / "v0001" / "clash.yaml").is_file()


def test_status_reports_last_push_without_credentials(config):
    _publish_artifacts(config)
    _enable(config)
    mirror.push_current(config, transport=httpx.MockTransport(_ok_handler([])))

    info = mirror.status(config)
    assert info["enabled"] is True
    assert info["configured"] is True
    assert info["public_url"] == f"{WORKER_BASE}/{URL_TOKEN}/clash.yaml"
    assert info["last_push"]["version"] == 1
    assert info["last_result"]["ok"] is True
    assert CF_TOKEN not in json.dumps(info, ensure_ascii=False)
