"""内置 mihomo 探测实例：生成专用配置、拉起/停止子进程、控制 API 封装。

职责（docs/03 §4、docs/INTERFACES.md §3.7）：
- 生成 ``data/probe/config.yaml``：external-controller 绑 127.0.0.1:<probe_controller_port>、
  mixed 端口仅绑 127.0.0.1:<probe_mixed_port>，加载全部真实节点，内置专用 select 组 PROBE；
- Popen 启动 mihomo 子进程（二进制路径读 AppConfig.mihomo_path），轮询控制 API 直到就绪；
- 找不到二进制 / 启动失败 / 就绪超时一律返回 False，绝不把异常抛进主链路
  （纯净度报告据此标记 unavailable，分发链路无感）。

安全纪律：日志不输出节点凭据字段；探测配置属 data/probe/（探测专用），不进分发产物。
"""
from __future__ import annotations

import logging
import shutil
import subprocess
import time
from pathlib import Path
from urllib.parse import quote

import httpx
import yaml

from app.config import AppConfig
from app.models import Node
from app.utils import atomic_write_text

logger = logging.getLogger("subhub.probe")

PROBE_GROUP = "PROBE"
"""探测实例内置的专用 select 组名：purity 经 PUT /proxies/PROBE 切换全局出口。"""

PROBE_DELAY_URL = "http://cp.cloudflare.com/generate_204"
"""节点可达性探测 URL（与 docs/02 测速参数规范一致）。"""

_CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
_START_TIMEOUT = 15.0      # 等待控制 API 就绪的上限（秒）
_POLL_INTERVAL = 0.25      # 就绪轮询间隔（秒）
_CONTROL_TIMEOUT = 5.0     # 控制 API 常规请求超时（秒）


class ProbeInstance:
    """单例化的 mihomo 探测子进程封装；全部公开方法不抛异常。"""

    def __init__(self, config: AppConfig, nodes: list[Node]) -> None:
        self._config = config
        self._nodes = self._dedupe_nodes(nodes)
        self._proc: subprocess.Popen[bytes] | None = None
        self._client = httpx.Client(
            base_url=f"http://127.0.0.1:{config.probe_controller_port}",
            timeout=httpx.Timeout(_CONTROL_TIMEOUT),
        )

    # ------------------------------------------------------------------ 属性

    @property
    def nodes(self) -> list[Node]:
        """探测实例加载的节点（去重后）。"""
        return list(self._nodes)

    @property
    def config_path(self) -> Path:
        return self._config.probe_config_path

    # ------------------------------------------------------------------ 配置生成

    def write_config(self) -> Path:
        """生成探测实例专用配置 data/probe/config.yaml（原子写），返回路径。

        要点：控制 API 与混合端口只绑 127.0.0.1（不对外）；proxies 为全部真实节点；
        规则链仅一条 MATCH,PROBE —— 经混合端口进来的流量全部走 PROBE 组出口，
        purity 通过 select() 切换该组选中节点。
        """
        proxies = [n.to_clash_proxy() for n in self._nodes]
        names = list(dict.fromkeys(n.name for n in self._nodes))
        doc = {
            "mixed-port": self._config.probe_mixed_port,
            "bind-address": "127.0.0.1",
            "allow-lan": False,
            "mode": "rule",
            "log-level": "warning",
            "external-controller": f"127.0.0.1:{self._config.probe_controller_port}",
            "proxies": proxies,
            "proxy-groups": [
                {"name": PROBE_GROUP, "type": "select", "proxies": [*names, "DIRECT"]},
            ],
            "rules": [f"MATCH,{PROBE_GROUP}"],
        }
        text = yaml.safe_dump(doc, allow_unicode=True, sort_keys=False)
        atomic_write_text(self._config.probe_config_path, text)
        logger.info(
            "已生成探测实例配置：%s（节点 %d 个，controller 127.0.0.1:%d）",
            self._config.probe_config_path, len(proxies), self._config.probe_controller_port,
        )
        return self._config.probe_config_path

    # ------------------------------------------------------------------ 生命周期

    def start(self) -> bool:
        """写配置并拉起 mihomo 子进程，等待控制 API 就绪；失败返回 False（不抛异常）。"""
        try:
            self.write_config()
        except Exception as exc:  # noqa: BLE001 —— 配置写失败同样不能打扰主链路
            logger.warning("探测实例配置生成失败，标记不可用：%s", exc)
            return False
        binary = self._resolve_binary()
        if binary is None:
            logger.warning(
                "未找到 mihomo 二进制（mihomo_path=%s），纯净度探测标记不可用",
                self._config.mihomo_path,
            )
            return False
        popen_kwargs: dict[str, object] = {}
        if _CREATE_NO_WINDOW:  # 仅 Windows 需要，避免拉起控制台窗口
            popen_kwargs["creationflags"] = _CREATE_NO_WINDOW
        try:
            self._proc = subprocess.Popen(
                [binary, "-f", str(self._config.probe_config_path), "-d", str(self._config.probe_dir)],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                **popen_kwargs,  # type: ignore[arg-type]
            )
        except (OSError, ValueError) as exc:
            logger.warning("mihomo 探测进程启动失败，纯净度探测标记不可用：%s", exc)
            self._proc = None
            return False
        if not self._wait_ready():
            logger.warning("mihomo 探测实例控制 API 未就绪，停止该进程")
            self.stop()
            return False
        logger.info(
            "mihomo 探测实例已就绪（controller 127.0.0.1:%d，mixed 127.0.0.1:%d）",
            self._config.probe_controller_port, self._config.probe_mixed_port,
        )
        return True

    def stop(self) -> None:
        """停止子进程并释放控制 API 客户端；幂等，可重复调用。"""
        proc, self._proc = self._proc, None
        if proc is not None:
            try:
                proc.terminate()
            except OSError:
                pass
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                try:
                    proc.kill()
                except OSError:
                    pass
                try:
                    proc.wait(timeout=3)
                except (subprocess.TimeoutExpired, OSError):
                    pass
            except OSError:
                pass
        try:
            self._client.close()
        except Exception:  # noqa: BLE001
            pass

    def is_available(self) -> bool:
        """探测实例子进程是否存活（崩溃后由 purity 标记 unavailable，下轮调度重建）。"""
        return self._proc is not None and self._proc.poll() is None

    # ------------------------------------------------------------------ 控制 API

    def select(self, node_name: str) -> bool:
        """PUT /proxies/PROBE 把 PROBE 组（全局出口）切到指定节点；失败返回 False。"""
        try:
            resp = self._client.put(
                f"/proxies/{quote(PROBE_GROUP, safe='')}",
                json={"name": node_name},
                timeout=_CONTROL_TIMEOUT,
            )
            return resp.status_code in (200, 204)
        except Exception as exc:  # noqa: BLE001
            logger.debug("切换 PROBE 出口到「%s」失败：%s", node_name, exc)
            return False

    def local_proxy_url(self) -> str:
        """探测用本地混合端口代理地址（仅 127.0.0.1 可达）。"""
        return f"http://127.0.0.1:{self._config.probe_mixed_port}"

    def delay(self, node_name: str) -> int | None:
        """GET /proxies/<name>/delay 探测节点可达性（docs/03 §4 的「控制 API 确认可用」）。

        不可达 / 控制 API 异常返回 None。
        """
        try:
            resp = self._client.get(
                f"/proxies/{quote(node_name, safe='')}/delay",
                params={"timeout": 3000, "url": PROBE_DELAY_URL},
                timeout=_CONTROL_TIMEOUT,
            )
            if resp.status_code != 200:
                return None
            value = resp.json().get("delay")
            return int(value) if isinstance(value, (int, float)) else None
        except Exception as exc:  # noqa: BLE001
            logger.debug("节点「%s」delay 探测失败：%s", node_name, exc)
            return None

    # ------------------------------------------------------------------ 内部

    def _resolve_binary(self) -> str | None:
        """解析 mihomo 二进制路径：含路径分隔符按文件存在性检查，否则查 PATH。"""
        raw = (self._config.mihomo_path or "").strip()
        if not raw:
            return None
        candidate = Path(raw)
        if candidate.is_absolute() or len(candidate.parts) > 1:
            return str(candidate) if candidate.is_file() else None
        return shutil.which(raw)

    def _wait_ready(self, timeout: float = _START_TIMEOUT) -> bool:
        """轮询控制 API /version 直到就绪；进程提前退出或超时返回 False。"""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self._proc is not None and self._proc.poll() is not None:
                logger.warning("mihomo 探测进程在就绪前退出（code=%s）", self._proc.returncode)
                return False
            try:
                resp = self._client.get("/version", timeout=1.0)
                if resp.status_code == 200:
                    return True
            except Exception:  # noqa: BLE001 —— 就绪前连接失败属预期，继续轮询
                pass
            time.sleep(_POLL_INTERVAL)
        return False

    @staticmethod
    def _dedupe_nodes(nodes: list[Node]) -> list[Node]:
        """按节点名去重（保持顺序）；重名会导致 mihomo 配置加载失败，防御性处理。"""
        seen: dict[str, Node] = {}
        for node in nodes:
            if node.name not in seen:
                seen[node.name] = node
        return list(seen.values())
