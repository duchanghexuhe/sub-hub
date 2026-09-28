"""scheduler 模块测试：三任务注册、间隔来自 config、状态落盘/内存、错误不外抛、main 可装配。

依赖 pipeline/rulesync/purity 属并行开发模块——一律经 monkeypatch 向 sys.modules
注入 fake 模块，同时覆盖「真实模块缺失时不崩溃」的路径。数据目录用 conftest 的
tmp_path 隔离，绝不读写仓库根 data/。
"""
from __future__ import annotations

import sys
import types
from datetime import timedelta
from types import SimpleNamespace

import pytest

from app.config import load_config
from app.models import Node
from app.scheduler import (
    JOB_HEALTH_PROBE,
    JOB_IDS,
    JOB_PURITY_SCAN,
    JOB_RULES_MIRROR,
    JOB_SUB_REFRESH,
    create_scheduler,
    read_scheduler_state,
)
from app.utils import read_json


def _fake_module(name: str, **attrs: object) -> types.ModuleType:
    module = types.ModuleType(name)
    for key, value in attrs.items():
        setattr(module, key, value)
    return module


# ------------------------------------------------------------------ 注册与间隔

def test_registers_four_jobs_with_config_intervals(data_dir, monkeypatch, store):
    monkeypatch.setenv("SUBHUB_REFRESH_MINUTES", "45")
    monkeypatch.setenv("SUBHUB_MIRROR_HOURS", "7")
    monkeypatch.setenv("SUBHUB_PURITY_HOUR", "5")
    monkeypatch.setenv("SUBHUB_HEALTH_MINUTES", "20")
    config = load_config()
    scheduler = create_scheduler(config, store)

    jobs = {job.id: job for job in scheduler.get_jobs()}
    assert set(jobs) == set(JOB_IDS)
    assert jobs[JOB_SUB_REFRESH].trigger.interval == timedelta(minutes=45)
    assert jobs[JOB_RULES_MIRROR].trigger.interval == timedelta(hours=7)
    assert str(jobs[JOB_PURITY_SCAN].trigger) == "cron[hour='5', minute='0']"
    assert jobs[JOB_HEALTH_PROBE].trigger.interval == timedelta(minutes=20)
    # create_scheduler 只装配不启动（启动/关闭挂在应用生命周期）
    assert scheduler.running is False


def test_health_probe_disabled_when_minutes_zero(data_dir, monkeypatch, store):
    """SUBHUB_HEALTH_MINUTES=0 → 不注册采样任务（其余三任务照常）。"""
    monkeypatch.setenv("SUBHUB_HEALTH_MINUTES", "0")
    scheduler = create_scheduler(load_config(), store)
    jobs = {job.id: job for job in scheduler.get_jobs()}
    assert JOB_HEALTH_PROBE not in jobs
    assert set(jobs) == set(JOB_IDS) - {JOB_HEALTH_PROBE}


def test_default_intervals_from_config(config, store):
    scheduler = create_scheduler(config, store)
    jobs = {job.id: job for job in scheduler.get_jobs()}
    assert jobs[JOB_SUB_REFRESH].trigger.interval == timedelta(minutes=30)
    assert jobs[JOB_RULES_MIRROR].trigger.interval == timedelta(hours=6)
    assert str(jobs[JOB_PURITY_SCAN].trigger) == "cron[hour='4', minute='0']"
    assert jobs[JOB_HEALTH_PROBE].trigger.interval == timedelta(minutes=15)


# ------------------------------------------------------------------ 作业执行与状态

def test_sub_refresh_job_calls_pipeline_and_records_state(config, store, monkeypatch):
    calls: dict[str, tuple] = {}

    def run_full_pipeline(cfg, st):
        calls["args"] = (cfg, st)
        return SimpleNamespace(
            published=True, version=3, node_count=12, filtered_count=2,
            stale_subs=[], errors=[],
        )

    monkeypatch.setitem(
        sys.modules, "app.pipeline",
        _fake_module("app.pipeline", run_full_pipeline=run_full_pipeline),
    )
    scheduler = create_scheduler(config, store)

    entry = scheduler.subhub_jobs[JOB_SUB_REFRESH]()

    assert calls["args"] == (config, store)
    assert entry["last_status"] == "ok"
    assert entry["last_status_label"] == "成功"
    assert "v0003" in entry["summary"]
    # 状态同时落盘与入内存（/api/health 读文件）
    on_disk = read_json(config.scheduler_state_path)
    assert on_disk[JOB_SUB_REFRESH]["last_status"] == "ok"
    assert read_scheduler_state(config)[JOB_SUB_REFRESH]["last_status"] == "ok"


def test_rules_mirror_job_calls_rulesync(config, store, monkeypatch):
    calls: dict[str, tuple] = {}

    def sync_rules(cfg, **kwargs):
        calls["args"] = (cfg,)
        return SimpleNamespace(updated=["claude-extra"], stale=[], failed_never=[],
                               last_success_at=None, checked_at="")

    monkeypatch.setitem(
        sys.modules, "app.rulesync",
        _fake_module("app.rulesync", sync_rules=sync_rules),
    )
    scheduler = create_scheduler(config, store)
    entry = scheduler.subhub_jobs[JOB_RULES_MIRROR]()
    assert calls["args"] == (config,)
    assert entry["last_status"] == "ok"
    assert "更新 1" in entry["summary"]


def test_purity_scan_job_passes_kept_nodes_full(config, store, monkeypatch):
    kept = Node(name="🇺🇸 美国家宽 x1", type="ss", server="1.2.3.4", port=8388,
                source_sub="kuai", credentials={"password": "x"})
    dropped = Node(name="剩余流量", type="ss", server="1.2.3.5", port=8388,
                   source_sub="kuai", credentials={"password": "x"},
                   filtered=True, filter_reason="流量")
    store.replace_nodes("kuai", [kept, dropped])

    calls: dict[str, object] = {}

    def scan(nodes, *, config, store, full=False):
        calls["nodes"] = list(nodes)
        calls["full"] = full
        return SimpleNamespace(results=[], unavailable=False, checked=1, skipped=0)

    monkeypatch.setitem(sys.modules, "app.purity", _fake_module("app.purity", scan=scan))
    scheduler = create_scheduler(config, store)
    entry = scheduler.subhub_jobs[JOB_PURITY_SCAN]()
    # 全量扫描：只传未过滤节点，full=True
    assert calls["nodes"] == [kept]
    assert calls["full"] is True
    assert entry["last_status"] == "ok"


def test_job_failure_is_recorded_not_raised(config, store, monkeypatch):
    def run_full_pipeline(cfg, st):
        raise RuntimeError("boom")

    monkeypatch.setitem(
        sys.modules, "app.pipeline",
        _fake_module("app.pipeline", run_full_pipeline=run_full_pipeline),
    )
    scheduler = create_scheduler(config, store)
    entry = scheduler.subhub_jobs[JOB_SUB_REFRESH]()  # 不抛异常
    assert entry["last_status"] == "error"
    assert "boom" in entry["last_error"]
    assert entry["last_status_label"] == "失败"
    on_disk = read_json(config.scheduler_state_path)
    assert on_disk[JOB_SUB_REFRESH]["last_status"] == "error"


def test_missing_module_recorded_as_error(config, store, monkeypatch):
    # sys.modules 置 None 模拟「模块未就绪」：import 失败被包装器捕获
    monkeypatch.setitem(sys.modules, "app.rulesync", None)
    scheduler = create_scheduler(config, store)
    entry = scheduler.subhub_jobs[JOB_RULES_MIRROR]()
    assert entry["last_status"] == "error"
    assert entry["last_error"]


def test_read_scheduler_state_falls_back_to_memory(config, store, monkeypatch):
    monkeypatch.setitem(
        sys.modules, "app.pipeline",
        _fake_module(
            "app.pipeline",
            run_full_pipeline=lambda cfg, st: SimpleNamespace(
                published=False, version=None, node_count=0, filtered_count=0,
                stale_subs=[], errors=[],
            ),
        ),
    )
    scheduler = create_scheduler(config, store)
    scheduler.subhub_jobs[JOB_SUB_REFRESH]()
    config.scheduler_state_path.unlink()
    state = read_scheduler_state(config)
    assert state[JOB_SUB_REFRESH]["last_status"] == "ok"


# ------------------------------------------------------------------ main 装配

def test_main_importable_and_baseline_check_runs(config):
    import app.main as main_mod

    assert callable(main_mod.main)
    # 规则基线检查：在 tmp 数据目录上运行，返回 bool 且不抛异常
    assert isinstance(main_mod.check_rules_baseline(config), bool)


def test_main_fails_fast_when_web_module_missing(config, monkeypatch):
    import app.main as main_mod

    monkeypatch.setitem(sys.modules, "app.web", None)
    with pytest.raises(SystemExit):
        main_mod.main()
