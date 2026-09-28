"""发布流水线（编排核心）：订阅刷新 → 解析 → 清洗 → 渲染 → 校验 → 原子发布。

实现 docs/01「流程 1：订阅 → 配置」与「失败路径与安全网」全表：

- 逐启用订阅抓取（``sub_id`` 显式指定时仅刷新该订阅，不看启停——用户明确动作）；
  抓取失败/超时/失效的订阅沿用其旧快照继续参与渲染（标记 stale，不清空其节点）；
- 401/403、解析异常或清洗后 0 个有效节点 → 抓取状态标记「订阅失效」并告警；
- 多订阅合并后统一清洗（假节点过滤 / 分类 / 重名消歧，消歧依赖合并上下文），
  成功后按订阅整批替换快照（含被滤节点）；
- 渲染输入 = store 当前全量有效节点（含失效订阅的旧快照）——
  「分发可用性 > 数据新鲜度」，绝不向客户端下发空配置或坏配置；
- 渲染时读取库内最新纯净度结果传入 templater，驱动 Claude 专用/备援组
  「评分降序、住宅恒在机房前」排序（docs/02 §2；无数据时静态序回退）；
- 规则集存在「上游/旧缓存/内置基线三者全缺」的空占位文件时拒绝发布
  （docs/01 安全网：空 ChinaMax/CNCIDR 会把国内流量全部改道代理）；
- 渲染或校验任一失败 → 拒绝发布、沿用上一版；有效节点数为 0 → 拒绝发布空配置；
- 发布由 validator.publish 原子完成（.tmp + rename、保留最近 5 版、记录版本号与
  新增/消失/改名 diff）；发布中断残留的版本目录（有目录无 meta.json = 未发布）
  在失败路径与下次运行开始时清理，不留半成品；
- 内容 hash 相对上一版有变化 → 通知 mirror 推送（失败仅记日志，不影响主链路）；
- 发布成功后由后台线程触发 purity 增量扫描（旁路，失败不影响发布）；
- 单节点解析异常逐条落盘 data/parse_anomalies.json（按订阅整批替换），
  供 UI「解析异常节点」列表展示（docs/01 安全网）。

依赖模块（fetcher/parser/cleaner/templater/validator/purity/mirror）按
docs/INTERFACES.md 契约在调用期延迟加载：某模块尚未集成时流水线返回明确的
错误状态而非崩溃；测试可在 sys.modules 注入同契约的假模块以 mock 各环节边界。

日志纪律：只输出订阅别名，不打印完整订阅 URL（如需打码用 app.utils.mask_url_host），
不输出节点凭据字段（Node.credentials）与任何密钥内容。
"""
from __future__ import annotations

import importlib
import logging
import shutil
import threading
from dataclasses import asdict, dataclass, field
from typing import Any

import yaml

from app import utils
from app.config import AppConfig
from app.models import ConfigVersion, FetchStatus, Node, Subscription
from app.store import Store

logger = logging.getLogger("subhub.pipeline")

__all__ = [
    "PipelineResult",
    "run_full_pipeline",
    "refresh_all",
    "rollback_to_version",
    "rollback",
    "current_version",
]

# 四产物的规范顺序（content_hash = 按此序拼接后 sha256，与 validator.publish 一致）
_ARTIFACT_ORDER: tuple[str, ...] = (
    "clash.yaml",
    "shadowrocket.conf",
    "clash-offline.yaml",
    "shadowrocket-offline.conf",
)

# 单节点解析异常记录文件（data/ 下，web /api/parse-anomalies 读取）
_ANOMALIES_FILENAME = "parse_anomalies.json"

_STALE_TAIL = "已沿用旧快照，当前为陈旧配置"

# 回退/镜像 diff 用：节点名列表 + (type, server, port) → 名字 的键位索引
_ProxyKey = tuple[str, str, int]
_ProxySummary = tuple[list[str], dict[_ProxyKey, str]]


@dataclass
class PipelineResult:
    """一次完整流水线的执行结果（web / scheduler 消费）。"""

    published: bool                     # False=校验拦截 / 0 节点 / 任一环节失败，沿用上一版
    version: int | None                 # 本次发布的新版本号；未发布为 None（当前版本用 current_version 查）
    node_count: int                     # 本次参与发布（或拟发布）的有效节点数
    filtered_count: int                 # 本次清洗命中的假节点数
    stale_subs: list[str] = field(default_factory=list)  # 抓取失效/超时但仍用旧快照的订阅别名
    errors: list[str] = field(default_factory=list)      # 中文错误与告警（含 PublishError.errors）


# ---------------------------------------------------------------- 对外入口

def run_full_pipeline(config: AppConfig, store: Store, *, sub_id: int | None = None) -> PipelineResult:
    """完整链路：抓取 → 解析 → 合并清洗 → 快照落盘 → 渲染 4 产物 → 校验 → 原子发布。

    任一环节失败都沿用上一版产物并返回明确状态（errors 中文说明），绝不抛出
    环节内异常；发布成功且内容 hash 有变化时通知 mirror 推送，随后后台触发
    purity 增量扫描。
    """
    logger.info("流水线开始：sub_id=%s", sub_id)
    # 防御：清理上次发布中断残留的半成品目录（有目录无 meta.json）
    _cleanup_unpublished_dirs(config)

    errors: list[str] = []
    stale_subs: list[str] = []

    subs = _resolve_target_subscriptions(store, sub_id, errors)
    if subs is None:
        return PipelineResult(published=False, version=None, node_count=0,
                              filtered_count=0, stale_subs=stale_subs, errors=errors)

    # ---- 阶段 1：逐订阅抓取 + 解析（失败沿用旧快照，不清空其节点）----
    parsed_by_sub, parse_anomalies, parse_attempted = _fetch_and_parse(
        config, store, subs, errors, stale_subs
    )
    # 解析异常节点落盘（docs/01 安全网：UI「解析异常节点」列表可见），失败不影响主链路
    _record_parse_anomalies(config, parse_attempted, parse_anomalies)

    # ---- 阶段 2：合并清洗 + 按订阅整批替换快照 ----
    kept, filtered, filtered_count = _clean_and_replace(store, parsed_by_sub, errors, stale_subs)

    # ---- 阶段 3：渲染（输入 = store 当前全量有效节点，含失效订阅旧快照）----
    render_nodes = store.list_nodes(filtered=False)
    if not render_nodes:
        logger.warning("拒绝发布：有效节点数为 0（空配置安全网），沿用上一版产物")
        errors.append("无有效节点，拒绝发布空配置，沿用上一版产物")
        return PipelineResult(published=False, version=None, node_count=0,
                              filtered_count=filtered_count, stale_subs=stale_subs, errors=errors)

    # ---- 阶段 3 前置：规则安全网（docs/01：仅当缓存与基线都缺失时才拒绝发布）----
    placeholder_rules = _placeholder_rule_names(config)
    if placeholder_rules:
        detail = "、".join(placeholder_rules)
        logger.warning("拒绝发布：规则集 %s 无上游内容且无旧缓存/内置基线（空占位），沿用上一版产物", detail)
        errors.append(f"规则集 {detail} 无任何可用来源（空占位），拒绝发布，沿用上一版产物")
        return PipelineResult(published=False, version=None, node_count=len(render_nodes),
                              filtered_count=filtered_count, stale_subs=stale_subs, errors=errors)

    try:
        artifacts = _render_artifacts(config, store, render_nodes)
    except Exception as exc:
        _record_stage_failure(errors, "渲染", exc)
        return PipelineResult(published=False, version=None, node_count=len(render_nodes),
                              filtered_count=filtered_count, stale_subs=stale_subs, errors=errors)

    # ---- 阶段 4：校验 + 原子发布（validator.publish：.tmp + rename，保留 5 版）----
    prev_meta = _load_version_meta(config, utils.latest_version(config.out_dir))
    meta = _publish_artifacts(config, store, artifacts, render_nodes, errors)
    if meta is None:
        return PipelineResult(published=False, version=None, node_count=len(render_nodes),
                              filtered_count=filtered_count, stale_subs=stale_subs, errors=errors)

    logger.info("配置已发布：v%04d，有效节点 %d 个（本轮过滤 %d 个）",
                meta.version, meta.node_count, filtered_count)

    # ---- 阶段 5：内容有变化才通知镜像推送（失败仅记日志）----
    if prev_meta is None or prev_meta.content_hash != meta.content_hash:
        _push_mirror_safe(config)
    else:
        logger.info("产物内容与上一版一致，跳过镜像推送")

    # ---- 阶段 6：后台触发纯净度增量扫描（旁路，失败不影响发布）----
    _start_purity_scan_bg(config, store, render_nodes)

    logger.info("流水线完成：v%04d，节点 %d，失效订阅：%s",
                meta.version, meta.node_count, "、".join(stale_subs) if stale_subs else "无")
    return PipelineResult(published=True, version=meta.version, node_count=meta.node_count,
                          filtered_count=filtered_count, stale_subs=stale_subs, errors=errors)


def refresh_all(config: AppConfig, store: Store, *, sub_id: int | None = None) -> PipelineResult:
    """刷新全部启用订阅并重建配置（run_full_pipeline 的别名，任务书口径 refresh_all）。"""
    return run_full_pipeline(config, store, sub_id=sub_id)


def rollback_to_version(config: AppConfig, store: Store, version: int) -> ConfigVersion:
    """回退：把 data/out/v<version> 的 4 份产物复制为新版本目录（meta.note='回退到 v<NNNN>'）。

    data/out 的第二个合法写入口（INTERFACES §4）。发布点与 publish 相同：
    产物先写完、meta.json 最后写；回退前对源产物做本地合法性检查（YAML 可解析、
    proxies 非空、conf 三段齐全），失败拒绝回退并抛错。保留最近 5 版。
    """
    if version not in utils.list_versions(config.out_dir):
        raise FileNotFoundError(f"版本 v{version:04d} 不存在，无法回退")
    src = utils.version_dir(config.out_dir, version)

    artifacts: dict[str, str] = {}
    for name in _ARTIFACT_ORDER:
        path = src / name
        if not path.is_file():
            raise FileNotFoundError(f"版本 v{version:04d} 缺少产物 {name}，无法回退")
        artifacts[name] = path.read_text(encoding="utf-8")
    _sanity_check_artifacts(artifacts)

    new_summary = _proxy_summary(artifacts["clash.yaml"])
    prev_meta = current_version(config)
    prev_summary: _ProxySummary = ([], {})
    if prev_meta is not None:
        prev_path = utils.version_dir(config.out_dir, prev_meta.version) / "clash.yaml"
        try:
            prev_summary = _proxy_summary(prev_path.read_text(encoding="utf-8"))
        except OSError:
            prev_summary = ([], {})

    new_version = utils.next_version(config.out_dir)
    dst = utils.new_version_dir(config.out_dir, new_version)
    try:
        for name in _ARTIFACT_ORDER:
            utils.atomic_write_text(dst / name, artifacts[name])
        meta = ConfigVersion(
            version=new_version,
            created_at=utils.now_iso(),
            node_count=len(new_summary[0]),
            content_hash=utils.content_hash("".join(artifacts[name] for name in _ARTIFACT_ORDER)),
            diff_summary=_diff_summary(prev_summary, new_summary),
            note=f"回退到 v{version:04d}",
        )
        utils.write_json(dst / "meta.json", asdict(meta))   # meta.json 最后写 = 发布点
    except Exception:
        shutil.rmtree(dst, ignore_errors=True)              # 写入中断不留半成品
        raise
    utils.prune_versions(config.out_dir, keep=5)
    logger.info("回退完成：v%04d → v%04d（节点 %d 个）", version, new_version, meta.node_count)

    # 回退后的配置同样要让公网镜像拿到（内容有变化才推；失败仅记日志）
    if prev_meta is None or prev_meta.content_hash != meta.content_hash:
        _push_mirror_safe(config)
    return meta


def rollback(config: AppConfig, store: Store, version: int) -> ConfigVersion:
    """rollback_to_version 的别名（任务书口径 rollback(version)）。"""
    return rollback_to_version(config, store, version)


def current_version(config: AppConfig) -> ConfigVersion | None:
    """当前生效版本（data/out 下最新且带 meta.json 的版本）；从未发布过返回 None。"""
    return _load_version_meta(config, utils.latest_version(config.out_dir))


# ---------------------------------------------------------------- 阶段 1：抓取 + 解析

def _resolve_target_subscriptions(
    store: Store, sub_id: int | None, errors: list[str]
) -> list[Subscription] | None:
    """确定本次要抓取的订阅；返回 None 表示直接终止（显式 sub_id 不存在）。"""
    if sub_id is not None:
        sub = store.get_subscription(int(sub_id))
        if sub is None:
            errors.append(f"订阅不存在：id={sub_id}")
            return None
        return [sub]
    enabled = [s for s in store.list_subscriptions() if s.enabled]
    if not enabled:
        errors.append("无启用订阅，本次不抓取；将基于现有节点快照重算配置")
        return []
    return enabled


def _fetch_and_parse(
    config: AppConfig, store: Store, subs: list[Subscription],
    errors: list[str], stale_subs: list[str],
) -> tuple[dict[str, tuple[str, list[Node]]], list[dict], set[str]]:
    """逐订阅抓取并解析。返回 ({订阅别名: (抓取时间, 解析节点)}, 解析异常记录, 实际解析过的订阅集)。

    状态落盘归 pipeline（fetcher 契约）：任何结果都 set_fetch_status；OK 时才
    set_userinfo。抓取失败/超时/解析为空的订阅不 replace_nodes，旧快照原样保留。
    单节点解析异常逐条收集（docs/01 安全网），由调用方落盘供 UI 展示。
    """
    parsed_by_sub: dict[str, tuple[str, list[Node]]] = {}
    anomalies: list[dict] = []
    parse_attempted: set[str] = set()
    try:
        fetcher = _import_stage("fetcher")
        parser_mod = _import_stage("parser")
    except ModuleNotFoundError as exc:
        logger.error("依赖模块缺失，抓取阶段终止：%s", exc.name)
        errors.append(f"依赖模块缺失，无法抓取/解析：{exc.name}，全部订阅沿用旧快照")
        return parsed_by_sub, anomalies, parse_attempted

    for sub in subs:
        try:
            fr = fetcher.fetch_subscription(sub, config=config)
        except Exception as exc:  # 契约外异常：防御兜底，视同抓取失败
            logger.warning("订阅 %s 抓取异常：%s，沿用旧快照", sub.name, exc)
            store.set_fetch_status(sub.id, FetchStatus.FAILED)
            stale_subs.append(sub.name)
            errors.append(f"订阅 {sub.name} 抓取异常：{exc}，{_STALE_TAIL}")
            continue

        store.set_fetch_status(sub.id, fr.status)
        if fr.status != FetchStatus.OK:
            reason = fr.error or fr.status.label
            logger.warning("订阅失效：%s（%s），%s", sub.name, reason, _STALE_TAIL)
            stale_subs.append(sub.name)
            errors.append(f"订阅失效：{sub.name}（{reason}），{_STALE_TAIL}")
            continue

        store.set_userinfo(sub.id, fr.userinfo)
        try:
            nodes, sub_anomalies = _parse_subscription(parser_mod, fr.content or b"", sub.name)
        except Exception as exc:
            logger.warning("订阅 %s 解析异常：%s，沿用旧快照", sub.name, exc)
            store.set_fetch_status(sub.id, FetchStatus.FAILED)
            stale_subs.append(sub.name)
            errors.append(f"订阅 {sub.name} 解析异常：{exc}，{_STALE_TAIL}")
            continue
        parse_attempted.add(sub.name)
        anomalies.extend(sub_anomalies)
        if not nodes:
            # 0 个有效节点 → 订阅失效（docs/01 安全网），沿用旧快照
            logger.warning("订阅失效：%s（返回 0 个节点），%s", sub.name, _STALE_TAIL)
            store.set_fetch_status(sub.id, FetchStatus.INVALID)
            stale_subs.append(sub.name)
            errors.append(f"订阅失效：{sub.name}（返回 0 个节点），{_STALE_TAIL}")
            continue
        parsed_by_sub[sub.name] = (fr.fetched_at, nodes)

    return parsed_by_sub, anomalies, parse_attempted


def _parse_subscription(parser_mod: Any, content: bytes, sub_name: str) -> tuple[list[Node], list[dict]]:
    """调用 parser：优先 parse_payload_detailed 拿单节点解析异常清单（docs/01 安全网），
    旧契约 parser（仅 parse_payload）退回无异常记录模式。返回 (节点, 异常 dict 列表)。"""
    detailed = getattr(parser_mod, "parse_payload_detailed", None)
    if detailed is None:
        return list(parser_mod.parse_payload(content, sub_name)), []
    nodes, parsed_anomalies = detailed(content, sub_name)
    serialized: list[dict] = []
    for item in parsed_anomalies or []:
        to_dict = getattr(item, "to_dict", None)
        serialized.append(to_dict() if callable(to_dict) else dict(vars(item)))
    return list(nodes), serialized


def _record_parse_anomalies(config: AppConfig, attempted_subs: set[str], anomalies: list[dict]) -> None:
    """解析异常节点落盘 data/parse_anomalies.json（docs/01 安全网：UI「解析异常节点」列表可见）。

    按订阅整批替换：本次实际解析过的订阅以其最新异常列表覆盖（无异常则清空），
    未重新解析的订阅保留上次记录（与节点快照的整批替换语义对齐）。
    记录只含订阅别名/节点名或协议片段/中文原因，不含凭据与订阅 URL。
    落盘失败只记日志，不影响主链路。
    """
    if not attempted_subs:
        return
    path = config.data_dir / _ANOMALIES_FILENAME
    prev = utils.read_json(path, None)
    subs: dict[str, Any] = {}
    if isinstance(prev, dict) and isinstance(prev.get("subs"), dict):
        subs = {str(k): v for k, v in prev["subs"].items() if isinstance(v, dict)}
    by_sub: dict[str, list[dict]] = {}
    for item in anomalies:
        by_sub.setdefault(str(item.get("source_sub", "")), []).append(item)
    for sub_name in attempted_subs:
        subs[sub_name] = {"checked_at": utils.now_iso(), "anomalies": by_sub.get(sub_name, [])}
    try:
        utils.write_json(path, {"updated_at": utils.now_iso(), "subs": subs})
    except OSError as exc:
        logger.warning("解析异常记录落盘失败（不影响主链路）：%s", exc)


# ---------------------------------------------------------------- 阶段 2：清洗 + 快照

def _clean_and_replace(
    store: Store,
    parsed_by_sub: dict[str, tuple[str, list[Node]]],
    errors: list[str],
    stale_subs: list[str],
) -> tuple[list[Node], list[Node], int]:
    """合并全量节点做一次清洗（消歧依赖跨订阅上下文），按订阅整批替换快照。

    单订阅刷新（parsed_by_sub 只含本次抓取的订阅）时，把库里**其他订阅**的现有
    节点还原为消歧前原名并入清洗输入——否则跨订阅同名节点的「 [别名]」后缀会
    因上下文缺失而丢失，下次全量又加回，节点名来回翻转（docs/02 §1 消歧语义、
    docs/04 验收 #8）。上下文节点只参与消歧判定，其快照不回写。
    清洗后某订阅 0 个有效节点 → 标记「订阅失效」且不替换其快照（保留旧节点）。
    清洗环节自身失败 → 所有订阅都沿用旧快照。
    返回 (kept, filtered, filtered_count)，只含本次抓取订阅的节点（同口径计数）。
    """
    kept: list[Node] = []
    filtered: list[Node] = []
    if not parsed_by_sub:
        return kept, filtered, 0

    refreshed_subs = set(parsed_by_sub)
    merged = [n for _, nodes in parsed_by_sub.values() for n in nodes]
    # 消歧上下文：其他订阅的存量节点还原为消歧前原名（disambiguate 按原名聚合判定）
    for node in store.list_nodes():
        if node.source_sub in refreshed_subs:
            continue
        node.name = node.orig_name or node.name
        node.orig_name = None
        merged.append(node)
    try:
        cleaner_mod = _import_stage("cleaner")
        result = cleaner_mod.clean(merged)
        kept = [n for n in result.kept if n.source_sub in refreshed_subs]
        filtered = [n for n in result.filtered if n.source_sub in refreshed_subs]
    except ModuleNotFoundError as exc:
        logger.error("依赖模块缺失，清洗跳过：%s", exc.name)
        errors.append(f"依赖模块缺失，无法清洗：{exc.name}，全部订阅沿用旧快照")
        return [], [], 0
    except Exception as exc:
        logger.exception("节点清洗失败，全部订阅沿用旧快照")
        errors.append(f"节点清洗失败：{exc}，全部订阅沿用旧快照")
        return [], [], 0

    for sub_name, (fetched_at, _nodes) in parsed_by_sub.items():
        sub_kept = [n for n in kept if n.source_sub == sub_name]
        if not sub_kept:
            logger.warning("订阅失效：%s（清洗后 0 个有效节点），%s", sub_name, _STALE_TAIL)
            sub = store.find_subscription_by_name(sub_name)
            if sub is not None:
                store.set_fetch_status(sub.id, FetchStatus.INVALID)
            stale_subs.append(sub_name)
            errors.append(f"订阅失效：{sub_name}（清洗后 0 个有效节点），{_STALE_TAIL}")
            continue
        sub_filtered = [n for n in filtered if n.source_sub == sub_name]
        store.replace_nodes(sub_name, sub_kept + sub_filtered, fetched_at=fetched_at)

    logger.info("节点清洗完成：有效 %d 个，过滤 %d 个，快照已更新（%s）",
                len(kept), len(filtered), "、".join(parsed_by_sub))
    return kept, filtered, len(filtered)


# ---------------------------------------------------------------- 阶段 3：渲染

def _placeholder_rule_names(config: AppConfig) -> list[str]:
    """列出 data/rules/ 里的空规则占位文件（rulesync 提供探测；模块缺失或探测
    失败时返回空 = 不拦截发布，可用性优先，见 docs/01 总原则）。"""
    try:
        rulesync = _import_stage("rulesync")
    except ModuleNotFoundError:
        return []
    detect = getattr(rulesync, "placeholder_rule_files", None)
    if detect is None:
        return []
    try:
        return list(detect(config))
    except Exception:
        logger.warning("规则占位探测异常（不拦截本次发布）", exc_info=True)
        return []


def _render_artifacts(config: AppConfig, store: Store, nodes: list[Node]) -> dict[str, str]:
    """渲染 4 份产物（主版本 + 离线自包含版）。渲染失败向上抛，由调用方转为错误状态。

    纯净度数据回灌（docs/02 §2）：读取库内最新检测结果传入渲染，驱动
    Claude 专用/备援组「评分降序、住宅恒在机房前」排序；尚无检测数据时
    templater 按静态序回退（实测数据优先于节点名猜测）。
    """
    templater = _import_stage("templater")
    rules = templater.load_rules_manifest()
    purity = store.latest_purity_results()
    return {
        "clash.yaml": templater.render_clash(nodes, config=config, rules=rules,
                                             offline=False, purity=purity),
        "shadowrocket.conf": templater.render_sr_conf(nodes, config=config, rules=rules,
                                                      offline=False, purity=purity),
        "clash-offline.yaml": templater.render_clash(nodes, config=config, rules=rules,
                                                     offline=True, purity=purity),
        "shadowrocket-offline.conf": templater.render_sr_conf(nodes, config=config, rules=rules,
                                                              offline=True, purity=purity),
    }


# ---------------------------------------------------------------- 阶段 4：校验 + 发布

def _publish_artifacts(
    config: AppConfig, store: Store, artifacts: dict[str, str], nodes: list[Node],
    errors: list[str],
) -> ConfigVersion | None:
    """调用 validator.publish 完成校验与原子发布；失败清理半成品并返回 None。"""
    try:
        validator = _import_stage("validator")
    except ModuleNotFoundError as exc:
        logger.error("依赖模块缺失，无法校验发布：%s", exc.name)
        errors.append(f"依赖模块缺失，无法校验发布：{exc.name}，沿用上一版产物")
        return None
    publish_error = getattr(validator, "PublishError", None)
    try:
        return validator.publish(config, store, artifacts, nodes=nodes)
    except Exception as exc:
        _cleanup_unpublished_dirs(config)   # 发布中途异常不留半成品
        if publish_error is not None and isinstance(exc, publish_error):
            detail = "；".join(getattr(exc, "errors", None) or [str(exc)])
            logger.warning("发布被校验拦截，沿用上一版产物：%s", detail)
            errors.append("发布被校验拦截，沿用上一版产物")
            errors.extend(getattr(exc, "errors", None) or [])
        else:
            logger.exception("发布过程异常，沿用上一版产物")
            errors.append(f"发布过程异常：{exc}，沿用上一版产物")
        return None


def _cleanup_unpublished_dirs(config: AppConfig) -> list[int]:
    """清理发布中断残留的版本目录（有目录而无 meta.json = 未发布成功）。"""
    removed: list[int] = []
    for v in utils.list_versions(config.out_dir):
        vdir = utils.version_dir(config.out_dir, v)
        if (vdir / "meta.json").is_file():
            continue
        shutil.rmtree(vdir, ignore_errors=True)
        if not vdir.exists():
            removed.append(v)
            logger.warning("已清理未完成的版本目录 v%04d（上次发布中断残留）", v)
    return removed


# ---------------------------------------------------------------- 旁路：mirror / purity

def _push_mirror_safe(config: AppConfig) -> None:
    """通知 mirror 推送当前产物。任何失败只记日志，绝不影响主链路。"""
    try:
        mirror = _import_stage("mirror")
        result = mirror.push_current(config)
        if getattr(result, "ok", False):
            # 不记录镜像 URL（含访问凭据），只记提供方
            logger.info("镜像推送完成（provider=%s）", getattr(result, "provider", None))
        else:
            logger.warning("镜像推送未执行：%s", getattr(result, "error", None))
    except ModuleNotFoundError:
        logger.info("镜像模块未部署，跳过推送")
    except Exception:
        logger.warning("镜像推送异常（不影响发布）", exc_info=True)


def _purity_scan_safe(config: AppConfig, store: Store, nodes: list[Node]) -> None:
    """纯净度增量扫描线程体（仅新增/未测节点）。任何失败只记日志。"""
    try:
        purity = _import_stage("purity")
        report = purity.scan(nodes, config=config, store=store, full=False)
        logger.info("纯净度增量扫描完成：检测 %d 个，跳过 %d 个",
                    getattr(report, "checked", -1), getattr(report, "skipped", -1))
    except ModuleNotFoundError:
        logger.info("纯净度模块未部署，跳过增量扫描")
    except Exception:
        logger.warning("纯净度增量扫描失败（不影响发布）", exc_info=True)


def _start_purity_scan_bg(config: AppConfig, store: Store, nodes: list[Node]) -> None:
    """发布后在后台线程触发增量扫描（docs/01：旁路，不阻塞分发）。"""
    thread = threading.Thread(
        target=_purity_scan_safe, args=(config, store, nodes), name="subhub-purity-scan",
        daemon=True,
    )
    thread.start()


# ---------------------------------------------------------------- 版本元数据 / diff

def _load_version_meta(config: AppConfig, version: int | None) -> ConfigVersion | None:
    """读取 v<version>/meta.json 为 ConfigVersion；缺失或损坏返回 None（宽松解析）。"""
    if version is None:
        return None
    data = utils.read_json(utils.version_dir(config.out_dir, version) / "meta.json")
    if not isinstance(data, dict) or "version" not in data:
        logger.warning("版本元数据缺失或损坏：v%04d", version)
        return None
    return ConfigVersion(
        version=int(data["version"]),
        created_at=str(data.get("created_at", "")),
        node_count=int(data.get("node_count", 0)),
        content_hash=str(data.get("content_hash", "")),
        diff_summary=dict(data.get("diff_summary") or {}),
        note=data.get("note"),
    )


def _proxy_summary(clash_text: str) -> _ProxySummary:
    """从 clash.yaml 文本提取节点名列表与 (type, server, port)→名字 键位索引。"""
    names: list[str] = []
    keyed: dict[_ProxyKey, str] = {}
    try:
        doc = yaml.safe_load(clash_text)
    except yaml.YAMLError:
        return names, keyed
    proxies = doc.get("proxies") if isinstance(doc, dict) else None
    for item in proxies or []:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name", ""))
        names.append(name)
        try:
            port = int(item.get("port", 0))
        except (TypeError, ValueError):
            port = 0
        keyed[(str(item.get("type", "")), str(item.get("server", "")), port)] = name
    return names, keyed


def _diff_summary(old: _ProxySummary, new: _ProxySummary) -> dict[str, Any]:
    """节点 diff：新增 / 消失 / 改名（同名位 (type, server, port) 不变视为改名）。"""
    old_names, old_keys = old
    new_names, new_keys = new
    old_set, new_set = set(old_names), set(new_names)

    renamed: list[list[str]] = []
    matched_old: set[str] = set()
    matched_new: set[str] = set()
    for key, new_name in new_keys.items():
        old_name = old_keys.get(key)
        if old_name is not None and old_name != new_name \
                and old_name in old_set and new_name in new_set:
            renamed.append([old_name, new_name])
            matched_old.add(old_name)
            matched_new.add(new_name)

    added = [n for n in new_names if n not in old_set and n not in matched_new]
    removed = [n for n in old_names if n not in new_set and n not in matched_old]
    return {"added": added, "removed": removed, "renamed": renamed}


def _sanity_check_artifacts(artifacts: dict[str, str]) -> None:
    """回退前的本地安全检查；不合法抛 ValueError（中文说明），绝不回退到坏产物。"""
    errors: list[str] = []
    try:
        doc = yaml.safe_load(artifacts["clash.yaml"])
    except yaml.YAMLError as exc:
        errors.append(f"clash.yaml 不是合法 YAML：{exc}")
        doc = None
    if doc is not None and (not isinstance(doc, dict) or not doc.get("proxies")):
        errors.append("clash.yaml 缺少 proxies 列表")
    for name in ("shadowrocket.conf", "shadowrocket-offline.conf"):
        for section in ("[Proxy]", "[Proxy Group]", "[Rule]"):
            if section not in artifacts[name]:
                errors.append(f"{name} 缺少 {section} 段")
    if errors:
        raise ValueError("回退被拒绝，源产物非法：" + "；".join(errors))


# ---------------------------------------------------------------- 杂项

def _import_stage(module_name: str):
    """按契约延迟加载 app.<module>（集成前缺失抛 ModuleNotFoundError，由调用方转为状态）。"""
    return importlib.import_module(f"app.{module_name}")


def _record_stage_failure(errors: list[str], stage: str, exc: Exception) -> None:
    """统一记录环节失败的中文错误（日志不含 URL 与凭据）。"""
    if isinstance(exc, ModuleNotFoundError):
        logger.error("依赖模块缺失，%s 阶段终止：%s", stage, exc.name)
        errors.append(f"依赖模块缺失，{stage}失败：{exc.name}，沿用上一版产物")
    else:
        logger.exception("%s失败，沿用上一版产物", stage)
        errors.append(f"{stage}失败：{exc}，沿用上一版产物")
