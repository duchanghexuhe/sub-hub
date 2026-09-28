"""FastAPI 应用：管理 API + 配置分发端点 + 单页管理 UI（docs/03 §2/§3）。

模块边界：web 只做编排与读盘——抓取/渲染/校验/发布归 fetcher/templater/validator/pipeline，
纯净度归 purity/probe，镜像归 mirror，定时归 scheduler。上述并行模块一律经 FastAPI
依赖注入（get_pipeline / get_purity / get_mirror）延迟导入：模块未就绪时对应端点
返回 503 {code:"module_unavailable"}，绝不影响分发端点与纯存储类端点。

分发语义（docs/01 安全网 / docs/03 §2）：
- 分发端点任何后端故障都不 5xx 空响应：从最新版本往旧找，返回上一份有效产物；
  一个产物都没有时返回占位配置（200）。
- 命中时附 ETag（产物内容 sha256）与 subscription-userinfo（启用订阅汇总：
  upload/download 求和，total/expire 取最小非零值，docs/INTERFACES §5.6），304 支持。
- token 校验失败一律 404 {code:"not_found"}；?qr 返回 qrcode 生成的内嵌 SVG 扫码页。

安全纪律：
- 订阅 URL 只以 mask_url_tail（仅尾 6 位）出现在响应中；日志一律 mask_url_host。
- 节点凭据字段（Node.credentials）与 server 地址不出现在任何响应里。
- /rules/{file} 只允许 .yaml/.list 单段文件名，防目录穿越。
"""
from __future__ import annotations

import html
import importlib
import io
import json
import logging
import re
import secrets
import threading
from contextlib import asynccontextmanager
from dataclasses import asdict, is_dataclass
from pathlib import Path
from types import ModuleType
from typing import Any, Callable, Iterator

import qrcode
import qrcode.image.svg
from fastapi import Depends, FastAPI, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import HTMLResponse, JSONResponse, Response
from pydantic import BaseModel
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.config import AppConfig, load_config
from app.models import FetchStatus, Node, PurityResult, Subscription
from app.store import Store
from app.utils import (
    content_hash,
    list_versions,
    latest_version,
    mask_url_host,
    mask_url_tail,
    now_iso,
    read_json,
    version_dir,
)

logger = logging.getLogger("subhub.web")

# --------------------------------------------------------------------- 常量

_ARTIFACTS: dict[str, dict[str, str]] = {
    "clash.yaml": {"media_type": "text/yaml; charset=utf-8", "label": "mihomo 主配置"},
    "shadowrocket.conf": {"media_type": "text/plain; charset=utf-8", "label": "SR 主配置"},
    "clash-offline.yaml": {"media_type": "text/yaml; charset=utf-8", "label": "mihomo 离线自包含配置"},
    "shadowrocket-offline.conf": {"media_type": "text/plain; charset=utf-8", "label": "SR 离线自包含配置"},
}

_PREVIEW_FMT: dict[str, str] = {
    "clash": "clash.yaml",
    "sr": "shadowrocket.conf",
}

_NODE_TAGS = {"residential", "iplc", "fake"}

_RULE_FILE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*\.(yaml|list)$")

_MIRROR_PROVIDERS = {"cf-kv", "github"}

_INDEX_PATH = Path(__file__).parent / "web_static" / "index.html"
_BOOTSTRAP_PLACEHOLDER = "__BOOTSTRAP_JSON__"

_STATUS_CODE_NAMES = {
    400: "bad_request",
    401: "unauthorized",
    403: "forbidden",
    404: "not_found",
    405: "method_not_allowed",
    409: "conflict",
    500: "internal_error",
    502: "bad_gateway",
    503: "service_unavailable",
}

_PLACEHOLDER_TEXT = "# sub-hub：暂无可用配置产物\n# 服务端尚未成功发布任何版本；请在管理页面添加订阅并刷新。\n"


class ApiError(Exception):
    """业务错误：统一错误体 {code, message}（docs/03 §2）。"""

    def __init__(self, status_code: int, code: str, message: str) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message


# --------------------------------------------------------------------- 依赖


def get_cfg(request: Request) -> AppConfig:
    return request.app.state.config


def get_st(request: Request) -> Store:
    return request.app.state.store


def _import_or_503(module_name: str, zh_name: str) -> ModuleType:
    try:
        return importlib.import_module(module_name)
    except ImportError as exc:  # 并行模块尚未落地（或部署不完整）
        raise ApiError(
            503, "module_unavailable", f"{zh_name}模块未就绪，请检查部署完整性后重试"
        ) from exc


def get_pipeline() -> ModuleType:
    return _import_or_503("app.pipeline", "流水线")


def get_purity() -> ModuleType:
    return _import_or_503("app.purity", "纯净度")


def get_mirror() -> ModuleType:
    return _import_or_503("app.mirror", "镜像")


def _optional_module(module_name: str) -> ModuleType | None:
    try:
        return importlib.import_module(module_name)
    except ImportError:
        return None


# --------------------------------------------------------------------- 请求模型


class SubCreateBody(BaseModel):
    name: str
    url: str
    enabled: bool = True


class SubPatchBody(BaseModel):
    name: str | None = None
    url: str | None = None
    enabled: bool | None = None


class RefreshBody(BaseModel):
    sub_id: int | None = None


class PurityScanBody(BaseModel):
    full: bool = False


class RollbackBody(BaseModel):
    version: int


class MirrorBody(BaseModel):
    provider: str
    credentials: dict[str, Any] | str | None = None
    enabled: bool = True


# --------------------------------------------------------------------- 序列化


def _sub_public(sub: Subscription) -> dict[str, Any]:
    """订阅对外形态：URL 只给打码（尾 6 位），绝不返回明文。"""
    status = sub.last_fetch_status
    return {
        "id": sub.id,
        "name": sub.name,
        "enabled": sub.enabled,
        "url_masked": mask_url_tail(sub.url),
        "last_fetch_at": sub.last_fetch_at,
        "last_fetch_status": status.value if status else None,
        "last_fetch_status_label": status.label if status else "从未抓取",
        "userinfo": sub.userinfo,
    }


def _purity_public(r: PurityResult) -> dict[str, Any]:
    return {
        "node_name": r.node_name,
        "source_sub": r.source_sub,
        "exit_ip": r.exit_ip,
        "country": r.country,
        "asn": r.asn,
        "org": r.org,
        "isp": r.isp,
        "ip_type": r.ip_type,
        "hosting": r.hosting,
        "proxy": r.proxy,
        "mobile": r.mobile,
        "claude_rank": r.claude_rank,
        "checked_at": r.checked_at,
    }


def _node_public(node: Node, purity: dict[str, Any] | None) -> dict[str, Any]:
    """节点对外形态：只含展示字段；凭据（credentials）与 server 地址不外泄。"""
    return {
        "name": node.name,
        "orig_name": node.orig_name,
        "type": node.type,
        "region": node.region,
        "residential": node.residential,
        "iplc": node.iplc,
        "rate": node.rate,
        "filtered": node.filtered,
        "filter_reason": node.filter_reason,
        "source_sub": node.source_sub,
        "purity": purity,
    }


def _pipeline_summary(result: Any) -> dict[str, Any]:
    """PipelineResult → dict（getattr 兜底，兼容测试替身）。"""
    return {
        "published": getattr(result, "published", None),
        "version": getattr(result, "version", None),
        "node_count": getattr(result, "node_count", None),
        "filtered_count": getattr(result, "filtered_count", None),
        "stale_subs": list(getattr(result, "stale_subs", None) or []),
        "errors": list(getattr(result, "errors", None) or []),
    }


def _rank_sort_key(r: PurityResult) -> tuple[int, int, str]:
    """Claude 适配度排序：评分降序（None 最后），住宅恒在机房前，同名按名称。"""
    rank = r.claude_rank if r.claude_rank is not None else -1
    return (-rank, 0 if r.ip_type == "residential" else 1, r.node_name)


def _fallback_recommendations(latest: list[PurityResult], limit: int = 3) -> list[PurityResult]:
    """purity 模块未就绪时的静态回退排序（与 docs/02 Claude 组静态语义一致）。"""
    ranked = [r for r in latest if r.claude_rank is not None]
    ranked.sort(key=_rank_sort_key)
    return ranked[:limit]


def _aggregate_userinfo(subs: list[Subscription]) -> dict[str, int] | None:
    """启用订阅 userinfo 汇总：upload/download 求和；total/expire 取最小非零（INTERFACES §5.6）。"""
    upload = 0
    download = 0
    totals: list[int] = []
    expires: list[int] = []
    for sub in subs:
        if not sub.enabled or not sub.userinfo:
            continue
        ui = sub.userinfo
        upload += int(ui.get("upload") or 0)
        download += int(ui.get("download") or 0)
        if ui.get("total"):
            totals.append(int(ui["total"]))
        if ui.get("expire"):
            expires.append(int(ui["expire"]))
    if not (upload or download or totals or expires):
        return None
    merged: dict[str, int] = {"upload": upload, "download": download}
    if totals:
        merged["total"] = min(totals)
    if expires:
        merged["expire"] = min(expires)
    return merged


def _userinfo_header(ui: dict[str, int]) -> str:
    return "; ".join(f"{k}={ui[k]}" for k in ("upload", "download", "total", "expire") if k in ui)


# --------------------------------------------------------------------- 产物读取（分发回退核心）


def _find_artifact(config: AppConfig, filename: str) -> tuple[str, int] | None:
    """从最新版本向旧遍历，返回第一份非空有效产物内容与版本号（docs/01 安全网）。"""
    for v in sorted(list_versions(config.out_dir), reverse=True):
        path = version_dir(config.out_dir, v) / filename
        try:
            if path.is_file() and path.stat().st_size > 0:
                return path.read_text(encoding="utf-8", errors="replace"), v
        except OSError as exc:
            logger.warning("读取产物失败，尝试更旧版本：%s（%s）", path.name, exc)
            continue
    return None


def _qr_page(url: str) -> str:
    """?qr 扫码页：qrcode 库生成的内嵌 SVG，极简单页（docs/03 §2）。"""
    qr = qrcode.QRCode(
        image_factory=qrcode.image.svg.SvgPathImage,
        box_size=8,
        border=4,
        error_correction=qrcode.constants.ERROR_CORRECT_M,
    )
    qr.add_data(url)
    qr.make(fit=True)
    buf = io.BytesIO()
    qr.make_image().save(buf)
    svg = buf.getvalue().decode("utf-8")
    return (
        '<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        "<title>sub-hub 扫码导入</title></head>"
        '<body style="font-family:system-ui,sans-serif;text-align:center;padding:24px;background:#14171f;color:#e6e9f0">'
        "<h3>扫码导入订阅</h3>"
        f'<div style="display:inline-block;background:#fff;padding:12px;border-radius:8px">{svg}</div>'
        f'<p><input readonly style="width:90%;max-width:560px;padding:6px" value="{html.escape(url)}" onclick="this.select()"></p>'
        '<p style="font-size:12px;color:#8a93a6">用客户端「扫码」功能导入；该链接含 token，请勿外传</p>'
        "</body></html>"
    )


def _bootstrap(config: AppConfig) -> dict[str, Any]:
    """注入首页的 token 与当前状态。"""
    return {
        "token": config.token,
        "baseUrl": config.base_url,
        "subUrls": {
            "clash": f"{config.sub_url_prefix}/clash.yaml",
            "sr": f"{config.sub_url_prefix}/shadowrocket.conf",
            "clashOffline": f"{config.sub_url_prefix}/clash-offline.yaml",
            "srOffline": f"{config.sub_url_prefix}/shadowrocket-offline.conf",
        },
        "currentVersion": latest_version(config.out_dir),
        "generatedAt": now_iso(),
    }


def _read_bootstrap_html(config: AppConfig) -> str:
    try:
        tpl = _INDEX_PATH.read_text(encoding="utf-8")
    except OSError:
        logger.warning("管理界面文件缺失：%s", _INDEX_PATH.name)
        return "<!doctype html><meta charset='utf-8'><p>管理界面文件缺失（web_static/index.html）</p>"
    payload = json.dumps(_bootstrap(config), ensure_ascii=False).replace("</", "<\\/")
    return tpl.replace(_BOOTSTRAP_PLACEHOLDER, payload)


def _mirror_result_to_dict(result: Any) -> dict[str, Any]:
    return {
        "ok": bool(getattr(result, "ok", False)),
        "provider": getattr(result, "provider", None),
        "url": getattr(result, "url", None),
        "error": getattr(result, "error", None),
        "pushed_at": getattr(result, "pushed_at", None) or now_iso(),
    }


def _provider_settings_key(provider: str | None) -> str | None:
    """提供方 → mirror.json 里凭据子字典的键（mirror 模块读取的位置）。"""
    if provider == "cf-kv":
        return "cf_kv"
    if provider == "github":
        return "github"
    return None


def _sanitize_mirror_settings(settings: dict[str, Any] | None) -> dict[str, Any]:
    """镜像设置对外形态：凭据只报「已配置」，绝不回显内容。

    mirror 模块的提供方凭据存放在 settings["cf_kv"] / settings["github"] 子字典
    （含 api_token 等敏感字段），与历史遗留的 settings["credentials"] 一并隐藏。
    """
    source = settings or {}
    s = dict(source)
    legacy_posted = bool(s.pop("credentials", None))
    for key in ("cf_kv", "github"):
        s.pop(key, None)
    sub = source.get(_provider_settings_key(source.get("provider")))  # type: ignore[arg-type]
    configured = legacy_posted or (
        isinstance(sub, dict) and any(str(v or "").strip() for v in sub.values())
    )
    s["credentials_configured"] = configured
    return s


def _rules_state(config: AppConfig) -> dict[str, Any] | None:
    """规则镜像状态（UI「规则源状态」用）；优先 rulesync.load_sync_state。"""
    mod = _optional_module("app.rulesync")
    if mod is not None and hasattr(mod, "load_sync_state"):
        try:
            report = mod.load_sync_state(config)
            if report is None:
                return None
            if is_dataclass(report):
                return asdict(report)
            return dict(vars(report)) if hasattr(report, "__dict__") else None
        except Exception as exc:  # 状态文件损坏不应拖垮 /api/health
            logger.warning("规则镜像状态读取失败：%s", exc)
            return None
    return read_json(config.rules_dir / "state.json", None)


# --------------------------------------------------------------------- lifespan


def _build_lifespan(config: AppConfig, store: Store) -> Callable[[FastAPI], Iterator[None]]:
    @asynccontextmanager
    async def lifespan(app: FastAPI) -> Iterator[None]:
        # 启动同步放后台线程：全量上游拉取（26 条）可耗时数十秒，阻塞会推迟端口
        # 监听，违背「分发可用性 > 数据新鲜度」；基线已内置，期间客户端拉缓存/基线。
        threading.Thread(
            target=_startup_rulesync, args=(config,), name="subhub-startup-rulesync", daemon=True
        ).start()
        sched = _startup_scheduler(config, store)
        app.state.scheduler = sched
        try:
            yield
        finally:
            if sched is not None:
                try:
                    sched.shutdown(wait=False)
                except Exception:
                    logger.warning("调度器关停异常（忽略）", exc_info=True)

    return lifespan


def _startup_rulesync(config: AppConfig) -> None:
    """启动时规则镜像（best-effort：上游失败沿用旧缓存，模块缺失/异常均不阻塞服务）。"""
    try:
        rulesync = importlib.import_module("app.rulesync")
    except ImportError:
        logger.warning("rulesync 模块未就绪，跳过启动时规则镜像")
        return
    try:
        rulesync.sync_rules(config)
    except Exception as exc:
        logger.warning("启动时规则镜像失败（不影响服务）：%s", exc)


def _startup_scheduler(config: AppConfig, store: Store) -> Any | None:
    try:
        scheduler = importlib.import_module("app.scheduler")
    except ImportError:
        logger.warning("scheduler 模块未就绪，跳过定时任务启动")
        return None
    try:
        sched = scheduler.create_scheduler(config, store)
        sched.start()
        return sched
    except Exception as exc:
        logger.warning("调度器启动失败（不影响服务）：%s", exc)
        return None


# --------------------------------------------------------------------- 应用工厂


def create_app(config: AppConfig | None = None, store: Store | None = None) -> FastAPI:
    """docs/INTERFACES §3.10：None 时 load_config() / Store(...)。"""
    config = config or load_config()
    store = store or Store(config.db_path, config.secret_key_path)
    app = FastAPI(title="sub-hub", version="0.1.0", lifespan=_build_lifespan(config, store))
    app.state.config = config
    app.state.store = store
    _install_error_handlers(app)
    _register_routes(app)
    return app


def _install_error_handlers(app: FastAPI) -> None:
    def api_error_handler(request: Request, exc: ApiError) -> JSONResponse:
        return JSONResponse({"code": exc.code, "message": exc.message}, status_code=exc.status_code)

    def http_exception_handler(request: Request, exc: StarletteHTTPException) -> JSONResponse:
        code = _STATUS_CODE_NAMES.get(exc.status_code, f"http_{exc.status_code}")
        return JSONResponse({"code": code, "message": str(exc.detail)}, status_code=exc.status_code)

    def validation_exception_handler(request: Request, exc: RequestValidationError) -> JSONResponse:
        errors = exc.errors()
        first = errors[0] if errors else {}
        loc = ".".join(str(x) for x in first.get("loc", []) if x != "body") or "body"
        msg = first.get("msg", "请求参数不合法")
        return JSONResponse(
            {"code": "bad_request", "message": f"请求参数不合法：{loc}（{msg}）"},
            status_code=400,
        )

    def unhandled_exception_handler(request: Request, exc: Exception) -> JSONResponse:
        logger.exception("未处理异常：%s %s", request.method, request.url.path)
        return JSONResponse({"code": "internal_error", "message": "服务器内部错误"}, status_code=500)

    app.add_exception_handler(ApiError, api_error_handler)
    app.add_exception_handler(StarletteHTTPException, http_exception_handler)
    app.add_exception_handler(RequestValidationError, validation_exception_handler)
    app.add_exception_handler(Exception, unhandled_exception_handler)


# --------------------------------------------------------------------- 路由


def _register_routes(app: FastAPI) -> None:
    # ---------- 基础 ----------

    @app.get("/api/health")
    def health(cfg: AppConfig = Depends(get_cfg)) -> dict[str, Any]:
        return {
            "status": "ok",
            "time": now_iso(),
            "current_version": latest_version(cfg.out_dir),
            "scheduler": read_json(cfg.scheduler_state_path, {}) or {},
            "rules": _rules_state(cfg),
        }

    @app.get("/", include_in_schema=False)
    def index(cfg: AppConfig = Depends(get_cfg)) -> HTMLResponse:
        return HTMLResponse(_read_bootstrap_html(cfg))

    # ---------- 订阅管理 ----------

    @app.get("/api/subs")
    def list_subs(st: Store = Depends(get_st)) -> dict[str, Any]:
        subs = st.list_subscriptions()
        return {"subs": [_sub_public(s) for s in subs], "total": len(subs)}

    @app.post("/api/subs")
    def create_sub(
        payload: SubCreateBody,
        cfg: AppConfig = Depends(get_cfg),
        st: Store = Depends(get_st),
        pipeline: ModuleType = Depends(get_pipeline),
    ) -> dict[str, Any]:
        name = payload.name.strip()
        url = payload.url.strip()
        if not name:
            raise ApiError(400, "bad_request", "订阅别名不能为空")
        if not (url.startswith("http://") or url.startswith("https://")):
            raise ApiError(400, "bad_url", "订阅 URL 必须以 http:// 或 https:// 开头")
        if st.find_subscription_by_name(name) is not None:
            raise ApiError(409, "name_conflict", f"订阅别名已存在：{name}")

        sub_id = st.add_subscription(name, url, payload.enabled)
        try:
            result = pipeline.run_full_pipeline(cfg, st, sub_id=sub_id)
        except Exception as exc:
            st.delete_subscription(sub_id)
            logger.warning("订阅 %s 首次抓取链路异常（%s）", name, mask_url_host(url))
            raise ApiError(502, "pipeline_failed", f"订阅抓取链路异常：{exc}") from exc

        sub = st.get_subscription(sub_id)
        kept = st.list_nodes(sub_name=name, filtered=False)
        if sub is None or sub.last_fetch_status != FetchStatus.OK or not kept:
            st.delete_subscription(sub_id)
            reason = sub.last_fetch_status.label if sub and sub.last_fetch_status else "抓取未执行"
            logger.warning("订阅验证失败，已拒绝添加：%s（%s，%s）", name, reason, mask_url_host(url))
            raise ApiError(
                400,
                "invalid_subscription",
                f"订阅验证失败（{reason}：未拿到有效节点或抓取失效），已拒绝添加",
            )
        return {"subscription": _sub_public(sub), "result": _pipeline_summary(result)}

    @app.patch("/api/subs/{sub_id}")
    def patch_sub(
        sub_id: int,
        payload: SubPatchBody,
        cfg: AppConfig = Depends(get_cfg),
        st: Store = Depends(get_st),
        pipeline: ModuleType = Depends(get_pipeline),
    ) -> dict[str, Any]:
        sub = st.get_subscription(sub_id)
        if sub is None:
            raise ApiError(404, "not_found", "订阅不存在")
        if payload.name is None and payload.url is None and payload.enabled is None:
            raise ApiError(400, "bad_request", "未提供要更新的字段（name/url/enabled）")
        new_name: str | None = None
        if payload.name is not None:
            new_name = payload.name.strip()
            if not new_name:
                raise ApiError(400, "bad_request", "订阅别名不能为空")
            other = st.find_subscription_by_name(new_name)
            if other is not None and other.id != sub_id:
                raise ApiError(409, "name_conflict", f"订阅别名已存在：{new_name}")
        new_url: str | None = None
        if payload.url is not None:
            new_url = payload.url.strip()
            if not (new_url.startswith("http://") or new_url.startswith("https://")):
                raise ApiError(400, "bad_url", "订阅 URL 必须以 http:// 或 https:// 开头")
        st.update_subscription(sub_id, name=new_name, url=new_url, enabled=payload.enabled)
        result = pipeline.run_full_pipeline(cfg, st)  # 变更类操作 → 完整链路
        updated = st.get_subscription(sub_id)
        return {
            "subscription": _sub_public(updated) if updated else _sub_public(sub),
            "result": _pipeline_summary(result),
        }

    @app.delete("/api/subs/{sub_id}")
    def delete_sub(
        sub_id: int,
        cfg: AppConfig = Depends(get_cfg),
        st: Store = Depends(get_st),
        pipeline: ModuleType = Depends(get_pipeline),
    ) -> dict[str, Any]:
        sub = st.get_subscription(sub_id)
        if sub is None:
            raise ApiError(404, "not_found", "订阅不存在")
        st.delete_subscription(sub_id)
        result = pipeline.run_full_pipeline(cfg, st)  # 移除后重算配置
        return {"ok": True, "deleted": sub.name, "result": _pipeline_summary(result)}

    @app.post("/api/refresh")
    def refresh(
        payload: RefreshBody | None = None,
        cfg: AppConfig = Depends(get_cfg),
        st: Store = Depends(get_st),
        pipeline: ModuleType = Depends(get_pipeline),
    ) -> dict[str, Any]:
        sub_id = payload.sub_id if payload is not None else None
        if sub_id is not None and st.get_subscription(sub_id) is None:
            raise ApiError(404, "not_found", "订阅不存在")
        result = pipeline.run_full_pipeline(cfg, st, sub_id=sub_id)
        return {"ok": True, **_pipeline_summary(result)}

    # ---------- 节点与纯净度 ----------

    @app.get("/api/nodes")
    def nodes(
        region: str | None = Query(default=None),
        tag: str | None = Query(default=None),
        sub_id: int | None = Query(default=None),
        cfg: AppConfig = Depends(get_cfg),
        st: Store = Depends(get_st),
    ) -> dict[str, Any]:
        sub_name: str | None = None
        if sub_id is not None:
            sub = st.get_subscription(sub_id)
            if sub is None:
                raise ApiError(404, "not_found", "订阅不存在")
            sub_name = sub.name
        region = (region or "").strip() or None
        filtered: bool | None = None
        if tag is not None:
            tag = tag.strip().lower()
            if tag == "fake":
                filtered = True
            elif tag not in _NODE_TAGS:
                raise ApiError(400, "bad_tag", "tag 仅支持 residential / iplc / fake")
        node_list = st.list_nodes(sub_name=sub_name, region=region, filtered=filtered)
        if tag in ("residential", "iplc"):
            node_list = [n for n in node_list if getattr(n, tag)]
        purity_map = {(r.node_name, r.source_sub): r for r in st.latest_purity_results()}
        items = [
            _node_public(
                n,
                _purity_public(purity_map[(n.name, n.source_sub)]) if (n.name, n.source_sub) in purity_map else None,
            )
            for n in node_list
        ]
        return {"total": len(items), "nodes": items}

    @app.get("/api/parse-anomalies")
    def parse_anomalies(cfg: AppConfig = Depends(get_cfg)) -> dict[str, Any]:
        """解析异常节点列表（docs/01 安全网：单节点解析失败跳过后 UI 可见）。

        数据源 data/parse_anomalies.json（pipeline 按订阅整批替换写入）；
        文件缺失/损坏返回空列表，绝不让展示类端点 5xx。
        """
        doc = read_json(cfg.data_dir / "parse_anomalies.json", None)
        anomalies: list[dict[str, Any]] = []
        updated_at: str | None = None
        if isinstance(doc, dict) and isinstance(doc.get("subs"), dict):
            for sub_name, entry in doc["subs"].items():
                if not isinstance(entry, dict):
                    continue
                checked_at = entry.get("checked_at")
                if checked_at and (updated_at is None or str(checked_at) > updated_at):
                    updated_at = str(checked_at)
                for item in entry.get("anomalies") or []:
                    if isinstance(item, dict):
                        row = dict(item)
                        row.setdefault("source_sub", str(sub_name))
                        anomalies.append(row)
        anomalies.sort(key=lambda r: (str(r.get("source_sub", "")), str(r.get("label") or "")))
        return {"total": len(anomalies), "updated_at": updated_at, "anomalies": anomalies}

    @app.get("/api/purity/report")
    def purity_report(cfg: AppConfig = Depends(get_cfg), st: Store = Depends(get_st)) -> dict[str, Any]:
        latest = st.latest_purity_results()
        all_nodes = st.list_nodes()
        node_map = {(n.name, n.source_sub): n for n in all_nodes}

        purity_mod = _optional_module("app.purity")
        recommendations: list[PurityResult] = []
        if purity_mod is not None and hasattr(purity_mod, "claude_recommendations") and latest:
            try:
                recommendations = list(
                    purity_mod.claude_recommendations(latest, [n for n in all_nodes if not n.filtered])
                )
            except Exception as exc:
                logger.warning("Claude 推荐计算失败，使用静态回退排序：%s", exc)
        if not recommendations:
            recommendations = _fallback_recommendations(latest)

        change_rows: list[dict[str, Any]] = []
        lost_keys: set[tuple[str, str]] = set()
        for c in st.attribute_changes():
            lost = c.is_residential_lost
            if lost:
                lost_keys.add((c.node_name, c.source_sub))
            change_rows.append(
                {
                    "node_name": c.node_name,
                    "source_sub": c.source_sub,
                    "prev": {"ip_type": c.prev.ip_type, "checked_at": c.prev.checked_at},
                    "curr": {"ip_type": c.curr.ip_type, "checked_at": c.curr.checked_at},
                    "is_residential_lost": lost,
                }
            )

        rows = []
        for r in sorted(latest, key=_rank_sort_key):
            node = node_map.get((r.node_name, r.source_sub))
            rows.append(
                {
                    **_purity_public(r),
                    "node": {
                        "name": r.node_name,
                        "source_sub": r.source_sub,
                        "region": node.region if node else None,
                        "residential": node.residential if node else False,
                    },
                    "residential_lost": (r.node_name, r.source_sub) in lost_keys,
                }
            )
        return {
            "checked": len(latest),
            "results": rows,
            "changes": change_rows,
            "recommendations": [_purity_public(r) for r in recommendations],
        }

    @app.post("/api/purity/scan")
    def purity_scan(
        payload: PurityScanBody | None = None,
        cfg: AppConfig = Depends(get_cfg),
        st: Store = Depends(get_st),
        purity: ModuleType = Depends(get_purity),
    ) -> dict[str, Any]:
        full = bool(payload.full) if payload is not None else False
        nodes = st.list_nodes(filtered=False)
        report = purity.scan(nodes, config=cfg, store=st, full=full)
        return {
            "ok": True,
            "checked": getattr(report, "checked", 0),
            "skipped": getattr(report, "skipped", 0),
            "unavailable": bool(getattr(report, "unavailable", False)),
            "full": full,
            "results": [_purity_public(r) for r in getattr(report, "results", None) or []],
        }

    @app.get("/api/health/history")
    def health_history(
        hours: int = 24,
        st: Store = Depends(get_st),
    ) -> dict[str, Any]:
        """节点稳定性窗口聚合（健康时间块条数据源）；health 模块缺失时返回空集。"""
        hours = min(max(hours, 1), 168)
        health_mod = _optional_module("app.health")
        if health_mod is None or not hasattr(health_mod, "history_report"):
            return {"window_hours": hours, "sample_total": 0, "nodes": []}
        return health_mod.history_report(st, window_hours=hours)

    # ---------- 配置产物 ----------

    @app.get("/api/config/preview")
    def config_preview(
        fmt: str = Query(...),
        cfg: AppConfig = Depends(get_cfg),
    ) -> Response:
        fmt = (fmt or "").strip().lower()
        filename = _PREVIEW_FMT.get(fmt)
        if filename is None:
            raise ApiError(400, "bad_fmt", "fmt 仅支持 clash | sr")
        found = _find_artifact(cfg, filename)
        if found is None:
            raise ApiError(404, "no_artifact", "尚无已发布的配置产物，请先添加订阅并刷新")
        return Response(found[0], media_type="text/plain; charset=utf-8")

    @app.get("/api/config/versions")
    def config_versions(cfg: AppConfig = Depends(get_cfg)) -> dict[str, Any]:
        """版本时间线（UI 支撑端点，读取 validator.publish 写下的 meta.json）。"""
        versions: list[dict[str, Any]] = []
        for v in sorted(list_versions(cfg.out_dir), reverse=True):
            meta = read_json(version_dir(cfg.out_dir, v) / "meta.json", None)
            if not isinstance(meta, dict):
                continue
            versions.append(
                {
                    "version": int(meta.get("version", v)),
                    "created_at": meta.get("created_at"),
                    "node_count": meta.get("node_count"),
                    "content_hash": meta.get("content_hash"),
                    "diff_summary": meta.get("diff_summary") or {},
                    "note": meta.get("note"),
                }
            )
        return {"versions": versions, "current": versions[0]["version"] if versions else None}

    @app.post("/api/config/rollback")
    def rollback(
        payload: RollbackBody,
        cfg: AppConfig = Depends(get_cfg),
        st: Store = Depends(get_st),
        pipeline: ModuleType = Depends(get_pipeline),
    ) -> dict[str, Any]:
        target = version_dir(cfg.out_dir, payload.version)
        if payload.version < 1 or not (target / "clash.yaml").is_file():
            raise ApiError(404, "version_not_found", f"版本 v{payload.version:04d} 不存在或产物不完整")
        cv = pipeline.rollback_to_version(cfg, st, payload.version)
        return {
            "ok": True,
            "current_version": getattr(cv, "version", None),
            "node_count": getattr(cv, "node_count", None),
            "note": getattr(cv, "note", None),
        }

    # ---------- 分发端点（token 路径） ----------

    @app.get("/sub/{token}/{artifact}")
    def distribute(
        token: str,
        artifact: str,
        request: Request,
        qr: str | None = Query(default=None),
        cfg: AppConfig = Depends(get_cfg),
        st: Store = Depends(get_st),
    ) -> Response:
        if artifact not in _ARTIFACTS or not secrets.compare_digest(token, cfg.token):
            raise ApiError(404, "not_found", "链接不存在或 token 无效")
        target_url = f"{cfg.sub_url_prefix}/{artifact}"
        if qr is not None:
            return HTMLResponse(_qr_page(target_url))

        headers: dict[str, str] = {}
        userinfo = _aggregate_userinfo(st.list_subscriptions())
        if userinfo:
            headers["subscription-userinfo"] = _userinfo_header(userinfo)

        found = _find_artifact(cfg, artifact)
        if found is None:
            logger.warning("分发无任何有效产物，返回占位配置：%s", artifact)
            return Response(_PLACEHOLDER_TEXT, media_type=_ARTIFACTS[artifact]["media_type"], headers=headers)
        content, version = found
        etag = f'"{content_hash(content)}"'
        headers["ETag"] = etag
        headers["X-Subhub-Version"] = f"v{version:04d}"
        if etag in request.headers.get("if-none-match", ""):
            return Response(status_code=304, headers=headers)
        return Response(content, media_type=_ARTIFACTS[artifact]["media_type"], headers=headers)

    @app.get("/rules/{file}")
    def rules(file: str, request: Request, cfg: AppConfig = Depends(get_cfg)) -> Response:
        if not _RULE_FILE_RE.match(file) or ".." in file:
            raise ApiError(404, "not_found", "规则文件不存在")
        path = cfg.rules_dir / file
        try:
            if not path.is_file():
                raise ApiError(404, "not_found", "规则文件不存在")
            data = path.read_bytes()
        except OSError as exc:
            raise ApiError(404, "not_found", "规则文件不存在") from exc
        etag = f'"{content_hash(data)}"'
        headers = {"ETag": etag}
        if etag in request.headers.get("if-none-match", ""):
            return Response(status_code=304, headers=headers)
        media = "text/yaml; charset=utf-8" if file.endswith(".yaml") else "text/plain; charset=utf-8"
        return Response(data, media_type=media, headers=headers)

    # ---------- 镜像推送 ----------

    @app.get("/api/mirror")
    def mirror_get(cfg: AppConfig = Depends(get_cfg), mirror: ModuleType = Depends(get_mirror)) -> dict[str, Any]:
        return {"settings": _sanitize_mirror_settings(mirror.load_mirror_settings(cfg))}

    @app.post("/api/mirror")
    def mirror_post(
        payload: MirrorBody,
        cfg: AppConfig = Depends(get_cfg),
        mirror: ModuleType = Depends(get_mirror),
    ) -> dict[str, Any]:
        provider = (payload.provider or "").strip()
        if provider not in _MIRROR_PROVIDERS:
            raise ApiError(400, "bad_provider", "provider 仅支持 cf-kv | github")
        settings = dict(mirror.load_mirror_settings(cfg) or {})
        settings.update(
            {
                "provider": provider,
                "enabled": payload.enabled,
                "updated_at": now_iso(),
            }
        )
        if payload.credentials is not None:
            # 凭据并入 mirror 模块实际读取的提供方子字典（cf_kv / github），
            # 「保存并立即试推」才能真正生效（docs/03 §2）；dict 深入合并保留
            # 已保存字段，非 dict 载荷存历史遗留键（仅作标记，回显一律隐藏）。
            sub_key = _provider_settings_key(provider)
            if isinstance(payload.credentials, dict) and sub_key is not None:
                sub = dict(settings.get(sub_key) or {})
                sub.update(payload.credentials)
                settings[sub_key] = sub
            else:
                settings["credentials"] = payload.credentials
        mirror.save_mirror_settings(cfg, settings)
        push = _mirror_push(mirror, cfg)
        fresh = mirror.load_mirror_settings(cfg)
        return {"settings": _sanitize_mirror_settings(fresh), "push": push}

    @app.post("/api/mirror/push")
    def mirror_push(cfg: AppConfig = Depends(get_cfg), mirror: ModuleType = Depends(get_mirror)) -> dict[str, Any]:
        return _mirror_push(mirror, cfg)


def _mirror_push(mirror: ModuleType, config: AppConfig) -> dict[str, Any]:
    """手动/试推；mirror 模块自身吞错，这里再兜一层防 5xx。"""
    try:
        return _mirror_result_to_dict(mirror.push_current(config))
    except Exception as exc:
        logger.warning("镜像推送调用异常（不影响本地分发）：%s", exc)
        return {"ok": False, "provider": None, "url": None, "error": f"推送异常：{exc}", "pushed_at": now_iso()}
