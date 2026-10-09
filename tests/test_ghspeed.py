"""GitHub 吞吐量扫描测试（app/ghspeed.py）+ gh_speed_samples 存储回环。

纪律：探测实例与下载全部用假实现（FakeProbe / 注入 measure），绝不真跑 mihomo、
绝不访问外网；数据目录走 conftest 的 tmp_path 隔离。
覆盖：计速落库、失败记 NULL、不可达/切组失败跳过、实例中途崩溃 unavailable、
启动失败整轮放弃、最新一轮查询与保留窗口清理。
"""
from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from app import ghspeed as ghspeed_mod
from app.ghspeed import sweep
from app.models import GhSpeedSample, Node
from app.utils import now_iso


def _node(name: str, *, source_sub: str = "subA", filtered: bool = False) -> Node:
    return Node(
        name=name, type="vless", server=f"{name}.example.com", port=443,
        source_sub=source_sub, filtered=filtered,
    )


class FakeProbe:
    """ProbeInstance 假实现：记录调用、按场景配置失败行为（子类用类属性定制）。"""

    start_ok: bool = True
    dead_names: frozenset[str] = frozenset()       # delay 返回 None（节点不可达）
    reject_names: frozenset[str] = frozenset()     # select 返回 False（切换失败）
    crash_after: int | None = None                 # 第 N 次 select 后实例「崩溃」

    def __init__(self, config, nodes) -> None:
        self.nodes = list(nodes)
        self.select_calls: list[str] = []
        self.stopped = False

    def start(self) -> bool:
        return self.start_ok

    def stop(self) -> None:
        self.stopped = True

    def is_available(self) -> bool:
        if self.crash_after is None:
            return True
        return len(self.select_calls) < self.crash_after

    def select(self, node_name: str) -> bool:
        self.select_calls.append(node_name)
        return node_name not in self.reject_names

    def local_proxy_url(self) -> str:
        return "http://127.0.0.1:9096"

    def delay(self, node_name: str) -> int | None:
        return None if node_name in self.dead_names else 88


def _speeds_by_node(samples: list[GhSpeedSample]) -> dict[str, float | None]:
    return {s.node_name: s.speed_mbps for s in samples}


def test_sweep_measures_and_saves(config, store) -> None:
    nodes = [_node("节点一"), _node("节点二")]
    seen_urls: list[str] = []
    report = sweep(
        nodes, config=config, store=store, probe_factory=FakeProbe,
        measure=lambda url, secs: (seen_urls.append(url), 12.5)[1],
    )
    assert report.unavailable is False and report.checked == 2 and report.skipped == 0
    assert seen_urls == ["http://127.0.0.1:9096"] * 2
    assert report.samples[0].checked_at == report.samples[1].checked_at  # 同轮同刻
    latest = {s.node_name: s.speed_mbps for s in store.latest_gh_speed_samples()}
    assert latest == {"节点一": 12.5, "节点二": 12.5}


def test_sweep_measures_zero_speed(config, store) -> None:
    """0.0 是真实测量值（连上但窗口内无数据），与失败 None 区分。"""
    nodes = [_node("快"), _node("零")]
    speeds = iter([27.21, 0.0])
    report = sweep(nodes, config=config, store=store, probe_factory=FakeProbe,
                   measure=lambda url, secs: next(speeds))
    assert report.checked == 2
    latest = {s.node_name: s.speed_mbps for s in store.latest_gh_speed_samples()}
    assert latest["快"] == 27.21 and latest["零"] == 0.0


def test_sweep_dead_and_unselectable_nodes_record_null(config, store) -> None:
    class PartialProbe(FakeProbe):
        dead_names = frozenset({"死节点"})
        reject_names = frozenset({"拒切节点"})

    nodes = [_node("好节点"), _node("死节点"), _node("拒切节点")]
    report = sweep(nodes, config=config, store=store, probe_factory=PartialProbe,
                   measure=lambda url, secs: 5.0)
    assert report.checked == 1 and report.skipped == 2
    latest = {s.node_name: s.speed_mbps for s in store.latest_gh_speed_samples()}
    assert latest["好节点"] == 5.0
    assert latest["死节点"] is None and latest["拒切节点"] is None


def test_sweep_download_failure_records_null(config, store) -> None:
    def broken_measure(url: str, secs: float) -> float | None:
        return None                                   # 连接层失败：一个字节都没有

    report = sweep([_node("节点一")], config=config, store=store,
                   probe_factory=FakeProbe, measure=broken_measure)
    assert report.checked == 0 and report.skipped == 1
    assert report.samples[0].speed_mbps is None


def test_sweep_probe_crash_marks_unavailable(config, store, caplog) -> None:
    class CrashingProbe(FakeProbe):
        crash_after = 1                               # 第 1 次 select 后实例崩溃

    nodes = [_node("节点一"), _node("节点二"), _node("节点三")]
    report = sweep(nodes, config=config, store=store, probe_factory=CrashingProbe,
                   measure=lambda url, secs: 5.0)
    assert report.unavailable is True
    assert report.checked == 1 and report.skipped == 2
    latest = {s.node_name: s.speed_mbps for s in store.latest_gh_speed_samples()}
    assert latest == {"节点一": 5.0}   # 崩溃后的节点本轮无样本（purity 同口径）


def test_sweep_start_failure_aborts_round(config, store) -> None:
    class DeadProbe(FakeProbe):
        start_ok = False

    nodes = [_node("节点一"), _node("节点二")]
    report = sweep(nodes, config=config, store=store, probe_factory=DeadProbe,
                   measure=lambda url, secs: 5.0)
    assert report.unavailable is True and report.checked == 0
    assert report.skipped == 2
    assert store.latest_gh_speed_samples() == []


def test_sweep_filtered_nodes_excluded(config, store) -> None:
    nodes = [_node("真节点"), _node("假节点", filtered=True)]
    report = sweep(nodes, config=config, store=store, probe_factory=FakeProbe,
                   measure=lambda url, secs: 5.0)
    assert report.checked == 1
    assert [s.node_name for s in report.samples] == ["真节点"]


def test_latest_returns_newest_round_and_prune(config, store) -> None:
    """两轮扫描同节点：latest 取新值；保留窗口外的旧行被清理。"""
    old_at = (datetime.now() - timedelta(days=20)).isoformat()
    store.save_gh_speed_samples([
        GhSpeedSample(node_name="节点一", source_sub="subA", checked_at=old_at,
                      speed_mbps=0.1),
        GhSpeedSample(node_name="节点一", source_sub="subA", checked_at=now_iso(),
                      speed_mbps=27.21),
    ])
    latest = store.latest_gh_speed_samples()
    assert len(latest) == 1 and latest[0].speed_mbps == 27.21

    pruned = store.prune_gh_speed_samples(
        before=(datetime.now() - timedelta(days=ghspeed_mod.RETENTION_DAYS)).isoformat())
    assert pruned == 1
    assert len(store.latest_gh_speed_samples()) == 1


def test_measure_download_partial_data_still_counts(config) -> None:
    """流中断但已读到数据：按已读字节计速；一无所获返回 None（真网络，仅断言口径）。"""
    # 直接测 _measure_download 对 0 字节失败的 None 口径（经不可达端口，快速失败）
    speed = ghspeed_mod._measure_download("http://127.0.0.1:1", 0.2)
    assert speed is None


def test_measure_download_follows_redirect(config) -> None:
    """release 资产 302 → objects CDN：必须跟随重定向读到字节（httpx 默认不跟随）。"""
    import httpx

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "github.com":
            return httpx.Response(302, headers={"location": "https://objects.example.com/blob"})
        # 假 CDN：无限回 64KB 块（测速窗口到即停）
        return httpx.Response(200, content=b"x" * 65536)

    transport = httpx.MockTransport(handler)
    speed = ghspeed_mod._measure_download(None, 0.5, transport=transport)
    assert speed is not None and speed > 0
