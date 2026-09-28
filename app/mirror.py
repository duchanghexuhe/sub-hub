"""可选模块：配置产物公网镜像推送（默认关闭，docs/01 模块表、docs/04 §2）。

提供方：
- cf-kv   Cloudflare Workers KV：经 Cloudflare API v4 写入 KV；公网侧由自部署
          Worker（deploy/cf-worker/worker.js）校验 URL 中 token 后读取返回。
- github  GitHub 公开仓库随机路径：经 Contents API 上传，客户端拉 raw 链接。

语义（docs/04 §2）：
- 默认 enabled=False；产物含全部节点凭证，推送属敏感动作，必须用户显式开启。
- 推送时机：渲染产物内容变化后（对比最近一次推送的内容 hash，无变化不推）。
- 任何失败仅记日志并返回 MirrorResult，绝不向 pipeline 外抛异常（不影响主链路）。
- status() 返回脱敏状态供 UI 展示（不含任何凭据字段）。

安全纪律：日志不得输出完整订阅 URL（打码到 host，用 mask_url_host）与任何
token/凭据；提供方凭据仅落 data/mirror.json（本地 0600 语义）与请求头。
"""
from __future__ import annotations

import base64
import logging
import secrets
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote

import httpx

from app.config import AppConfig
from app.utils import (
    content_hash,
    latest_version,
    mask_url_host,
    now_iso,
    read_json,
    version_dir,
    write_json,
)

logger = logging.getLogger("subhub.mirror")

PROVIDERS: tuple[str, ...] = ("cf-kv", "github")

# 与 validator.publish 相同的产物顺序（内容 hash 口径保持一致）
ARTIFACT_ORDER: tuple[str, ...] = (
    "clash.yaml",
    "shadowrocket.conf",
    "clash-offline.yaml",
    "shadowrocket-offline.conf",
)

_CF_API_BASE = "https://api.cloudflare.com/client/v4"
_GITHUB_API_BASE = "https://api.github.com"


class MirrorConfigError(Exception):
    """镜像配置不完整/非法（中文消息，直接进入 MirrorResult.error）。"""


class MirrorPushError(Exception):
    """推送被提供方拒绝（中文消息，直接进入 MirrorResult.error）。"""


@dataclass
class MirrorResult:
    """一次推送尝试的结果（错误为中文描述；不抛出到调用方之外）。"""

    ok: bool
    provider: str | None       # "cf-kv" | "github"
    url: str | None            # 公网订阅 URL（主产物 clash.yaml）
    error: str | None
    pushed_at: str


# ------------------------------------------------------------------ 设置读写

def _default_settings() -> dict[str, Any]:
    return {
        "enabled": False,
        "provider": None,              # "cf-kv" | "github"
        "cf_kv": {
            "api_token": "",           # Cloudflare API Token（Workers KV Storage: Edit）
            "account_id": "",
            "namespace_id": "",
            "url_token": "",           # URL 中携带、由 Worker 强制校验的 token
            "worker_base": "",         # https://<worker>.<account>.workers.dev
        },
        "github": {
            "api_token": "",
            "owner": "",
            "repo": "",
            "branch": "main",
            "path": "",                # 随机路径（首次推送生成后固定，保证订阅 URL 稳定）
        },
        "last_push": None,             # {content_hash, pushed_at, provider, version, urls, files}
        "last_result": None,           # {ok, skipped, provider, error, at}
    }


def _merge_defaults(stored: dict[str, Any] | None) -> dict[str, Any]:
    """存量设置叠加到默认结构上（补齐新增字段，保留用户配置）。"""
    merged = _default_settings()
    for key, value in (stored or {}).items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key].update(value)
        else:
            merged[key] = value
    return merged


def load_mirror_settings(config: AppConfig) -> dict[str, Any]:
    """读 data/mirror.json；文件缺失/损坏时返回默认结构（enabled=False）。"""
    return _merge_defaults(read_json(config.mirror_settings_path, default={}))


def save_mirror_settings(config: AppConfig, settings: dict[str, Any]) -> None:
    """原子写 data/mirror.json（含提供方凭据，属本地敏感配置）。"""
    write_json(config.mirror_settings_path, settings)


# ------------------------------------------------------------------ 推送入口

def push_current(
    config: AppConfig, *, transport: httpx.BaseTransport | None = None
) -> MirrorResult:
    """推送最新已发布产物到镜像提供方。

    - 未启用 → ok=False, error="镜像未启用"，不做任何请求；
    - 内容 hash 与最近一次推送一致 → 跳过请求（ok=True，last_result.skipped=True）；
    - 任何异常（含网络错误）都被捕获并记日志，绝不抛出到 pipeline 外（docs/01）。
    transport 参数供测试注入 httpx.MockTransport，生产保持默认。
    """
    settings = load_mirror_settings(config)
    try:
        return _push(config, settings, transport)
    except (MirrorConfigError, MirrorPushError) as exc:
        return _fail(config, settings, str(exc))
    except Exception as exc:  # noqa: BLE001 —— 兜底：任何失败不影响主链路
        logger.warning(
            "镜像推送异常（不影响本地分发）：%s: %s", type(exc).__name__, exc
        )
        return _fail(config, settings, f"推送异常：{type(exc).__name__}: {exc}")


def _push(
    config: AppConfig,
    settings: dict[str, Any],
    transport: httpx.BaseTransport | None,
) -> MirrorResult:
    provider = settings.get("provider")
    if not settings.get("enabled"):
        logger.info("镜像模块未启用，跳过推送")
        return MirrorResult(ok=False, provider=provider, url=None, error="镜像未启用", pushed_at=now_iso())
    if provider not in PROVIDERS:
        return _fail(config, settings, "镜像提供方未配置（provider 须为 cf-kv 或 github）")

    version = latest_version(config.out_dir)
    if version is None:
        return _fail(config, settings, "尚无已发布产物，无法推送")
    artifacts = _collect_artifacts(config, version)
    if not artifacts:
        return _fail(config, settings, f"产物目录 v{version:04d} 中没有可推送的文件")

    digest = content_hash(
        b"".join(artifacts[name] for name in ARTIFACT_ORDER if name in artifacts)
    )
    last_push = settings.get("last_push") or {}
    if last_push.get("content_hash") == digest and last_push.get("provider") == provider:
        logger.info("镜像内容无变化（v%04d），跳过推送", version)
        result = MirrorResult(
            ok=True,
            provider=provider,
            url=(last_push.get("urls") or {}).get("clash.yaml"),
            error=None,
            pushed_at=now_iso(),
        )
        _save_last_result(config, settings, result, skipped=True)
        return result

    logger.info("开始镜像推送：提供方 %s，产物版本 v%04d，文件 %d 份", provider, version, len(artifacts))
    if provider == "cf-kv":
        urls = _push_cf_kv(settings, artifacts, transport)
    else:
        urls = _push_github(config, settings, artifacts, transport)

    pushed_at = now_iso()
    settings["last_push"] = {
        "content_hash": digest,
        "pushed_at": pushed_at,
        "provider": provider,
        "version": version,
        "urls": urls,
        "files": [name for name in ARTIFACT_ORDER if name in artifacts],
    }
    result = MirrorResult(ok=True, provider=provider, url=urls.get("clash.yaml"), error=None, pushed_at=pushed_at)
    _save_last_result(config, settings, result, skipped=False)
    # 公网订阅地址仅打码到 host 记日志（纪律同订阅 URL）
    logger.info("镜像推送成功：提供方 %s，公网地址 %s", provider, mask_url_host(result.url or ""))
    return result


def _fail(config: AppConfig, settings: dict[str, Any], message: str) -> MirrorResult:
    """记日志 + 记录 last_result（供 UI 展示），返回失败态 MirrorResult（绝不抛出）。"""
    logger.warning("镜像推送失败：%s", message)
    result = MirrorResult(
        ok=False,
        provider=settings.get("provider"),
        url=None,
        error=message,
        pushed_at=now_iso(),
    )
    _save_last_result(config, settings, result, skipped=False)
    return result


def _save_last_result(
    config: AppConfig,
    settings: dict[str, Any],
    result: MirrorResult,
    *,
    skipped: bool,
) -> None:
    settings["last_result"] = {
        "ok": result.ok,
        "skipped": skipped,
        "provider": result.provider,
        "error": result.error,
        "at": result.pushed_at,
    }
    try:
        save_mirror_settings(config, settings)
    except OSError as exc:
        logger.warning("镜像状态写入失败（推送结果以返回值为准）：%s", exc)


def _collect_artifacts(config: AppConfig, version: int) -> dict[str, bytes]:
    directory = version_dir(config.out_dir, version)
    artifacts: dict[str, bytes] = {}
    for name in ARTIFACT_ORDER:
        path = directory / name
        if path.is_file():
            artifacts[name] = path.read_bytes()
    return artifacts


# ------------------------------------------------------------------ 提供方：cf-kv

def _push_cf_kv(
    settings: dict[str, Any],
    artifacts: dict[str, bytes],
    transport: httpx.BaseTransport | None,
) -> dict[str, str]:
    """经 Cloudflare API v4 写 Workers KV：key = <url_token>/<文件名>。"""
    cf = settings.get("cf_kv") or {}
    api_token = (cf.get("api_token") or "").strip()
    account_id = (cf.get("account_id") or "").strip()
    namespace_id = (cf.get("namespace_id") or "").strip()
    url_token = (cf.get("url_token") or "").strip()
    worker_base = (cf.get("worker_base") or "").strip().rstrip("/")
    if not (api_token and account_id and namespace_id and url_token and worker_base):
        raise MirrorConfigError(
            "cf-kv 配置不完整（需要 api_token/account_id/namespace_id/url_token/worker_base）"
        )
    values_base = (
        f"{_CF_API_BASE}/accounts/{account_id}/storage/kv/namespaces/{namespace_id}/values"
    )
    headers = {"Authorization": f"Bearer {api_token}"}
    urls: dict[str, str] = {}
    with httpx.Client(transport=transport, headers=headers, timeout=30.0) as client:
        for name, data in artifacts.items():
            key = f"{url_token}/{name}"
            resp = client.put(f"{values_base}/{quote(key, safe='')}", content=data)
            if resp.status_code != 200:
                raise MirrorPushError(f"cf-kv 写入 {name} 失败：HTTP {resp.status_code}")
            try:
                body = resp.json()
            except ValueError:
                body = {}
            if body.get("success") is False:
                raise MirrorPushError(f"cf-kv 写入 {name} 被拒绝：{body.get('errors')}")
            urls[name] = f"{worker_base}/{url_token}/{name}"
    return urls


# ------------------------------------------------------------------ 提供方：github

def _push_github(
    config: AppConfig,
    settings: dict[str, Any],
    artifacts: dict[str, bytes],
    transport: httpx.BaseTransport | None,
) -> dict[str, str]:
    """经 Contents API 上传到公开仓库随机路径，客户端拉 raw 链接（须代理）。"""
    gh = settings.setdefault("github", {})
    api_token = (gh.get("api_token") or "").strip()
    owner = (gh.get("owner") or "").strip()
    repo = (gh.get("repo") or "").strip()
    branch = (gh.get("branch") or "main").strip() or "main"
    path = (gh.get("path") or "").strip()
    if not (api_token and owner and repo):
        raise MirrorConfigError("github 配置不完整（需要 api_token/owner/repo）")
    if not path:
        # 64 位十六进制随机路径（docs/04 §2：仅靠路径保密）；生成后固定，保证 URL 稳定
        path = secrets.token_hex(32)
        gh["path"] = path
        save_mirror_settings(config, settings)
        logger.info("已生成 GitHub 镜像随机路径并保存于 mirror.json")

    contents_base = f"{_GITHUB_API_BASE}/repos/{owner}/{repo}/contents"
    headers = {
        "Authorization": f"Bearer {api_token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    urls: dict[str, str] = {}
    with httpx.Client(transport=transport, headers=headers, timeout=30.0) as client:
        for name, data in artifacts.items():
            remote_path = f"{path}/{name}"
            existing = client.get(f"{contents_base}/{remote_path}", params={"ref": branch})
            sha: str | None = None
            if existing.status_code == 200:
                sha = existing.json().get("sha")
            elif existing.status_code != 404:
                raise MirrorPushError(
                    f"github 读取 {name} 现状失败：HTTP {existing.status_code}"
                )
            body: dict[str, Any] = {
                "message": f"sub-hub 镜像更新：{name}",
                "content": base64.b64encode(data).decode("ascii"),
                "branch": branch,
            }
            if sha:
                body["sha"] = sha
            resp = client.put(f"{contents_base}/{remote_path}", json=body)
            if resp.status_code not in (200, 201):
                raise MirrorPushError(f"github 上传 {name} 失败：HTTP {resp.status_code}")
            urls[name] = (
                f"https://raw.githubusercontent.com/{owner}/{repo}/{branch}/{remote_path}"
            )
    return urls


# ------------------------------------------------------------------ 状态（UI 用）

def _provider_configured(settings: dict[str, Any], provider: str | None) -> bool:
    if provider == "cf-kv":
        cf = settings.get("cf_kv") or {}
        return all(
            (cf.get(k) or "").strip()
            for k in ("api_token", "account_id", "namespace_id", "url_token", "worker_base")
        )
    if provider == "github":
        gh = settings.get("github") or {}
        return all((gh.get(k) or "").strip() for k in ("api_token", "owner", "repo"))
    return False


def status(config: AppConfig) -> dict[str, Any]:
    """镜像模块脱敏状态（/api/mirror 与 UI 展示用；不含任何凭据字段）。"""
    settings = load_mirror_settings(config)
    provider = settings.get("provider")
    provider = provider if provider in PROVIDERS else None
    last_push = settings.get("last_push") or {}
    last_result = settings.get("last_result") or {}
    return {
        "enabled": bool(settings.get("enabled")),
        "provider": provider,
        "configured": _provider_configured(settings, provider),
        "public_url": (last_push.get("urls") or {}).get("clash.yaml"),
        "urls": dict(last_push.get("urls") or {}),
        "last_push": (
            {k: last_push.get(k) for k in ("pushed_at", "provider", "version", "content_hash", "files")}
            if last_push
            else None
        ),
        "last_result": dict(last_result) if last_result else None,
    }
