"""节点健康采样与稳定性聚合：定时测延迟/可达性并落库，供「健康时间块条」消费。

与 docs/03 §4 纯净度共用探测实例（同一组 127.0.0.1 端口），采样经
probe_instance_lock 串行化；采样是旁路任务：探测实例不可用时本轮记
unavailable，不影响分发主链路。

单点延迟只代表采样瞬间，本模块的价值在**时间序列**：每轮对全部真实节点
delay 探测一次（经控制 API 直测节点，不切换 PROBE 出口），一行一个
HealthSample；history_report 按 24h 窗口聚合出成功率/均延迟/连续失败，
UI 渲染为逐采样时间块条（绿=通畅 黄=通但慢 红=失败 灰=无数据）。

保留策略：样本默认保留 7 天（远大于展示窗口），每轮采样后清理过期行。
安全纪律：只记录节点名/订阅名/时延，无凭据字段；日志不输出订阅 URL。
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta

from app.config import AppConfig
from app.models import HealthSample, Node
from app.probe import ProbeInstance, probe_instance_lock
from app.store import Store
from app.utils import now_iso

logger = logging.getLogger("subhub.health")

RETENTION_DAYS = 7
"""健康样本保留天数；过期行在每轮采样后清理。"""

SLOW_DELAY_MS = 500
"""「通但慢」分界：块条黄色档阈值（mgmt UI 与 docs/02 测速口径一致）。"""


@dataclass(frozen=True)
class HealthReport:
    """一轮采样的摘要（调度状态展示用）。"""

    checked: int
    ok_count: int
    unavailable: bool

    @property
    def ok_rate(self) -> float:
        return self.ok_count / self.checked if self.checked else 0.0


def sample_once(nodes: list[Node], *, config: AppConfig, store: Store) -> HealthReport:
    """对全部真实节点做一轮延迟/可达性采样并落库；异常不外抛（旁路安全网）。

    逐节点经探测实例控制 API 的 /proxies/<name>/delay 直测（不走 PROBE 出口
    切换，故仅 delay 可用性，与纯净度的 select+lookup 分工不同）。
    """
    real = [n for n in nodes if not n.filtered]
    if not real:
        return HealthReport(checked=0, ok_count=0, unavailable=False)
    at = now_iso()
    probe = ProbeInstance(config, real)
    samples: list[HealthSample] = []
    unavailable = False
    try:
        with probe_instance_lock():
            try:
                started = probe.start()
            except Exception as exc:  # noqa: BLE001 —— 启动异常降级为 unavailable
                logger.warning("探测实例启动异常，本轮健康采样放弃：%s", exc)
                started = False
            if not started:
                logger.warning("探测实例不可用，本轮健康采样放弃（%d 个节点）", len(real))
                return HealthReport(checked=len(real), ok_count=0, unavailable=True)
            for node in real:
                if not probe.is_available():
                    logger.warning("探测实例中途失效，本轮健康采样提前结束（已采 %d 个）",
                                   len(samples))
                    unavailable = True
                    break
                delay = probe.delay(node.name)
                samples.append(HealthSample(
                    node_name=node.name, source_sub=node.source_sub,
                    checked_at=at, delay_ms=delay,
                ))
    finally:
        try:
            probe.stop()
        except Exception as exc:  # noqa: BLE001
            logger.warning("探测实例停止失败（忽略）：%s", exc)
    if samples:
        store.save_health_samples(samples)
    pruned = store.prune_health_samples(
        before=(datetime.now() - timedelta(days=RETENTION_DAYS)).isoformat())
    ok_count = sum(1 for s in samples if s.ok)
    logger.info("健康采样完成：%d/%d 可达（轮时刻 %s），清理过期样本 %d 行",
                ok_count, len(samples), at, max(pruned, 0))
    return HealthReport(checked=len(samples), ok_count=ok_count, unavailable=unavailable)


def history_report(store: Store, *, window_hours: int = 24) -> dict:
    """按窗口聚合各节点健康样本，产出时间块条数据（UI 直接消费）。

    每节点：success_rate/avg_delay/down_streak 汇总 + 逐样本块序列；
    排序：成功率降序 → 均延迟升序 → 节点名。窗口内无样本的节点不产出
    （UI 显示「暂无采样数据」）。
    """
    since = (datetime.now() - timedelta(hours=window_hours)).isoformat()
    samples = store.list_health_samples(since=since)
    by_node: dict[tuple[str, str], list[HealthSample]] = {}
    for s in samples:
        by_node.setdefault((s.node_name, s.source_sub), []).append(s)

    rows: list[dict] = []
    for (node_name, source_sub), group in by_node.items():
        group.sort(key=lambda s: s.checked_at)
        oks = [s.delay_ms for s in group if s.ok]
        down_streak = 0
        for s in reversed(group):
            if s.ok:
                break
            down_streak += 1
        rows.append({
            "node_name": node_name,
            "source_sub": source_sub,
            "sample_count": len(group),
            "ok_rate": round(len(oks) / len(group), 4) if group else 0.0,
            "avg_delay": round(sum(oks) / len(oks)) if oks else None,
            "max_delay": max(oks) if oks else None,
            "last_delay": group[-1].delay_ms,
            "last_checked_at": group[-1].checked_at,
            "down_streak": down_streak,
            "samples": [{"checked_at": s.checked_at, "delay_ms": s.delay_ms}
                        for s in group],
        })
    rows.sort(key=lambda r: (-r["ok_rate"],
                             r["avg_delay"] if r["avg_delay"] is not None else 1 << 30,
                             r["node_name"]))
    return {
        "window_hours": window_hours,
        "sample_total": len(samples),
        "generated_at": now_iso(),
        "nodes": rows,
    }
