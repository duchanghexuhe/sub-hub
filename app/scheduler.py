"""定时任务调度：APScheduler BackgroundScheduler 三任务（docs/01 模块表）。

任务清单（间隔/时刻全部来自 AppConfig，不硬编码）：
- sub_refresh   每 sub_refresh_minutes 分钟 → pipeline.run_full_pipeline（订阅刷新全链路）
- rules_mirror  每 rules_mirror_hours 小时  → rulesync.sync_rules（上游规则缓存镜像）
- purity_scan   每天 purity_scan_hour:00    → purity.scan 全量（出口 IP 纯净度）
- health_probe  每 health_probe_minutes 分钟 → health.sample_once（延迟/可达性采样，0=关闭）

执行状态：每个任务最近执行时间/状态/摘要同时写入内存与
data/scheduler_state.json（/api/health 读文件，二者内容一致）。
任何任务异常都在包装器内捕获记录，绝不外抛——调度故障不影响分发主链路（docs/01 安全网）。

pipeline/rulesync/purity 为并行开发模块，作业体内延迟导入；缺失时记错误状态而非崩溃。
启动与关闭：由 app/web.py 的 lifespan 调 create_scheduler().start()/shutdown()
（INTERFACES §3.10），本模块与 main 均不重复启动，防止双调度器。
"""
from __future__ import annotations

import logging
import threading
from typing import Any, Callable

from apscheduler.schedulers.background import BackgroundScheduler

from app.config import AppConfig
from app.store import Store
from app.utils import now_iso, read_json, write_json

logger = logging.getLogger("subhub.scheduler")

# job id 固定，供 /api/health 与状态文件识别
JOB_SUB_REFRESH = "sub_refresh"
JOB_RULES_MIRROR = "rules_mirror"
JOB_PURITY_SCAN = "purity_scan"
JOB_HEALTH_PROBE = "health_probe"
JOB_GH_SPEED_PROBE = "gh_speed_probe"
JOB_IDS: tuple[str, ...] = (JOB_SUB_REFRESH, JOB_RULES_MIRROR, JOB_PURITY_SCAN,
                            JOB_HEALTH_PROBE, JOB_GH_SPEED_PROBE)

_STATE_LABELS = {"ok": "成功", "error": "失败"}

# 内存态：{scheduler_state_path 字符串: {job_id: entry}}，与状态文件同步写
_MEMORY_STATE: dict[str, dict[str, dict[str, Any]]] = {}
_STATE_LOCK = threading.Lock()


# ------------------------------------------------------------------ 作业体

def _job_sub_refresh(config: AppConfig, store: Store) -> str:
    from app.pipeline import run_full_pipeline

    result = run_full_pipeline(config, store)
    version = f"v{result.version:04d}" if result.published else "未发布（沿用上一版）"
    return f"发布 {version}，节点 {result.node_count}，过滤 {result.filtered_count}"


def _job_rules_mirror(config: AppConfig) -> str:
    from app.rulesync import sync_rules

    report = sync_rules(config)
    return (
        f"更新 {len(report.updated)} 条，沿用缓存 {len(report.stale)} 条，"
        f"无兜底 {len(report.failed_never)} 条"
    )


def _job_purity_scan(config: AppConfig, store: Store) -> str:
    from app.purity import scan

    nodes = store.list_nodes(filtered=False)
    report = scan(nodes, config=config, store=store, full=True)
    summary = f"检测 {report.checked} 个，跳过 {report.skipped} 个"
    if report.unavailable:
        summary += "（探测实例不可用，主链路无感）"
    return summary


def _job_health_probe(config: AppConfig, store: Store) -> str:
    from app.health import sample_once

    nodes = store.list_nodes(filtered=False)
    report = sample_once(nodes, config=config, store=store)
    if report.unavailable and not report.checked:
        return "探测实例不可用，本轮健康采样放弃（主链路无感）"
    summary = f"采样 {report.checked} 个，可达 {report.ok_count} 个（{report.ok_rate:.0%}）"
    if report.unavailable:
        summary += "（探测实例中途失效，尾部节点本轮无样本）"
    return summary


def _job_gh_speed_probe(config: AppConfig, store: Store) -> str:
    from app.ghspeed import sweep

    nodes = store.list_nodes(filtered=False)
    report = sweep(nodes, config=config, store=store)
    if report.unavailable and not report.checked:
        return "探测实例不可用，本轮 GitHub 测速放弃（主链路无感）"
    summary = f"实测 {report.checked} 个，失败/跳过 {report.skipped} 个"
    if report.unavailable:
        summary += "（探测实例中途失效，尾部节点本轮无样本）"
    return summary


# ------------------------------------------------------------------ 状态记录

def _merge_state(config: AppConfig, job_id: str, entry: dict[str, Any]) -> None:
    """把单个任务的最近执行状态合并进状态文件与内存（线程安全）。"""
    path = config.scheduler_state_path
    with _STATE_LOCK:
        state = read_json(path, default={})
        if not isinstance(state, dict):
            state = {}
        state[job_id] = entry
        try:
            write_json(path, state)
        except OSError as exc:
            logger.warning("调度状态写入失败（不影响任务本身）：%s", exc)
        _MEMORY_STATE.setdefault(str(path), {})[job_id] = dict(entry)


def read_scheduler_state(config: AppConfig) -> dict[str, dict[str, Any]]:
    """读取四任务最近执行状态（状态文件为主、内存兜底；/api/health 用）。"""
    path = str(config.scheduler_state_path)
    with _STATE_LOCK:
        file_state = read_json(config.scheduler_state_path, default={})
        mem_state = _MEMORY_STATE.get(path, {})
    merged: dict[str, dict[str, Any]] = {k: dict(v) for k, v in mem_state.items() if isinstance(v, dict)}
    if isinstance(file_state, dict):
        for job_id, entry in file_state.items():
            if isinstance(entry, dict):
                merged[job_id] = entry
    return {job_id: merged[job_id] for job_id in JOB_IDS if job_id in merged}


def _wrap(config: AppConfig, job_id: str, body: Callable[[], str]) -> Callable[[], dict[str, Any]]:
    """任务包装器：捕获一切异常并记录状态，绝不向调度器外抛错。"""

    def run() -> dict[str, Any]:
        started = now_iso()
        logger.info("定时任务 [%s] 开始执行", job_id)
        try:
            summary = body()
            entry: dict[str, Any] = {
                "last_run": started,
                "last_status": "ok",
                "last_status_label": _STATE_LABELS["ok"],
                "last_error": None,
                "summary": summary,
            }
            logger.info("定时任务 [%s] 完成：%s", job_id, summary)
        except Exception as exc:  # noqa: BLE001 —— 错误不外抛（docs/01 安全网）
            detail = f"{type(exc).__name__}: {exc}"
            entry = {
                "last_run": started,
                "last_status": "error",
                "last_status_label": _STATE_LABELS["error"],
                "last_error": detail,
                "summary": None,
            }
            logger.warning("定时任务 [%s] 执行失败：%s", job_id, detail)
        _merge_state(config, job_id, entry)
        return entry

    run.__name__ = f"job_{job_id}"
    return run


# ------------------------------------------------------------------ 装配

def create_scheduler(config: AppConfig, store: Store) -> BackgroundScheduler:
    """构造尚未启动的调度器（启动/关闭由 web lifespan 挂钩，见 INTERFACES §3.10）。

    三个 job 的 id 固定为 JOB_IDS；触发器参数全部来自 config。附加属性：
    - scheduler.subhub_jobs：{job_id: 包装器}，便于手动触发与测试；
    - scheduler.subhub_state_path：状态文件路径，便于 web 关联展示。
    health_probe 在 SUBHUB_HEALTH_MINUTES ≤ 0 时不注册（关闭采样）。
    """
    scheduler = BackgroundScheduler(
        job_defaults={"coalesce": True, "max_instances": 1, "misfire_grace_time": 300},
    )
    bodies: dict[str, tuple[str, dict[str, Any], Callable[[], str]]] = {
        JOB_SUB_REFRESH: (
            "interval",
            {"minutes": config.sub_refresh_minutes},
            lambda: _job_sub_refresh(config, store),
        ),
        JOB_RULES_MIRROR: (
            "interval",
            {"hours": config.rules_mirror_hours},
            lambda: _job_rules_mirror(config),
        ),
        JOB_PURITY_SCAN: (
            "cron",
            {"hour": config.purity_scan_hour, "minute": 0},
            lambda: _job_purity_scan(config, store),
        ),
    }
    if config.health_probe_minutes > 0:
        bodies[JOB_HEALTH_PROBE] = (
            "interval",
            {"minutes": config.health_probe_minutes},
            lambda: _job_health_probe(config, store),
        )
    if config.ghprobe_hours > 0:
        bodies[JOB_GH_SPEED_PROBE] = (
            "interval",
            {"hours": config.ghprobe_hours},
            lambda: _job_gh_speed_probe(config, store),
        )
    wrappers: dict[str, Callable[[], dict[str, Any]]] = {}
    for job_id, (trigger, trigger_kwargs, body) in bodies.items():
        wrapped = _wrap(config, job_id, body)
        wrappers[job_id] = wrapped
        scheduler.add_job(
            wrapped,
            trigger=trigger,
            id=job_id,
            name=job_id,
            **trigger_kwargs,
        )
        logger.info(
            "已注册定时任务 [%s]（%s %s）", job_id, trigger, trigger_kwargs
        )
    scheduler.subhub_jobs = wrappers  # type: ignore[attr-defined]
    scheduler.subhub_state_path = config.scheduler_state_path  # type: ignore[attr-defined]
    return scheduler
