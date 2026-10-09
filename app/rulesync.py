"""rulesync — 规则集缓存镜像（docs/01 流程 2、docs/02 §3、INTERFACES §3.6）。

职责：按 rules_manifest.yaml 逐条拉取上游规则集的 Clash .yaml 与 SR .list 双格式，
缓存到 data/rules/<clash_file>|<sr_file>（文件名与 manifest 一致，供 GET /rules/{file}
直接服务）。设计原则是「永不失效」：

- 上游失败 → 沿用旧缓存（绝不删除、绝不写坏文件，全部原子写）；
- 无旧缓存 → 回退容器内置基线 app/baseline_rules/（首次部署离线可用）；
- 基线也缺 → 写空规则占位文件（合法的空 payload），避免 /rules 端点 404；
  发布侧（pipeline）经 placeholder_rule_files 探测到占位即拒绝发布（docs/01
  安全网：空 ChinaMax/CNCIDR 会把国内流量全部改道代理）；
- source=builtin（自维护 claude-extra 等）不依赖网络，直接从基线目录复制，
  基线缺失时可按 manifest 的 domains 列表现场生成。

每次同步把每条规则的鲜度状态写入 data/rules/state.json，web UI 据此显示
「规则源 N 小时未更新」。本模块永不向上抛异常，返回的 RulesSyncReport 携带全部结果。

日志纪律：本模块只处理公开的规则仓库 URL，不涉及订阅 URL 与节点凭据；日志文本一律中文。
"""
from __future__ import annotations

import logging
import re
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

import httpx
import yaml

from app.utils import atomic_write_bytes, atomic_write_text, now_iso, read_json, write_json

if TYPE_CHECKING:  # 仅类型检查用；templater 由其他模块实现，运行时不依赖
    from app.templater import RuleEntry

logger = logging.getLogger("subhub.rulesync")

# ---------------------------------------------------------------- 常量

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_MANIFEST_PATH = REPO_ROOT / "rules_manifest.yaml"      # 仓库根规则清单
BASELINE_DIR = Path(__file__).resolve().parent / "baseline_rules"  # 内置基线（随容器打包）
DEFAULT_FALLBACK_PROXY = "http://127.0.0.1:7897"               # 构建内置基线时的本机代理兜底
STATE_FILENAME = "state.json"                                  # data/rules/state.json
FETCH_WORKERS = 8                                              # 上游并发下载线程数
_USER_AGENT = "sub-hub rulesync"

_PLACEHOLDER_MARK = "占位文件"


# ---------------------------------------------------------------- 数据结构

@dataclass
class RuleStatus:
    """单条规则的同步结果（UI 据此显示「N 小时未更新」）。

    source 语义：upstream=本次从上游刷新；builtin=自维护内置复制；
    cache=沿用旧缓存；baseline=回退内置基线；none=无任何可用来源。
    files：文件名 → fresh / cache / baseline / missing。
    """

    name: str
    ok: bool = False
    source: str = "none"
    updated_at: str | None = None
    error: str | None = None
    files: dict[str, str] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "ok": self.ok,
            "source": self.source,
            "updated_at": self.updated_at,
            "error": self.error,
            "files": dict(self.files),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "RuleStatus":
        return cls(
            name=str(data.get("name", "")),
            ok=bool(data.get("ok", False)),
            source=str(data.get("source", "none")),
            updated_at=data.get("updated_at"),
            error=data.get("error"),
            files=dict(data.get("files") or {}),
        )


@dataclass
class RulesSyncReport:
    """一次规则镜像的结果（契约见 INTERFACES §3.6；details 为扩展字段，
    承载需求要求的每条 {updated_at, ok, source} 鲜度状态）。"""

    updated: list[str]                  # 本次成功刷新的规则名
    stale: list[str]                    # 沿用旧缓存的规则名（上游失败——永不失效）
    failed_never: list[str]             # 无缓存且上游失败的规则名（仅内置基线兜底）
    last_success_at: str | None
    checked_at: str
    details: dict[str, RuleStatus] = field(default_factory=dict)


# ---------------------------------------------------------------- manifest 读取与 URL 拼装

def read_manifest(path: Path | None = None) -> dict[str, Any]:
    """读取并解析规则清单（缺省仓库根 rules_manifest.yaml）。列表顺序即规则链顺序，不重排。"""
    manifest_path = Path(path) if path else DEFAULT_MANIFEST_PATH
    data = yaml.safe_load(manifest_path.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or not isinstance(data.get("rules"), list) or not data["rules"]:
        raise ValueError(f"规则清单格式非法（缺少 rules 列表）：{manifest_path.name}")
    return data


def _entry_get(entry: Any, key: str, default: Any = None) -> Any:
    """兼容 dict 与 templater.RuleEntry 两种形态取字段。"""
    if isinstance(entry, dict):
        return entry.get(key, default)
    return getattr(entry, key, default)


def resolve_entries(
    manifest: "list[Any] | None" = None, *, manifest_path: Path | None = None
) -> list[dict[str, Any]]:
    """把 manifest 参数归一为 raw dict 列表（rulesync 内部统一消费 dict 形态）。

    - None → 读仓库根 rules_manifest.yaml；
    - dict → 原样使用；
    - templater.RuleEntry 等对象 → 转 dict，其未携带的 source/upstream_file/domains
      等字段按 name 从仓库根清单回填（清单缺失时给默认值）。
    """
    if manifest is None:
        return [dict(e) for e in read_manifest(manifest_path)["rules"]]
    raw_by_name: dict[str, dict[str, Any]] = {}
    try:
        for e in read_manifest(manifest_path)["rules"]:
            if isinstance(e, dict) and e.get("name"):
                raw_by_name[str(e["name"])] = dict(e)
    except (OSError, ValueError, yaml.YAMLError) as exc:
        logger.warning("回填规则清单不可用，仅使用传入条目自身字段：%s", exc)
    resolved: list[dict[str, Any]] = []
    for entry in manifest:
        if isinstance(entry, dict):
            merged = dict(entry)
        else:
            merged = {
                k: getattr(entry, k)
                for k in ("name", "category", "policy", "behavior", "clash_file", "sr_file")
                if hasattr(entry, k)
            }
        extra = raw_by_name.get(str(merged.get("name")), {})
        for k, v in extra.items():
            merged.setdefault(k, v)
        merged.setdefault("source", "blackmatrix7")
        merged.setdefault("upstream_file", merged.get("name"))
        resolved.append(merged)
    return resolved


def upstream_urls(
    entry: Any, sources: dict[str, str] | None = None
) -> tuple[str | None, str | None]:
    """按 manifest sources 模板拼装该规则的 (clash_url, sr_url)。

    - builtin → (None, None)（不走网络）；
    - blackmatrix7 → 模板 {dir}=upstream_file（上游规则目录），{file}=文件基名
      （upstream_clash / upstream_sr 覆盖，缺省同 upstream_file）；
    - loyalsoldier → 单个 txt（payload 格式），rulesync 负责转换出双格式，故 sr 为 None。
    """
    src = sources if sources is not None else read_manifest().get("sources", {})
    source = str(_entry_get(entry, "source", "blackmatrix7"))
    if source == "builtin":
        return None, None
    if source == "blackmatrix7":
        dir_name = _entry_get(entry, "upstream_file")
        clash_tpl, sr_tpl = src.get("blackmatrix7_clash"), src.get("blackmatrix7_sr")
        if not dir_name or not clash_tpl or not sr_tpl:
            return None, None
        clash_name = _entry_get(entry, "upstream_clash") or dir_name
        sr_name = _entry_get(entry, "upstream_sr") or dir_name
        clash = clash_tpl.format(dir=dir_name, file=clash_name)
        sr = sr_tpl.format(dir=dir_name, file=sr_name)
        return clash, sr
    if source == "loyalsoldier":
        tpl, f = src.get("loyalsoldier"), _entry_get(entry, "upstream_file")
        return (tpl.format(file=f) if (tpl and f) else None), None
    logger.warning("未知规则来源 %r（规则 %s），视为无上游", source, _entry_get(entry, "name"))
    return None, None


# ---------------------------------------------------------------- 下载与内容校验

def _http_get(url: str, *, timeout: float, proxy: str | None = None) -> bytes:
    """GET 上游规则文件。直连=不经任何代理（忽略环境代理变量）；proxy 给定时经其转发。"""
    kwargs: dict[str, Any] = {"timeout": timeout, "follow_redirects": True, "trust_env": False}
    if proxy:
        kwargs["proxy"] = proxy
    with httpx.Client(**kwargs) as client:
        resp = client.get(url, headers={"User-Agent": _USER_AGENT})
        resp.raise_for_status()
        return resp.content


def fetch_bytes(url: str, *, timeout: float = 30.0, proxy: str | None = None) -> bytes | None:
    """下载上游文件：成功返回内容；任何网络/HTTP 错误记日志并返回 None（不抛出）。"""
    try:
        return _http_get(url, timeout=timeout, proxy=proxy)
    except Exception as exc:  # noqa: BLE001 —— 上游任何失败都不得中断整体镜像
        logger.warning("规则下载失败：%s（%s: %s）", url, type(exc).__name__, exc)
        return None


def _fetch_with_fallback(url: str, *, timeout: float, proxy: str | None) -> bytes | None:
    """构建基线用：先直连，失败走指定代理，仍失败返回 None。"""
    data = fetch_bytes(url, timeout=timeout)
    if data is None and proxy:
        logger.info("直连失败，改经本机代理重试：%s", url)
        data = fetch_bytes(url, timeout=timeout, proxy=proxy)
    return data


def is_clash_payload(data: bytes) -> bool:
    """校验内容是合法的 rule-provider YAML：{'payload': [非空列表]}。"""
    try:
        doc = yaml.safe_load(data.decode("utf-8", errors="replace"))
    except yaml.YAMLError:
        return False
    return isinstance(doc, dict) and isinstance(doc.get("payload"), list) and len(doc["payload"]) > 0


def is_rule_list(data: bytes) -> bool:
    """校验 SR .list：至少一条非注释行（TYPE,value 或裸域名）。"""
    for raw in data.decode("utf-8", errors="replace").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        return "," in line or ("." in line and " " not in line)
    return False


def payload_yaml_to_list(data: bytes) -> str:
    """Loyalsoldier txt（payload YAML）→ SR .list 行。

    行含 `/` 视为 CIDR：含冒号输出 IP-CIDR6、否则 IP-CIDR；其余为裸域行
    （`+.域` / 前导点 / 裸域，如 gfw.txt 全量 `+.域`），输出 DOMAIN-SUFFIX
    （去列表语法前缀）——旧实现对域名行也无条件转 IP-CIDR，gfw 类纯域名
    上游会被整体转成死规则。
    """
    doc = yaml.safe_load(data.decode("utf-8", errors="replace")) or {}
    lines = ["# 本文件由 sub-hub rulesync 自上游 txt（payload 格式）转换生成"]
    for item in doc.get("payload") or []:
        raw = str(item).strip().strip("'\"")
        if not raw:
            continue
        if "/" in raw:
            kind = "IP-CIDR6" if ":" in raw else "IP-CIDR"
            lines.append(f"{kind},{raw}")
            continue
        if raw.startswith("+."):
            raw = raw[2:]
        elif raw.startswith("."):
            raw = raw[1:]
        lines.append(f"DOMAIN-SUFFIX,{raw}")
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------- 内置基线与占位

def _render_builtin_rule(domains: list[str], ipcidrs: list[str] | None = None) -> tuple[str, str]:
    """按 manifest 的 domains/ips 生成 (yaml 文本, list 文本)——DOMAIN-SUFFIX / IP-CIDR 形态。

    IP 行不带 no-resolve：SR 端 templater._payload_line_to_sr_rule 对含逗号行原样
    追加策略，落成 `IP-CIDR,段,DIRECT`（no-resolve 夹在值与策略之间是非法语法）；
    IPv6 段输出 IP-CIDR6（IP-CIDR 不匹配 v6 目标，SR 语法要求 IP-CIDR6）。
    """
    header = [
        "# 本文件由 sub-hub rulesync 依据 rules_manifest.yaml 的 domains/ips 生成（内置自维护规则）",
        "# 请勿手工编辑；增删请改 rules_manifest.yaml 后重跑 build_baselines()",
    ]
    payload_lines = [f"DOMAIN-SUFFIX,{d}" for d in domains]
    for i in (ipcidrs or []):
        kind = "IP-CIDR6" if ":" in i else "IP-CIDR"
        payload_lines.append(f"{kind},{i}")
    yaml_text = "\n".join(header + ["payload:"] + [f"  - {ln}" for ln in payload_lines]) + "\n"
    list_text = "\n".join(header + payload_lines) + "\n"
    return yaml_text, list_text


def _placeholder_text(fname: str) -> str:
    """空规则占位：内容合法（mihomo 空 payload / 空列表），避免 /rules 端点 404。"""
    why = f"# {_PLACEHOLDER_MARK}：上游拉取失败且无旧缓存/基线，先以空规则占位，避免分发端点 404"
    hint = "# 网络恢复后重跑 app.rulesync.build_baselines()，或等待定时镜像自动补齐"
    if fname.endswith(".yaml"):
        return f"{why}\n{hint}\npayload: []\n"
    return f"{why}\n{hint}\n"


def _is_placeholder(path: Path) -> bool:
    try:
        return _PLACEHOLDER_MARK in path.read_text(encoding="utf-8", errors="replace")[:200]
    except OSError:
        return False


def placeholder_rule_files(config: Any) -> list[str]:
    """探测 data/rules/ 中的空规则占位文件，返回文件名列表（发布侧安全网数据源）。

    占位文件 = 上游、旧缓存、内置基线三者全缺的痕迹（本模块 _sync_rules_inner
    写入）。docs/01 安全网：该状态下对应规则集为空规则，pipeline 据此拒绝发布，
    避免「国内直连规则为空 → 流量全部改走代理」的坏配置下发。
    """
    rules_dir = Path(config.rules_dir)
    if not rules_dir.is_dir():
        return []
    return sorted(
        path.name for path in rules_dir.iterdir()
        if path.is_file() and path.suffix in (".yaml", ".list") and _is_placeholder(path)
    )


def _builtin_content(fname: str, domains: list[str], ipcidrs: list[str] | None = None) -> bytes | None:
    """builtin 规则的文件内容：内置基线优先，缺失时按 manifest domains/ips 现场生成。"""
    src = BASELINE_DIR / fname
    if src.exists():
        return src.read_bytes()
    if domains or ipcidrs:
        yaml_text, list_text = _render_builtin_rule(domains, ipcidrs)
        return (yaml_text if fname.endswith(".yaml") else list_text).encode("utf-8")
    return None


def _mtime_iso(path: Path) -> str | None:
    try:
        return datetime.fromtimestamp(path.stat().st_mtime).isoformat(timespec="microseconds")
    except OSError:
        return None


# ---------------------------------------------------------------- 同步主流程

def sync_rules(
    config: Any,
    *,
    manifest: "list[Any] | None" = None,
    timeout: float = 30.0,
) -> RulesSyncReport:
    """规则镜像主入口（INTERFACES §3.6）：逐条上游拉取 → data/rules/ 双格式原子落盘。

    单条失败：保留旧文件、记入 stale（无缓存则回退内置基线并记入 failed_never）；
    状态写 data/rules/state.json；本函数任何情况下不抛异常。
    """
    checked_at = now_iso()
    try:
        return _sync_rules_inner(config, manifest, timeout, checked_at)
    except Exception as exc:  # noqa: BLE001 —— 永不向上抛（docs/01 安全网）
        logger.error("规则镜像异常终止（不影响已缓存规则）：%s: %s", type(exc).__name__, exc)
        return RulesSyncReport(
            updated=[], stale=[], failed_never=[], last_success_at=None, checked_at=checked_at
        )


def _sync_rules_inner(
    config: Any, manifest: "list[Any] | None", timeout: float, checked_at: str
) -> RulesSyncReport:
    rules_dir = Path(config.rules_dir)
    rules_dir.mkdir(parents=True, exist_ok=True)
    prev = load_sync_state(config)
    prev_details = prev.details if prev else {}

    if manifest is None:
        doc = read_manifest()
        entries = [dict(e) for e in doc["rules"]]
        sources = dict(doc.get("sources") or {})
    else:
        entries = resolve_entries(manifest)
        try:
            sources = dict(read_manifest().get("sources") or {})
        except (OSError, ValueError, yaml.YAMLError):
            sources = {}

    # ---- 阶段一：并发下载全部上游文件（builtin 无 URL，自动跳过）----
    plan: list[tuple[str, str, str]] = []          # (规则名, 格式, url)
    for e in entries:
        name = str(_entry_get(e, "name", ""))
        clash_url, sr_url = upstream_urls(e, sources)
        if clash_url:
            plan.append((name, "clash", clash_url))
        if sr_url:
            plan.append((name, "sr", sr_url))
    fetched: dict[tuple[str, str], bytes | None] = {}
    if plan:
        unique_urls = sorted({u for _, _, u in plan})
        # 直连失败时按 SUBHUB_RULES_PROXY 回落重试一次（仅规则上游；机场订阅永不走代理）
        rules_proxy = (getattr(config, "rules_proxy", "") or "").strip() or None
        with ThreadPoolExecutor(max_workers=FETCH_WORKERS) as pool:
            contents = list(
                pool.map(
                    lambda u: _fetch_with_fallback(u, timeout=timeout, proxy=rules_proxy),
                    unique_urls,
                )
            )
        url_bytes = dict(zip(unique_urls, contents))
        for rname, kind, url in plan:
            fetched[(rname, kind)] = url_bytes.get(url)

    # ---- 阶段二：逐条落盘与状态判定 ----
    updated: list[str] = []
    stale: list[str] = []
    failed_never: list[str] = []
    details: dict[str, RuleStatus] = {}

    for e in entries:
        name = str(_entry_get(e, "name", ""))
        source = str(_entry_get(e, "source", "blackmatrix7"))
        domains = [str(d).strip() for d in (_entry_get(e, "domains") or []) if str(d).strip()]
        clash_file = str(_entry_get(e, "clash_file", f"{name}.yaml"))
        sr_file = str(_entry_get(e, "sr_file", f"{name}.list"))
        status = RuleStatus(name=name)
        details[name] = status

        # ---- builtin：不依赖网络，直接取内置基线（或按 domains/ips 生成）----
        if source == "builtin":
            ipcidrs = [str(i).strip() for i in (_entry_get(e, "ips") or []) if str(i).strip()]
            all_ok = True
            for fname in (clash_file, sr_file):
                content = _builtin_content(fname, domains, ipcidrs)
                target = rules_dir / fname
                if content is not None:
                    atomic_write_bytes(target, content)
                    status.files[fname] = "fresh"
                else:
                    atomic_write_text(target, _placeholder_text(fname))
                    status.files[fname] = "missing"
                    all_ok = False
            if all_ok:
                status.ok, status.source, status.updated_at = True, "builtin", checked_at
                updated.append(name)
            else:
                status.error = "内置基线缺失且无 domains 可生成，已写空占位"
                failed_never.append(name)
            continue

        # ---- 上游来源：校验各格式内容有效性 ----
        clash_data = fetched.get((name, "clash"))
        if clash_data is not None and not is_clash_payload(clash_data):
            logger.warning("规则 %s 的 Clash 格式内容非法（忽略本次下载）", name)
            clash_data = None
        if source == "loyalsoldier":
            # 单 txt 来源：.yaml 存原文，.list 由 payload 转换
            sr_data = payload_yaml_to_list(clash_data).encode("utf-8") if clash_data else None
        else:
            sr_data = fetched.get((name, "sr"))
            if sr_data is not None and not is_rule_list(sr_data):
                logger.warning("规则 %s 的 SR 格式内容非法（忽略本次下载）", name)
                sr_data = None

        errors: list[str] = []
        for kind, fname, data in (("Clash", clash_file, clash_data), ("SR", sr_file, sr_data)):
            target = rules_dir / fname
            if data is not None:
                atomic_write_bytes(target, data)
                status.files[fname] = "fresh"
            elif target.exists():
                status.files[fname] = "cache"
                errors.append(f"{fname} 沿用旧缓存")
            else:
                baseline = BASELINE_DIR / fname
                if baseline.exists():
                    atomic_write_bytes(target, baseline.read_bytes())
                    status.files[fname] = "baseline"
                    errors.append(f"{fname} 回退内置基线")
                else:
                    atomic_write_text(target, _placeholder_text(fname))
                    status.files[fname] = "missing"
                    errors.append(f"{fname} 无任何可用来源，已写空占位")

        fresh_files = [f for f, v in status.files.items() if v == "fresh"]
        cache_files = [f for f, v in status.files.items() if v == "cache"]
        baseline_files = [f for f, v in status.files.items() if v == "baseline"]

        if len(fresh_files) == len(status.files):
            status.ok, status.source, status.updated_at = True, "upstream", checked_at
            updated.append(name)
        elif fresh_files:
            # 部分刷新：新内容已写入，缺的格式沿用旧缓存/基线（记 stale，不丢格式）
            status.ok = False
            status.source = "cache" if cache_files else "baseline"
            status.updated_at = checked_at
            status.error = "上游部分格式失败：" + "；".join(errors)
            stale.append(name)
        else:
            # 全部未刷新：有旧缓存 → stale（保持上次鲜度）；无缓存 → failed_never（基线兜底）
            status.ok = False
            if cache_files:
                status.source = "cache"
                status.updated_at = (prev_details.get(name).updated_at
                                     if isinstance(prev_details.get(name), RuleStatus) else None) \
                    or _mtime_iso(rules_dir / cache_files[0])
                status.error = "上游拉取失败：" + "；".join(errors)
                stale.append(name)
            else:
                status.source = "baseline" if baseline_files else "none"
                if baseline_files:
                    status.updated_at = _mtime_iso(rules_dir / baseline_files[0])
                status.error = "上游拉取失败且无旧缓存：" + "；".join(errors)
                failed_never.append(name)

    last_success_at = checked_at if updated else (prev.last_success_at if prev else None)
    report = RulesSyncReport(
        updated=updated,
        stale=stale,
        failed_never=failed_never,
        last_success_at=last_success_at,
        checked_at=checked_at,
        details=details,
    )
    write_json(rules_dir / STATE_FILENAME, {
        "checked_at": report.checked_at,
        "last_success_at": report.last_success_at,
        "updated": report.updated,
        "stale": report.stale,
        "failed_never": report.failed_never,
        "details": {n: s.to_dict() for n, s in report.details.items()},
    })
    logger.info(
        "规则镜像完成：刷新 %d 条、沿用缓存 %d 条、基线兜底 %d 条（共 %d 条）",
        len(updated), len(stale), len(failed_never), len(entries),
    )
    return report


def sync(
    config: Any,
    *,
    manifest: "list[Any] | None" = None,
    timeout: float = 30.0,
) -> RulesSyncReport:
    """公开入口别名（与 sync_rules 等价）：返回含每条 {updated_at, ok, source} 的同步报告。"""
    return sync_rules(config, manifest=manifest, timeout=timeout)


def load_sync_state(config: Any) -> RulesSyncReport | None:
    """读取 data/rules/state.json（UI「规则源 N 小时未更新」数据源）；缺失/损坏返回 None。"""
    data = read_json(Path(config.rules_dir) / STATE_FILENAME)
    if not isinstance(data, dict) or "checked_at" not in data:
        return None
    details = {
        str(n): RuleStatus.from_dict(v)
        for n, v in (data.get("details") or {}).items()
        if isinstance(v, dict)
    }
    return RulesSyncReport(
        updated=[str(x) for x in (data.get("updated") or [])],
        stale=[str(x) for x in (data.get("stale") or [])],
        failed_never=[str(x) for x in (data.get("failed_never") or [])],
        last_success_at=data.get("last_success_at"),
        checked_at=str(data["checked_at"]),
        details=details,
    )


def hours_since(ts: str | None, *, now: datetime | None = None) -> float | None:
    """距 ts 的小时数（UI 显示「N 小时未更新」）；ts 缺失或非法返回 None。"""
    if not ts:
        return None
    try:
        t = datetime.fromisoformat(ts)
    except (TypeError, ValueError):
        return None
    ref = now or datetime.now()
    return max(0.0, (ref - t).total_seconds() / 3600.0)


# ---------------------------------------------------------------- 构建期：内置基线与上游校准

def check_upstream_urls(
    *, manifest_path: Path | None = None, proxy: str | None = None, timeout: float = 20.0
) -> list[dict[str, Any]]:
    """逐条实测上游 URL 可达性（路径校准用，非同步主流程）。

    返回 [{name, source, clash_url, clash_ok, sr_url, sr_ok}]；直连失败且给定了
    proxy 时再经代理试一次。builtin 条目不含 URL 字段。
    """
    doc = read_manifest(manifest_path)
    sources = dict(doc.get("sources") or {})
    results: list[dict[str, Any]] = []
    for e in doc["rules"]:
        clash_url, sr_url = upstream_urls(e, sources)
        row: dict[str, Any] = {
            "name": str(_entry_get(e, "name", "")),
            "source": str(_entry_get(e, "source", "")),
        }
        if clash_url is None and sr_url is None:
            results.append(row)
            continue
        for key, url in (("clash", clash_url), ("sr", sr_url)):
            row[f"{key}_url"] = url
            if url is None:
                row[f"{key}_ok"] = False
                continue
            data = _fetch_with_fallback(url, timeout=timeout, proxy=proxy)
            row[f"{key}_ok"] = data is not None
        results.append(row)
    return results


def build_baselines(
    *,
    target_dir: Path | None = None,
    manifest_path: Path | None = None,
    proxy: str | None = DEFAULT_FALLBACK_PROXY,
    timeout: float = 30.0,
) -> dict[str, dict[str, str]]:
    """（构建镜像/开发期执行）把每条规则的当前版本下载进内置基线目录。

    网络顺序：直连 → 本机代理（默认 http://127.0.0.1:7897）→ 仍失败时：
      已有基线文件 → 保留旧内容（旧占位保持占位）；无基线文件 → 写空规则占位。

    builtin 条目不走网络：已有文件（如手写的 claude-extra）保持不动，缺失时按
    manifest domains 生成。返回 {规则名: {文件名: fresh|kept|generated|placeholder}}，
    其中 placeholder 即结果里需要说明的缺失项。
    """
    doc = read_manifest(manifest_path)
    sources = dict(doc.get("sources") or {})
    target = Path(target_dir) if target_dir else BASELINE_DIR
    target.mkdir(parents=True, exist_ok=True)
    summary: dict[str, dict[str, str]] = {}

    for e in doc["rules"]:
        name = str(_entry_get(e, "name", ""))
        source = str(_entry_get(e, "source", "blackmatrix7"))
        clash_file = str(_entry_get(e, "clash_file", f"{name}.yaml"))
        sr_file = str(_entry_get(e, "sr_file", f"{name}.list"))
        files: dict[str, str] = {}
        summary[name] = files

        if source == "builtin":
            domains = [str(d).strip() for d in (_entry_get(e, "domains") or []) if str(d).strip()]
            ipcidrs = [str(i).strip() for i in (_entry_get(e, "ips") or []) if str(i).strip()]
            for fname in (clash_file, sr_file):
                p = target / fname
                if p.exists():
                    files[fname] = "kept"
                elif domains or ipcidrs:
                    yaml_text, list_text = _render_builtin_rule(domains, ipcidrs)
                    atomic_write_text(p, yaml_text if fname.endswith(".yaml") else list_text)
                    files[fname] = "generated"
                else:
                    atomic_write_text(p, _placeholder_text(fname))
                    files[fname] = "placeholder"
            continue

        # 上游来源：loyalsoldier 单 txt → 一次下载转换双格式；blackmatrix7 双 URL
        clash_url, sr_url = upstream_urls(e, sources)
        want: list[tuple[str, str | None]] = []
        if source == "loyalsoldier":
            want = [(clash_file, clash_url), (sr_file, None)]
        else:
            want = [(clash_file, clash_url), (sr_file, sr_url)]

        raw_txt: bytes | None = None
        for fname, url in want:
            p = target / fname
            if source == "loyalsoldier":
                if url is not None:
                    raw_txt = _fetch_with_fallback(url, timeout=timeout, proxy=proxy)
                if raw_txt is not None and is_clash_payload(raw_txt):
                    atomic_write_bytes(p, raw_txt if fname.endswith(".yaml")
                                       else payload_yaml_to_list(raw_txt).encode("utf-8"))
                    files[fname] = "fresh"
                elif p.exists():
                    files[fname] = "placeholder" if _is_placeholder(p) else "kept"
                else:
                    atomic_write_text(p, _placeholder_text(fname))
                    files[fname] = "placeholder"
                continue
            data = _fetch_with_fallback(url, timeout=timeout, proxy=proxy) if url else None
            valid = data is not None and (
                is_clash_payload(data) if fname.endswith(".yaml") else is_rule_list(data))
            if valid:
                atomic_write_bytes(p, data)
                files[fname] = "fresh"
            elif p.exists():
                files[fname] = "placeholder" if _is_placeholder(p) else "kept"
            else:
                atomic_write_text(p, _placeholder_text(fname))
                files[fname] = "placeholder"
        logger.info("内置基线处理完成：%s（%s）", name, files)
    return summary
