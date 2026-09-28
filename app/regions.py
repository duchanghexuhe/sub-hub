"""地区识别映射表：国旗 emoji → 中文关键词 → 英文关键词/缩写 三级匹配。

对应 docs/02 §1「地区识别」。映射表内置于此模块（REGION_RULES，普通 dict），
结构对 UI 开放：运行期向该 dict 追加 RegionRule 即可扩充新地区，cleaner 的
classify 每次调用动态读取，无需改代码、无需重启。

三级匹配语义（match_region）：
  1. 国旗 emoji：最强信号，字面包含即命中；
  2. 中文关键词：字面子串；
  3. 英文关键词/缩写：大小写不敏感；纯字母数字词自动加 \\b 词边界，
     防止「RUS 误命中 US」「200GB 误命中 GB」「Node 误命中 DE」一类子串误伤。

RegionRule.pattern 是 flag/中文/英文 三级合一的组合正则（大小写不敏感），
templater 生成地区组 filter 时可直接引用。

覆盖地区（docs/02 §1）：香港/台湾/日本/新加坡/美国/韩国/英国/德国 + 常见
城市词；美国关键词为文档所列全量（美国/US/United States/洛杉矶/圣何塞/
硅谷/西雅图/凤凰城/纽约/芝加哥/达拉斯/DMIT/Los Angeles/San Jose 等）。
未识别地区的节点由 templater 归入 OTHER_REGION_LABEL（「🌍 其他」），不丢失。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Iterable, Sequence

# 未识别地区节点的归属组名（docs/02 §1：未识别归「🌍 其他」）
OTHER_REGION_LABEL = "🌍 其他"

_WORD_LIKE = re.compile(r"[A-Za-z0-9]+")
_NEVER_MATCH = re.compile(r"(?!)")  # 永不命中的占位正则（空关键词规则用）


def _en_part(keyword: str) -> str:
    """英文关键词 → 正则片段：纯字母数字词加词边界（防子串误伤）。"""
    keyword = keyword.strip()
    if _WORD_LIKE.fullmatch(keyword):
        return rf"\b{re.escape(keyword)}\b"
    return re.escape(keyword)


def build_keyword_pattern(
    cn_keywords: Iterable[str],
    en_keywords: Iterable[str],
    extra_literals: Sequence[str] = (),
) -> re.Pattern[str] | None:
    """把「额外字面量（如国旗 emoji）+ 中文子串 + 英文词边界关键词」合并为
    单个正则（大小写不敏感）。

    供 REGION_RULES 编译与 cleaner 的属性关键词（家宽/专线）复用；
    无任何关键词时返回 None。
    """
    parts = [re.escape(x) for x in extra_literals if x]
    parts += [re.escape(kw) for kw in cn_keywords if kw]
    parts += [_en_part(kw) for kw in en_keywords if kw and kw.strip()]
    if not parts:
        return None
    return re.compile("|".join(parts), re.IGNORECASE)


@dataclass
class RegionRule:
    """单地区匹配规则。

    Attributes:
        code:        地区代码（ISO 3166-1 alpha-2，如 US/HK/TW），写入 Node.region。
        name_zh:     中文展示名（如「美国」；组名 = flag + name_zh）。
        pattern:     三级合一的组合正则；传 None 时按 flag/cn/en 关键词自动编译。
        flag:        国旗 emoji（第一级匹配，可为空）。
        cn_keywords: 第二级中文关键词（字面子串）。
        en_keywords: 第三级英文关键词/缩写（大小写不敏感 + 词边界）。
    """

    code: str
    name_zh: str
    pattern: re.Pattern[str] | None = None
    flag: str = ""
    cn_keywords: tuple[str, ...] = ()
    en_keywords: tuple[str, ...] = ()
    _en_pattern: re.Pattern[str] | None = field(default=None, repr=False, compare=False)

    def __post_init__(self) -> None:
        # UI 运行期可能传 list，统一转 tuple
        self.cn_keywords = tuple(self.cn_keywords or ())
        self.en_keywords = tuple(self.en_keywords or ())
        if self.pattern is None:
            built = build_keyword_pattern(
                self.cn_keywords, self.en_keywords, extra_literals=(self.flag,)
            )
            self.pattern = built if built is not None else _NEVER_MATCH
        if self._en_pattern is None:
            parts = [_en_part(kw) for kw in self.en_keywords if kw and kw.strip()]
            self._en_pattern = (
                re.compile("|".join(parts), re.IGNORECASE) if parts else _NEVER_MATCH
            )


# 内置映射表（docs/02 §1；顺序即匹配优先顺序，UI 可追加新地区）
REGION_RULES: dict[str, RegionRule] = {
    "HK": RegionRule(
        code="HK", name_zh="香港", flag="🇭🇰",
        cn_keywords=("香港",), en_keywords=("HK", "Hong Kong"),
    ),
    "TW": RegionRule(
        code="TW", name_zh="台湾", flag="🇹🇼",
        cn_keywords=("台湾", "臺灣", "台北", "臺北", "新北", "高雄"),
        en_keywords=("TW", "Taiwan", "Taipei"),
    ),
    "JP": RegionRule(
        code="JP", name_zh="日本", flag="🇯🇵",
        cn_keywords=("日本", "东京", "東京", "大阪", "名古屋"),
        en_keywords=("JP", "Japan", "Tokyo", "Osaka"),
    ),
    "SG": RegionRule(
        code="SG", name_zh="新加坡", flag="🇸🇬",
        cn_keywords=("新加坡", "狮城"), en_keywords=("SG", "Singapore"),
    ),
    "US": RegionRule(
        code="US", name_zh="美国", flag="🇺🇸",
        # docs/02 §1 所列全部关键词 + 常见补充（美利坚/USA/城市英文等）
        cn_keywords=("美国", "美利坚", "洛杉矶", "圣何塞", "硅谷", "西雅图",
                     "凤凰城", "纽约", "芝加哥", "达拉斯"),
        en_keywords=("US", "USA", "United States", "America", "DMIT",
                     "Los Angeles", "San Jose", "Seattle", "Phoenix",
                     "New York", "NYC", "Chicago", "Dallas", "Silicon Valley"),
    ),
    "KR": RegionRule(
        code="KR", name_zh="韩国", flag="🇰🇷",
        cn_keywords=("韩国", "首爾", "首尔"), en_keywords=("KR", "Korea", "Seoul"),
    ),
    "GB": RegionRule(
        code="GB", name_zh="英国", flag="🇬🇧",
        cn_keywords=("英国", "倫敦", "伦敦", "英伦"),
        en_keywords=("UK", "GB", "United Kingdom", "Britain", "London"),
    ),
    "DE": RegionRule(
        code="DE", name_zh="德国", flag="🇩🇪",
        cn_keywords=("德国", "法蘭克福", "法兰克福", "柏林", "慕尼黑"),
        en_keywords=("DE", "Germany", "Frankfurt", "Berlin"),
    ),
}


def match_region(name: str | None) -> RegionRule | None:
    """对节点名做三级匹配：国旗 emoji → 中文关键词 → 英文关键词/缩写。

    每一级按 REGION_RULES 声明顺序逐规则检查，返回首个命中的规则；
    全部未命中返回 None（节点归「🌍 其他」）。
    """
    if not name:
        return None
    # 第一级：国旗 emoji（最强信号，跨规则优先）
    for rule in REGION_RULES.values():
        if rule.flag and rule.flag in name:
            return rule
    # 第二级：中文关键词
    for rule in REGION_RULES.values():
        for kw in rule.cn_keywords:
            if kw in name:
                return rule
    # 第三级：英文关键词/缩写（大小写不敏感 + 词边界）
    for rule in REGION_RULES.values():
        en = rule._en_pattern
        if en is None:  # UI 运行期构造的规则未预编译时兜底
            en = rule._en_pattern = _compile_en_fallback(rule)
        if en.search(name):
            return rule
    return None


def _compile_en_fallback(rule: RegionRule) -> re.Pattern[str]:
    parts = [_en_part(kw) for kw in rule.en_keywords if kw and kw.strip()]
    return re.compile("|".join(parts), re.IGNORECASE) if parts else _NEVER_MATCH


def region_group_name(code: str | None) -> str:
    """地区组组名（templater 用）：US → 「🇺🇸 美国」；未识别 → 「🌍 其他」。"""
    if code is None:
        return OTHER_REGION_LABEL
    rule = REGION_RULES.get(code)
    if rule is None:
        return OTHER_REGION_LABEL
    return f"{rule.flag} {rule.name_zh}".strip()
