"""集中式配置：全部可调项唯一定义于此，经环境变量覆盖，其余模块只读。

环境变量一览（均可缺省）：
  SUBHUB_DATA_DIR              数据目录，默认 ./data（相对启动时工作目录）
  SUBHUB_HOST / SUBHUB_PORT    服务监听地址，默认 127.0.0.1:8399
  SUBHUB_NAS_HOST              NAS 局域网地址（构造分发链接用），默认 192.168.31.10
  SUBHUB_REFRESH_MINUTES       订阅刷新间隔（分钟），默认 30
  SUBHUB_MIRROR_HOURS          规则镜像间隔（小时），默认 6
  SUBHUB_PURITY_HOUR           纯净度每日扫描时刻（0-23 点），默认 4
  SUBHUB_HEALTH_MINUTES        节点健康采样间隔（分钟），默认 15（0=关闭采样）
  SUBHUB_MIHOMO_PATH           mihomo 二进制路径（探测实例用），默认 "mihomo"（查 PATH）
  SUBHUB_PROBE_CONTROLLER_PORT 探测实例 external-controller 端口，默认 9095
  SUBHUB_PROBE_MIXED_PORT      探测实例混合端口，默认 9096
  SUBHUB_SKIP_ANYTLS           SR conf 是否跳过 anytls 节点，默认开（1/true/yes/on）
  SUBHUB_FETCH_UA              抓取订阅使用的 Clash UA
  SUBHUB_AUTO_MAX_RATE         常规自动组倍率上限，默认 1.0（全库无达标倍率时自动放宽到最低档）
  SUBHUB_GHPROBE_HOURS         GitHub 吞吐量扫描间隔（小时），默认 6（0=关闭扫描）
  SUBHUB_GHPROBE_SECONDS       每节点测速时长（秒），默认 5
  SUBHUB_GHPROBE_TOP_N         🏆 GitHub 优选组收录的实测最快节点数，默认 5

token：分发 token 存 data/token，首次启动自动生成 32 位 hex；secret.key 由
store 层负责（0600）。密钥与订阅 URL 永不进日志/对话/配置产物。
"""
from __future__ import annotations

import logging
import os
import secrets
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping

from app.utils import atomic_write_text, now_iso, restrict_permissions

logger = logging.getLogger("subhub.config")

DATA_DIR_ENV = "SUBHUB_DATA_DIR"
DEFAULT_DATA_DIR = "./data"
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8399
DEFAULT_NAS_HOST = "192.168.31.10"

_TRUTHY = {"1", "true", "yes", "on"}


def _env_int(env: Mapping[str, str], key: str, default: int) -> int:
    raw = env.get(key)
    if raw is None or not raw.strip():
        return default
    try:
        return int(raw.strip())
    except ValueError:
        logger.warning("环境变量 %s=%r 不是整数，使用默认值 %d", key, raw, default)
        return default


def _env_bool(env: Mapping[str, str], key: str, default: bool) -> bool:
    raw = env.get(key)
    if raw is None or not raw.strip():
        return default
    return raw.strip().lower() in _TRUTHY


def _env_float(env: Mapping[str, str], key: str, default: float) -> float:
    raw = env.get(key)
    if raw is None or not raw.strip():
        return default
    try:
        return float(raw.strip())
    except ValueError:
        logger.warning("环境变量 %s=%r 不是数字，使用默认值 %s", key, raw, default)
        return default


@dataclass(frozen=True)
class AppConfig:
    """全部配置的唯一切面；各模块通过 load_config() 获取，不自行读环境变量。"""

    data_dir: Path
    host: str
    port: int
    nas_lan_host: str
    token: str                       # 分发 token，32 位 hex（data/token）
    sub_refresh_minutes: int         # 定时任务 1：订阅刷新间隔
    rules_mirror_hours: int          # 定时任务 2：规则镜像间隔
    purity_scan_hour: int            # 定时任务 3：纯净度每日扫描时刻（点）
    health_probe_minutes: int        # 定时任务 4：节点健康采样间隔（分钟；0=关闭）
    mihomo_path: str
    skip_anytls: bool                # SR conf 跳过 anytls（客户端 <6.3 不支持）
    probe_controller_port: int
    probe_mixed_port: int
    fetch_user_agent: str
    auto_max_rate: float             # 常规自动组倍率上限（低倍率省流；全库无达标时放宽到最低档）
    ghprobe_hours: int               # 定时任务 5：GitHub 吞吐量扫描间隔（小时；0=关闭）
    ghprobe_seconds: float           # 每节点测速时长（秒）
    ghprobe_top_n: int               # 🏆 GitHub 优选组收录的实测最快节点数
    rules_proxy: str = ""            # 规则上游直连失败时的代理回落（空=不回落）；机场订阅不受此影响

    # ---------- 派生路径 ----------
    @property
    def cache_dir(self) -> Path:
        return self.data_dir / "cache"       # 订阅原始快照（每订阅保留 3 份）

    @property
    def out_dir(self) -> Path:
        return self.data_dir / "out"         # 配置产物版本目录（保留 5 版）

    @property
    def rules_dir(self) -> Path:
        return self.data_dir / "rules"       # 规则集缓存镜像

    @property
    def probe_dir(self) -> Path:
        return self.data_dir / "probe"       # 探测实例工作目录

    @property
    def db_path(self) -> Path:
        return self.data_dir / "subhub.db"

    @property
    def secret_key_path(self) -> Path:
        return self.data_dir / "secret.key"

    @property
    def token_path(self) -> Path:
        return self.data_dir / "token"

    @property
    def probe_config_path(self) -> Path:
        return self.probe_dir / "config.yaml"

    @property
    def scheduler_state_path(self) -> Path:
        return self.data_dir / "scheduler_state.json"

    @property
    def mirror_settings_path(self) -> Path:
        return self.data_dir / "mirror.json"

    # ---------- 派生 URL ----------
    @property
    def base_url(self) -> str:
        return f"http://{self.nas_lan_host}:{self.port}"

    @property
    def sub_url_prefix(self) -> str:
        """分发链接前缀：http://<nas>:<port>/sub/<token>，后接 /clash.yaml 等。"""
        return f"{self.base_url}/sub/{self.token}"


def _ensure_token(data_dir: Path) -> str:
    """读取 data/token；不存在则生成 32 位 hex 并原子写入（0600 尽力而为）。"""
    token_path = data_dir / "token"
    if token_path.exists():
        token = token_path.read_text(encoding="utf-8").strip()
        if token:
            return token
    token = secrets.token_hex(16)  # 32 位 hex
    atomic_write_text(token_path, token + "\n")
    restrict_permissions(token_path)
    logger.info("首次启动：已生成分发 token（%s）", now_iso())
    return token


def load_config(env: Mapping[str, str] | None = None) -> AppConfig:
    """按调用时的环境变量构造配置（测试可在调用前 monkeypatch 环境变量）。

    副作用：创建 data/ 及其子目录（cache/out/rules/probe）；首次启动生成
    data/token。不生成 secret.key（由 Store 首次初始化时生成）。
    """
    env = os.environ if env is None else env
    data_dir = Path(env.get(DATA_DIR_ENV, DEFAULT_DATA_DIR)).expanduser().resolve()
    for sub in ("", "cache", "out", "rules", "probe"):
        (data_dir / sub if sub else data_dir).mkdir(parents=True, exist_ok=True)
    config = AppConfig(
        data_dir=data_dir,
        host=env.get("SUBHUB_HOST", DEFAULT_HOST).strip() or DEFAULT_HOST,
        port=_env_int(env, "SUBHUB_PORT", DEFAULT_PORT),
        nas_lan_host=env.get("SUBHUB_NAS_HOST", DEFAULT_NAS_HOST).strip() or DEFAULT_NAS_HOST,
        token=_ensure_token(data_dir),
        sub_refresh_minutes=_env_int(env, "SUBHUB_REFRESH_MINUTES", 30),
        rules_mirror_hours=_env_int(env, "SUBHUB_MIRROR_HOURS", 6),
        purity_scan_hour=_env_int(env, "SUBHUB_PURITY_HOUR", 4),
        health_probe_minutes=_env_int(env, "SUBHUB_HEALTH_MINUTES", 15),
        mihomo_path=env.get("SUBHUB_MIHOMO_PATH", "mihomo").strip() or "mihomo",
        skip_anytls=_env_bool(env, "SUBHUB_SKIP_ANYTLS", True),
        probe_controller_port=_env_int(env, "SUBHUB_PROBE_CONTROLLER_PORT", 9095),
        probe_mixed_port=_env_int(env, "SUBHUB_PROBE_MIXED_PORT", 9096),
        fetch_user_agent=env.get("SUBHUB_FETCH_UA", "clash-verge/v2.0.0").strip()
        or "clash-verge/v2.0.0",
        auto_max_rate=_env_float(env, "SUBHUB_AUTO_MAX_RATE", 1.0),
        ghprobe_hours=_env_int(env, "SUBHUB_GHPROBE_HOURS", 6),
        ghprobe_seconds=_env_float(env, "SUBHUB_GHPROBE_SECONDS", 5.0),
        ghprobe_top_n=_env_int(env, "SUBHUB_GHPROBE_TOP_N", 5),
        rules_proxy=env.get("SUBHUB_RULES_PROXY", "").strip(),
    )
    return config
