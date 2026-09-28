"""模板渲染引擎：分组计算 + mihomo YAML / Shadowrocket conf 双格式渲染。

docs/02 §2~§5 的实现：
- 组清单与语义（🛑 Claude 专用防封号核心、地区组按需生成、AI/流媒体/大厂分组）；
- 所有 url-test/fallback 组统一测速参数（interval 120 / 备援 90、tolerance 40、
  max-failed-times 3、timeout 3000、探活 http://cp.cloudflare.com/generate_204）；
- 主版本 rule-providers / RULE-SET 全部指向 NAS（读 AppConfig.base_url）；
- 离线自包含版规则内联（mihomo type: inline + payload；SR 把 .list 展开进 [Rule]）。

公开签名以 docs/INTERFACES.md §3.4 为准；另按模块要求补充：
- build_groups/render_* 增加可选关键字参数 purity（纯净度结果列表，可为空），
  用于 Claude 专用组「评分降序、住宅恒在机房前」的排序；
- render_all() 便捷入口：一次产出四份产物文本与统计（组数/节点数/SR 跳过 anytls 数）。

安全纪律：日志只输出计数、组名与节点名，绝不打印订阅 URL 与节点凭据字段。
产物内容确定性强（不含时间戳），同一节点集重复渲染结果一致。
"""
from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml
from jinja2 import Environment, FileSystemLoader

from app.config import AppConfig
from app.models import Node, PurityResult

logger = logging.getLogger("subhub.templater")

# ---------------------------------------------------------------- 常量（docs/02 §2/§3/§4）

PROBE_URL = "http://cp.cloudflare.com/generate_204"  # 探活 URL（cloudflare 204）
HEALTH_INTERVAL = 120          # url-test/fallback 统一间隔（秒）
CLAUDE_BACKUP_INTERVAL = 90    # 🧷 Claude 备援间隔（秒），lazy=false
HEALTH_TOLERANCE = 40          # 防延迟抖动来回切换（ms）
HEALTH_MAX_FAILED = 3          # 连续失败即标记不可用
HEALTH_TIMEOUT = 3000          # 快速判死（ms）
RULE_PROVIDER_INTERVAL = 86400  # rule-providers 更新间隔（秒）

# 固定组名（地区组之外的全部组，顺序即 proxy-groups 中的出现顺序）
G_MAIN = "🚀 节点选择"
G_AUTO = "♻️ 常规自动"
G_US = "⛳ 美国优质"
G_CLAUDE = "🛑 Claude 专用"
G_CLAUDE_BACKUP = "🧷 Claude 备援"
G_OPENAI = "🎁 OpenAI"
G_GEMINI = "🤖 Gemini"
G_COPILOT = "🐙 Copilot"
G_AI = "🧠 通用 AI"
G_TG = "📲 Telegram"
G_NETFLIX = "🎬 Netflix"
G_DISNEY = "🏰 Disney+"
G_YOUTUBE = "📺 YouTube"
G_SPOTIFY = "🎵 Spotify"
G_GOOGLE = "📢 谷歌服务"
G_MICROSOFT = "Ⓜ️ 微软服务"
G_APPLE = "🍎 苹果服务"
G_GAME = "🎮 游戏平台"
G_FINAL = "🐟 漏网之鱼"

FIXED_GROUP_ORDER: tuple[str, ...] = (
    G_MAIN, G_AUTO, G_US, G_CLAUDE, G_CLAUDE_BACKUP,
    G_OPENAI, G_GEMINI, G_COPILOT, G_AI, G_TG,
    G_NETFLIX, G_DISNEY, G_YOUTUBE, G_SPOTIFY,
    G_GOOGLE, G_MICROSOFT, G_APPLE, G_GAME, G_FINAL,
)

# 未识别地区节点的归属组（docs/02 §1：不会丢失）
OTHER_REGION_GROUP = "🌍 其他"
# 地区组展示名（code → f"{emoji} {中文名}"）；顺序决定组顺序（docs/02 §2 组清单顺序）
REGION_GROUP_META: dict[str, tuple[str, str]] = {
    "HK": ("🇭🇰", "香港"), "TW": ("🇹🇼", "台湾"), "JP": ("🇯🇵", "日本"),
    "SG": ("🇸🇬", "新加坡"), "US": ("🇺🇸", "美国"), "KR": ("🇰🇷", "韩国"),
    "GB": ("🇬🇧", "英国"), "DE": ("🇩🇪", "德国"),
    # 常见扩展地区（cleaner 识别出即自动成组）
    "FR": ("🇫🇷", "法国"), "RU": ("🇷🇺", "俄罗斯"), "IN": ("🇮🇳", "印度"),
    "CA": ("🇨🇦", "加拿大"), "AU": ("🇦🇺", "澳大利亚"), "NL": ("🇳🇱", "荷兰"),
    "TR": ("🇹🇷", "土耳其"), "BR": ("🇧🇷", "巴西"), "AR": ("🇦🇷", "阿根廷"),
    "VN": ("🇻🇳", "越南"), "TH": ("🇹🇭", "泰国"), "PH": ("🇵🇭", "菲律宾"),
    "MY": ("🇲🇾", "马来西亚"), "ID": ("🇮🇩", "印尼"), "IT": ("🇮🇹", "意大利"),
    "ES": ("🇪🇸", "西班牙"), "CH": ("🇨🇭", "瑞士"), "SE": ("🇸🇪", "瑞典"),
    "PL": ("🇵🇱", "波兰"), "UA": ("🇺🇦", "乌克兰"), "AE": ("🇦🇪", "阿联酋"),
    "MX": ("🇲🇽", "墨西哥"), "IL": ("🇮🇱", "以色列"),
}
REGION_CANONICAL_ORDER: tuple[str, ...] = ("HK", "TW", "JP", "SG", "US", "KR", "GB", "DE")

# 组上的 filter 正则（对显式 proxies 成员仅作语义标注；成员清单以 Python 计算为准）。
# 美国正则取 docs/02 §2 模板示意原文。
US_FILTER = r"(?i)美|US|United States|🇺🇸|DMIT|洛杉矶|圣何塞|硅谷"
REGION_FILTERS: dict[str, str] = {
    "HK": r"(?i)港|HK|Hong ?Kong|🇭🇰",
    "TW": r"(?i)台|TW|Taiwan|🇹🇼",
    "JP": r"(?i)日本|JP|Japan|东京|大阪|🇯🇵",
    "SG": r"(?i)新加坡|狮城|SG|Singapore|🇸🇬",
    "US": US_FILTER,
    "KR": r"(?i)韩|KR|Korea|首尔|🇰🇷",
    "GB": r"(?i)英|UK|GB|United Kingdom|伦敦|🇬🇧",
    "DE": r"(?i)德|DE|Germany|法兰克福|🇩🇪",
}

DEFAULT_MANIFEST_PATH = Path(__file__).resolve().parent.parent / "rules_manifest.yaml"
_TEMPLATES_DIR = Path(__file__).resolve().parent / "templates"

_env = Environment(
    loader=FileSystemLoader(str(_TEMPLATES_DIR)),
    autoescape=False,
    trim_blocks=True,
    lstrip_blocks=True,
    keep_trailing_newline=True,
)


# ---------------------------------------------------------------- 数据结构与清单

@dataclass(frozen=True)
class RuleEntry:
    """rules_manifest.yaml 单项的内存形态（docs/INTERFACES.md §3.4）。"""

    name: str
    category: str
    policy: str          # 规则链中该规则集的出口组名或 DIRECT
    behavior: str        # domain / ipcidr / classical
    clash_file: str      # mihomo 缓存文件名（<name>.yaml）
    sr_file: str         # SR 缓存文件名（<name>.list）


def load_rules_manifest(path: Path | None = None) -> list[RuleEntry]:
    """读取规则清单，按文件顺序返回（顺序即规则链顺序，不得重排）。"""
    manifest_path = Path(path) if path else DEFAULT_MANIFEST_PATH
    doc = yaml.safe_load(manifest_path.read_text(encoding="utf-8")) or {}
    entries: list[RuleEntry] = []
    for item in doc.get("rules") or []:
        try:
            entries.append(
                RuleEntry(
                    name=str(item["name"]),
                    category=str(item.get("category", "")),
                    policy=str(item["policy"]),
                    behavior=str(item["behavior"]),
                    clash_file=str(item["clash_file"]),
                    sr_file=str(item["sr_file"]),
                )
            )
        except KeyError as exc:
            raise ValueError(f"rules_manifest 条目缺少字段 {exc}：{item}") from exc
    return entries


# ---------------------------------------------------------------- 统计与汇总结果

@dataclass
class RenderStats:
    """一次渲染的统计（UI/发布日志用）。"""

    group_count: int                    # 分组数（两格式一致，由 validator 保证）
    node_count: int                     # 参与渲染的真实节点数
    sr_skipped_anytls: int              # SR 因 anytls 开关被跳过的节点数
    region_group_names: list[str] = field(default_factory=list)  # 本次生成的地区组名
    sr_skipped_other: int = 0           # SR 因不支持类型被跳过的节点数


@dataclass
class RenderResult:
    """四份产物文本 + 统计。"""

    clash_yaml: str                     # mihomo 主版本
    sr_conf: str                        # SR 主版本
    clash_offline_yaml: str             # mihomo 离线自包含版
    sr_offline_conf: str                # SR 离线自包含版
    stats: RenderStats


# ---------------------------------------------------------------- 内部小工具

def _real_nodes(nodes: list[Node]) -> list[Node]:
    """防御性过滤：templater 只消费清洗后的真实节点（filtered=True 不渲染）。"""
    real = [n for n in nodes if not n.filtered]
    dropped = len(nodes) - len(real)
    if dropped:
        logger.warning("templater 收到 %d 个被滤节点，已忽略（cleaner 应已剔除）", dropped)
    return real


def _yaml_scalar(value: Any) -> str:
    """YAML 标量的确定序列（JSON 双引号串是合法 YAML 标量，emoji 安全）。"""
    return json.dumps(value, ensure_ascii=False)


def _yaml_block(mapping: dict[str, Any], *, indent: int = 2, dash: bool = False) -> str:
    """把一个 dict 序列化为 YAML 块文本（列表项用 dash=True，首行带 ``- ``）。

    用 PyYAML 保证嵌套结构（ws-opts/alpn 等凭据字段）的合法性，
    jinja2 模板只负责骨架与循环。
    """
    text = yaml.safe_dump(
        mapping, allow_unicode=True, sort_keys=False,
        default_flow_style=False, width=4096,
    )
    lines = text.rstrip("\n").split("\n")
    pad = " " * indent
    if dash:
        return "\n".join([f"{pad}- {lines[0]}"] + [f"{pad}  {ln}" for ln in lines[1:]])
    return "\n".join(f"{pad}{ln}" for ln in lines)


def region_group_name(code: str | None) -> str:
    """地区代码 → 地区组名；未识别代码显示为「🌍 <code>」，None → 🌍 其他。"""
    if code is None:
        return OTHER_REGION_GROUP
    if code in REGION_GROUP_META:
        emoji, zh = REGION_GROUP_META[code]
        return f"{emoji} {zh}"
    return f"🌍 {code}"


def _ordered_present_regions(codes: set[str | None]) -> list[str | None]:
    """按固定顺序排列出现的地区：docs/02 顺序 → 其余字母序 → 🌍 其他 恒最后。"""
    known = [c for c in REGION_CANONICAL_ORDER if c in codes]
    extras = sorted(c for c in codes if c is not None and c not in REGION_CANONICAL_ORDER)
    other: list[str | None] = [None] if None in codes else []
    return known + extras + other


def _region_filter(code: str | None) -> str | None:
    if code is None:
        return None  # 🌍 其他：无地区特征，正则无意义
    if code in REGION_FILTERS:
        return REGION_FILTERS[code]
    return re.escape(code)


def _url_test_group(name: str, members: list[str], *, filter_regex: str | None = None,
                    interval: int = HEALTH_INTERVAL) -> dict[str, Any]:
    group: dict[str, Any] = {"name": name, "type": "url-test"}
    if filter_regex:
        group["filter"] = filter_regex
    group["proxies"] = members
    group["url"] = PROBE_URL
    group["interval"] = interval
    group["tolerance"] = HEALTH_TOLERANCE
    group["max-failed-times"] = HEALTH_MAX_FAILED
    group["timeout"] = HEALTH_TIMEOUT
    return group


def _purity_map(purity: list[PurityResult] | None) -> dict[tuple[str, str], PurityResult]:
    """纯净度结果按 (source_sub, node_name) 索引。

    node_name 为**消歧后最终名**（纯净度检测发生在消歧之后，INTERFACES §5.5：
    节点改名视为新节点重新检测）。查询侧必须用 (node.source_sub, node.name)
    对齐——不能用 Node.key（其名字分量是消歧前原名），否则被重名消歧改名的
    节点永远查不到自己的检测结果。
    """
    return {(p.source_sub, p.node_name): p for p in purity or []}


def _static_claude_tier(node: Node) -> int:
    """docs/02 §2 静态排序分层：美国家宽 0 → 其他美国 1 → 港/新家宽 2 → 其余 3。"""
    if node.region == "US":
        return 0 if node.residential else 1
    if node.region in ("HK", "SG") and node.residential:
        return 2
    return 3


def order_claude_candidates(nodes: list[Node],
                            purity: list[PurityResult] | None = None) -> list[Node]:
    """Claude 专用/备援候选池排序（防封号核心，docs/02 §2）。

    - 无检测数据：静态排序「美国家宽 → 其他美国 → 香港/新加坡家宽 → 其余」；
    - 有评分：按 claude_rank 降序，住宅恒在机房之前；未评分节点排在已评分之后，
      其内部仍按静态序（实测数据优先于节点名猜测，docs/04 验收 #7）。
    """
    pmap = _purity_map(purity)

    def sort_key(node: Node) -> tuple:
        # 查询键用消歧后最终名（与 _purity_map 的键语义一致，见其 docstring）
        result = pmap.get((node.source_sub, node.name))
        tier = _static_claude_tier(node)
        if result is not None and result.claude_rank is not None:
            residential_after = 0 if result.ip_type == "residential" else 1
            return (0, -int(result.claude_rank), residential_after, tier, node.name)
        return (1, tier, node.name)

    return sorted(nodes, key=sort_key)


# ---------------------------------------------------------------- 分组计算

def build_groups(nodes: list[Node], *, config: AppConfig,
                 purity: list[PurityResult] | None = None,
                 region_presence_nodes: list[Node] | None = None) -> list[dict]:
    """按 docs/02 §2 计算全部 proxy-groups，返回 mihomo 原生 dict 结构。

    - 无节点的地区组不生成（🌍 其他 同理，仅当存在未识别地区节点时生成）；
    - region_presence_nodes：地区组「存在性」的判定节点集。SR 跳过 anytls 时传入
      全量节点，保证两格式组集合一致（某地区在 SR 可见集为空时成员回退 [♻️ 常规自动]）；
    - 美国系组（⛳ 美国优质 / 🎁 / 🤖 / 🐙）恒生成，无美国可见节点时成员回退
      [♻️ 常规自动]，避免空 url-test 组导致 mihomo 拒载；
    - purity：纯净度结果（可为空），驱动 Claude 专用组排序。
    """
    _ = config  # 预留：探活参数未来外置到 AppConfig
    real = _real_nodes(nodes)
    presence = _real_nodes(region_presence_nodes) if region_presence_nodes is not None else real

    by_region: dict[str | None, list[Node]] = {}
    for n in real:
        by_region.setdefault(n.region, []).append(n)
    present_codes = _ordered_present_regions({n.region for n in presence})
    region_names = [region_group_name(c) for c in present_codes]

    all_names = [n.name for n in real]
    us_names = [n.name for n in real if n.region == "US"] or [G_AUTO]
    claude_members = [n.name for n in order_claude_candidates(real, purity)]

    groups: list[dict] = []
    # 1. 🚀 节点选择：常规自动 → 各地区组 → DIRECT → 全部节点
    groups.append({
        "name": G_MAIN, "type": "select",
        "proxies": [G_AUTO, *region_names, "DIRECT", *all_names],
    })
    # 2. ♻️ 常规自动：全部真实节点
    groups.append(_url_test_group(G_AUTO, all_names))
    # 3. 地区组（按需生成；url-test）
    for code in present_codes:
        members = [n.name for n in by_region.get(code, [])] or [G_AUTO]
        groups.append(_url_test_group(region_group_name(code), members,
                                      filter_regex=_region_filter(code)))
    # 4. ⛳ 美国优质（含家宽+机房）
    groups.append(_url_test_group(G_US, us_names, filter_regex=US_FILTER))
    # 5. 🛑 Claude 专用（select 手动锁定）+ 6. 🧷 Claude 备援（fallback 90s lazy=false）
    groups.append({"name": G_CLAUDE, "type": "select", "proxies": list(claude_members)})
    groups.append({
        "name": G_CLAUDE_BACKUP, "type": "fallback",
        "proxies": list(claude_members),
        "url": PROBE_URL, "interval": CLAUDE_BACKUP_INTERVAL,
        "tolerance": HEALTH_TOLERANCE, "max-failed-times": HEALTH_MAX_FAILED,
        "timeout": HEALTH_TIMEOUT, "lazy": False,
    })
    # 7. 🎁 OpenAI / 🤖 Gemini / 🐙 Copilot（url-test 美国节点）
    groups.append(_url_test_group(G_OPENAI, us_names, filter_regex=US_FILTER))
    groups.append(_url_test_group(G_GEMINI, us_names, filter_regex=US_FILTER))
    groups.append(_url_test_group(G_COPILOT, us_names, filter_regex=US_FILTER))
    # 8. 🧠 通用 AI（兜底入口）
    groups.append({
        "name": G_AI, "type": "select",
        "proxies": [G_CLAUDE, G_US, G_OPENAI, *region_names],
    })
    # 9. 📲 Telegram（全部节点，追求延迟）
    groups.append(_url_test_group(G_TG, all_names))
    # 10. 流媒体四个 select（成员=各地区组，保持手动）
    for name in (G_NETFLIX, G_DISNEY, G_YOUTUBE, G_SPOTIFY):
        groups.append({"name": name, "type": "select", "proxies": list(region_names)})
    # 11. 大厂三 select（默认值分别为 ♻️ 常规自动 / DIRECT / DIRECT，首位即默认）
    groups.append({"name": G_GOOGLE, "type": "select",
                   "proxies": [G_AUTO, G_MAIN, *region_names, "DIRECT"]})
    for name in (G_MICROSOFT, G_APPLE):
        groups.append({"name": name, "type": "select",
                       "proxies": ["DIRECT", G_AUTO, G_MAIN, *region_names]})
    # 12. 🎮 游戏平台（默认 DIRECT）
    groups.append({"name": G_GAME, "type": "select",
                   "proxies": ["DIRECT", G_AUTO, G_MAIN, *region_names]})
    # 13. 🐟 漏网之鱼（MATCH 落点）
    groups.append({"name": G_FINAL, "type": "select",
                   "proxies": [G_MAIN, G_AUTO, "DIRECT"]})
    return groups


# ---------------------------------------------------------------- 规则链与 rule-providers

def _clash_rule_lines(rules: list[RuleEntry]) -> list[str]:
    """mihomo rules 列表：RULE-SET,<名>,<组> 按清单顺序 + MATCH 落漏网之鱼。"""
    lines = [f"RULE-SET,{e.name},{e.policy}" for e in rules]
    lines.append(f"MATCH,{G_FINAL}")
    return lines


def _build_rule_providers(rules: list[RuleEntry], config: AppConfig,
                          *, offline: bool) -> dict[str, dict[str, Any]]:
    """主版本：type http 指向 NAS；离线版：type inline + payload 内联。"""
    providers: dict[str, dict[str, Any]] = {}
    for entry in rules:
        if offline:
            providers[entry.name] = {
                "type": "inline",
                "behavior": entry.behavior,
                "payload": _inline_payload(entry, config),
            }
        else:
            providers[entry.name] = {
                "type": "http",
                "behavior": entry.behavior,
                "url": f"{config.base_url}/rules/{entry.clash_file}",
                "interval": RULE_PROVIDER_INTERVAL,
            }
    return providers


def _read_cached_payload(entry: RuleEntry, config: AppConfig) -> list[str] | None:
    """读 data/rules/ 里 rulesync 缓存的规则内容（yaml payload 优先，.list 兜底）。"""
    yaml_path = config.rules_dir / entry.clash_file
    if yaml_path.exists():
        try:
            doc = yaml.safe_load(yaml_path.read_text(encoding="utf-8"))
            payload = doc.get("payload") if isinstance(doc, dict) else None
            if isinstance(payload, list):
                return [str(x).strip() for x in payload if str(x).strip()]
        except (OSError, yaml.YAMLError) as exc:
            logger.warning("规则缓存解析失败，改用其他来源：%s（%s）", entry.name, exc)
    list_path = config.rules_dir / entry.sr_file
    if list_path.exists():
        try:
            lines = []
            for raw in list_path.read_text(encoding="utf-8").splitlines():
                line = raw.strip()
                if line and not line.startswith("#"):
                    lines.append(line)
            if lines:
                return lines
        except OSError as exc:
            logger.warning("规则缓存读取失败：%s（%s）", entry.name, exc)
    return None


def _manifest_builtin_domains(rule_name: str) -> list[str]:
    """从清单里取自维护条目的 domains（claude-extra 离线兜底）。"""
    try:
        doc = yaml.safe_load(DEFAULT_MANIFEST_PATH.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError):
        return []
    for item in doc.get("rules") or []:
        if str(item.get("name")) == rule_name and isinstance(item.get("domains"), list):
            return [str(d).strip() for d in item["domains"] if str(d).strip()]
    return []


def _inline_payload(entry: RuleEntry, config: AppConfig) -> list[str]:
    """离线版内联 payload：缓存 yaml → 缓存 .list → 清单内置 domains → 空（告警）。"""
    cached = _read_cached_payload(entry, config)
    if cached is not None:
        return cached
    domains = _manifest_builtin_domains(entry.name)
    if domains:
        return domains
    logger.warning("离线内联缺少规则缓存，payload 为空：%s（请先运行规则镜像）", entry.name)
    return []


def _payload_line_to_sr_rule(line: str, policy: str) -> str:
    """.list 缓存行 / payload 行 → 完整 SR 规则（补全前缀与策略）。"""
    if "," in line:            # 已是规则片段（DOMAIN-SUFFIX,x / IP-CIDR,x/24,no-resolve）
        return f"{line},{policy}"
    if re.fullmatch(r"\d{1,3}(\.\d{1,3}){3}/\d{1,2}", line):  # 裸 CIDR
        return f"IP-CIDR,{line},{policy}"
    return f"DOMAIN-SUFFIX,{line},{policy}"  # 裸域名（domain 行为）


def _sr_rule_lines(rules: list[RuleEntry], config: AppConfig, *, offline: bool) -> list[str]:
    """SR [Rule] 行：主版本 RULE-SET 指 NAS 的 .list；离线版直接展开规则内容。"""
    lines: list[str] = []
    for entry in rules:
        if offline:
            payload = _inline_payload(entry, config)
            lines.extend(_payload_line_to_sr_rule(line, entry.policy) for line in payload)
        else:
            lines.append(f"RULE-SET,{config.base_url}/rules/{entry.sr_file},{entry.policy}")
    lines.append(f"FINAL,{G_FINAL}")
    return lines


# ---------------------------------------------------------------- SR 节点/分组行

def _prepare_sr_nodes(real_nodes: list[Node],
                      config: AppConfig) -> tuple[list[Node], int, int]:
    """按开关剔除 anytls（<6.3 不支持），并剔除无法映射的类型；返回 (可见节点, anytls 跳过数, 其他跳过数)。"""
    sr_nodes: list[Node] = []
    skipped_anytls = 0
    skipped_other = 0
    for node in real_nodes:
        if node.type.lower() == "anytls" and config.skip_anytls:
            skipped_anytls += 1
            continue
        if _sr_proxy_line(node) is None:
            skipped_other += 1
            continue
        sr_nodes.append(node)
    if skipped_anytls:
        logger.info("SR conf 跳过 anytls 节点 %d 个（开关 skip_anytls=%s）",
                    skipped_anytls, config.skip_anytls)
    if skipped_other:
        logger.warning("SR conf 跳过暂不支持类型的节点 %d 个", skipped_other)
    return sr_nodes, skipped_anytls, skipped_other


def _sr_transport_params(cred: dict[str, Any]) -> list[str]:
    """Clash 传输层字段 → SR（Surge 风格）参数。"""
    network = cred.get("network")
    if network == "ws":
        opts = cred.get("ws-opts") or {}
        headers = opts.get("headers") or {}
        host = headers.get("Host") or headers.get("host")
        parts = ["ws=true", f"ws-path={opts.get('path', '/')}"]
        if host:
            parts.append(f"ws-headers=Host:{host}")
        return parts
    if network == "grpc":
        service = (cred.get("grpc-opts") or {}).get("grpc-service-name", "")
        return ["grpc=true", f"grpc-service-name={service}"]
    return []


def _sr_proxy_line(node: Node) -> str | None:
    """[Proxy] 行：vless/ss/trojan/hysteria2 等逐节点直映（Surge 风格键值）。

    无法映射的类型返回 None（调用方计数跳过）。凭据写入产物属预期行为，
    但绝不进日志。
    """
    cred = node.credentials
    ntype = node.type.lower()
    params: list[str] = []
    sni = cred.get("sni") or cred.get("servername") or cred.get("peer")
    if cred.get("skip-cert-verify"):
        params.append("skip-cert-verify=true")
    transport = _sr_transport_params(cred)

    if ntype == "ss":
        cipher = cred.get("cipher") or cred.get("encrypt-method") or "none"
        params += [f"encrypt-method={cipher}", f"password={cred.get('password', '')}"]
    elif ntype == "vmess":
        params += [f"username={cred.get('uuid', '')}",
                   f"encrypt-method={cred.get('cipher', 'auto')}"]
        if cred.get("tls"):
            params.append("tls=true")
        params += transport
    elif ntype == "vless":
        params += [f"username={cred.get('uuid', '')}"]
        if cred.get("tls"):
            params.append("tls=true")
        if cred.get("flow"):
            params.append(f"flow={cred['flow']}")
        params += transport
    elif ntype == "trojan":
        params += [f"password={cred.get('password', '')}"]
        params += transport
    elif ntype == "hysteria2":
        params += [f"password={cred.get('password', '')}"]
        if cred.get("obfs"):
            params.append(f"obfs={cred['obfs']}")
        if cred.get("obfs-password"):
            params.append(f"obfs-password={cred['obfs-password']}")
    elif ntype == "anytls":
        params += [f"password={cred.get('password', '')}"]
    elif ntype == "tuic":
        params += [f"uuid={cred.get('uuid', '')}",
                   f"password={cred.get('password', '')}"]
        if cred.get("congestion-controller"):
            params.append(f"congestion-controller={cred['congestion-controller']}")
    else:
        logger.warning("SR 暂不支持节点类型 %r，已跳过：%s", node.type, node.name)
        return None

    if sni and ntype != "ss":
        params.append(f"sni={sni}")
    if cred.get("udp"):
        params.append("udp-relay=true")
    return ", ".join([f"{node.name} = {ntype}", node.server, str(node.port), *params])


def _sr_group_line(group: dict[str, Any]) -> str:
    """[Proxy Group] 行：与 mihomo 同名同语义，参数用 url=…, interval=…, tolerance=… 形式。"""
    parts = [f"{group['name']} = {group['type']}"]
    parts.extend(str(m) for m in group.get("proxies", []))
    for key in ("url", "interval", "tolerance"):
        if key in group:
            parts.append(f"{key}={group[key]}")
    return ", ".join(parts)


# ---------------------------------------------------------------- 渲染入口

def render_clash(nodes: list[Node], *, config: AppConfig, rules: list[RuleEntry],
                 offline: bool = False, purity: list[PurityResult] | None = None) -> str:
    """渲染 mihomo（Clash Verge）YAML。offline=True 时规则内联（离线自包含版）。"""
    real = _real_nodes(nodes)
    groups = build_groups(real, config=config, purity=purity)
    proxies = [n.to_clash_proxy() for n in real]
    providers = _build_rule_providers(rules, config, offline=offline)
    template = _env.get_template("clash.yaml.j2")
    return template.render(
        variant_label="离线自包含版" if offline else "主版本",
        offline=offline,
        base_url=config.base_url,
        node_count=len(real),
        group_count=len(groups),
        proxy_blocks=[_yaml_block(p, dash=True) for p in proxies],
        group_blocks=[_yaml_block(g, dash=True) for g in groups],
        provider_blocks=[_yaml_block({name: spec}) for name, spec in providers.items()],
        rule_lines=[_yaml_scalar(line) for line in _clash_rule_lines(rules)],
    )


def render_sr_conf(nodes: list[Node], *, config: AppConfig, rules: list[RuleEntry],
                   offline: bool = False, purity: list[PurityResult] | None = None) -> str:
    """渲染 Shadowrocket conf。offline=True 时 .list 内容展开进 [Rule]。

    组与 mihomo 同名同语义：分组结构按全量节点判定（region_presence_nodes），
    成员按 SR 可见节点（anytls 按开关跳过）填充，空地区组回退 ♻️ 常规自动。
    """
    real = _real_nodes(nodes)
    sr_nodes, skipped_anytls, _ = _prepare_sr_nodes(real, config)
    groups = build_groups(sr_nodes, config=config, purity=purity,
                          region_presence_nodes=real)
    proxy_lines = [line for line in (_sr_proxy_line(n) for n in sr_nodes) if line]
    template = _env.get_template("sr.conf.j2")
    return template.render(
        variant_label="离线自包含版" if offline else "主版本",
        offline=offline,
        base_url=config.base_url,
        node_count=len(sr_nodes),
        group_count=len(groups),
        skipped_anytls=skipped_anytls,
        proxy_lines=proxy_lines,
        group_lines=[_sr_group_line(g) for g in groups],
        rule_lines=_sr_rule_lines(rules, config, offline=offline),
    )


def render_all(nodes: list[Node], *, config: AppConfig, rules: list[RuleEntry],
               purity: list[PurityResult] | None = None) -> RenderResult:
    """一次渲染四份产物 + 统计（pipeline 推荐入口）。"""
    real = _real_nodes(nodes)
    sr_nodes, skipped_anytls, skipped_other = _prepare_sr_nodes(real, config)
    groups = build_groups(real, config=config, purity=purity)
    region_names = [region_group_name(c)
                    for c in _ordered_present_regions({n.region for n in real})]
    stats = RenderStats(
        group_count=len(groups),
        node_count=len(real),
        sr_skipped_anytls=skipped_anytls,
        region_group_names=region_names,
        sr_skipped_other=skipped_other,
    )
    logger.info("渲染完成：节点 %d 个，分组 %d 个，SR 跳过 anytls %d 个",
                stats.node_count, stats.group_count, stats.sr_skipped_anytls)
    return RenderResult(
        clash_yaml=render_clash(real, config=config, rules=rules, offline=False, purity=purity),
        sr_conf=render_sr_conf(real, config=config, rules=rules, offline=False, purity=purity),
        clash_offline_yaml=render_clash(real, config=config, rules=rules, offline=True, purity=purity),
        sr_offline_conf=render_sr_conf(real, config=config, rules=rules, offline=True, purity=purity),
        stats=stats,
    )
