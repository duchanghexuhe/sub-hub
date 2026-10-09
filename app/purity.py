"""纯净度检测引擎（docs/03 §4）：逐节点出口 IP 属性检测 + Claude 适配度评分。

单节点检测流程：
  1. 探测实例可用性确认：实例存活（ProbeInstance.is_available）+ 控制 API delay 探测节点可达；
  2. PUT /proxies/PROBE 把全局出口切到该节点；
  3. 经混合端口请求 ip-api（PurityProvider 接口，默认 IpApiProvider，内置 45 req/min 限速）；
  4. 按出口 IP 经同一出口补查 ipinfo widget（PurityEnhancer 接口，默认 IpInfoWidgetClient：
     免 key 增强源，asn.type 权威住宅/机房判定 + privacy 风险标记降档；失败仅忽略）；
  5. 按评分表分类（住宅宽带 3 / 中小机房 2 / 大厂云 ASN 集 1 / 已标记代理 0）→ store 落库；
  6. 与上次结果比对，「住宅→机房」打 warning 告警日志（UI 侧由 store.attribute_changes 标红）。

支持 full 全量与增量（仅新增/未测节点）两种模式；探测实例启动失败/中途崩溃 →
报告 unavailable=True，绝不把探测异常抛出 scan，主分发链路无感。

安全纪律：日志不打印节点凭据字段与完整订阅 URL。
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Any, Protocol

import httpx

from app.config import AppConfig
from app.models import Node, PurityResult
from app.probe import ProbeInstance, probe_instance_lock
from app.store import Store
from app.utils import now_iso

logger = logging.getLogger("subhub.purity")

_IPAPI_URL = "http://ip-api.com/json/?fields=status,country,as,asname,org,isp,proxy,hosting,mobile"

_IPINFO_WIDGET_URL = "https://ipinfo.io/widget/demo/{ip}"
"""ipinfo.io 免费小部件接口（按 IP 查询，免 key；数据源选型借鉴 xykt IPQuality）。

返回 data.asn.type（isp/hosting/business/education）、data.company.type、
data.privacy.{vpn,proxy,tor,hosting} 与顶层 is_hosting/is_mobile 权威布尔：
前者是比关键词表权威的住宅/机房判定，后者表示出口已被 VPN/代理风控库收录
（Claude 风控侧同样可见的信号）。2026-10-09 实测 schema（8.8.8.8 → hosting）。
"""

_IPINFO_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)

# 大厂云 ASN/组织关键词（docs/03 §4 集合 + 常见延伸，命中则降为 1 分）
_BIG_CLOUD_KEYWORDS: tuple[str, ...] = (
    "amazon", "aws", "google cloud", "google llc", "goog", "microsoft", "azure",
    "digitalocean", "vultr", "hetzner", "ovh", "linode", "oracle", "alibaba",
    "tencent cloud", "akamai", "cloudfront",
)

# 住宅/宽带 ISP 特征关键词（docs/03 §4「Telecom/Comcast/Verizon/宽带商…」的具体化，可继续扩充）
_ISP_KEYWORDS: tuple[str, ...] = (
    "telecom", "comcast", "verizon", "at&t", "t-mobile", "sprint", "charter",
    "spectrum", "frontier", "optimum", "cablevision", "broadband", "fiber",
    "fibre", "telefonica", "telefónica", "vodafone", "orange", "bouygues",
    "free sas", "deutsche telekom", "telekom", "virgin media", "sky uk",
    "jio", "airtel", "bsnl", "chunghwa", "hinet", "taiwan mobile", "pccw",
    "hkt", "smarone", "hong kong broadband", "hkbn", "china telecom",
    "china unicom", "china mobile", "cmcc", "singtel", "starhub", "m1 limited",
    "viewqwest", "myrepublic", "kt corp", "sk broadband", "lg uplus",
    "kddi", "ntt", "softbank", "iij",
)

_IP_TYPE_LABELS = {
    "residential": "住宅",
    "datacenter": "机房",
    "mobile": "移动",
    "unknown": "未知",
}


class PurityProviderError(RuntimeError):
    """PurityProvider 查询失败（网络异常 / 限流未恢复 / 返回非 success）。"""


class PurityProvider(Protocol):
    """纯净度数据源接口；v2 可替换 ipinfo/ipqualityscore 等增强实现。"""

    def lookup(self, *, proxy_url: str | None = None) -> dict:
        """proxy_url=None 直连；否则经该代理请求，返回含出口 IP 与属性信号的原始 JSON dict。"""
        ...


class IpApiProvider:
    """ip-api.com 免费接口实现（45 req/min → 相邻请求间隔 ≥ min_interval 秒）。"""

    def __init__(
        self,
        *,
        timeout: float = 10.0,
        min_interval: float = 1.4,
        backoff: float = 1.0,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self._timeout = timeout
        self._min_interval = max(0.0, min_interval)
        self._backoff = max(0.0, backoff)
        self._transport = transport          # 测试注入 MockTransport / 本地假服务用
        self._last_request_at = float("-inf")

    def lookup(self, *, proxy_url: str | None = None) -> dict:
        response = self._rate_limited_get(proxy_url)
        if response.status_code == 429:
            logger.warning("ip-api 触发限流（429），退避 %.1f 秒后重试一次", self._backoff)
            time.sleep(self._backoff)
            response = self._rate_limited_get(proxy_url)
        if response.status_code != 200:
            raise PurityProviderError(f"ip-api 请求失败：HTTP {response.status_code}")
        try:
            data = response.json()
        except ValueError as exc:
            raise PurityProviderError("ip-api 响应不是合法 JSON") from exc
        if not isinstance(data, dict) or data.get("status") != "success":
            message = data.get("message") if isinstance(data, dict) else None
            raise PurityProviderError(f"ip-api 查询失败：{message or data!r}")
        return data

    def _rate_limited_get(self, proxy_url: str | None) -> httpx.Response:
        """限速门闩：距上次请求发起不足 min_interval 则先补齐间隔（失败尝试同样计入）。"""
        if self._min_interval > 0:
            wait = self._min_interval - (time.monotonic() - self._last_request_at)
            if wait > 0:
                time.sleep(wait)
        self._last_request_at = time.monotonic()
        try:
            with httpx.Client(proxy=proxy_url, timeout=self._timeout, transport=self._transport) as client:
                return client.get(_IPAPI_URL)
        except httpx.HTTPError as exc:
            raise PurityProviderError(f"ip-api 请求异常：{type(exc).__name__}") from exc


class IpInfoWidgetError(RuntimeError):
    """ipinfo widget 增强查询失败（网络异常 / 非 200 / 非法 JSON）。"""


class PurityEnhancer(Protocol):
    """纯净度增强源接口：按已知出口 IP 补查第二数据源；只增强、不主判。"""

    def lookup(self, *, ip: str, proxy_url: str | None = None) -> dict:
        """ip 为已实测的出口 IP；proxy_url 为查询所用出口代理（None=直连）。返回原始 JSON dict。"""
        ...


class IpInfoWidgetClient:
    """ipinfo.io widget 免 key 客户端（docs/03 §4 增强源，默认实现）。

    按 ip-api 已实测的出口 IP 补查，不承担主判：任何失败由 _enhance_raw 降级为
    无增强数据落库，绝不影响主链路。量级为每节点每天 ≤1 次（≤~100 次/天），无需限速。
    """

    def __init__(
        self,
        *,
        timeout: float = 10.0,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self._timeout = timeout
        self._transport = transport          # 测试注入 MockTransport / 本地假服务用

    def lookup(self, *, ip: str, proxy_url: str | None = None) -> dict:
        try:
            with httpx.Client(
                proxy=proxy_url,
                timeout=self._timeout,
                transport=self._transport,
                headers={"User-Agent": _IPINFO_UA},
            ) as client:
                response = client.get(_IPINFO_WIDGET_URL.format(ip=ip))
        except httpx.HTTPError as exc:
            raise IpInfoWidgetError(f"ipinfo widget 请求异常：{type(exc).__name__}") from exc
        if response.status_code != 200:
            raise IpInfoWidgetError(f"ipinfo widget 请求失败：HTTP {response.status_code}")
        try:
            data = response.json()
        except ValueError as exc:
            raise IpInfoWidgetError("ipinfo widget 响应不是合法 JSON") from exc
        if not isinstance(data, dict):
            raise IpInfoWidgetError(f"ipinfo widget 响应类型异常：{type(data).__name__}")
        return data


@dataclass
class PurityReport:
    """一轮扫描汇总。"""

    results: list[PurityResult] = field(default_factory=list)
    unavailable: bool = False   # 探测实例启动失败/中途崩溃 → True，主链路无感
    checked: int = 0            # 本次实际检测并落库的节点数
    enhanced: int = 0           # 其中成功合入 ipinfo 增强数据的节点数
    skipped: int = 0            # 未检测节点数：增量模式下已有有效结果的节点 + 检测异常/不可达/实例崩溃跳过的节点


# ---------------------------------------------------------------------- 评分与分类

def _ipinfo_data(raw: dict[str, Any] | None) -> dict[str, Any]:
    """取 provider raw 中嵌套的 ipinfo 增强数据（无增强/类型不符 → 空 dict）。"""
    if not isinstance(raw, dict):
        return {}
    info = raw.get("ipinfo")
    return info if isinstance(info, dict) else {}


def _ipinfo_asn_type(info: dict[str, Any]) -> str:
    """ipinfo asn.type（isp/hosting/business/education…），缺省返回空串。"""
    asn = info.get("asn")
    value = asn.get("type") if isinstance(asn, dict) else None
    return str(value).strip().lower() if value else ""


def _ipinfo_flagged(info: dict[str, Any]) -> bool:
    """privacy 风险标记：vpn/proxy/tor 任一为 true → 出口已被 VPN/代理风控库收录。"""
    privacy = info.get("privacy")
    if not isinstance(privacy, dict):
        return False
    return any(privacy.get(flag) is True for flag in ("vpn", "proxy", "tor"))


def _ipinfo_hosting(info: dict[str, Any]) -> bool:
    """顶层 is_hosting 权威机房标记（真实返回里与 asn.type=hosting 一致）。"""
    return info.get("is_hosting") is True


def _ipinfo_mobile(info: dict[str, Any]) -> bool:
    """顶层 is_mobile 权威移动出口标记（比 ip-api mobile 字段库更权威）。"""
    return info.get("is_mobile") is True


def _signals_text(result: PurityResult) -> str:
    """拼接 ASN/组织/ISP 文本（含 raw.asname 与 ipinfo 增强的 asn/company 名）用于关键词匹配。"""
    parts: list[Any] = [result.asn, result.org, result.isp]
    raw = result.raw if isinstance(result.raw, dict) else None
    if raw is not None:
        parts.append(raw.get("asname"))
        info = raw.get("ipinfo")
        if isinstance(info, dict):
            for key in ("asn", "company"):
                section = info.get(key)
                if isinstance(section, dict):
                    parts.append(section.get("name"))
    return " ".join(str(part) for part in parts if part).lower()


def _contains_any(text: str, keywords: tuple[str, ...]) -> bool:
    return any(keyword in text for keyword in keywords)


def _is_isp_like(result: PurityResult) -> bool:
    """ASN/组织/ISP 文本命中住宅宽带商特征。"""
    return _contains_any(_signals_text(result), _ISP_KEYWORDS)


def _is_big_cloud(result: PurityResult) -> bool:
    """ASN/组织文本命中大厂云 ASN 集合（docs/03 §4：AWS/GCP/Azure/DO/Vultr/Hetzner/OVH/Linode…）。"""
    return _contains_any(_signals_text(result), _BIG_CLOUD_KEYWORDS)


def classify_ip_type(result: PurityResult, node: Node | None) -> str:
    """出口 IP 类型分类：residential / datacenter / mobile / unknown。

    ipinfo 增强（若有）为权威信号：asn.type=hosting / 顶层 is_hosting 判机房
    （补 ip-api 漏标），asn.type=isp 判住宅（替代关键词表猜测），
    顶层 is_mobile 判移动；无增强时回退 ip-api + 关键词表。
    """
    info = _ipinfo_data(result.raw)
    asn_type = _ipinfo_asn_type(info)
    if result.hosting or asn_type == "hosting" or _ipinfo_hosting(info):
        return "datacenter"
    if result.mobile or _ipinfo_mobile(info):
        return "mobile"
    if asn_type == "isp" or _is_isp_like(result) or (node is not None and node.residential):
        return "residential"
    return "unknown"


def claude_rank_result(result: PurityResult, node: Node | None) -> int:
    """docs/03 §4 评分表：住宅 3 / 中小机房 2 / 大厂云 ASN 集 1 / 已标记代理 0。

    - proxy=true 一票降权（已被标记代理，最易触发风控）；
    - hosting=true（ip-api）、ipinfo asn.type=hosting 或顶层 is_hosting 走机房分支：
      大厂云 ASN 集 1 分（匹配文本含 ipinfo asn/company 名），中小机房（如 DMIT）2 分；
    - mobile（ip-api）或 ipinfo 顶层 is_mobile 视为消费级运营商出口，与住宅同档 3 分；
    - ipinfo asn.type=isp（权威）或命中 ISP 特征（或 node.residential）→ 住宅 3 分；
    - 其余（无任何信号）归 unknown，按「可用但不优先」记 2 分；
    - ipinfo privacy 命中 vpn/proxy/tor → 封顶 2 分：住宅 ASN 的出口一旦被 VPN/代理
      风控库收录，即属高共存滥用出口，Claude 风控侧同样可见该收录状态，不再给满分。
    """
    if result.proxy:
        return 0
    info = _ipinfo_data(result.raw)
    asn_type = _ipinfo_asn_type(info)
    if result.hosting or asn_type == "hosting" or _ipinfo_hosting(info):
        rank = 1 if _is_big_cloud(result) else 2
    elif result.mobile or _ipinfo_mobile(info):
        rank = 3
    elif asn_type == "isp" or _is_isp_like(result) or (node is not None and node.residential):
        rank = 3
    else:
        rank = 2
    if _ipinfo_flagged(info):
        rank = min(rank, 2)
    return rank


def build_purity_result(node: Node, raw: dict[str, Any], *, checked_at: str | None = None) -> PurityResult:
    """把 provider 原始 JSON 组装为 PurityResult（含分类与评分）。"""
    data = raw if isinstance(raw, dict) else {}
    result = PurityResult(
        node_name=node.name,
        source_sub=node.source_sub,
        checked_at=checked_at or now_iso(),
        exit_ip=data.get("query"),
        country=data.get("country"),
        asn=data.get("as"),
        org=data.get("org"),
        isp=data.get("isp"),
        hosting=_opt_bool(data.get("hosting")),
        proxy=_opt_bool(data.get("proxy")),
        mobile=_opt_bool(data.get("mobile")),
        raw=data,
    )
    result.ip_type = classify_ip_type(result, node)
    result.claude_rank = claude_rank_result(result, node)
    return result


def _opt_bool(value: Any) -> bool | None:
    return None if value is None else bool(value)


# ---------------------------------------------------------------------- 扫描

def _select_targets(real_nodes: list[Node], prev_map: dict[tuple[str, str], PurityResult], *, full: bool) -> list[Node]:
    """full=True 全量；增量只测「无结果或已失效」（上次无出口 IP/未分类 = 无有效数据）的节点。"""
    if full:
        return list(real_nodes)
    targets: list[Node] = []
    for node in real_nodes:
        prev = prev_map.get((node.name, node.source_sub))
        if prev is None or (prev.exit_ip is None and prev.ip_type is None):
            targets.append(node)
    return targets


def _maybe_warn_attr_change(prev: PurityResult | None, curr: PurityResult) -> None:
    """与上次结果比对生成属性变化告警；「住宅→机房」为标红告警场景。"""
    if prev is None or not prev.ip_type or not curr.ip_type or prev.ip_type == curr.ip_type:
        return
    prev_label = _IP_TYPE_LABELS.get(prev.ip_type, prev.ip_type)
    curr_label = _IP_TYPE_LABELS.get(curr.ip_type, curr.ip_type)
    if prev.ip_type == "residential":
        logger.warning(
            "纯净度属性变化告警：节点 %s 出口由「%s」变为「%s」，建议重新锁定 Claude 专用节点",
            curr.node_name, prev_label, curr_label,
        )
    else:
        logger.info("纯净度属性变化：节点 %s 出口由「%s」变为「%s」", curr.node_name, prev_label, curr_label)


def _enhance_raw(
    raw: dict[str, Any],
    enhancer: PurityEnhancer,
    *,
    proxy_url: str | None,
    node_name: str,
) -> tuple[dict[str, Any], bool]:
    """按 ip-api 返回的出口 IP 补查增强源并合入 raw；返回 (新 raw, 是否增强成功)。

    增强属 best-effort：查询失败/返回为空一律原样返回、只记 info 日志，
    绝不影响该节点主数据落库（docs/03 §4「只增强、不主判」纪律）。
    """
    exit_ip = raw.get("query") if isinstance(raw, dict) else None
    if not exit_ip:
        return raw, False
    try:
        info = enhancer.lookup(ip=str(exit_ip), proxy_url=proxy_url)
    except Exception as exc:  # noqa: BLE001 —— 任何增强失败都只降级为无增强数据
        logger.info("节点 %s ipinfo 增强查询失败（忽略）：%s", node_name, exc)
        return raw, False
    if not isinstance(info, dict) or not info:
        return raw, False
    return {**raw, "ipinfo": info}, True


def scan(
    nodes: list[Node],
    *,
    config: AppConfig,
    store: Store,
    provider: PurityProvider | None = None,
    enhancer: PurityEnhancer | None = None,
    full: bool = False,
) -> PurityReport:
    """对节点执行一轮纯净度扫描（旁路，不阻塞分发；异常不外抛）。

    逐节点：探测实例可用性确认（delay）→ select 切换出口 → 经混合端口 lookup →
    按出口 IP 补查增强源（best-effort）→ 组装 PurityResult → store.save_purity_result；
    单节点失败跳过计数，不影响其余节点。enhancer=None 时默认启用 IpInfoWidgetClient
    （测试/关停场景传入显式实现替换）。
    """
    real_nodes = [n for n in nodes if not n.filtered]
    prev_map = {(r.node_name, r.source_sub): r for r in store.latest_purity_results()}
    targets = _select_targets(real_nodes, prev_map, full=full)
    if not targets:
        logger.info("纯净度扫描：无待测节点（full=%s，共 %d 个真实节点）", full, len(real_nodes))
        return PurityReport(results=[], unavailable=False, checked=0, skipped=0)
    provider = provider if provider is not None else IpApiProvider()
    if enhancer is None:
        enhancer = IpInfoWidgetClient()
    # 探测实例端口与健康采样（health）共享，生命周期全程持锁串行化
    with probe_instance_lock():
        probe = ProbeInstance(config, real_nodes)
        try:
            try:
                started = probe.start()
            except Exception as exc:  # noqa: BLE001 —— 启动异常同样降级为 unavailable
                logger.warning("探测实例启动异常，纯净度报告标记 unavailable：%s", exc)
                started = False
            if not started:
                logger.warning("探测实例不可用，本轮纯净度扫描放弃（%d 个待测节点）", len(targets))
                return PurityReport(results=[], unavailable=True, checked=0, skipped=len(targets))
            results: list[PurityResult] = []
            checked = 0
            enhanced = 0
            error_skips = 0
            unavailable = False
            for node in targets:
                if not probe.is_available():
                    logger.warning("探测实例中途失效，剩余 %d 个节点跳过检测",
                                   len(targets) - checked - error_skips)
                    unavailable = True
                    error_skips += len(targets) - checked - error_skips
                    break
                if probe.delay(node.name) is None:
                    logger.warning("节点 %s 探测不可达（delay 失败），跳过检测", node.name)
                    error_skips += 1
                    continue
                if not probe.select(node.name):
                    logger.warning("节点 %s 出口切换失败（PUT /proxies/PROBE），跳过检测", node.name)
                    error_skips += 1
                    continue
                try:
                    raw = provider.lookup(proxy_url=probe.local_proxy_url())
                    raw, did_enhance = _enhance_raw(
                        raw, enhancer,
                        proxy_url=probe.local_proxy_url(), node_name=node.name,
                    )
                    enhanced += int(did_enhance)
                    result = build_purity_result(node, raw)
                    store.save_purity_result(result)
                    results.append(result)
                    checked += 1
                    _maybe_warn_attr_change(prev_map.get((node.name, node.source_sub)), result)
                except Exception as exc:  # noqa: BLE001 —— 单节点失败只跳过计数
                    logger.warning("节点 %s 纯净度检测失败，已跳过：%s", node.name, exc)
                    error_skips += 1
            if checked:
                logger.info("纯净度扫描完成：检测 %d 个节点，其中 ipinfo 增强 %d 个", checked, enhanced)
            return PurityReport(
                results=results,
                unavailable=unavailable,
                checked=checked,
                enhanced=enhanced,
                skipped=(len(real_nodes) - len(targets)) + error_skips,
            )
        finally:
            try:
                probe.stop()
            except Exception as exc:  # noqa: BLE001
                logger.warning("探测实例停止失败（忽略）：%s", exc)


# ---------------------------------------------------------------------- 推荐排序

def _static_tier(node: Node) -> int:
    """docs/02 §2 Claude 专用组静态排序：美国家宽 0 → 其他美国 1 → 港/新家宽 2 → 其余 3。"""
    if node.region == "US" and node.residential:
        return 0
    if node.region == "US":
        return 1
    if node.region in ("HK", "SG") and node.residential:
        return 2
    return 3


def claude_recommendations(latest: list[PurityResult], nodes: list[Node]) -> list[PurityResult]:
    """Claude 专用组推荐 Top3：claude_rank 降序（住宅恒在机房前），同分按静态排序居前。

    只推荐当前仍存在的节点（已下线节点剔除）；无任何可用数据时返回空列表，
    调用方（templater/web）按 docs/02 静态排序回退。
    """
    node_map = {(n.name, n.source_sub): n for n in nodes if not n.filtered}
    candidates = [r for r in latest if (r.node_name, r.source_sub) in node_map]

    def _sort_key(result: PurityResult) -> tuple[int, int, str]:
        node = node_map[(result.node_name, result.source_sub)]
        rank = result.claude_rank if result.claude_rank is not None else -1
        return (-rank, _static_tier(node), result.node_name)

    candidates.sort(key=_sort_key)
    return candidates[:3]
