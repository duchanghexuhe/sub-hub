"""cleaner / regions 测试：假节点过滤、地区三级识别、属性标记、重名消歧。

依据 docs/02 §1 与 INTERFACES.md §3.3：
- fixture：tests/fixtures/sub_a.yaml（12 真实 + 2 假）、sub_b.yaml（3 真实 + 1 假，
  其中「美国 洛杉矶 家庭宽带 01」与 sub_a 同名）；
- parser 模块未就绪，直接用 yaml + Node.from_clash_proxy 构造统一节点模型；
- 数据纪律：只读 conftest 提供的 fixture 文件，写库走 conftest 的临时目录
  store fixture，绝不读写仓库根 data/。
"""
from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from app.cleaner import (
    FAKE_NODE_PATTERNS,
    REGION_RULES,
    CleanResult,
    classify,
    clean,
    disambiguate,
    filter_fake_nodes,
)
from app.models import Node
from app.regions import OTHER_REGION_LABEL, RegionRule, match_region, region_group_name


# ---------------------------------------------------------------- 工具

def load_nodes(path: Path, source_sub: str) -> list[Node]:
    payload = yaml.safe_load(path.read_text(encoding="utf-8"))
    return [Node.from_clash_proxy(item, source_sub) for item in payload["proxies"]]


def merged_nodes(sub_a_path: Path, sub_b_path: Path) -> list[Node]:
    """pipeline 合并语义：两个订阅的节点拼接为全量列表。"""
    return load_nodes(sub_a_path, "sub_a") + load_nodes(sub_b_path, "sub_b")


def make_node(name: str, source_sub: str = "t") -> Node:
    return Node(name=name, type="vless", server="s.example.com", port=443,
                source_sub=source_sub)


# ---------------------------------------------------------------- 假节点过滤

def test_sub_a_fake_nodes_all_filtered(sub_a_path: Path) -> None:
    nodes = load_nodes(sub_a_path, "sub_a")
    assert len(nodes) == 14  # 12 真实 + 2 假（fixture 约定）
    result = clean(nodes)
    assert len(result.kept) == 12
    assert len(result.filtered) == 2
    assert {n.name for n in result.filtered} == {
        "剩余流量：200GB",
        "官网：example-airport-a.com 请续费",
    }


def test_sub_b_fake_node_filtered(sub_b_path: Path) -> None:
    nodes = load_nodes(sub_b_path, "sub_b")
    result = clean(nodes)
    assert len(result.filtered) == 1
    assert result.filtered[0].name == "套餐到期：2026-12-31"
    assert result.filtered[0].filter_reason == "套餐到期"


def test_merged_clean_counts(sub_a_path: Path, sub_b_path: Path) -> None:
    nodes = merged_nodes(sub_a_path, sub_b_path)
    assert len(nodes) == 18
    result = clean(nodes)
    assert isinstance(result, CleanResult)
    assert len(result.kept) == 15
    assert len(result.filtered) == 3
    assert {n.source_sub for n in result.filtered} == {"sub_a", "sub_b"}


def test_filtered_nodes_marked_with_reason_not_dropped(
    sub_a_path: Path, sub_b_path: Path
) -> None:
    """命中者必须带 filtered 标记与原因返回（不丢弃，可查询防误杀）。"""
    result = clean(merged_nodes(sub_a_path, sub_b_path))
    for node in result.filtered:
        assert node.filtered is True
        assert node.filter_reason
        assert node.filter_reason in node.name
    for node in result.kept:
        assert node.filtered is False
        assert node.filter_reason is None


def test_fake_keywords_case_insensitive() -> None:
    names = ["EXPIRE: 2026-12-31", "Traffic Info", "Tg 频道"]
    nodes = [make_node(n) for n in names]
    kept = filter_fake_nodes(nodes)
    assert kept == []
    assert {n.name for n in nodes if n.filtered} == set(names)
    assert all(n.filter_reason for n in nodes)


def test_fake_keywords_emoji_tolerant() -> None:
    """表情装饰不阻断命中（子串匹配，节点名里 emoji 夹带仍被滤）。"""
    names = ["🔔 剩余流量：100GB", "📢 官网 请续费", "⏰ 到期时间：明天", "🈲 流量重置"]
    nodes = [make_node(n) for n in names]
    kept = filter_fake_nodes(nodes)
    assert kept == []
    assert len(FAKE_NODE_PATTERNS) == len(
        ("剩余流量", "套餐到期", "到期时间", "重置", "官网", "官址", "网址", "续费",
         "订阅", "流量", "expire", "traffic", "电报", "频道", "群", "tg", "telegram")
    )  # 黑名单与 docs/02 §1 原文一一对应


def test_blacklist_matches_docs_wording() -> None:
    """黑名单关键词照 docs/02 §1 原文，一个不少。"""
    docs_keywords = ["剩余流量", "套餐到期", "到期时间", "重置", "官网", "官址",
                     "网址", "续费", "订阅", "流量", "expire", "traffic",
                     "电报", "频道", "群", "tg", "telegram"]
    for kw in docs_keywords:
        assert any(p.search(kw) for p in FAKE_NODE_PATTERNS), kw


# ---------------------------------------------------------------- 地区识别

# sub_a 全部 12 个真实节点的期望地区（Premium Node 01 无地区特征 → None）
_SUB_A_EXPECTED_REGIONS = {
    "🇺🇸 美国 洛杉矶 家庭宽带 01": "US",
    "🇺🇸 美国 DMIT 高防机房 01": "US",
    "🇺🇸 美国 圣何塞 专线 x2": "US",
    "🇭🇰 香港 01": "HK",
    "🇭🇰 香港 IEPL 专线 02": "HK",
    "🇹🇼 台湾 01": "TW",
    "🇯🇵 日本 东京 01": "JP",
    "🇸🇬 新加坡 01": "SG",
    "🇰🇷 韩国 首尔 01": "KR",
    "🇬🇧 英国 伦敦 01": "GB",
    "🇩🇪 德国 法兰克福 01": "DE",
    "Premium Node 01": None,
}


def test_region_classification_on_sub_a(sub_a_path: Path) -> None:
    nodes = load_nodes(sub_a_path, "sub_a")
    kept = classify(filter_fake_nodes(nodes))
    by_name = {n.name: n for n in kept}
    assert set(by_name) == set(_SUB_A_EXPECTED_REGIONS)
    for name, code in _SUB_A_EXPECTED_REGIONS.items():
        assert by_name[name].region == code, f"{name} 应归 {code}"


def test_unknown_region_goes_to_other_group() -> None:
    """无地区特征的节点 region=None，归属组名为「🌍 其他」（不丢失）。"""
    assert OTHER_REGION_LABEL == "🌍 其他"
    node = make_node("Premium Node 01")
    classify([node])
    assert node.region is None
    assert region_group_name(None) == "🌍 其他"
    assert region_group_name("US") == "🇺🇸 美国"


def test_region_three_tier_matching() -> None:
    assert match_region("🇯🇵 IEPL 01").code == "JP"        # 一级：国旗 emoji
    assert match_region("东京 01").code == "JP"            # 二级：中文关键词
    assert match_region("Tokyo 02").code == "JP"           # 三级：英文全词
    assert match_region("US West 01").code == "US"         # 三级：缩写
    assert match_region("United States 01").code == "US"   # 三级：英文全名
    assert match_region("洛杉矶 01").code == "US"          # 二级：城市中文


def test_region_abbr_word_boundary_no_false_positive() -> None:
    """缩写带词边界：RUS 不命中 US、200GB 不命中 GB、Node 不命中 DE。"""
    assert match_region("RUS 01") is None
    assert match_region("节点 200GB") is None
    assert match_region("Premium Node 01") is None


def test_region_rules_cover_required_codes() -> None:
    """港台日新美韩英德全覆盖，顺序即 docs/02 组清单顺序。"""
    assert list(REGION_RULES) == ["HK", "TW", "JP", "SG", "US", "KR", "GB", "DE"]
    for code, rule in REGION_RULES.items():
        assert rule.code == code
        assert rule.flag and rule.name_zh and rule.pattern is not None


def test_us_rule_covers_all_doc_keywords() -> None:
    """美国规则覆盖 docs/02 §1 所列全部关键词。"""
    doc_keywords = ["🇺🇸", "美国", "US", "United States", "洛杉矶", "圣何塞", "硅谷",
                    "西雅图", "凤凰城", "纽约", "芝加哥", "达拉斯", "DMIT",
                    "Los Angeles", "San Jose"]
    pattern = REGION_RULES["US"].pattern
    for kw in doc_keywords:
        assert pattern.search(f"节点 {kw} 01"), kw


def test_region_rules_extensible_at_runtime() -> None:
    """映射表为普通 dict：UI 运行期追加 RegionRule 即生效，无需改代码。"""
    added = RegionRule(code="ZZ", name_zh="测试地区", flag="🇿🇿",
                       cn_keywords=("测试地",), en_keywords=("ZZ",))
    REGION_RULES["ZZ"] = added
    try:
        assert match_region("🇿🇿 测试地 01").code == "ZZ"
        assert match_region("zz-01").code == "ZZ"
    finally:
        del REGION_RULES["ZZ"]  # 还原，避免影响其他用例
    assert match_region("🇿🇿 测试地 01") is None


# ---------------------------------------------------------------- 属性标记

def test_attributes_on_sub_a(sub_a_path: Path) -> None:
    """家宽/专线/倍率标记与 fixture 命名一一对应。"""
    nodes = load_nodes(sub_a_path, "sub_a")
    kept = classify(filter_fake_nodes(nodes))
    by_name = {n.name: n for n in kept}

    lax = by_name["🇺🇸 美国 洛杉矶 家庭宽带 01"]     # 家宽
    assert (lax.residential, lax.iplc, lax.rate) == (True, False, 1.0)
    dmit = by_name["🇺🇸 美国 DMIT 高防机房 01"]      # 机房，非家宽
    assert (dmit.residential, dmit.iplc, dmit.rate) == (False, False, 1.0)
    sjc = by_name["🇺🇸 美国 圣何塞 专线 x2"]         # 专线 + x2 倍率
    assert (sjc.residential, sjc.iplc, sjc.rate) == (False, True, 2.0)
    hk_iepl = by_name["🇭🇰 香港 IEPL 专线 02"]       # IEPL 亦算专线
    assert (hk_iepl.residential, hk_iepl.iplc, hk_iepl.rate) == (False, True, 1.0)
    tw = by_name["🇹🇼 台湾 01"]                      # 普通节点默认值
    assert (tw.residential, tw.iplc, tw.rate) == (False, False, 1.0)


def test_sub_b_japan_residential(sub_b_path: Path) -> None:
    nodes = load_nodes(sub_b_path, "sub_b")
    kept = classify(filter_fake_nodes(nodes))
    osaka = next(n for n in kept if "大阪" in n.name)
    assert osaka.region == "JP"
    assert osaka.residential is True


@pytest.mark.parametrize(
    "name,expected",
    [
        ("🇺🇸 美国 x2", 2.0),
        ("🇺🇸 美国 x3", 3.0),
        ("🇺🇸 美国 2x", 2.0),
        ("🇺🇸 美国 3x", 3.0),
        ("🇺🇸 美国 x2.5", 2.5),
        ("🇺🇸 美国 倍率3x", 3.0),
        ("🇺🇸 美国 高倍 x10", 10.0),
        ("🇺🇸 美国 普通节点", 1.0),
        ("🇺🇸 美国 MAX 01", 1.0),   # 字母串里的 x 不误判
        ("🇺🇸 美国 ax3 节点", 1.0),  # 紧贴字母的倍率不误判
    ],
)
def test_rate_parsing(name: str, expected: float) -> None:
    node = make_node(name)
    classify([node])
    assert node.rate == expected


@pytest.mark.parametrize(
    "name",
    ["US Home 01", "ISP 住宅 02", "家庭宽带", "家宽节点", "Home Broadband"],
)
def test_residential_keywords(name: str) -> None:
    node = make_node(name)
    classify([node])
    assert node.residential is True, name


@pytest.mark.parametrize(
    "name,expected",
    [("专线 01", True), ("IEPL", True), ("IPLC 02", True), ("普通节点", False)],
)
def test_iplc_keywords(name: str, expected: bool) -> None:
    node = make_node(name)
    classify([node])
    assert node.iplc is expected


# ---------------------------------------------------------------- 重名消歧

def test_cross_sub_duplicate_gets_suffix(sub_a_path: Path, sub_b_path: Path) -> None:
    """跨订阅同名节点改名「原名 [订阅短代号]」，orig_name 记原名，key 稳定。"""
    original = "🇺🇸 美国 洛杉矶 家庭宽带 01"
    result = clean(merged_nodes(sub_a_path, sub_b_path))
    renamed = [n for n in result.kept if (n.orig_name or n.name) == original]
    assert len(renamed) == 2
    by_sub = {n.source_sub: n for n in renamed}
    assert by_sub["sub_a"].name == f"{original} [sub_a]"
    assert by_sub["sub_b"].name == f"{original} [sub_b]"
    for node in renamed:
        assert node.orig_name == original
        assert node.key == (node.source_sub, original)  # 纯净度结果键不受改名影响
        assert node.region == "US" and node.residential is True  # 分类先于消歧


def test_unique_nodes_not_renamed(sub_a_path: Path, sub_b_path: Path) -> None:
    """无跨订阅冲突的名字保持原样（新加坡 01/02 名字不同不算重名）。"""
    result = clean(merged_nodes(sub_a_path, sub_b_path))
    for node in result.kept:
        if node.name in ("🇸🇬 新加坡 01", "🇸🇬 新加坡 02", "🇯🇵 日本 大阪 家庭宽带 02"):
            assert node.orig_name is None
            assert "[" not in node.name


def test_disambiguate_idempotent() -> None:
    nodes = [make_node("dup", "a"), make_node("dup", "b"), make_node("only", "a")]
    once = disambiguate(nodes)
    names_once = [n.name for n in once]
    assert names_once == ["dup [a]", "dup [b]", "only"]
    twice = disambiguate(once)
    assert [n.name for n in twice] == names_once


# ---------------------------------------------------------------- 落库可查询

def test_filtered_nodes_queryable_via_store(store, sub_a_path: Path) -> None:
    """pipeline 语义（全量落库，含被滤节点）后可按 filtered 查询。"""
    nodes = load_nodes(sub_a_path, "sub_a")
    result = clean(nodes)
    store.replace_nodes("sub_a", nodes)  # 全量（12 真实 + 2 假）
    assert len(store.list_nodes()) == 14
    filtered = store.list_nodes(filtered=True)
    assert {n.name for n in filtered} == {n.name for n in result.filtered}
    assert all(n.filter_reason for n in filtered)
    assert len(store.list_nodes(filtered=False)) == 12
