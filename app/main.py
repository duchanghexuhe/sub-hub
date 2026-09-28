"""sub-hub 入口：装配 config/store/web 并以 uvicorn 启动。

启动时初始化 data/ 与分发 token（load_config）、SQLite 与 secret.key（Store），
并检查规则基线就位（仅提示不阻塞启动）。调度器的启动与关闭挂在应用生命周期：
web lifespan 调 scheduler.create_scheduler().start()/shutdown()（INTERFACES §3.10），
main 作为入口随应用启停，不重复启动以免双调度器。监听地址分别读环境变量
SUBHUB_HOST / SUBHUB_PORT，默认 127.0.0.1:8399。
"""
from __future__ import annotations

import logging
from pathlib import Path

import uvicorn

from app.config import AppConfig, load_config
from app.store import Store

logger = logging.getLogger("subhub.main")


def check_rules_baseline(config: AppConfig) -> bool:
    """检查规则基线就位：内置基线目录存在，且 data/rules/ 已有缓存文件。缺失仅告警。"""
    baseline_dir = Path(__file__).resolve().parent / "baseline_rules"
    cached = list(config.rules_dir.glob("*")) if config.rules_dir.exists() else []
    if not baseline_dir.exists():
        logger.warning("规则基线目录缺失（app/baseline_rules），首次同步前客户端规则拉取可能为空")
    if not cached:
        logger.info("data/rules/ 暂无缓存，等待规则镜像任务/启动同步填充")
    return baseline_dir.exists() and bool(cached)


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s [%(name)s] %(message)s")
    cfg = load_config()  # 初始化 data/ 子目录，首次生成 32 位 hex 分发 token
    logger.info("sub-hub 启动：数据目录 %s，监听 %s:%d，分发基址 %s", cfg.data_dir, cfg.host, cfg.port, cfg.base_url)
    store = Store(cfg.db_path, cfg.secret_key_path)  # 初始化 SQLite + secret.key（0600 尽力而为）
    try:
        logger.info("规则基线检查：%s", "就绪" if check_rules_baseline(cfg) else "未就绪（不阻塞启动）")
        try:
            from app.web import create_app  # web 模块并行开发，延迟导入
        except ImportError as exc:
            logger.error("web 模块未就绪，无法启动：%s", exc)
            raise SystemExit(1) from exc
        uvicorn.run(create_app(config=cfg, store=store), host=cfg.host, port=cfg.port)
    finally:
        store.close()


if __name__ == "__main__":
    main()
