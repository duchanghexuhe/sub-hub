"""通用工具：原子写文件、URL 打码、内容 hash、版本目录管理。

本模块为最底层共享工具，不依赖 app 内其他模块。
安全纪律：任何日志都不得输出完整订阅 URL 与节点凭据，统一用 mask_url_host /
mask_url_tail 打码。
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import re
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

logger = logging.getLogger("subhub.utils")

_VERSION_DIR_RE = re.compile(r"^v(\d+)$")


# ---------------------------------------------------------------- 文件原子写

def restrict_permissions(path: Path) -> None:
    """尽力把文件权限收紧到 0600（Windows 上是尽力而为，失败不抛错）。"""
    try:
        os.chmod(path, 0o600)
    except OSError:
        logger.debug("收紧文件权限失败（忽略）：%s", path.name)


def atomic_write_text(path: Path, text: str, *, encoding: str = "utf-8") -> None:
    """先写同目录 .tmp 再 os.replace，保证读到的一定是完整内容。"""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding=encoding)
    os.replace(tmp, path)


def atomic_write_bytes(path: Path, data: bytes) -> None:
    """同 atomic_write_text，字节版。"""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_bytes(data)
    os.replace(tmp, path)


def write_json(path: Path, data: Any) -> None:
    """原子写 JSON（ensure_ascii=False，缩进 2）。"""
    atomic_write_text(Path(path), json.dumps(data, ensure_ascii=False, indent=2))


def read_json(path: Path, default: Any = None) -> Any:
    """读 JSON；文件不存在或解析失败返回 default。"""
    path = Path(path)
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        logger.warning("JSON 读取/解析失败，返回默认值：%s", path.name)
        return default


def now_iso() -> str:
    """本地时间 ISO8601（含微秒，字符串比较即时序）。"""
    return datetime.now().isoformat(timespec="microseconds")


# ---------------------------------------------------------------- 打码

def mask_url_host(url: str) -> str:
    """日志用打码：保留 scheme://host:port，路径与查询替换为「…」。

    例：https://sub.example.com:8443/token?x=1 -> https://sub.example.com:8443/…
    解析失败返回 "<invalid-url>"，绝不原样输出。
    """
    try:
        parts = urlsplit(url.strip())
        if not parts.scheme or not parts.netloc:
            return "<invalid-url>"
        return f"{parts.scheme}://{parts.netloc}/…"
    except ValueError:
        return "<invalid-url>"


def mask_url_tail(url: str) -> str:
    """UI 展示用打码：仅保留末 6 位（docs/03 §1），其余以「…」代替。"""
    url = (url or "").strip()
    if len(url) <= 6:
        return "…" + url
    return "…" + url[-6:]


# ---------------------------------------------------------------- hash

def content_hash(data: str | bytes) -> str:
    """sha256 十六进制摘要（产物 ETag / 变更检测用）。"""
    if isinstance(data, str):
        data = data.encode("utf-8")
    return hashlib.sha256(data).hexdigest()


# ---------------------------------------------------------------- 版本目录

def new_version_dir(out_dir: Path, version: int) -> Path:
    """返回并创建 data/out/v<零填充4位> 目录。"""
    path = Path(out_dir) / f"v{version:04d}"
    path.mkdir(parents=True, exist_ok=True)
    return path


def list_versions(out_dir: Path) -> list[int]:
    """列出 out_dir 下全部版本号，升序返回。"""
    out_dir = Path(out_dir)
    if not out_dir.exists():
        return []
    versions = []
    for child in out_dir.iterdir():
        m = _VERSION_DIR_RE.match(child.name)
        if child.is_dir() and m:
            versions.append(int(m.group(1)))
    versions.sort()
    return versions


def latest_version(out_dir: Path) -> int | None:
    """最新版本号；无任何版本时返回 None。"""
    versions = list_versions(out_dir)
    return versions[-1] if versions else None


def next_version(out_dir: Path) -> int:
    """下一个待发布版本号（从 1 开始）。"""
    latest = latest_version(out_dir)
    return 1 if latest is None else latest + 1


def version_dir(out_dir: Path, version: int) -> Path:
    """按版本号取目录路径（不创建）。"""
    return Path(out_dir) / f"v{version:04d}"


def prune_versions(out_dir: Path, *, keep: int = 5) -> list[int]:
    """只保留最近 keep 个版本，返回被删除的版本号列表（升序）。"""
    versions = list_versions(out_dir)
    if len(versions) <= keep:
        return []
    removed = versions[:-keep]
    for v in removed:
        target = version_dir(out_dir, v)
        try:
            for child in sorted(target.rglob("*"), reverse=True):
                if child.is_file() or child.is_symlink():
                    child.unlink(missing_ok=True)
                elif child.is_dir():
                    child.rmdir()
            target.rmdir()
        except OSError:
            logger.warning("清理旧版本目录失败：%s", target.name)
    return removed
