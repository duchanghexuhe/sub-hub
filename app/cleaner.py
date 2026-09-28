"""节点清洗与分类（docs/02 §1）：假节点过滤、地区/属性识别、重名消歧。

处理顺序（INTERFACES.md §3.3 固定管线）：
    filter_fake_nodes → classify → disambiguate；clean() 依次调用三者。

约定：
- clean() 的输入是 parser 输出的**多订阅合并后**的全量节点（pipeline 负责合并）；
  disambiguate 依赖合并后的上下文才能发现跨订阅同名。
- 被滤节点不丢弃：就地标记 filtered=True + filter_reason=命中关键词，随
  CleanResult.filtered 返回，pipeline 全量落库后可在 UI「已过滤节点」区查询。
- 地区映射表内置在 app/regions.py（REGION_RULES，dict 结构，UI 可运行期补充），
  本模块按契约以同名再导出。
- 日志纪律：只输出节点名与命中关键词，绝不输出节点凭据字段与订阅 URL。
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass

from app.models import Node
from app.regions import (  # noqa: F401  REGION_RULES 按契约从 cleaner 命名空间再导出
    OTHER_REGION_LABEL,
    REGION_RULES,
    RegionRule,
    build_keyword_pattern,
    match_region,
)

logger = logging.getLogger("subhub.cleaner")

__all__ = [
    "FAKE_NODE_PATTERNS",
    "REGION_RULES",
    "OTHER_REGION_LABEL",
    "RegionRule",
    "CleanResult",
    "clean",
    "filter_fake_nodes",
    "classify",
    "disambiguate",
]

# ---------------------------------------------------------------- 假节点黑名单

# docs/02 §1 黑名单原文：
# 剩余流量|套餐到期|到期时间|重置|官网|官址|网址|续费|订阅|流量|expire|traffic|电报|频道|群|tg|telegram
_FAKE_KEYWORDS: tuple[str, ...] = (
    "剩余流量", "套餐到期", "到期时间", "重置", "官网", "官址", "网址", "续费",
    "订阅", "流量", "expire", "traffic", "电报", "频道", "群", "tg", "telegram",
    # docs/02 §1 之外的运营补充：推广/引导下载类假节点（如「安装最新版软件使用」）
    "安装", "客户端", "软件",
)

# 大小写不敏感（expire/traffic/tg/telegram 等）；子串匹配使节点名中的表情装饰
# （如「📢 剩余流量」）不影响命中。
FAKE_NODE_PATTERNS: list[re.Pattern] = [
    re.compile(re.escape(kw), re.IGNORECASE) for kw in _FAKE_KEYWORDS
]


def _match_fake_keyword(name: str) -> str | None:
    """返回节点名命中的第一个黑名单关键词；未命中返回 None。"""
    for kw, pattern in zip(_FAKE_KEYWORDS, FAKE_NODE_PATTERNS):
        if pattern.search(name):
            return kw
    return None


def filter_fake_nodes(nodes: list[Node]) -> list[Node]:
    """假节点过滤：命中者就地置 filtered=True、filter_reason=命中关键词，
    然后从返回列表剔除（节点对象仍由调用方经 CleanResult.filtered 持有，可查）。"""
    survivors: list[Node] = []
    for node in nodes:
        reason = _match_fake_keyword(node.name)
        if reason is None:
            survivors.append(node)
            continue
        node.filtered = True
        node.filter_reason = reason
        logger.info("已过滤假节点：%s（命中关键词：%s）", node.name, reason)
    return survivors


# ---------------------------------------------------------------- 属性标记

# docs/02 §1 属性关键词（大小写不敏感，英文词带词边界）
_RESIDENTIAL_PATTERN = build_keyword_pattern(("家庭", "家宽", "住宅"),
                                             ("ISP", "Home", "residential"))
_IPLC_PATTERN = build_keyword_pattern(("专线",), ("IEPL", "IPLC"))

# 倍率：x2 / x3 / 2x / 3x / x2.5 …（解析为数值；v1 只标注不过滤）
# 前后断言防止把「x2」误读进普通字母数字串（如 base64 状名字、Max2x 之外的误伤）。
_RATE_PATTERN = re.compile(
    r"(?<![0-9A-Za-z])(?:[x×](?P<mul>\d+(?:\.\d+)?)|(?P<pre>\d+(?:\.\d+)?)\s*[x×])(?![0-9A-Za-z])",
    re.IGNORECASE,
)


def _parse_rate(name: str) -> float:
    """从节点名解析倍率（x2→2.0、2x→2.0、x2.5→2.5）；无倍率默认 1.0。"""
    matched = _RATE_PATTERN.search(name)
    if matched is None:
        return 1.0
    raw = matched.group("mul") or matched.group("pre")
    try:
        value = float(raw)
    except ValueError:  # 理论不可达（正则已保证数字），兜底防御
        return 1.0
    return value if value > 0 else 1.0


# ---------------------------------------------------------------- 地区识别

def classify(nodes: list[Node]) -> list[Node]:
    """就地填写 region / residential / iplc / rate，返回原列表。

    region 三级匹配（国旗 emoji → 中文 → 英文/缩写）见 app.regions.match_region；
    未识别为 None（templater 归「🌍 其他」组，节点不丢失）。
    """
    for node in nodes:
        rule = match_region(node.name)
        node.region = rule.code if rule is not None else None
        node.residential = bool(_RESIDENTIAL_PATTERN and _RESIDENTIAL_PATTERN.search(node.name))
        node.iplc = bool(_IPLC_PATTERN and _IPLC_PATTERN.search(node.name))
        node.rate = _parse_rate(node.name)
    return nodes


# ---------------------------------------------------------------- 重名消歧

def disambiguate(nodes: list[Node]) -> list[Node]:
    """跨订阅同名消歧：同名出现在 ≥2 个订阅时，name 追加「 [source_sub]」后缀
    （source_sub 即订阅管理里的短代号/机场别名），orig_name 记录改名前原名。

    - 只在多订阅合并上下文中有意义（clean 负责顺序）；
    - 已带 orig_name 的节点视为已消歧，跳过 → 函数幂等；
    - 同名且同订阅（机场内部重名，异常输入）不加后缀——后缀无法区分它们。
    """
    subs_by_name: dict[str, set[str]] = {}
    for node in nodes:
        subs_by_name.setdefault(node.name, set()).add(node.source_sub)
    renamed = 0
    for node in nodes:
        if node.orig_name:
            continue
        if len(subs_by_name.get(node.name, ())) > 1:
            original = node.name
            node.name = f"{original} [{node.source_sub}]"
            node.orig_name = original
            renamed += 1
    if renamed:
        logger.info("重名消歧：%d 个跨订阅同名节点已追加订阅别名后缀", renamed)
    return nodes


# ---------------------------------------------------------------- 入口

@dataclass
class CleanResult:
    """清洗结果：kept=真实节点（已分类+消歧），filtered=被滤节点（可查询）。"""

    kept: list[Node]
    filtered: list[Node]


def clean(nodes: list[Node]) -> CleanResult:
    """清洗入口（docs/02 §1）：滤假节点 → 地区/属性分类 → 重名消歧。

    输入为多订阅合并后的全量节点；被滤节点不丢弃，随 filtered 返回供
    pipeline 落库与 UI 查询。
    """
    survivors = filter_fake_nodes(nodes)
    filtered = [n for n in nodes if n.filtered]
    survivors = classify(survivors)
    kept = disambiguate(survivors)
    logger.info(
        "清洗完成：保留 %d 个真实节点，过滤 %d 个假节点", len(kept), len(filtered)
    )
    return CleanResult(kept=kept, filtered=filtered)
