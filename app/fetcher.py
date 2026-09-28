"""订阅抓取：httpx 同步抓取 + subscription-userinfo 解析 + 原始内容快照缓存。

职责边界（docs/01 模块表与流程 1、docs/INTERFACES.md §3.1）：
- fetcher 只负责「抓取 + 快照缓存 + 状态返回」；store.set_fetch_status /
  store.set_userinfo / store.replace_nodes 由 pipeline 统一调用（本模块不持有 store）。
- 抓取失败/超时/订阅失效**不抛异常**：统一封装为 FetchResult（status = TIMEOUT /
  FAILED / INVALID，error 为中文描述、URL 打码到 host），由 pipeline 决定沿用
  旧快照继续渲染（docs/01「失败路径与安全网」：分发可用性 > 数据新鲜度）。
- 快照落盘 data/cache/<订阅名>-<时间戳>.yaml，每订阅只保留最近 3 份；提供
  load_latest_cache 读回最近一份快照的能力（失败时人工排查 / 上层兜底复用）。

安全纪律：任何日志与错误信息不得出现完整订阅 URL（一律 mask_url_host 打码到
host），不得打印节点凭据字段。httpx 库自身会在 INFO 级打印完整请求 URL
（订阅 URL 即机场凭证），本模块装载全局过滤器把该日志同样打码到 host。
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

import httpx

from app.config import AppConfig
from app.models import FetchStatus, Subscription
from app.utils import atomic_write_bytes, mask_url_host, now_iso

logger = logging.getLogger("subhub.fetcher")

# subscription-userinfo 头只认这四个键（docs/01 流程 1：流量/到期）
_USERINFO_KEYS = frozenset({"upload", "download", "total", "expire"})

# 快照：每订阅保留最近 3 份（docs/01 流程 1）
_CACHE_KEEP = 3
_CACHE_TS_FORMAT = "%Y%m%dT%H%M%S%f"          # 定宽可排序：8+1+12 位
_CACHE_TS_RE = re.compile(r"(\d{8}T\d{12})")
# 文件名安全化：仅替换文件系统非法字符与控制字符（保留中文，保证订阅名唯一性）
_UNSAFE_FS_CHARS = re.compile(r'[\\/:*?"<>|\x00-\x1f]+')

# httpx 的「HTTP Request: GET <完整URL>」INFO 日志会把订阅 URL 原样写进日志——
# 订阅 URL 即机场凭证，统一经此过滤器打码到 host（对 rulesync 等其他 httpx
# 使用方同样生效，只是日志粒度变粗，属可接受代价）。
_HTTPX_URL_RE = re.compile(r"https?://[^\s\"']+")


class _HttpxUrlRedactFilter(logging.Filter):
    """把 httpx 日志记录里的完整 URL 替换为 mask_url_host 打码形式。

    httpx 0.28 的请求日志以 `URL('https://…')` 对象作为 %s 参数传入，
    str 参数内的 URL（其他版本/其他模块）也要一并处理。
    """

    def filter(self, record: logging.LogRecord) -> bool:
        if record.args:
            record.args = tuple(_redact_log_arg(arg) for arg in record.args)
        elif isinstance(record.msg, str):
            record.msg = _HTTPX_URL_RE.sub(lambda m: mask_url_host(m.group(0)), record.msg)
        return True


def _redact_log_arg(arg: object) -> object:
    """单条日志参数：str 内的 URL / httpx.URL 对象 → 打码到 host；其余原样。"""
    if isinstance(arg, str):
        return _HTTPX_URL_RE.sub(lambda m: mask_url_host(m.group(0)), arg)
    if isinstance(arg, httpx.URL):
        return mask_url_host(str(arg))
    return arg


_HTTPX_REDACT_FILTER = _HttpxUrlRedactFilter()


def _install_httpx_log_redaction() -> None:
    """给 httpx logger 装打码过滤器（幂等；模块导入时调用一次）。"""
    httpx_logger = logging.getLogger("httpx")
    for existing in httpx_logger.filters:
        if isinstance(existing, _HttpxUrlRedactFilter):
            return
    httpx_logger.addFilter(_HTTPX_REDACT_FILTER)


_install_httpx_log_redaction()


@dataclass
class FetchResult:
    """单次抓取结果（失败也不抛异常，由 pipeline 依 status 决定沿用旧快照）。"""

    sub_id: int
    sub_name: str
    status: FetchStatus                 # OK / INVALID(401/403) / TIMEOUT / FAILED
    content: bytes | None               # 原始响应体（Clash YAML 或 base64）
    userinfo: dict[str, int] | None     # subscription-userinfo 解析结果
    error: str | None                   # 中文错误描述（URL 已打码到 host）
    fetched_at: str = field(default_factory=now_iso)


def parse_userinfo_header(value: str | None) -> dict[str, int] | None:
    """解析 subscription-userinfo 响应头。

    "upload=123; download=456; total=789; expire=1790000000" → 对应 dict；
    只认 upload/download/total/expire 四键；缺项与非整数项忽略；
    全部无效或入参为空 → None。
    """
    if not value or not str(value).strip():
        return None
    parsed: dict[str, int] = {}
    for chunk in str(value).split(";"):
        chunk = chunk.strip()
        if not chunk or "=" not in chunk:
            continue
        key, _, raw = chunk.partition("=")
        key = key.strip().lower()
        raw = raw.strip().strip('"')
        if key not in _USERINFO_KEYS or not raw:
            continue
        try:
            parsed[key] = int(raw)
        except ValueError:
            try:  # 个别机场会发 "expire=1790000000.0"
                parsed[key] = int(float(raw))
            except ValueError:
                logger.debug("userinfo 项无法解析为整数，忽略：%s=%s", key, raw)
    return parsed or None


def fetch_subscription(
    sub: Subscription,
    *,
    config: AppConfig,
    timeout: float = 20.0,
    transport: httpx.BaseTransport | None = None,
) -> FetchResult:
    """抓取单个订阅（同步 httpx.Client，固定 Clash UA 直出 YAML）。

    契约（docs/INTERFACES.md §3.1）：
    - 请求头 User-Agent = config.fetch_user_agent；
    - HTTP 401/403 → FetchStatus.INVALID；超时 → TIMEOUT；其余网络/HTTP 错误 → FAILED；
    - 任何失败都不抛异常，调用方一定拿到 FetchResult；
    - 成功（status=OK）时自动写快照缓存（save_cache，失败仅告警不影响结果）。

    transport 参数仅供测试注入 httpx.MockTransport（对契约调用方不可见）。
    """
    masked = mask_url_host(sub.url)
    if not (sub.url or "").strip():
        logger.warning("订阅「%s」URL 为空，无法抓取", sub.name)
        return FetchResult(
            sub_id=sub.id, sub_name=sub.name, status=FetchStatus.FAILED,
            content=None, userinfo=None, error="订阅 URL 为空",
        )
    try:
        with httpx.Client(
            timeout=timeout,
            follow_redirects=True,
            headers={"User-Agent": config.fetch_user_agent},
            transport=transport,
            trust_env=False,  # 机场订阅一律直连，绝不经环境代理（机场常拒绝境外出口）
        ) as client:
            response = client.get(sub.url)
    except httpx.TimeoutException as exc:
        logger.warning("订阅「%s」抓取超时（%s）：%s", sub.name, masked, type(exc).__name__)
        return FetchResult(
            sub_id=sub.id, sub_name=sub.name, status=FetchStatus.TIMEOUT,
            content=None, userinfo=None, error=f"抓取超时（>{timeout:g}s）：{masked}",
        )
    except httpx.HTTPError as exc:
        # 只记异常类型名，不透传 exc 文本（可能内含完整 URL）
        logger.warning("订阅「%s」网络错误（%s）：%s", sub.name, masked, type(exc).__name__)
        return FetchResult(
            sub_id=sub.id, sub_name=sub.name, status=FetchStatus.FAILED,
            content=None, userinfo=None, error=f"网络错误：{type(exc).__name__}（{masked}）",
        )
    except Exception as exc:  # 契约保证：任何意外都不外抛
        logger.warning("订阅「%s」抓取异常（%s）：%s", sub.name, masked, type(exc).__name__)
        return FetchResult(
            sub_id=sub.id, sub_name=sub.name, status=FetchStatus.FAILED,
            content=None, userinfo=None, error=f"抓取异常：{type(exc).__name__}（{masked}）",
        )

    if response.status_code in (401, 403):
        logger.warning("订阅「%s」订阅失效：HTTP %d（%s）", sub.name, response.status_code, masked)
        return FetchResult(
            sub_id=sub.id, sub_name=sub.name, status=FetchStatus.INVALID,
            content=None, userinfo=None,
            error=f"订阅失效：HTTP {response.status_code}，token 无效或被停用（{masked}）",
        )
    if response.status_code != 200:
        logger.warning("订阅「%s」抓取失败：HTTP %d（%s）", sub.name, response.status_code, masked)
        return FetchResult(
            sub_id=sub.id, sub_name=sub.name, status=FetchStatus.FAILED,
            content=None, userinfo=None,
            error=f"HTTP {response.status_code}（{masked}）",
        )

    content = response.content or b""
    if not content.strip():
        logger.warning("订阅「%s」返回空内容（%s）", sub.name, masked)
        return FetchResult(
            sub_id=sub.id, sub_name=sub.name, status=FetchStatus.FAILED,
            content=None, userinfo=None, error=f"订阅返回空内容（{masked}）",
        )

    userinfo = parse_userinfo_header(response.headers.get("subscription-userinfo"))
    try:
        save_cache(config, sub.name, content)
    except OSError as exc:
        # 快照属 best-effort 审计件，写失败不影响本次抓取结果（content 仍在内存交给 pipeline）
        logger.warning("订阅「%s」快照写入失败（不影响抓取结果）：%s", sub.name, exc)
    logger.info(
        "订阅「%s」抓取成功：%d 字节，userinfo %s（%s）",
        sub.name, len(content), "已解析" if userinfo else "缺失", masked,
    )
    return FetchResult(
        sub_id=sub.id, sub_name=sub.name, status=FetchStatus.OK,
        content=content, userinfo=userinfo, error=None,
    )


# ---------------------------------------------------------------- 快照缓存

def save_cache(config: AppConfig, sub_name: str, content: bytes) -> Path:
    """原始响应体快照到 data/cache/<订阅名>-<时间戳>.yaml，每订阅只留最近 3 份。

    时间戳为本地时间定宽格式（%Y%m%dT%H%M%S%f），字典序即时序；返回快照路径。
    """
    safe = _safe_cache_name(sub_name)
    ts = datetime.now().strftime(_CACHE_TS_FORMAT)
    path = config.cache_dir / f"{safe}-{ts}.yaml"
    atomic_write_bytes(path, content)
    _prune_cache(config.cache_dir, safe)
    logger.info("订阅「%s」快照已保存：%s（%d 字节）", sub_name, path.name, len(content))
    return path


def latest_cache_path(config: AppConfig, sub_name: str) -> Path | None:
    """该订阅最近一份快照的路径；从未成功抓取过返回 None。"""
    entries = _cache_files(config.cache_dir, _safe_cache_name(sub_name))
    return entries[-1][1] if entries else None


def load_latest_cache(config: AppConfig, sub_name: str) -> bytes | None:
    """读回该订阅最近一份快照内容；无快照或读取失败返回 None。"""
    path = latest_cache_path(config, sub_name)
    if path is None:
        return None
    try:
        return path.read_bytes()
    except OSError as exc:
        logger.warning("订阅「%s」快照读取失败：%s（%s）", sub_name, path.name, type(exc).__name__)
        return None


def _safe_cache_name(sub_name: str) -> str:
    """订阅名 → 文件名安全片段：替换路径分隔符/非法字符/控制字符，保留中文。"""
    name = _UNSAFE_FS_CHARS.sub("_", (sub_name or "").strip()).strip(" .")
    return name or "sub"


def _cache_files(cache_dir: Path, safe_name: str) -> list[tuple[str, Path]]:
    """列出该订阅已有快照 [(时间戳, 路径)]，按时间戳升序（fullmatch 防跨订阅误匹配）。"""
    pattern = re.compile(re.escape(safe_name) + r"-" + _CACHE_TS_RE.pattern + r"\.yaml")
    entries: list[tuple[str, Path]] = []
    if cache_dir.is_dir():
        for child in cache_dir.iterdir():
            match = pattern.fullmatch(child.name)
            if child.is_file() and match:
                entries.append((match.group(1), child))
    entries.sort(key=lambda item: item[0])
    return entries


def _prune_cache(cache_dir: Path, safe_name: str, *, keep: int = _CACHE_KEEP) -> list[Path]:
    """只保留最近 keep 份快照，返回被删除的路径列表（升序）。"""
    entries = _cache_files(cache_dir, safe_name)
    if len(entries) <= keep:
        return []
    removed = [path for _, path in entries[:-keep]]
    for path in removed:
        try:
            path.unlink(missing_ok=True)
        except OSError as exc:
            logger.warning("清理旧快照失败：%s（%s）", path.name, type(exc).__name__)
    return removed
