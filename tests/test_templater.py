"""templater + validator 模块测试。

覆盖（docs/02 §2~§5）：
- 组名齐全、地区组按需生成、测速参数数值、Claude 专用组排序（静态 + 纯净度）；
- 规则链顺序、离线版无外链、双格式组集合一致；
- validator：合法产物通过、篡改产物拒绝、publish 版本目录与 diff/清理。

节点准备说明：parser/cleaner 属其他模块，这里用 fixture 文件 + 极简分类
（仅覆盖 fixture 命名）构造等价的合并清洗后节点清单；数据目录经 conftest
的 data_dir fixture 隔离到 tmp_path，绝不触碰仓库根的 data/。
"""
from __future__ import annotations

import re
from dataclasses import replace
from pathlib import Path

import pytest
import yaml

from app.config import AppConfig
from app.models import Node, PurityResult
from app.templater import (
    FIXED_GROUP_ORDER,
    G_AI,
    G_APPLE,
    G_AUTO,
    G_CLAUDE,
    G_CLAUDE_BACKUP,
    G_COPILOT,
    G_DISNEY,
    G_FINAL,
    G_GAME,
    G_GEMINI,
    G_GOOGLE,
    G_MAIN,
    G_MICROSOFT,
    G_NETFLIX,
    G_OPENAI,
    G_SPOTIFY,
    G_TG,
    G_US,
    G_YOUTUBE,
    OTHER_REGION_GROUP,
    PROBE_URL,
    LAN_GUARD_RULES,
    RULE_PROVIDER_INTERVAL,
    RenderResult,
    build_groups,
    load_rules_manifest,
    order_claude_candidates,
    region_group_name,
    render_all,
    render_clash,
    render_sr_conf,
)
from app.utils import content_hash, list_versions, read_json, version_dir
from app.validator import (
    ARTIFACT_KEYS,
    PublishError,
    check_consistency,
    publish,
    validate_clash_yaml,
    validate_sr_conf,
)

# ---------------------------------------------------------------- 节点构造辅助

_REGION_KEYWORDS: tuple[tuple[str, str], ...] = (
    ("美国", "US"), ("香港", "HK"), ("台湾", "TW"), ("日本", "JP"),
    ("新加坡", "SG"), ("韩国", "KR"), ("英国", "GB"), ("德国", "DE"),
)
_FAKE_KEYWORDS = ("剩余流量", "官网", "套餐到期")


def _classify_like_cleaner(nodes: list[Node]) -> list[Node]:
    """测试用极简分类：地区/家宽/专线/倍率/假节点标记（仅覆盖 fixture 命名）。"""
    for node in nodes:
        for keyword, code in _REGION_KEYWORDS:
            if keyword in node.name:
                node.region = code
                break
        if "家庭" in node.name:
            node.residential = True
        if "专线" in node.name:
            node.iplc = True
        matched = re.search(r"x(\d)", node.name)
        if matched:
            node.rate = float(matched.group(1))
        if any(keyword in node.name for keyword in _FAKE_KEYWORDS):
            node.filtered = True
            node.filter_reason = "命中假节点黑名单"
    return nodes


def _load_fixture_nodes(path: Path, sub_name: str) -> list[Node]:
    doc = yaml.safe_load(path.read_text(encoding="utf-8"))
    return _classify_like_cleaner(
        [Node.from_clash_proxy(proxy, sub_name) for proxy in doc["proxies"]]
    )


def _merge_disambiguate(groups: list[list[Node]]) -> list[Node]:
    """跨订阅同名：后者改名为「原名 [订阅别名]」（与 cleaner 契约一致）。"""
    merged: list[Node] = []
    seen: set[str] = set()
    for group in groups:
        for node in group:
            if node.name in seen:
                node.orig_name = node.name
                node.name = f"{node.name} [{node.source_sub}]"
            seen.add(node.name)
            merged.append(node)
    return merged


@pytest.fixture()
def nodes(sub_a_path: Path, sub_b_path: Path) -> list[Node]:
    """合并清洗后的全量节点：真实 15 个（被滤 3 个不参与渲染）。"""
    return _merge_disambiguate([
        _load_fixture_nodes(sub_a_path, "a"),
        _load_fixture_nodes(sub_b_path, "b"),
    ])


@pytest.fixture()
def rules():
    return load_rules_manifest()


@pytest.fixture()
def rule_cache(config: AppConfig) -> AppConfig:
    """模拟 rulesync 缓存（覆盖 yaml payload / .list / 裸 CIDR 三种形态）。"""
    (config.rules_dir / "claude-extra.yaml").write_text(
        "payload:\n"
        "  - api.anthropic.com\n"
        "  - claude.ai\n",
        encoding="utf-8",
    )
    (config.rules_dir / "Telegram.list").write_text(
        "# 上游注释行\n"
        "DOMAIN-SUFFIX,telegram.org\n",
        encoding="utf-8",
    )
    (config.rules_dir / "CNCIDR.list").write_text(
        "1.0.1.0/24\n"
        "1.0.2.0/24\n",
        encoding="utf-8",
    )
    return config


@pytest.fixture()
def rendered(nodes: list[Node], config: AppConfig, rules, rule_cache: AppConfig) -> RenderResult:
    return render_all(nodes, config=config, rules=rules)


def _artifacts(result: RenderResult) -> dict[str, str]:
    return {
        "clash.yaml": result.clash_yaml,
        "shadowrocket.conf": result.sr_conf,
        "shadowrocket.yaml": result.sr_yaml,
        "clash-offline.yaml": result.clash_offline_yaml,
        "shadowrocket-offline.conf": result.sr_offline_conf,
    }


def _clash_doc(text: str) -> dict:
    doc = yaml.safe_load(text)
    assert isinstance(doc, dict)
    return doc


def _sr_section(text: str, section: str) -> list[str]:
    lines: list[str] = []
    current = None
    for raw in text.splitlines():
        line = raw.strip()
        if line.startswith("[") and line.endswith("]"):
            current = line[1:-1]
            continue
        if current == section and line:
            lines.append(line)
    return lines


# ---------------------------------------------------------------- 清单与分组

def test_load_rules_manifest_order_and_policy():
    entries = load_rules_manifest()
    assert entries[0].name == "claude-extra"
    assert entries[0].policy == "🛑 Claude 专用"
    assert entries[-1].name == "Download"
    assert len(entries) == 29
    by_name = {e.name: e for e in entries}
    assert by_name["Claude"].policy == "🛑 Claude 专用"
    assert by_name["OpenAI"].policy == "🎁 OpenAI"
    assert by_name["Grok"].policy == "🧠 通用 AI"
    assert by_name["TikTok"].policy == "🚀 节点选择"
    assert by_name["GitHub"].policy == "📢 谷歌服务"
    assert by_name["CNCIDR"].policy == "DIRECT"


def test_build_groups_names_complete(nodes: list[Node], config: AppConfig):
    groups = build_groups(nodes, config=config)
    assert isinstance(groups, list) and all(isinstance(g, dict) for g in groups)
    names = [g["name"] for g in groups]
    # 19 个固定组 + 8 个地区组 + 🌍 其他 = 28，顺序符合 docs/02 §2 组清单
    region_names = [region_group_name(c) for c in ("HK", "TW", "JP", "SG", "US", "KR", "GB", "DE")]
    expected = [G_MAIN, G_AUTO, *region_names, OTHER_REGION_GROUP, *FIXED_GROUP_ORDER[2:]]
    assert names == expected
    assert len(names) == 28
    types = {g["name"]: g["type"] for g in groups}
    assert types[G_MAIN] == "select"
    assert types[G_AUTO] == "url-test"
    assert types[G_CLAUDE] == "select"
    assert types[G_CLAUDE_BACKUP] == "fallback"
    assert types[G_OPENAI] == types[G_GEMINI] == types[G_COPILOT] == "url-test"
    assert types[G_TG] == "url-test"
    assert types[G_NETFLIX] == types[G_DISNEY] == types[G_YOUTUBE] == types[G_SPOTIFY] == "select"
    assert types[G_GOOGLE] == types[G_MICROSOFT] == types[G_APPLE] == "select"
    assert types[G_GAME] == "select"
    assert types[G_FINAL] == "select"


def test_region_groups_generated_on_demand(config: AppConfig, sub_a_path: Path):
    nodes = [n for n in _load_fixture_nodes(sub_a_path, "a")
             if not n.filtered and n.region != "KR"]  # 剔除韩国节点
    names = [g["name"] for g in build_groups(nodes, config=config)]
    assert region_group_name("KR") not in names       # 无节点的地区不生成
    assert region_group_name("US") in names
    assert OTHER_REGION_GROUP in names                # Premium Node 01 无地区特征
    # 其他组的成员含未识别地区节点，不会丢失
    other = next(g for g in build_groups(nodes, config=config) if g["name"] == OTHER_REGION_GROUP)
    assert "Premium Node 01" in other["proxies"]


def test_unknown_region_code_gets_group(config: AppConfig):
    node = Node(name="🇫🇷 法国 01", type="vless", server="fr-01.example.com", port=443,
                source_sub="c", region="FR")
    groups = build_groups([node], config=config)
    names = [g["name"] for g in groups]
    assert "🇫🇷 法国" in names and OTHER_REGION_GROUP not in names


def test_region_group_falls_back_when_no_visible_member(config: AppConfig, sub_a_path: Path):
    """SR 跳过 anytls 后，纯 anytls 地区组保留（成员回退 ♻️ 常规自动），组集合不缩水。"""
    nodes = _load_fixture_nodes(sub_a_path, "a")
    sg_anytls = next(n for n in nodes if n.region == "SG")
    us = next(n for n in nodes if n.region == "US")
    sr_visible = [us]                      # SG 节点被跳过
    groups = build_groups(sr_visible, config=config, region_presence_nodes=[us, sg_anytls])
    sg_group = next(g for g in groups if g["name"] == region_group_name("SG"))
    assert sg_group["proxies"] == [G_AUTO]


def test_url_test_params_uniform(nodes: list[Node], config: AppConfig):
    groups = {g["name"]: g for g in build_groups(nodes, config=config)}
    for name in (G_AUTO, region_group_name("US"), G_US, G_OPENAI, G_TG):
        group = groups[name]
        assert group["url"] == PROBE_URL == "http://cp.cloudflare.com/generate_204"
        assert group["interval"] == 120
        assert group["tolerance"] == 40
        assert group["max-failed-times"] == 3
        assert group["timeout"] == 3000


def test_claude_backup_fallback_params(nodes: list[Node], config: AppConfig):
    groups = {g["name"]: g for g in build_groups(nodes, config=config)}
    backup = groups[G_CLAUDE_BACKUP]
    assert backup["type"] == "fallback"
    assert backup["interval"] == 90
    assert backup["lazy"] is False
    assert backup["url"] == PROBE_URL
    assert backup["tolerance"] == 40 and backup["max-failed-times"] == 3 and backup["timeout"] == 3000
    # 备援与专用组共享同一候选池
    assert backup["proxies"] == groups[G_CLAUDE]["proxies"]


def test_claude_static_order(nodes: list[Node], config: AppConfig, sub_a_path: Path):
    extra = Node(name="🇭🇰 香港 家宽 01", type="vless", server="hk-home.example.com",
                 port=443, source_sub="c", region="HK", residential=True)
    merged = nodes + [extra]
    ordered = order_claude_candidates([n for n in merged if not n.filtered])
    names = [n.name for n in ordered]
    # 美国家宽（a 在前、同名 b 消歧在后）→ 其他美国（DMIT → 专线x2）→ 港/新家宽 → 其余
    assert names[:4] == [
        "🇺🇸 美国 洛杉矶 家庭宽带 01",
        "🇺🇸 美国 洛杉矶 家庭宽带 01 [b]",
        "🇺🇸 美国 DMIT 高防机房 01",
        "🇺🇸 美国 圣何塞 专线 x2",
    ]
    assert names[4] == "🇭🇰 香港 家宽 01"
    assert "Premium Node 01" in names  # 无地区特征节点保留在池内


def test_claude_purity_order(nodes: list[Node], config: AppConfig):
    purity = [
        PurityResult(node_name="🇺🇸 美国 DMIT 高防机房 01", source_sub="a",
                     checked_at="2026-09-27T04:00:00.000000", claude_rank=3, ip_type="residential"),
        PurityResult(node_name="🇹🇼 台湾 01", source_sub="a",
                     checked_at="2026-09-27T04:00:00.000000", claude_rank=2, ip_type="residential"),
        PurityResult(node_name="🇺🇸 美国 洛杉矶 家庭宽带 01", source_sub="a",
                     checked_at="2026-09-27T04:00:00.000000", claude_rank=2, ip_type="datacenter"),
        PurityResult(node_name="🇰🇷 韩国 首尔 01", source_sub="a",
                     checked_at="2026-09-27T04:00:00.000000", claude_rank=0, ip_type="datacenter"),
    ]
    ordered = [n.name for n in order_claude_candidates(
        [n for n in nodes if not n.filtered], purity)]
    # 评分降序：DMIT(3) → 台湾(2,住宅) → 洛杉矶(2,实测机房，住宅恒在机房前) → 韩国(0)
    assert ordered[:4] == [
        "🇺🇸 美国 DMIT 高防机房 01",
        "🇹🇼 台湾 01",
        "🇺🇸 美国 洛杉矶 家庭宽带 01",
        "🇰🇷 韩国 首尔 01",
    ]
    # 已评分节点排在未评分节点之前（未评分段内部按静态序：美国家宽优先）
    assert ordered[4] == "🇺🇸 美国 洛杉矶 家庭宽带 01 [b]"


def test_claude_purity_lookup_by_disambiguated_name(nodes: list[Node], config: AppConfig):
    """纯净度按「消歧后最终名」关联：被重名消歧改名的节点也能命中自己的检测结果。

    docs/02 §2「有纯净度数据后按评分降序」对消歧节点同样生效（INTERFACES §5.5）。
    """
    # 3 分住宅结果挂在被消歧改名的 [b] 节点上；对照节点是未改名、静态序更靠前的美国家宽
    purity = [
        PurityResult(node_name="🇺🇸 美国 洛杉矶 家庭宽带 01 [b]", source_sub="b",
                     checked_at="2026-09-27T04:00:00.000000", claude_rank=3,
                     ip_type="residential"),
        PurityResult(node_name="🇺🇸 美国 洛杉矶 家庭宽带 01", source_sub="a",
                     checked_at="2026-09-27T04:00:00.000000", claude_rank=1,
                     ip_type="datacenter"),
    ]
    real = [n for n in nodes if not n.filtered]
    ordered = [n.name for n in order_claude_candidates(real, purity)]
    # 实测 3 分住宅（消歧名）压过静态序第一的未改名节点与其 1 分机房实测
    assert ordered[0] == "🇺🇸 美国 洛杉矶 家庭宽带 01 [b]"
    assert ordered[1] == "🇺🇸 美国 洛杉矶 家庭宽带 01"
    # 渲染层同口径：Claude 专用组首位即该消歧节点
    groups = {g["name"]: g for g in build_groups(real, config=config, purity=purity)}
    assert groups[G_CLAUDE]["proxies"][0] == "🇺🇸 美国 洛杉矶 家庭宽带 01 [b]"
    assert groups[G_CLAUDE_BACKUP]["proxies"][0] == "🇺🇸 美国 洛杉矶 家庭宽带 01 [b]"


def test_claude_groups_admit_only_pure_nodes(nodes: list[Node], config: AppConfig):
    """有纯净度数据时只收 claude_rank = 3（住宅级）的节点：2 分机房/未评分/0 分不得入组。"""
    purity = [
        PurityResult(node_name="🇺🇸 美国 洛杉矶 家庭宽带 01", source_sub="a",
                     checked_at="2026-09-27T04:00:00.000000", claude_rank=3,
                     ip_type="residential"),
        PurityResult(node_name="🇺🇸 美国 DMIT 高防机房 01", source_sub="a",
                     checked_at="2026-09-27T04:00:00.000000", claude_rank=2,
                     ip_type="datacenter"),
        PurityResult(node_name="🇰🇷 韩国 首尔 01", source_sub="a",
                     checked_at="2026-09-27T04:00:00.000000", claude_rank=0,
                     ip_type="datacenter"),
    ]
    groups = {g["name"]: g for g in build_groups(nodes, config=config, purity=purity)}
    # 仅 3 分住宅节点入组；2 分机房、0 分韩国、未检测的其余节点全部排除，专用/备援同池
    assert groups[G_CLAUDE]["proxies"] == ["🇺🇸 美国 洛杉矶 家庭宽带 01"]
    assert groups[G_CLAUDE_BACKUP]["proxies"] == ["🇺🇸 美国 洛杉矶 家庭宽带 01"]


def test_claude_groups_full_pool_without_purity(nodes: list[Node], config: AppConfig):
    """无任何合格数据（未扫描/探测不可用）时全量池兜底：mihomo 拒载空组。"""
    groups = {g["name"]: g for g in build_groups(nodes, config=config)}
    real = sorted(n.name for n in nodes if not n.filtered)
    assert sorted(groups[G_CLAUDE]["proxies"]) == real
    assert sorted(groups[G_CLAUDE_BACKUP]["proxies"]) == real


# ---------------------------------------------------------------- 稳定性融合（health.stability_index）

def _stat(name: str, sub: str, *, down_streak: int = 0,
          ok_rate: float = 1.0, avg_delay: int | None = 200) -> dict:
    """构造 health.stability_index 的摘要 dict（hard_down 线 = 连续失败 3 轮）。"""
    return {"node_name": name, "source_sub": sub, "sample_count": 96,
            "ok_rate": ok_rate, "avg_delay": avg_delay,
            "down_streak": down_streak, "hard_down": down_streak >= 3}


def test_url_test_groups_drop_hard_down_nodes(nodes: list[Node], config: AppConfig):
    """窗口内连续失败判死的节点从 url-test 自动选路组剔除；手动 select 主组保留全量。"""
    stability = [_stat("🇭🇰 香港 01", "a", down_streak=3, ok_rate=0.5)]
    groups = {g["name"]: g for g in build_groups(nodes, config=config, stability=stability)}
    assert "🇭🇰 香港 01" not in groups["🇭🇰 香港"]["proxies"]
    assert "🇭🇰 香港 01" not in groups[G_AUTO]["proxies"]
    assert "🇭🇰 香港 01" not in groups[G_TG]["proxies"]
    assert "🇭🇰 香港 IEPL 专线 02" in groups["🇭🇰 香港"]["proxies"]   # 存活同区节点不受影响
    assert "🇭🇰 香港 01" in groups[G_MAIN]["proxies"]                # 手动选择列表不减员


def test_region_group_falls_back_when_all_dead(nodes: list[Node], config: AppConfig):
    """整区节点判死：组不可空置，成员回退 [♻️ 常规自动]（与无美国节点同语义）。"""
    stability = [_stat("🇭🇰 香港 01", "a", down_streak=4, ok_rate=0.2),
                 _stat("🇭🇰 香港 IEPL 专线 02", "a", down_streak=3, ok_rate=0.3)]
    groups = {g["name"]: g for g in build_groups(nodes, config=config, stability=stability)}
    assert groups["🇭🇰 香港"]["proxies"] == [G_AUTO]


def test_claude_order_alive_first_on_same_rank(nodes: list[Node], config: AppConfig):
    """同评分层：存活的 3 分节点默认选中，判死者沉底但保留（准入只看纯净度）。"""
    purity = [
        PurityResult(node_name="🇺🇸 美国 洛杉矶 家庭宽带 01", source_sub="a",
                     checked_at="2026-09-27T04:00:00.000000", claude_rank=3,
                     ip_type="residential"),
        PurityResult(node_name="🇺🇸 美国 洛杉矶 家庭宽带 01 [b]", source_sub="b",
                     checked_at="2026-09-27T04:00:00.000000", claude_rank=3,
                     ip_type="residential"),
    ]
    stability = [_stat("🇺🇸 美国 洛杉矶 家庭宽带 01 [b]", "b",
                       down_streak=3, ok_rate=0.4)]
    real = [n for n in nodes if not n.filtered]
    groups = {g["name"]: g for g in build_groups(
        real, config=config, purity=purity, stability=stability)}
    members = groups[G_CLAUDE]["proxies"]
    assert members[0] == "🇺🇸 美国 洛杉矶 家庭宽带 01"        # 存活者即 select 默认选中
    assert members[-1] == "🇺🇸 美国 洛杉矶 家庭宽带 01 [b]"   # 判死者沉底
    assert groups[G_CLAUDE_BACKUP]["proxies"] == members     # 备援 fallback 同池同序


def test_claude_order_by_success_rate_on_same_rank(nodes: list[Node], config: AppConfig):
    """同评分且都存活：成功率升序决胜（均延为次级），采样数据自动定序。"""
    purity = [
        PurityResult(node_name="🇺🇸 美国 洛杉矶 家庭宽带 01", source_sub="a",
                     checked_at="2026-09-27T04:00:00.000000", claude_rank=3,
                     ip_type="residential"),
        PurityResult(node_name="🇺🇸 美国 洛杉矶 家庭宽带 01 [b]", source_sub="b",
                     checked_at="2026-09-27T04:00:00.000000", claude_rank=3,
                     ip_type="residential"),
    ]
    stability = [_stat("🇺🇸 美国 洛杉矶 家庭宽带 01", "a", ok_rate=0.7, avg_delay=300),
                 _stat("🇺🇸 美国 洛杉矶 家庭宽带 01 [b]", "b", ok_rate=0.95, avg_delay=180)]
    ordered = [n.name for n in order_claude_candidates(
        [n for n in nodes if not n.filtered], purity, stability)]
    assert ordered[0] == "🇺🇸 美国 洛杉矶 家庭宽带 01 [b]"
    assert ordered[1] == "🇺🇸 美国 洛杉矶 家庭宽带 01"


def test_stability_empty_keeps_baseline(nodes: list[Node], config: AppConfig):
    """无采样数据（stability 为空/None）时与不传完全一致：缺数据绝不改变行为。"""
    baseline = {g["name"]: g["proxies"] for g in build_groups(nodes, config=config)}
    for stability in ([], None):
        merged = {g["name"]: g["proxies"]
                  for g in build_groups(nodes, config=config, stability=stability)}
        assert merged == baseline


# ---------------------------------------------------------------- 常规自动：低倍率

def _rate_node(name: str, rate: float) -> Node:
    return Node(name=name, type="vless", server="s.example.com", port=443,
                source_sub="t", region="US", rate=rate)


def test_auto_group_low_rate_only(config: AppConfig):
    """阈值默认 1.0：常规自动只收 ≤1x 节点，高价档供其它专用组。"""
    nodes = [_rate_node("🇺🇸 美国 a x0.5", 0.5), _rate_node("🇺🇸 美国 b", 1.0),
             _rate_node("🇺🇸 美国 c x2", 2.0), _rate_node("🇺🇸 美国 d x3", 3.0)]
    groups = {g["name"]: g for g in build_groups(nodes, config=config)}
    assert groups[G_AUTO]["proxies"] == ["🇺🇸 美国 a x0.5", "🇺🇸 美国 b"]


def test_auto_group_threshold_relaxes_to_min_rate(config: AppConfig):
    """全库无 ≤阈值 节点时阈值放宽到最低倍率档：省流语义始终成立且组非空。"""
    nodes = [_rate_node("🇺🇸 美国 a x2", 2.0), _rate_node("🇺🇸 美国 b x3", 3.0)]
    groups = {g["name"]: g for g in build_groups(nodes, config=config)}
    assert groups[G_AUTO]["proxies"] == ["🇺🇸 美国 a x2"]


def test_auto_group_threshold_configurable(config: AppConfig):
    config = replace(config, auto_max_rate=3.0)
    nodes = [_rate_node("🇺🇸 美国 a x2", 2.0), _rate_node("🇺🇸 美国 b x3", 3.0)]
    groups = {g["name"]: g for g in build_groups(nodes, config=config)}
    assert groups[G_AUTO]["proxies"] == ["🇺🇸 美国 a x2", "🇺🇸 美国 b x3"]


def test_us_groups_fallback_without_us_nodes(config: AppConfig, sub_a_path: Path):
    nodes = [n for n in _load_fixture_nodes(sub_a_path, "a")
             if not n.filtered and n.region != "US"]
    groups = {g["name"]: g for g in build_groups(nodes, config=config)}
    for name in (G_US, G_OPENAI, G_GEMINI, G_COPILOT):
        assert groups[name]["proxies"] == [G_AUTO]  # 无美国节点时回退常规自动，组不空置


def test_ai_streaming_vendor_final_members(nodes: list[Node], config: AppConfig):
    groups = {g["name"]: g for g in build_groups(nodes, config=config)}
    region_names = [region_group_name(c) for c in ("HK", "TW", "JP", "SG", "US", "KR", "GB", "DE")]
    region_names.append(OTHER_REGION_GROUP)
    assert groups[G_AI]["proxies"] == [G_CLAUDE, G_US, G_OPENAI, *region_names]
    for name in (G_NETFLIX, G_DISNEY, G_YOUTUBE, G_SPOTIFY):
        assert groups[name]["proxies"] == region_names
    assert groups[G_GOOGLE]["proxies"][0] == G_AUTO          # 默认 ♻️ 常规自动
    assert groups[G_MICROSOFT]["proxies"][0] == "DIRECT"     # 默认 DIRECT
    assert groups[G_APPLE]["proxies"][0] == "DIRECT"
    assert groups[G_GAME]["proxies"][0] == "DIRECT"
    assert groups[G_FINAL]["proxies"] == ["DIRECT", G_MAIN, G_AUTO]


def test_filtered_nodes_never_rendered(nodes: list[Node], config: AppConfig, rules):
    real = [n for n in nodes if not n.filtered]
    clean = render_all(real, config=config, rules=rules)
    dirty = render_all(nodes, config=config, rules=rules)  # 混入被滤节点 → 防御性剔除
    assert dirty.stats.node_count == clean.stats.node_count == 15
    assert dirty.clash_yaml == clean.clash_yaml
    assert dirty.sr_conf == clean.sr_conf


# ---------------------------------------------------------------- 四份产物与规则链

def test_render_four_artifacts_valid_and_consistent(rendered: RenderResult):
    assert rendered.stats.node_count == 15
    assert rendered.stats.group_count == 28
    assert rendered.stats.sr_skipped_anytls == 2
    assert validate_clash_yaml(rendered.clash_yaml) == []
    assert validate_clash_yaml(rendered.clash_offline_yaml) == []
    assert validate_sr_conf(rendered.sr_conf) == []
    assert validate_sr_conf(rendered.sr_offline_conf) == []
    assert check_consistency(rendered.clash_yaml, rendered.sr_conf) == []


def test_clash_renders_sniffer_for_domainless_traffic(rendered: RenderResult):
    """sniffer 段必须随主/离线版渲染：浏览器 DoH/ECH 自行解析产生的裸 IP 连接
    不带域名，无 SNI/QUIC 嗅探时全部域名规则失效、claude.com 等直接落漏网之鱼
    （2026-09-28 校准）。"""
    for text in (rendered.clash_yaml, rendered.clash_offline_yaml):
        sniffer = _clash_doc(text)["sniffer"]
        assert sniffer["enable"] is True
        assert sniffer["parse-pure-ip"] is True
        assert set(sniffer["sniff"]) == {"HTTP", "TLS", "QUIC"}
        assert sniffer["sniff"]["QUIC"]["ports"] == [443, 8443]


def test_group_sets_equal_between_formats(rendered: RenderResult):
    clash_names = [g["name"] for g in _clash_doc(rendered.clash_yaml)["proxy-groups"]]
    sr_names = []
    for line in _sr_section(rendered.sr_conf, "Proxy Group"):
        sr_names.append(line.split("=", 1)[0].strip())
    assert clash_names == sr_names
    assert len(clash_names) == 28


def test_rule_chain_order_matches_manifest(rendered: RenderResult, rules, config: AppConfig):
    expected = [*LAN_GUARD_RULES, *(f"RULE-SET,{e.name},{e.policy}" for e in rules)]
    expected.append("MATCH,🐟 漏网之鱼")
    doc = _clash_doc(rendered.clash_yaml)
    assert doc["rules"] == expected
    assert doc["rules"][len(LAN_GUARD_RULES)] == "RULE-SET,claude-extra,🛑 Claude 专用"  # 自维护补丁最前
    assert doc["rules"][-1] == "MATCH,🐟 漏网之鱼"
    # SR 主版本规则链同序
    sr_rules = _sr_section(rendered.sr_conf, "Rule")
    assert sr_rules[:len(LAN_GUARD_RULES)] == list(LAN_GUARD_RULES)      # LAN 护栏前置
    assert sr_rules[len(LAN_GUARD_RULES)] == f"RULE-SET,{config.base_url}/rules/claude-extra.list,🛑 Claude 专用"
    assert sr_rules[-1] == "FINAL,🐟 漏网之鱼"
    assert len(sr_rules) == len(expected)


def test_main_versions_reference_nas_rules(rendered: RenderResult, config: AppConfig):
    doc = _clash_doc(rendered.clash_yaml)
    provider = doc["rule-providers"]["claude-extra"]
    # behavior 以 rules_manifest.yaml 声明为准（claude-extra payload 为 TYPE,value
    # classical 行，清单已校准为 classical——mihomo 对 behavior=domain 的
    # 非裸域名 payload 会拒绝加载）
    manifest = {e.name: e for e in load_rules_manifest()}
    assert provider == {
        "type": "http",
        "behavior": manifest["claude-extra"].behavior,
        "url": f"{config.base_url}/rules/claude-extra.yaml",
        "interval": RULE_PROVIDER_INTERVAL,
    }
    assert all(p["type"] == "http" and p["interval"] == RULE_PROVIDER_INTERVAL
               for p in doc["rule-providers"].values())
    assert f"{config.base_url}/rules/" in rendered.sr_conf


def test_offline_versions_have_no_external_links(rendered: RenderResult, config: AppConfig):
    for text in (rendered.clash_offline_yaml, rendered.sr_offline_conf):
        assert config.base_url not in text
        assert "192.168.31.10" not in text
    offline_doc = _clash_doc(rendered.clash_offline_yaml)
    assert all(p["type"] == "inline" for p in offline_doc["rule-providers"].values())
    assert "type: http" not in rendered.clash_offline_yaml
    assert all(not line.startswith("RULE-SET,")
               for line in _sr_section(rendered.sr_offline_conf, "Rule"))
    # 离线版其余完全一致：分组与规则链不变
    main_doc = _clash_doc(rendered.clash_yaml)
    assert offline_doc["rules"] == main_doc["rules"]
    assert [g["name"] for g in offline_doc["proxy-groups"]] == \
           [g["name"] for g in main_doc["proxy-groups"]]


def test_offline_inline_payload_sources(rendered: RenderResult, config: AppConfig):
    """yaml payload / .list / 裸 CIDR 三种缓存形态的内联与展开。"""
    offline_doc = _clash_doc(rendered.clash_offline_yaml)
    providers = offline_doc["rule-providers"]
    assert providers["claude-extra"]["payload"] == ["api.anthropic.com", "claude.ai"]
    assert providers["Telegram"]["payload"] == ["DOMAIN-SUFFIX,telegram.org"]  # 注释行被剔除
    assert providers["CNCIDR"]["payload"] == ["1.0.1.0/24", "1.0.2.0/24"]
    sr_rules = _sr_section(rendered.sr_offline_conf, "Rule")
    # yaml 裸域名 → 自动补 DOMAIN-SUFFIX 前缀再落组
    assert "DOMAIN-SUFFIX,api.anthropic.com,🛑 Claude 专用" in sr_rules
    # .list 规则片段 → 直接追加策略
    assert "DOMAIN-SUFFIX,telegram.org,📲 Telegram" in sr_rules
    # 裸 CIDR → 自动补 IP-CIDR 前缀
    assert "IP-CIDR,1.0.1.0/24,DIRECT" in sr_rules
    # 无缓存的规则集渲染为空 payload 并保持结构合法（告警由日志承担）
    assert providers["ChinaMax"]["payload"] == []
    # manifest 兜底（futu-extra 无缓存）：domains/ips 规范成 TYPE,value 行（classical 硬要求）
    assert providers["futu-extra"]["payload"][:2] == \
        ["DOMAIN-SUFFIX,futu.cn", "DOMAIN-SUFFIX,futu.com"]
    assert "DOMAIN-SUFFIX,futu5.com,DIRECT" in sr_rules


def test_sr_proxy_direct_mapping(rendered: RenderResult, config: AppConfig):
    proxy_lines = _sr_section(rendered.sr_conf, "Proxy")
    assert len(proxy_lines) == 13  # 15 真实节点 - 2 anytls
    vless = next(line for line in proxy_lines if "洛杉矶 家庭宽带 01 =" in line)
    assert " = vless, us-lax-01.example-airport-a.com, 443, " in vless
    assert "username=11111111-1111-4111-8111-111111111101" in vless
    assert "tls=true" in vless and "flow=xtls-rprx-vision" in vless
    # REALITY 握手必需参数（两套方言都输出）；缺公钥的 vless 在 SR 必然超时
    assert "reality-public-key=pbk-us-lax-test" in vless
    assert "reality-short-id=0123abcd" in vless
    assert "public-key=pbk-us-lax-test" in vless and "short-id=0123abcd" in vless
    # 无 reality-opts 的 vless 不得出现空值参数
    plain = next(line for line in proxy_lines if "DMIT 高防机房 01 =" in line)
    assert "public-key" not in plain and "reality-" not in plain
    ss_line = next(line for line in proxy_lines if "韩国 首尔 01 =" in line)
    assert " = ss, kr-01.example-airport-a.com, 8388, " in ss_line
    assert "encrypt-method=aes-256-gcm" in ss_line and "password=" in ss_line
    ws_line = next(line for line in proxy_lines if "圣何塞 专线 x2 =" in line)
    assert "ws=true" in ws_line and "ws-path=/ws" in ws_line and "ws-headers=Host:" in ws_line
    assert not any(" = anytls," in line for line in proxy_lines)  # anytls 已按开关跳过
    assert "anytls 跳过 2 个" in rendered.sr_conf


def test_sr_yaml_preserves_reality_and_inlines_rules(rendered: RenderResult, config: AppConfig):
    """SR YAML 产物：reality-opts 原样保留（conf 方言丢参数的根治方案）、规则全内联。"""
    import yaml as _yaml
    doc = _yaml.safe_load(rendered.sr_yaml)
    assert isinstance(doc, dict)
    vless = next(p for p in doc["proxies"]
                 if p["name"] == "🇺🇸 美国 洛杉矶 家庭宽带 01")
    assert vless["type"] == "vless"
    assert vless["reality-opts"] == {"public-key": "pbk-us-lax-test",
                                     "short-id": "0123abcd"}
    assert vless["flow"] == "xtls-rprx-vision"
    # 禁外链与 mihomo 运行项：单文件自包含，SR 导入即用
    assert "rule-providers" not in doc
    for key in ("mixed-port", "external-controller", "dns", "sniffer"):
        assert key not in doc
    assert not [r for r in doc["rules"] if str(r).startswith("RULE-SET,")]
    assert str(doc["rules"][-1]).startswith("MATCH,")
    # 分组集合与 mihomo 主版本一致
    main = _yaml.safe_load(rendered.clash_yaml)
    assert [g["name"] for g in doc["proxy-groups"]] == \
        [g["name"] for g in main["proxy-groups"]]
    # anytls 同口径跳过
    assert not [p for p in doc["proxies"] if p["type"] == "anytls"]


def test_sr_includes_anytls_when_switch_off(nodes: list[Node], config: AppConfig, rules):
    config_off = replace(config, skip_anytls=False)
    text = render_sr_conf(nodes, config=config_off, rules=rules)
    proxy_lines = _sr_section(text, "Proxy")
    assert sum(1 for line in proxy_lines if " = anytls," in line) == 2
    assert validate_sr_conf(text) == []
    assert check_consistency(render_clash(nodes, config=config_off, rules=rules), text) == []


def test_render_deterministic(nodes: list[Node], config: AppConfig, rules):
    first = render_all(nodes, config=config, rules=rules)
    second = render_all(nodes, config=config, rules=rules)
    assert _artifacts(first) == _artifacts(second)  # 无时间戳，同输入同产物


# ---------------------------------------------------------------- validator

def test_validate_clash_rejects_malformed():
    assert validate_clash_yaml("a: b: [c")  # YAML 语法错误
    assert validate_clash_yaml("foo: bar")  # 缺少全部结构
    text = (
        "mixed-port: 7897\nmode: rule\nunified-delay: true\ntcp-concurrent: true\n"
        "dns:\n  enable: true\n  enhanced-mode: fake-ip\n"
        "  nameserver: [https://doh.pub/dns-query]\n  fake-ip-filter: ['*.lan']\n"
        "proxies:\n  - name: n1\n    type: ss\n    server: s\n    port: 1\n"
        "proxy-groups:\n  - name: g1\n    type: select\n    proxies: [n2, g1x]\n"
        "rule-providers:\n  p1:\n    type: ftp\n    behavior: domain\n"
        "rules:\n  - 'RULE-SET,missing,g1'\n  - 'DIRECT,x'\n"
    )
    errors = validate_clash_yaml(text)
    assert any("external-controller" in e for e in errors)
    assert any("n2" in e for e in errors)       # 未知成员
    assert any("g1x" in e for e in errors)
    assert any("ftp" in e for e in errors)      # 非法 provider 类型
    assert any("missing" in e for e in errors)  # 引用不存在的 provider
    assert any("MATCH" in e for e in errors)    # 缺少 MATCH 兜底
    assert any("sniffer" in e for e in errors)  # 缺少 sniffer 段


def test_validate_sr_rejects_malformed():
    assert validate_sr_conf("没有段落")
    missing_rule = "[Proxy]\nn1 = ss, s.example.com, 8388, encrypt-method=aes-256-gcm, password=x\n"
    assert any("[Rule]" in e for e in validate_sr_conf(missing_rule))
    bad_group = missing_rule + "[Proxy Group]\ng1 = url-test, n1, interval=120\n[Rule]\nFINAL,DIRECT\n"
    errors = validate_sr_conf(bad_group)
    assert any("url=" in e for e in errors)      # 缺少 url=
    assert any("tolerance" in e for e in errors)
    unknown_member = missing_rule + "[Proxy Group]\ng1 = select, ghost\n[Rule]\nFINAL,DIRECT\n"
    assert any("ghost" in e for e in validate_sr_conf(unknown_member))


def test_check_consistency_detects_tampering(rendered: RenderResult):
    assert check_consistency(rendered.clash_yaml, rendered.sr_conf) == []
    renamed = rendered.sr_conf.replace("🎁 OpenAI = url-test", "🎁 开 AI = url-test")
    errors = check_consistency(rendered.clash_yaml, renamed)
    assert any("OpenAI" in e for e in errors)
    dropped = "\n".join(
        line for line in rendered.sr_conf.splitlines() if not line.startswith("RULE-SET,http://192.168.31.10:8399/rules/OpenAI.list")
    ) + "\n"
    assert check_consistency(rendered.clash_yaml, dropped)  # 规则条数不一致
    repolicy = rendered.sr_conf.replace(
        "RULE-SET,http://192.168.31.10:8399/rules/OpenAI.list,🎁 OpenAI",
        "RULE-SET,http://192.168.31.10:8399/rules/OpenAI.list,🐟 漏网之鱼")
    assert check_consistency(rendered.clash_yaml, repolicy)  # 同名不同策略


# ---------------------------------------------------------------- 发布

def test_publish_success_meta_and_hash(nodes: list[Node], config: AppConfig,
                                       rules, store, rendered: RenderResult):
    version = publish(config, store, _artifacts(rendered), nodes)
    assert version.version == 1
    assert version.node_count == 15
    vdir = version_dir(config.out_dir, 1)
    assert sorted(p.name for p in vdir.iterdir()) == sorted([*ARTIFACT_KEYS, "meta.json"])
    meta = read_json(vdir / "meta.json")
    assert meta["node_count"] == 15
    assert meta["note"] is None
    expected_hash = content_hash("".join(_artifacts(rendered)[k] for k in ARTIFACT_KEYS))
    assert meta["content_hash"] == version.content_hash == expected_hash
    # 首版 diff：相对无上一版，全部为新增
    assert version.diff_summary["removed"] == []
    assert len(version.diff_summary["added"]) == 15


def test_publish_diff_and_rename_heuristic(nodes: list[Node], config: AppConfig,
                                           rules, store, rendered: RenderResult):
    publish(config, store, _artifacts(rendered), nodes)
    # 改名场景：德国节点改名（server/port 不变）→ renamed 配对而非 added+removed
    renamed_nodes = list(nodes)
    for n in renamed_nodes:
        if n.name == "🇩🇪 德国 法兰克福 01":
            n.name = "🇩🇪 德国 法兰克福 02"
    result = render_all(renamed_nodes, config=config, rules=rules)
    version = publish(config, store, _artifacts(result), renamed_nodes)
    assert version.version == 2
    pairs = [tuple(pair) for pair in version.diff_summary["renamed"]]
    assert ("🇩🇪 德国 法兰克福 01", "🇩🇪 德国 法兰克福 02") in pairs


def test_publish_rejects_bad_artifacts(nodes: list[Node], config: AppConfig,
                                       rules, store, rendered: RenderResult):
    tampered = _artifacts(rendered)
    tampered["shadowrocket.conf"] = rendered.sr_conf.replace(
        "🎁 OpenAI = url-test", "🎁 开 AI = url-test")
    with pytest.raises(PublishError) as excinfo:
        publish(config, store, tampered, nodes)
    assert excinfo.value.errors
    assert any("不一致" in e or "OpenAI" in e for e in excinfo.value.errors)
    assert list_versions(config.out_dir) == []  # 未产生任何版本目录

    missing = {"clash.yaml": rendered.clash_yaml}
    with pytest.raises(PublishError):
        publish(config, store, missing, nodes)
    with pytest.raises(PublishError):
        publish(config, store, {**missing, "shadowrocket.conf": "",
                                "clash-offline.yaml": "", "shadowrocket-offline.conf": ""}, nodes)


def test_publish_prunes_old_versions(nodes: list[Node], config: AppConfig,
                                     rules, store, rendered: RenderResult):
    artifacts = _artifacts(rendered)
    for i in range(7):
        publish(config, store, artifacts, nodes, note=f"第 {i + 1} 次发布")
    versions = list_versions(config.out_dir)
    assert versions == [3, 4, 5, 6, 7]  # 只保留最近 5 版
    meta = read_json(version_dir(config.out_dir, 7) / "meta.json")
    assert meta["note"] == "第 7 次发布"
