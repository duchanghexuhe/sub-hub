"""双格式产物校验与发布（data/out/ 的唯一写入入口）。

职责（docs/INTERFACES.md §3.5）：
- validate_clash_yaml / validate_sr_conf：解析回读单份产物，检查结构合法性；
- check_consistency：回读两份主产物，校验组数、组名集合、规则条目（name+policy 序列）
  等价——不等价返回差异描述列表，pipeline 据此拒绝发布并回滚上一版；
- publish：4 份产物各自校验 → 主两份一致性校验 → 任一失败抛 PublishError →
  原子写入新版本目录（meta.json 最后写，作为发布点）→ 清理旧版本。

安全纪律：日志只输出版本号、计数与 hash 前缀，不打印订阅 URL 与节点凭据。
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import yaml

from app.config import AppConfig
from app.models import ConfigVersion, Node
from app.store import Store
from app.utils import (
    atomic_write_text,
    content_hash,
    new_version_dir,
    next_version,
    now_iso,
    prune_versions,
    read_json,
    version_dir,
    write_json,
)

logger = logging.getLogger("subhub.validator")

# artifacts 键固定为这四个（docs/INTERFACES.md §3.5），顺序即 content_hash 拼接顺序
ARTIFACT_KEYS: tuple[str, ...] = (
    "clash.yaml",
    "shadowrocket.conf",
    "clash-offline.yaml",
    "shadowrocket-offline.conf",
)

_ALLOWED_GROUP_TYPES = {"select", "url-test", "fallback", "load-balance", "relay"}
_ALLOWED_PROVIDER_TYPES = {"http", "inline", "file"}
_ALLOWED_BEHAVIORS = {"domain", "ipcidr", "classical"}
_BUILTIN_POLICIES = {"DIRECT", "REJECT", "REJECT-DROP", "PASS", "GLOBAL"}
_ALLOWED_SR_PROXY_TYPES = {
    "ss", "ssr", "vmess", "vless", "trojan", "hysteria", "hysteria2", "tuic",
    "anytls", "snell", "http", "https", "socks5", "socks5-tls", "wireguard", "ssh",
}
_ALLOWED_SR_GROUP_TYPES = {"select", "url-test", "fallback", "load-balance", "static", "ssid"}


class PublishError(Exception):
    """发布被校验拦截（pipeline 据此拒绝发布并沿用上一版）。"""

    def __init__(self, errors: list[str]) -> None:
        self.errors = list(errors)
        super().__init__("产物校验失败：" + "；".join(self.errors))


# ---------------------------------------------------------------- mihomo YAML 校验

def validate_clash_yaml(text: str) -> list[str]:
    """解析回读 mihomo YAML，检查全局段/proxies/proxy-groups/rules 结构；空列表=通过。"""
    errors: list[str] = []
    try:
        doc = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        return [f"mihomo YAML 解析失败：{exc}"]
    if not isinstance(doc, dict):
        return ["mihomo YAML 顶层必须是映射"]

    # 全局段（docs/02 §4）
    for key in ("mixed-port", "external-controller", "mode", "unified-delay", "tcp-concurrent"):
        if key not in doc:
            errors.append(f"缺少全局配置项 {key}")
    if doc.get("mode") != "rule":
        errors.append(f"mode 必须为 rule，当前为 {doc.get('mode')!r}")
    if doc.get("unified-delay") is not True:
        errors.append("unified-delay 必须为 true")
    if doc.get("tcp-concurrent") is not True:
        errors.append("tcp-concurrent 必须为 true")
    dns = doc.get("dns")
    if not isinstance(dns, dict):
        errors.append("缺少 dns 配置段")
    else:
        if dns.get("enhanced-mode") != "fake-ip":
            errors.append(f"dns.enhanced-mode 必须为 fake-ip，当前为 {dns.get('enhanced-mode')!r}")
        if not isinstance(dns.get("nameserver"), list) or not dns.get("nameserver"):
            errors.append("dns.nameserver 必须为非空列表")
        if not isinstance(dns.get("fake-ip-filter"), list) or not dns.get("fake-ip-filter"):
            errors.append("dns.fake-ip-filter 必须为非空列表")

    # proxies
    proxies = doc.get("proxies")
    if not isinstance(proxies, list) or not proxies:
        errors.append("proxies 必须为非空列表")
        proxies = []
    proxy_names: set[str] = set()
    for i, proxy in enumerate(proxies):
        if not isinstance(proxy, dict):
            errors.append(f"proxies 第 {i + 1} 项必须是映射")
            continue
        name = proxy.get("name")
        if not name or not isinstance(name, str):
            errors.append(f"proxies 第 {i + 1} 项缺少 name")
        elif name in proxy_names:
            errors.append(f"proxies 节点名重复：{name}")
        else:
            proxy_names.add(name)
        for key in ("type", "server", "port"):
            if key not in proxy:
                errors.append(f"proxies 节点 {name or i + 1} 缺少 {key}")

    # proxy-groups（先收集全部组名，再校验成员——组之间允许前向引用）
    groups = doc.get("proxy-groups")
    if not isinstance(groups, list) or not groups:
        errors.append("proxy-groups 必须为非空列表")
        groups = []
    group_names: set[str] = set()
    for i, group in enumerate(groups):
        if not isinstance(group, dict):
            errors.append(f"proxy-groups 第 {i + 1} 项必须是映射")
            continue
        gname = group.get("name")
        if not gname or not isinstance(gname, str):
            errors.append(f"proxy-groups 第 {i + 1} 项缺少 name")
        elif gname in group_names:
            errors.append(f"proxy-groups 组名重复：{gname}")
        else:
            group_names.add(gname)
        if group.get("type") not in _ALLOWED_GROUP_TYPES:
            errors.append(f"proxy-groups {gname or i + 1} 的 type 非法：{group.get('type')!r}")
        if gname in proxy_names:
            errors.append(f"组名与节点名冲突：{gname}")
    for i, group in enumerate(groups):
        if not isinstance(group, dict):
            continue
        gname = group.get("name") or f"#{i + 1}"
        gtype = group.get("type")
        members = group.get("proxies")
        if not isinstance(members, list) or not members:
            errors.append(f"proxy-groups {gname} 的成员列表为空")
        else:
            for member in members:
                if not isinstance(member, str):
                    errors.append(f"proxy-groups {gname} 存在非法成员：{member!r}")
                elif (member not in proxy_names and member not in group_names
                      and member not in _BUILTIN_POLICIES):
                    errors.append(f"proxy-groups {gname} 引用了不存在的成员：{member}")
        if gtype in ("url-test", "fallback"):
            for key in ("url", "interval", "tolerance"):
                if key not in group:
                    errors.append(f"测速组 {gname} 缺少参数 {key}")

    # rule-providers
    providers = doc.get("rule-providers")
    if not isinstance(providers, dict) or not providers:
        errors.append("rule-providers 必须为非空映射")
        providers = {}
    for pname, spec in providers.items():
        if not isinstance(spec, dict):
            errors.append(f"rule-providers {pname} 必须是映射")
            continue
        ptype = spec.get("type")
        if ptype not in _ALLOWED_PROVIDER_TYPES:
            errors.append(f"rule-providers {pname} 的 type 非法：{ptype!r}")
        if spec.get("behavior") not in _ALLOWED_BEHAVIORS:
            errors.append(f"rule-providers {pname} 的 behavior 非法：{spec.get('behavior')!r}")
        if ptype == "http":
            url = spec.get("url")
            if not isinstance(url, str) or not url.startswith(("http://", "https://")):
                errors.append(f"rule-providers {pname} 的 url 非法：{url!r}")
            if not isinstance(spec.get("interval"), int):
                errors.append(f"rule-providers {pname} 缺少 interval")
        if ptype == "inline" and not isinstance(spec.get("payload"), list):
            errors.append(f"rule-providers {pname} 缺少 payload 列表")

    # rules
    rules = doc.get("rules")
    if not isinstance(rules, list) or not rules:
        errors.append("rules 必须为非空列表")
        rules = []
    known_policies = group_names | _BUILTIN_POLICIES
    match_seen = False
    for i, rule in enumerate(rules):
        if not isinstance(rule, str):
            errors.append(f"rules 第 {i + 1} 条必须是字符串")
            continue
        parts = [p.strip() for p in rule.split(",")]
        if match_seen:
            errors.append(f"rules 第 {i + 1} 条出现在 MATCH 之后：{rule}")
            continue
        if parts[0] == "MATCH":
            match_seen = True
            if len(parts) != 2:
                errors.append(f"MATCH 规则格式非法：{rule}")
            if i != len(rules) - 1:
                errors.append("MATCH 必须是最后一条规则")
            continue
        policy = parts[-1]
        if policy not in known_policies:
            errors.append(f"rules 第 {i + 1} 条的策略不存在：{policy}")
        if parts[0] == "RULE-SET":
            if len(parts) != 3:
                errors.append(f"RULE-SET 规则格式非法：{rule}")
            elif parts[1] not in providers:
                errors.append(f"RULE-SET 引用了不存在的 rule-provider：{parts[1]}")
        elif len(parts) < 2:
            errors.append(f"rules 第 {i + 1} 条格式非法：{rule}")
    if rules and not match_seen:
        errors.append("rules 缺少 MATCH 兜底规则")

    return errors


# ---------------------------------------------------------------- SR conf 校验

def _split_sr_sections(text: str) -> tuple[dict[str, list[str]], list[str], list[str]]:
    """按 [Section] 切分 conf；返回 (段名→行列表, 段顺序, 解析错误)。"""
    sections: dict[str, list[str]] = {}
    order: list[str] = []
    errors: list[str] = []
    current: str | None = None
    for lineno, raw in enumerate(text.splitlines(), start=1):
        line = raw.strip()
        if not line or line.startswith("#") or line.startswith(";"):
            continue
        if line.startswith("[") and line.endswith("]"):
            current = line[1:-1].strip()
            if current in sections:
                errors.append(f"第 {lineno} 行段落重复：[{current}]")
            else:
                sections[current] = []
                order.append(current)
            continue
        if current is None:
            errors.append(f"第 {lineno} 行位于任何段落之外：{line[:40]}…")
            continue
        sections[current].append(line)
    return sections, order, errors


def _parse_sr_entry(line: str, lineno: int, what: str,
                    errors: list[str]) -> tuple[str, str, list[str]] | None:
    """解析「名字 = 其余」形式的行，返回 (名字, 类型, 其余字段)；非法报错并返回 None。"""
    if "=" not in line:
        errors.append(f"{what} 第 {lineno} 行缺少「=」：{line[:40]}…")
        return None
    name, rest = line.split("=", 1)
    name = name.strip()
    fields = [f.strip() for f in rest.split(",")]
    if not name or not fields[0]:
        errors.append(f"{what} 第 {lineno} 行格式非法：{line[:40]}…")
        return None
    return (name, fields[0], fields[1:])


def validate_sr_conf(text: str) -> list[str]:
    """解析回读 SR conf：三段式结构 + [Proxy] 行数与格式抽查；空列表=通过。"""
    sections, order, errors = _split_sr_sections(text)
    for required in ("Proxy", "Proxy Group", "Rule"):
        if required not in sections:
            errors.append(f"缺少段落 [{required}]")
    if errors:
        return errors
    if order != ["Proxy", "Proxy Group", "Rule"]:
        errors.append(f"段落顺序必须为 Proxy → Proxy Group → Rule，当前为 {order}")
        return errors

    # [Proxy]
    proxy_names: set[str] = set()
    for lineno, line in enumerate(sections["Proxy"], start=1):
        parsed = _parse_sr_entry(line, lineno, "[Proxy]", errors)
        if parsed is None:
            continue
        name, ptype, fields = parsed
        if ptype not in _ALLOWED_SR_PROXY_TYPES:
            errors.append(f"[Proxy] 第 {lineno} 行类型非法或不支持：{ptype}")
            continue
        if len(fields) < 2:
            errors.append(f"[Proxy] 第 {lineno} 行缺少 server/port：{name}")
            continue
        if not fields[1].isdigit():
            errors.append(f"[Proxy] 第 {lineno} 行端口非法：{fields[1]}")
            continue
        proxy_names.add(name)
    if not proxy_names:
        errors.append("[Proxy] 段没有可用的节点行")

    # [Proxy Group]（两段式：先收集组名——组之间允许前向引用——再校验成员与参数）
    group_entries: list[tuple[str, str, list[str], int]] = []
    group_names: set[str] = set()
    for lineno, line in enumerate(sections["Proxy Group"], start=1):
        parsed = _parse_sr_entry(line, lineno, "[Proxy Group]", errors)
        if parsed is None:
            continue
        name, gtype, fields = parsed
        if gtype not in _ALLOWED_SR_GROUP_TYPES:
            errors.append(f"[Proxy Group] 第 {lineno} 行类型非法：{gtype}")
            continue
        if name in group_names or name in proxy_names:
            errors.append(f"[Proxy Group] 组名重复或与节点重名：{name}")
        else:
            group_names.add(name)
        group_entries.append((name, gtype, fields, lineno))
    for name, gtype, fields, lineno in group_entries:
        members = [f for f in fields if "=" not in f]
        params = {f.split("=", 1)[0]: f.split("=", 1)[1] for f in fields if "=" in f}
        if not members:
            errors.append(f"[Proxy Group] {name} 成员列表为空")
        for member in members:
            if member not in proxy_names and member not in group_names \
                    and member not in _BUILTIN_POLICIES:
                errors.append(f"[Proxy Group] {name} 引用了不存在的成员：{member}")
        if gtype in ("url-test", "fallback"):
            for key in ("url", "interval", "tolerance"):
                if key not in params:
                    errors.append(f"测速组 {name} 缺少参数 {key}=")
            if "url" in params and not params["url"].startswith(("http://", "https://")):
                errors.append(f"测速组 {name} 的 url 非法：{params['url']}")
            for key in ("interval", "tolerance"):
                if key in params and not params[key].isdigit():
                    errors.append(f"测速组 {name} 的 {key} 必须是数字：{params[key]}")

    # [Rule]
    rule_lines = sections["Rule"]
    if not rule_lines:
        errors.append("[Rule] 段为空")
        return errors
    known_policies = group_names | _BUILTIN_POLICIES
    for lineno, line in enumerate(rule_lines, start=1):
        parts = [p.strip() for p in line.split(",")]
        if parts[0] == "FINAL":
            if len(parts) != 2:
                errors.append(f"[Rule] 第 {lineno} 行 FINAL 格式非法：{line}")
            continue
        policy = parts[-1]
        if policy not in known_policies:
            errors.append(f"[Rule] 第 {lineno} 行策略不存在：{policy}")
        if parts[0] == "RULE-SET" and len(parts) != 3:
            errors.append(f"[Rule] 第 {lineno} 行 RULE-SET 格式非法：{line}")
    if not rule_lines[-1].startswith("FINAL"):
        errors.append("[Rule] 最后一条必须是 FINAL")

    return errors


# ---------------------------------------------------------------- 一致性校验

def _clash_rule_seq(doc: dict[str, Any]) -> list[tuple[str, str]]:
    """mihomo rules → (规则集名或规则类型, 策略) 序列。"""
    seq: list[tuple[str, str]] = []
    for rule in doc.get("rules") or []:
        parts = [p.strip() for p in str(rule).split(",")]
        if parts[0] == "RULE-SET":
            seq.append((parts[1], parts[-1]))   # (rule-provider 名, 策略)
        else:
            seq.append((parts[0], parts[-1]))   # MATCH 等内联规则：(类型, 策略)
    return seq


def _provider_name_from_target(target: str) -> str:
    """SR RULE-SET 目标（URL 或文件名）→ 逻辑名：取路径基名并去掉扩展名。"""
    name = target.rstrip("/")
    if "/" in name:
        name = name.rsplit("/", 1)[-1]
    for ext in (".list", ".yaml", ".yml", ".txt"):
        if name.lower().endswith(ext):
            name = name[: -len(ext)]
            break
    return name


def _sr_rule_seq(lines: list[str]) -> list[tuple[str, str]]:
    """SR [Rule] 行 → (规则集名或规则类型, 策略) 序列（FINAL 归一化为 MATCH）。"""
    seq: list[tuple[str, str]] = []
    for line in lines:
        parts = [p.strip() for p in line.split(",")]
        if parts[0] == "FINAL":
            seq.append(("MATCH", parts[-1]))
        elif parts[0] == "RULE-SET":
            seq.append((_provider_name_from_target(parts[1]), parts[-1]))
        else:
            # 离线版展开的内联规则：按「规则类型, 策略」参与对比（主版本不会走到这）
            seq.append((parts[0], parts[-1]))
    return seq


def check_consistency(clash_text: str, sr_text: str) -> list[str]:
    """回读两份主产物：组数、组名集合、规则条目（name+policy 序列）必须等价。"""
    errors: list[str] = []
    try:
        clash_doc = yaml.safe_load(clash_text)
    except yaml.YAMLError as exc:
        return [f"mihomo YAML 解析失败：{exc}"]
    if not isinstance(clash_doc, dict):
        return ["mihomo YAML 顶层必须是映射"]
    sections, _, sr_errors = _split_sr_sections(sr_text)
    if sr_errors:
        return [f"SR conf 解析失败：{err}" for err in sr_errors]
    if "Proxy Group" not in sections or "Rule" not in sections:
        return ["SR conf 缺少 [Proxy Group] 或 [Rule] 段"]

    clash_groups = [g.get("name") for g in clash_doc.get("proxy-groups") or []
                    if isinstance(g, dict)]
    sr_groups: list[str] = []
    scratch: list[str] = []
    for line in sections["Proxy Group"]:
        parsed = _parse_sr_entry(line, 0, "[Proxy Group]", scratch)
        if parsed:
            sr_groups.append(parsed[0])

    # 组数与组名集合
    if len(clash_groups) != len(sr_groups):
        errors.append(f"组数不一致：mihomo {len(clash_groups)} 个 vs SR {len(sr_groups)} 个")
    missing = sorted(set(clash_groups) - set(sr_groups))
    extra = sorted(set(sr_groups) - set(clash_groups))
    for name in missing[:5]:
        errors.append(f"SR 缺少组：{name}")
    for name in extra[:5]:
        errors.append(f"SR 多出组：{name}")
    if len(missing) > 5:
        errors.append(f"SR 共缺少 {len(missing)} 个组（仅列出前 5 个）")
    if len(extra) > 5:
        errors.append(f"SR 共多出 {len(extra)} 个组（仅列出前 5 个）")

    # 规则条目序列（name + policy 逐条对齐，顺序即规则链）
    clash_seq = _clash_rule_seq(clash_doc)
    sr_seq = _sr_rule_seq(sections["Rule"])
    if len(clash_seq) != len(sr_seq):
        errors.append(f"规则条数不一致：mihomo {len(clash_seq)} 条 vs SR {len(sr_seq)} 条")
    diff_count = 0
    for i, (c, s) in enumerate(zip(clash_seq, sr_seq)):
        if c != s:
            errors.append(
                f"规则第 {i + 1} 条不一致：mihomo {c[0]}→{c[1]} vs SR {s[0]}→{s[1]}"
            )
            diff_count += 1
            if diff_count >= 5:
                errors.append("规则序列差异过多，仅列出前 5 处")
                break
    return errors


# ---------------------------------------------------------------- 发布（唯一写 data/out/）

def _node_brief(nodes: list[Node]) -> list[dict[str, Any]]:
    """meta.json 附带的节点简要信息（不含任何凭据），用于下一版 diff。"""
    return [{"name": n.name, "type": n.type, "server": n.server, "port": n.port}
            for n in nodes]


def _diff_summary(prev_meta: dict[str, Any] | None,
                  curr_brief: list[dict[str, Any]]) -> dict[str, Any]:
    """与上一版 meta.json 的节点名集合对比，产出 added/removed/renamed 摘要。

    renamed 启发式：新增节点与消失节点 (type, server, port) 相同视为改名
    （典型场景：重名消歧后缀变化）。
    """
    if prev_meta is None or not isinstance(prev_meta.get("nodes"), list):
        return {"added": sorted(n["name"] for n in curr_brief),
                "removed": [], "renamed": []}
    prev_nodes: list[dict[str, Any]] = prev_meta["nodes"]
    prev_names = {str(n.get("name")) for n in prev_nodes}
    curr_names = {str(n["name"]) for n in curr_brief}
    added = set(curr_names - prev_names)
    removed = set(prev_names - curr_names)
    prev_addr: dict[tuple[Any, ...], list[str]] = {}
    for n in prev_nodes:
        prev_addr.setdefault((n.get("type"), n.get("server"), n.get("port")), []).append(
            str(n.get("name")))
    renamed: list[list[str]] = []
    used_addr: set[tuple[Any, ...]] = set()
    for n in curr_brief:
        if n["name"] not in added:
            continue
        addr = (n["type"], n["server"], n["port"])
        if addr in used_addr:
            continue
        candidates = [p for p in prev_addr.get(addr, []) if p in removed]
        if candidates:
            renamed.append([candidates[0], n["name"]])
            added.discard(n["name"])
            removed.discard(candidates[0])
            used_addr.add(addr)
    return {"added": sorted(added), "removed": sorted(removed), "renamed": renamed}


def publish(config: AppConfig, store: Store, artifacts: dict[str, str],
            nodes: list[Node], *, note: str | None = None) -> ConfigVersion:
    """校验并发布四份产物到 data/out/v<NNNN>/（pipeline 的发布唯一入口）。

    流程：4 份各自 validate_* → check_consistency(主两份) → 任一失败抛 PublishError
    → 原子写入新版本目录 + meta.json（最后写，作为发布点）→ prune(keep=5)。
    """
    _ = store  # 契约签名保留（发布事件暂不落库）
    errors: list[str] = []

    missing = [k for k in ARTIFACT_KEYS if k not in artifacts]
    if missing:
        errors.append(f"缺少产物：{'、'.join(missing)}")
    unknown = sorted(set(artifacts) - set(ARTIFACT_KEYS))
    if unknown:
        errors.append(f"未知产物键：{'、'.join(unknown)}")
    if errors:
        raise PublishError(errors)

    validators = {
        "clash.yaml": validate_clash_yaml,
        "clash-offline.yaml": validate_clash_yaml,
        "shadowrocket.conf": validate_sr_conf,
        "shadowrocket-offline.conf": validate_sr_conf,
    }
    for key, validate in validators.items():
        problems = validate(artifacts[key])
        errors.extend(f"{key}：{p}" for p in problems)
    consistency = check_consistency(artifacts["clash.yaml"], artifacts["shadowrocket.conf"])
    errors.extend(f"一致性校验：{p}" for p in consistency)
    if errors:
        raise PublishError(errors)

    real_nodes = [n for n in nodes if not n.filtered]
    version = next_version(config.out_dir)
    vdir = new_version_dir(config.out_dir, version)

    prev_version = version - 1
    prev_meta: dict[str, Any] | None = None
    if prev_version >= 1:
        meta_path = version_dir(config.out_dir, prev_version) / "meta.json"
        if meta_path.exists():
            prev_meta = read_json(meta_path)
    curr_brief = _node_brief(real_nodes)
    diff_summary = _diff_summary(prev_meta, curr_brief)

    digest = content_hash("".join(artifacts[key] for key in ARTIFACT_KEYS))
    created_at = now_iso()
    meta: dict[str, Any] = {
        "version": version,
        "created_at": created_at,
        "node_count": len(real_nodes),
        "content_hash": digest,
        "diff_summary": diff_summary,
        "note": note,
        "nodes": curr_brief,  # 扩展字段：仅供下一版 diff 使用（无凭据）
    }
    for key in ARTIFACT_KEYS:
        atomic_write_text(vdir / key, artifacts[key])
    write_json(vdir / "meta.json", meta)  # 最后写（发布点，docs/INTERFACES.md §5.4）
    removed_versions = prune_versions(config.out_dir, keep=5)

    logger.info("已发布 v%04d：节点 %d 个，hash %s…，diff（+%d/-%d/改名 %d）%s",
                version, len(real_nodes), digest[:8],
                len(diff_summary["added"]), len(diff_summary["removed"]),
                len(diff_summary["renamed"]),
                f"，清理旧版本 {[f'v{v:04d}' for v in removed_versions]}" if removed_versions else "")
    return ConfigVersion(
        version=version,
        created_at=created_at,
        node_count=len(real_nodes),
        content_hash=digest,
        diff_summary=diff_summary,
        note=note,
    )
