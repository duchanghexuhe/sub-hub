"""统一数据模型：parser 产出、cleaner 加工、templater/purity/web 消费。

约定：
- 协议专有凭据字段（uuid/password/cipher/flow/tls/sni/ws-opts…）统一放在
  Node.credentials 字典；**日志与 UI 严禁直接输出该字典**（见 docs/INTERFACES.md）。
- 时间字段一律 ISO8601 本地时间字符串（app.utils.now_iso），字符串比较即时序。
- 字段定义依据 docs/02（节点模型/清洗）与 docs/03（存储/纯净度）。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class FetchStatus(str, Enum):
    """订阅最近一次抓取状态（UI 显示中文）。"""

    OK = "ok"
    INVALID = "invalid"    # 401/403 或 0 个有效节点 → 订阅失效
    TIMEOUT = "timeout"
    FAILED = "failed"
    NEVER = "never"        # 尚未抓取过

    @property
    def label(self) -> str:
        return _FETCH_STATUS_LABELS[self]


_FETCH_STATUS_LABELS: dict[FetchStatus, str] = {
    FetchStatus.OK: "成功",
    FetchStatus.INVALID: "订阅失效",
    FetchStatus.TIMEOUT: "超时",
    FetchStatus.FAILED: "失败",
    FetchStatus.NEVER: "从未抓取",
}


@dataclass
class Node:
    """统一节点模型。

    生命周期：parser 填 name/type/server/port/source_sub/credentials →
    cleaner 填 region/residential/iplc/rate/filtered/filter_reason 并在重名时
    改写 name（原名留 orig_name）→ templater/purity/web 只读消费。
    """

    name: str                        # 最终展示名（消歧后可能为「原名 [订阅别名]」）
    type: str                        # vless / anytls / ss / vmess / trojan / hysteria2 / tuic …
    server: str
    port: int
    source_sub: str                  # 所属订阅别名（subscriptions.name）
    credentials: dict[str, Any] = field(default_factory=dict)  # 协议专有字段（敏感）
    region: str | None = None        # 地区代码 US/HK/TW/JP/SG/KR/GB/DE…；None=未识别（归「🌍 其他」）
    residential: bool = False        # 家宽/住宅 IP（按节点名关键词标记，纯净度实测可推翻）
    iplc: bool = False               # 专线（IEPL/IPLC）
    rate: float = 1.0                # 倍率（节点名 x2 → 2.0）
    filtered: bool = False           # 命中假节点黑名单
    filter_reason: str | None = None # 命中的黑名单关键词
    orig_name: str | None = None     # 消歧改名前的原始名（未改名时为 None）

    @property
    def key(self) -> tuple[str, str]:
        """全局唯一键：所属订阅 + 原始名（纯净度结果以此关联）。"""
        return (self.source_sub, self.orig_name or self.name)

    def to_clash_proxy(self) -> dict[str, Any]:
        """还原为 Clash/mihomo proxies 列表项（templater 序列化用）。"""
        proxy: dict[str, Any] = {
            "name": self.name,
            "type": self.type,
            "server": self.server,
            "port": self.port,
        }
        proxy.update(self.credentials)
        return proxy

    @classmethod
    def from_clash_proxy(cls, proxy: dict[str, Any], source_sub: str) -> "Node":
        """从 Clash proxies 列表项构造（parser 主路径可复用）。"""
        data = dict(proxy)
        return cls(
            name=str(data.pop("name")),
            type=str(data.pop("type")),
            server=str(data.pop("server")),
            port=int(data.pop("port")),
            source_sub=source_sub,
            credentials=data,
        )


@dataclass
class Subscription:
    """订阅（模型持有明文 URL；落盘密文由 store 负责，UI 展示须打码）。"""

    id: int
    name: str                                   # 别名/短代号，唯一，如 "kuai"
    url: str
    enabled: bool = True
    last_fetch_at: str | None = None            # ISO8601
    last_fetch_status: FetchStatus | None = None
    userinfo: dict[str, int] | None = None      # {upload, download, total, expire}（字节/epoch 秒）


@dataclass
class PurityResult:
    """单节点出口 IP 检测结果（一次检测一条，追加式存储）。"""

    node_name: str
    source_sub: str
    checked_at: str                             # ISO8601
    exit_ip: str | None = None
    country: str | None = None
    asn: str | None = None                      # 如 "AS906 DMIT Cloud Services"
    org: str | None = None
    isp: str | None = None
    ip_type: str | None = None                  # residential | datacenter | mobile | unknown
    hosting: bool | None = None                 # ip-api hosting 信号（机房/托管）
    proxy: bool | None = None                   # ip-api proxy 信号（已被标记代理 → 降权）
    mobile: bool | None = None
    claude_rank: int | None = None              # 3=住宅首选 2=中小机房 1=大厂云 0=已标记代理；None=未评分
    raw: dict[str, Any] | None = None           # provider 原始响应（ip-api json）


@dataclass
class AttrChange:
    """属性变化对比（store.attribute_changes 产出；「家宽→机房」UI 标红）。"""

    node_name: str
    source_sub: str
    prev: PurityResult
    curr: PurityResult

    @property
    def is_residential_lost(self) -> bool:
        """上次住宅、本次机房 → 需要重锁 Claude 节点的告警场景。"""
        return self.prev.ip_type == "residential" and self.curr.ip_type != "residential"


@dataclass
class HealthSample:
    """单次节点健康采样（health 定时写入，管理页「健康时间块条」消费）。

    延迟只代表采样瞬间，单点无意义；由 UI 按 24h 窗口聚合出成功率/均延迟/
    连续失败，形成时间块条。delay_ms=None 表示该轮探活失败（超时/不可达）。
    """

    node_name: str
    source_sub: str
    checked_at: str                             # ISO8601（同一轮采样共用同一时刻）
    delay_ms: int | None = None

    @property
    def ok(self) -> bool:
        return self.delay_ms is not None


@dataclass
class GhSpeedSample:
    """单节点 GitHub CDN 吞吐量采样（ghspeed 定时写入，🐱 GitHub 组排序消费）。

    经节点出口实测 GitHub release 资产（Fastly 路径）的下载速度，MB/s。
    speed_mbps=None 表示该轮测速失败（select 失败/连不上/流中断）；
    0.0 是真实测量值（连上了但窗口内没读到数据），排序时按最慢处理。
    """

    node_name: str
    source_sub: str
    checked_at: str                             # ISO8601（同一轮扫描共用同一时刻）
    speed_mbps: float | None = None


@dataclass
class ConfigVersion:
    """一次发布的元数据（写入 data/out/v<NNNN>/meta.json，由 utils 版本目录管理）。"""

    version: int
    created_at: str                     # ISO8601
    node_count: int                     # 本次发布真实节点数
    content_hash: str                   # 全部产物拼接内容的 sha256（ETag 基准）
    diff_summary: dict[str, Any] = field(default_factory=dict)  # {added:[], removed:[], renamed:[[old,new]]}
    note: str | None = None             # 备注，如 "回退到 v0007"
