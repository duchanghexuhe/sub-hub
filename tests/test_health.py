"""health 模块测试：采样落库、窗口聚合、保留清理、API 数据面。

探测实例以假类替换（不起真实 mihomo 子进程、不占端口）；存储走 conftest
的 config/store fixture（tmp_path 隔离，绝不触碰仓库根 data/）。
"""
from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from app import health
from app.models import HealthSample, Node


# ---------------------------------------------------------------- 工具与替身

def _node(name: str, sub: str = "t") -> Node:
    return Node(name=name, type="vless", server="s.example.com", port=443,
                source_sub=sub)


class _FakeProbe:
    """替身探测实例：delay 按名字查表；类属性控制 start 可用性与延迟表。"""

    started = True
    delays: dict[str, int | None] = {}

    def __init__(self, config, nodes):  # noqa: ARG002 —— 签名对齐真身
        self._nodes = nodes

    def start(self) -> bool:
        return _FakeProbe.started

    def stop(self) -> None:
        pass

    def is_available(self) -> bool:
        return _FakeProbe.started

    def delay(self, name: str) -> int | None:
        return _FakeProbe.delays.get(name)


@pytest.fixture()
def fake_probe(monkeypatch: pytest.MonkeyPatch) -> type[_FakeProbe]:
    monkeypatch.setattr(health, "ProbeInstance", _FakeProbe)
    _FakeProbe.started = True
    _FakeProbe.delays = {}
    return _FakeProbe


@pytest.fixture()
def client(config, store):
    """TestClient（不进入 lifespan，避免拉起调度器/真实镜像）。"""
    from fastapi.testclient import TestClient

    from app import web

    application = web.create_app(config=config, store=store)
    c = TestClient(application)
    yield c
    application.dependency_overrides.clear()


def _sample(name: str, minutes_ago: int, delay_ms: int | None,
            sub: str = "t") -> HealthSample:
    at = (datetime.now() - timedelta(minutes=minutes_ago)).isoformat()
    return HealthSample(node_name=name, source_sub=sub, checked_at=at,
                        delay_ms=delay_ms)


# ---------------------------------------------------------------- 采样

def test_sample_once_writes_samples(store, fake_probe):
    nodes = [_node("🇺🇸 美国 a"), _node("🇭🇰 香港 b"), _node("🇯🇵 日本 c")]
    fake_probe.delays = {"🇺🇸 美国 a": 120, "🇭🇰 香港 b": None, "🇯🇵 日本 c": 800}

    report = health.sample_once(nodes, config=None, store=store)  # type: ignore[arg-type]

    assert (report.checked, report.ok_count, report.unavailable) == (3, 2, False)
    assert report.ok_rate == pytest.approx(2 / 3)
    rows = store.list_health_samples(since="2000-01-01")
    assert len(rows) == 3
    by_name = {r.node_name: r for r in rows}
    assert by_name["🇭🇰 香港 b"].delay_ms is None       # 失败样本 delay=NULL
    assert by_name["🇺🇸 美国 a"].delay_ms == 120
    # 同一轮采样共用同一 checked_at
    assert len({r.checked_at for r in rows}) == 1


def test_sample_once_unavailable_when_probe_down(store, fake_probe):
    fake_probe.started = False
    report = health.sample_once([_node("🇺🇸 美国 a")], config=None, store=store)  # type: ignore[arg-type]
    assert report.unavailable is True
    assert store.list_health_samples(since="2000-01-01") == []


def test_sample_once_prunes_expired_samples(store, fake_probe):
    """保留窗口（7 天）外的过期样本在每轮采样后清理。"""
    store.save_health_samples([
        _sample("旧节点", 8 * 24 * 60, 100),   # 8 天前
        _sample("新节点", 10, 150),            # 10 分钟前
    ])
    health.sample_once([_node("新节点")], config=None, store=store)  # type: ignore[arg-type]
    names = {r.node_name for r in store.list_health_samples(since="2000-01-01")}
    assert "旧节点" not in names and "新节点" in names


def test_sample_once_empty_nodes_noop(store, fake_probe):
    report = health.sample_once([], config=None, store=store)  # type: ignore[arg-type]
    assert (report.checked, report.unavailable) == (0, False)


# ---------------------------------------------------------------- 窗口聚合

def test_history_report_aggregates_and_sorts(store):
    store.save_health_samples([
        # 节点 A：4 采 3 达（尾部失败 → down_streak=1），均延 200
        _sample("A", 90, 100), _sample("A", 60, 200),
        _sample("A", 30, 300), _sample("A", 5, None),
        # 节点 B：2 采 2 达，均延 150 → 排在 A 前（成功率优先）
        _sample("B", 30, 100), _sample("B", 5, 200),
    ])
    rep = health.history_report(store, window_hours=24)
    assert rep["window_hours"] == 24 and rep["sample_total"] == 6
    rows = {r["node_name"]: r for r in rep["nodes"]}
    assert [r["node_name"] for r in rep["nodes"]] == ["B", "A"]
    b, a = rows["B"], rows["A"]
    assert (b["ok_rate"], b["avg_delay"], b["down_streak"]) == (1.0, 150, 0)
    assert (a["ok_rate"], a["avg_delay"], a["down_streak"]) == (0.75, 200, 1)
    assert a["last_delay"] is None and b["last_delay"] == 200
    assert len(a["samples"]) == 4


def test_history_report_respects_window(store):
    """窗口外的样本不进聚合（24h 窗口只统计最近一天）。"""
    store.save_health_samples([
        _sample("A", 25 * 60, 100),   # 25 小时前 → 窗口外
        _sample("A", 60, 200),        # 1 小时前 → 窗口内
    ])
    rep = health.history_report(store, window_hours=24)
    assert rep["sample_total"] == 1
    assert rep["nodes"][0]["sample_count"] == 1


def test_history_report_empty(store):
    rep = health.history_report(store, window_hours=24)
    assert rep["nodes"] == [] and rep["sample_total"] == 0


# ---------------------------------------------------------------- API 数据面

def test_health_history_api(client, store):
    store.save_health_samples([_sample("🇺🇸 美国 a", 10, 120)])
    resp = client.get("/api/health/history?hours=24")
    assert resp.status_code == 200
    body = resp.json()
    assert body["window_hours"] == 24
    assert body["nodes"][0]["node_name"] == "🇺🇸 美国 a"
    assert body["nodes"][0]["avg_delay"] == 120
