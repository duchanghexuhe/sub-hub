"""GitHub 吞吐量扫描：经探测实例逐节点实测 GitHub release 资产下载速度。

背景（2026-10-07 实测）：Fastly CDN（objects / pkg-containers.githubusercontent.com）
对机场出口 IP 做吞吐量限速，且按出口区分——同一时刻 6 个香港节点全部 ~0.1MB/s
而日本/美国 10~27MB/s；mihomo url-test 只测延迟（HTTP RTT）对此完全失明，
按延迟自动选路会把 GitHub 流量精准送进被限速的出口。本模块用真实下载补上
这个盲区：结果落库 gh_speed_samples，渲染时驱动 🐱 GitHub 组生成
「🏆 GitHub 优选」（fallback，成员=实测最快 top-N）并重排节点，随配置下发。

与 purity/health 的分工：共用 ProbeInstance 与 probe_instance_lock 串行；
health 只测延迟（控制 API 直测，不切出口），purity 测出口 IP 属性（切出口 +
查外部 API），本模块测出口吞吐量（切出口 + 经混合端口流式下载）。

测速目标钉死一个 Fastly 路径的 release 资产（约 100MB，5 秒窗口内读不完），
与 ghcr 镜像层 / release 下载的实际路径同为 Fastly——测谁选谁，不猜代理。
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta

import httpx

from app.models import GhSpeedSample, Node
from app.probe import ProbeInstance, probe_instance_lock
from app.store import Store
from app.utils import now_iso

logger = logging.getLogger("subhub.ghspeed")

PROBE_DOWNLOAD_URL = (
    "https://github.com/prometheus/prometheus/releases/download/"
    "v2.53.0/prometheus-2.53.0.windows-amd64.zip"
)
RETENTION_DAYS = 14
_CHUNK_SIZE = 65536


@dataclass
class GhspeedReport:
    """一轮吞吐量扫描的摘要（入库行数/失败数与 purity 报告口径一致）。"""

    samples: list[GhSpeedSample] = field(default_factory=list)
    unavailable: bool = False
    checked: int = 0
    skipped: int = 0


def _measure_download(proxy_url: str | None, seconds: float, transport=None) -> float | None:
    """经探测实例混合端口流式下载，返回 MB/s；没读到任何数据返回 None。

    proxy_url=None 时直连（仅测试注入 transport 用）；窗口内读到部分数据后流中断，
    按已读字节/实际耗时计（部分数据也是真实信号，下轮扫描会修正）；连接层直接
    失败（一个字节都没有）才是 None。
    """
    deadline = time.monotonic() + seconds
    total = 0
    started = time.monotonic()
    try:
        # trust_env=False：环境代理变量会让流量绕开节点出口，测出来的是别的路径；
        # follow_redirects=True：release 资产 302 到 objects.githubusercontent.com
        # （httpx 默认不跟随，3xx 不触发 raise_for_status，会静默读到 0 字节——
        # 2026-10-07 全量测速静默跳过的根因）
        with httpx.Client(
            proxy=proxy_url,
            trust_env=False,
            follow_redirects=True,
            transport=transport,
            timeout=httpx.Timeout(connect=10.0, read=15.0, write=10.0, pool=10.0),
        ) as client:
            with client.stream("GET", PROBE_DOWNLOAD_URL) as resp:
                resp.raise_for_status()
                for chunk in resp.iter_bytes(chunk_size=_CHUNK_SIZE):
                    total += len(chunk)
                    if time.monotonic() >= deadline:
                        break
    except Exception as exc:  # noqa: BLE001 —— 单节点失败不中断整轮
        logger.debug("测速下载中断（已读 %.1f MB）：%s", total / 1048576, exc)
    elapsed = max(time.monotonic() - started, 0.001)
    if total == 0:
        return None
    return total / elapsed / 1048576.0


def sweep(nodes: list[Node], *, config, store: Store,
          probe_factory=None, measure=None) -> GhspeedReport:
    """全量扫描：逐节点实测 GitHub CDN 下载速度并落库（同轮共用同一时刻）。

    probe_factory/measure 为测试注入点（默认 ProbeInstance / _measure_download）。
    探测实例不可用时整轮放弃（unavailable=True），与 purity 口径一致。
    """
    real_nodes = [n for n in nodes if not n.filtered]
    if not real_nodes:
        logger.info("GitHub 吞吐量扫描：无真实节点，跳过")
        return GhspeedReport()
    factory = probe_factory or ProbeInstance
    measure_fn = measure or _measure_download
    seconds = max(config.ghprobe_seconds, 1.0)

    with probe_instance_lock():
        probe = factory(config, real_nodes)
        try:
            try:
                started = probe.start()
            except Exception as exc:  # noqa: BLE001
                logger.warning("探测实例启动异常，本轮 GitHub 测速放弃：%s", exc)
                started = False
            if not started:
                logger.warning("探测实例不可用，本轮 GitHub 测速放弃（%d 个节点）",
                               len(real_nodes))
                return GhspeedReport(unavailable=True, skipped=len(real_nodes))
            at = now_iso()
            samples: list[GhSpeedSample] = []
            checked = 0
            skipped = 0
            unavailable = False
            for node in real_nodes:
                if not probe.is_available():
                    logger.warning("探测实例中途失效，剩余 %d 个节点跳过测速",
                                   len(real_nodes) - checked - skipped)
                    unavailable = True
                    skipped += len(real_nodes) - checked - skipped
                    break
                # delay 预检：死节点 3 秒内跳过，省掉整段下载窗口
                if probe.delay(node.name) is None:
                    logger.info("节点 %s 不可达（delay 失败），跳过测速", node.name)
                    samples.append(GhSpeedSample(node_name=node.name,
                                                 source_sub=node.source_sub,
                                                 checked_at=at, speed_mbps=None))
                    skipped += 1
                    continue
                if not probe.select(node.name):
                    logger.warning("节点 %s 出口切换失败，跳过测速", node.name)
                    samples.append(GhSpeedSample(node_name=node.name,
                                                 source_sub=node.source_sub,
                                                 checked_at=at, speed_mbps=None))
                    skipped += 1
                    continue
                try:
                    speed = measure_fn(probe.local_proxy_url(), seconds)
                except Exception as exc:  # noqa: BLE001
                    logger.warning("节点 %s 测速异常，记失败：%s", node.name, exc)
                    speed = None
                if speed is None:
                    # delay 已通但下载 0 字节：多半是目标 URL/重定向/出口侧问题，显式告警
                    logger.warning("节点 %s delay 可达但下载 0 字节，记失败", node.name)
                samples.append(GhSpeedSample(node_name=node.name,
                                             source_sub=node.source_sub,
                                             checked_at=at, speed_mbps=speed))
                if speed is None:
                    skipped += 1
                else:
                    checked += 1
            if samples:
                store.save_gh_speed_samples(samples)
            pruned = store.prune_gh_speed_samples(
                before=(datetime.now() - timedelta(days=RETENTION_DAYS)).isoformat())
            ranked = sorted((s for s in samples if s.speed_mbps is not None),
                            key=lambda s: -(s.speed_mbps or 0.0))
            top = (f"{ranked[0].node_name} {ranked[0].speed_mbps:.2f}MB/s"
                   if ranked else "无有效测量")
            logger.info("GitHub 吞吐量扫描完成：%d 实测 / %d 跳过（轮时刻 %s），"
                        "最快 %s，清理过期样本 %d 行",
                        checked, skipped, at, top, max(pruned, 0))
            return GhspeedReport(samples=samples, unavailable=unavailable,
                                 checked=checked, skipped=skipped)
        finally:
            try:
                probe.stop()
            except Exception as exc:  # noqa: BLE001
                logger.warning("探测实例停止失败（忽略）：%s", exc)
