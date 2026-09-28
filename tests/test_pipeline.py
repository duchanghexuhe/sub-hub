"""pipeline 单元测试：向 sys.modules 注入同契约假模块，mock fetcher/parser/cleaner/
templater/validator/purity/mirror 各环节边界（真实模块属并行开发，集成后无需改动本文件——
monkeypatch 会在用例结束时还原 sys.modules，真实模块自动生效）。

覆盖任务书清单：正常发布、渲染校验失败拒绝发布并沿用上一版、空节点拒绝发布、
订阅失效沿用旧配置、版本目录保留 5 份、发布中途异常不留半成品；
另覆盖：回退、diff 记录、镜像变化推送、纯净度后台钩子、单订阅刷新、禁用订阅语义。

说明：假 validator/假 cleaner 按各自 INTERFACES 契约做了简化实现（假节点过滤、
消歧、校验、diff/保留 5 版），因此「diff 内容」「保留 5 版」等断言验证的是
pipeline 与契约的接线正确性；真实 validator/cleaner 的内部逻辑由其各自测试保证。
"""
from __future__ import annotations

import re
import sys
import threading
import types
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from app import pipeline, utils
from app.config import AppConfig
from app.models import ConfigVersion, FetchStatus, Node, PurityResult
from app.store import Store

_FAKE_NAME_RE = re.compile("剩余流量|套餐到期|到期时间|官网")


# ---------------------------------------------------------------- 假模块装配

@dataclass
class FakeFetchResult:
    """对齐 fetcher.FetchResult 契约。"""

    sub_id: int
    sub_name: str
    status: FetchStatus
    content: bytes | None = None
    userinfo: dict | None = None
    error: str | None = None
    fetched_at: str = ""


class Harness:
    """可编程假模块集合：每个用例独立实例，monkeypatch 结束自动还原。"""

    def __init__(self, monkeypatch: pytest.MonkeyPatch, fixtures_dir: Path) -> None:
        self.monkeypatch = monkeypatch
        self.fixtures_dir = fixtures_dir
        # 抓取脚本与调用记录
        self.fetch_calls: list[str] = []
        self.fetch_spec: dict[str, dict] = {}
        # validator 可编程故障
        self.fail_publish_midway = False
        self.publish_errors: list[str] | None = None
        # templater 可编程故障
        self.render_garbage = False
        # 每次 render 收到的 purity / stability 参数（对齐真实 templater 契约的回灌断言）
        self.render_purity: list = []
        self.render_stability: list = []
        # mirror 记录
        self.mirror_enabled = True
        self.mirror_attempts = 0
        self.mirror_pushes = 0
        # purity 记录
        self.purity_calls: list[dict] = []
        self.purity_event = threading.Event()
        self.published: list[ConfigVersion] = []
        self._install_all()

    # ---- 测试脚本设置 ------------------------------------------------

    def set_sub(
        self,
        name: str,
        path: Path | None = None,
        *,
        status: FetchStatus = FetchStatus.OK,
        userinfo: dict | None = None,
        error: str | None = None,
        content: bytes | None = None,
    ) -> None:
        if content is None:
            content = path.read_bytes() if path is not None else b""
        self.fetch_spec[name] = {
            "status": status, "content": content, "userinfo": userinfo, "error": error,
        }

    # ---- 模块安装 ----------------------------------------------------

    def _module(self, name: str, **attrs: object) -> types.ModuleType:
        mod = types.ModuleType(f"app.{name}")
        for key, value in attrs.items():
            setattr(mod, key, value)
        self.monkeypatch.setitem(sys.modules, f"app.{name}", mod)
        return mod

    def _install_all(self) -> None:
        self._install_fetcher()
        self._install_parser()
        self._install_cleaner()
        self._install_templater()
        self._install_validator()
        self._install_mirror()
        self._install_purity()

    def _install_fetcher(self) -> None:
        harness = self

        def fetch_subscription(sub, *, config, timeout=20.0):
            harness.fetch_calls.append(sub.name)
            spec = harness.fetch_spec.get(sub.name)
            if spec is None:
                return FakeFetchResult(sub.id, sub.name, FetchStatus.FAILED,
                                       None, None, "无抓取脚本", utils.now_iso())
            return FakeFetchResult(sub.id, sub.name, spec["status"], spec["content"],
                                   spec["userinfo"], spec["error"], utils.now_iso())

        def parse_userinfo_header(value):
            return None  # pipeline 不直接调用，占位以满足契约

        def save_cache(config, sub_name, content):
            return config.cache_dir / f"{sub_name}.yaml"  # fetcher 自身职责，占位

        self._module("fetcher", fetch_subscription=fetch_subscription,
                     parse_userinfo_header=parse_userinfo_header, save_cache=save_cache)

    def _install_parser(self) -> None:
        def parse_payload(content: bytes, source_sub: str) -> list[Node]:
            if not content:
                return []
            try:
                doc = yaml.safe_load(content.decode("utf-8"))
            except (UnicodeDecodeError, yaml.YAMLError):
                return []
            proxies = doc.get("proxies") if isinstance(doc, dict) else None
            if not proxies:
                return []
            return [Node.from_clash_proxy(p, source_sub) for p in proxies]

        self._module("parser", parse_payload=parse_payload,
                     parse_clash_yaml=parse_payload, parse_base64_subscription=lambda c, s: [],
                     parse_proxy_uri=lambda u, s: None)

    def _install_cleaner(self) -> None:
        def clean(nodes: list[Node]):
            kept, filtered = [], []
            for n in nodes:
                m = _FAKE_NAME_RE.search(n.name)
                if m:
                    n.filtered = True
                    n.filter_reason = m.group(0)
                    filtered.append(n)
                else:
                    kept.append(n)
            counts = Counter(n.name for n in kept)
            for n in kept:
                if counts[n.name] > 1:  # 跨订阅同名 → 消歧
                    n.orig_name = n.name
                    n.name = f"{n.name} [{n.source_sub}]"
            return SimpleNamespace(kept=kept, filtered=filtered)

        self._module("cleaner", clean=clean, filter_fake_nodes=lambda nodes: nodes,
                     classify=lambda nodes: nodes, disambiguate=lambda nodes: nodes,
                     FAKE_NODE_PATTERNS=[], REGION_RULES={})

    def _install_templater(self) -> None:
        harness = self

        def load_rules_manifest(path=None):
            return []

        def build_groups(nodes, *, config):
            names = [n.name for n in nodes]
            return [{"name": "🚀 节点选择", "type": "select", "proxies": names}]

        def render_clash(nodes, *, config, rules, offline=False, purity=None, stability=None):
            harness.render_purity.append(list(purity or []))
            harness.render_stability.append(list(stability or []))
            if harness.render_garbage:
                return "proxies: [未闭合"   # 必然触发 YAML 解析错误
            doc = {
                "variant": "offline" if offline else "main",
                "proxies": [n.to_clash_proxy() for n in nodes],
                "proxy-groups": build_groups(nodes, config=config),
                "rules": ["MATCH,🚀 节点选择"],
            }
            return yaml.safe_dump(doc, allow_unicode=True, sort_keys=False)

        def render_sr_conf(nodes, *, config, rules, offline=False, purity=None, stability=None):
            if harness.render_garbage:
                return "垃圾内容，无段落结构"
            names = [n.name for n in nodes] or ["DIRECT"]
            lines = [f"# variant={'offline' if offline else 'main'}", "[Proxy]"]
            lines.extend(f"{n.name} = {n.type}:{n.server}:{n.port}" for n in nodes)
            lines.append("[Proxy Group]")
            lines.append("🚀 节点选择 = select, " + ", ".join(names))
            lines.append("[Rule]")
            lines.append("FINAL,🚀 节点选择")
            return "\n".join(lines) + "\n"

        self._module("templater", load_rules_manifest=load_rules_manifest,
                     build_groups=build_groups, render_clash=render_clash,
                     render_sr_conf=render_sr_conf)

    def _install_validator(self) -> None:
        harness = self

        class PublishError(Exception):
            def __init__(self, errors: list[str]) -> None:
                super().__init__("；".join(errors))
                self.errors = list(errors)

        def validate_clash_yaml(text: str) -> list[str]:
            try:
                doc = yaml.safe_load(text)
            except yaml.YAMLError as exc:
                return [f"clash.yaml 不是合法 YAML：{exc}"]
            if not isinstance(doc, dict) or not doc.get("proxies"):
                return ["clash.yaml 缺少 proxies"]
            return []

        def validate_sr_conf(text: str) -> list[str]:
            return [f"conf 缺少 {s} 段" for s in ("[Proxy]", "[Proxy Group]", "[Rule]")
                    if s not in text]

        def check_consistency(clash_text: str, sr_text: str) -> list[str]:
            try:
                groups = (yaml.safe_load(clash_text) or {}).get("proxy-groups", [])
            except yaml.YAMLError:
                return ["clash YAML 无法回读"]
            conf_groups = (sr_text.count("= select") + sr_text.count("= url-test")
                           + sr_text.count("= fallback"))
            if len(groups) != conf_groups:
                return [f"组数不一致：clash {len(groups)} vs conf {conf_groups}"]
            return []

        def publish(config, store, artifacts, nodes, note=None):
            errors: list[str] = []
            errors += validate_clash_yaml(artifacts["clash.yaml"])
            errors += validate_sr_conf(artifacts["shadowrocket.conf"])
            errors += validate_clash_yaml(artifacts["clash-offline.yaml"])
            errors += validate_sr_conf(artifacts["shadowrocket-offline.conf"])
            errors += check_consistency(artifacts["clash.yaml"], artifacts["shadowrocket.conf"])
            if harness.publish_errors is not None:
                errors.extend(harness.publish_errors)
            if errors:
                raise PublishError(errors)
            if harness.fail_publish_midway:
                v = utils.next_version(config.out_dir)
                d = utils.new_version_dir(config.out_dir, v)
                utils.atomic_write_text(d / "clash.yaml", artifacts["clash.yaml"])
                raise RuntimeError("模拟发布中途异常：仅写入部分产物")
            version = utils.next_version(config.out_dir)
            vdir = utils.new_version_dir(config.out_dir, version)
            for name in pipeline._ARTIFACT_ORDER:
                utils.atomic_write_text(vdir / name, artifacts[name])
            prev_versions = [v for v in utils.list_versions(config.out_dir) if v < version]
            diff: dict = {"added": [], "removed": [], "renamed": []}
            if prev_versions:
                prev_text = (utils.version_dir(config.out_dir, prev_versions[-1])
                             / "clash.yaml").read_text(encoding="utf-8")
                diff = pipeline._diff_summary(
                    pipeline._proxy_summary(prev_text),
                    pipeline._proxy_summary(artifacts["clash.yaml"]),
                )
            meta = ConfigVersion(
                version=version,
                created_at=utils.now_iso(),
                node_count=len(nodes),
                content_hash=utils.content_hash(
                    "".join(artifacts[n] for n in pipeline._ARTIFACT_ORDER)),
                diff_summary=diff,
                note=note,
            )
            utils.write_json(vdir / "meta.json", vars(meta))
            utils.prune_versions(config.out_dir, keep=5)
            harness.published.append(meta)
            return meta

        self._module("validator", PublishError=PublishError,
                     validate_clash_yaml=validate_clash_yaml,
                     validate_sr_conf=validate_sr_conf,
                     check_consistency=check_consistency, publish=publish)

    def _install_mirror(self) -> None:
        harness = self

        def push_current(config):
            harness.mirror_attempts += 1
            if not harness.mirror_enabled:
                return SimpleNamespace(ok=False, provider=None, url=None,
                                       error="镜像未启用", pushed_at=utils.now_iso())
            harness.mirror_pushes += 1
            return SimpleNamespace(ok=True, provider="cf-kv", url="https://mirror.example/sub",
                                   error=None, pushed_at=utils.now_iso())

        self._module("mirror", push_current=push_current,
                     load_mirror_settings=lambda config: {"enabled": harness.mirror_enabled},
                     save_mirror_settings=lambda config, s: None)

    def _install_purity(self) -> None:
        harness = self

        def scan(nodes, *, config, store, provider=None, full=False):
            harness.purity_calls.append({"nodes": [n.name for n in nodes], "full": full})
            harness.purity_event.set()
            return SimpleNamespace(results=[], unavailable=False, checked=0, skipped=0)

        self._module("purity", scan=scan)


# ---------------------------------------------------------------- fixture 与工具

@pytest.fixture()
def harness(monkeypatch: pytest.MonkeyPatch, fixtures_dir: Path) -> Harness:
    return Harness(monkeypatch, fixtures_dir)


def _add_sub(store: Store, name: str, *, enabled: bool = True) -> int:
    return store.add_subscription(name, f"https://airport-{name}.example/token-{name}",
                                  enabled=enabled)


def _dump(doc) -> bytes:
    return yaml.safe_dump(doc, allow_unicode=True, sort_keys=False).encode("utf-8")


def _read_clash(config: AppConfig, version: int) -> str:
    return (utils.version_dir(config.out_dir, version) / "clash.yaml").read_text(encoding="utf-8")


def _all_dirs_have_meta(config: AppConfig) -> bool:
    return all((utils.version_dir(config.out_dir, v) / "meta.json").is_file()
               for v in utils.list_versions(config.out_dir))


# ---------------------------------------------------------------- 用例

def test_normal_publish(harness: Harness, config: AppConfig, store: Store,
                        sub_a_path: Path, sub_b_path: Path) -> None:
    """正常发布：双订阅 → v1 发布成功，快照/状态/userinfo/钩子全部正确。"""
    a_id = _add_sub(store, "a")
    b_id = _add_sub(store, "b")
    harness.set_sub("a", sub_a_path, userinfo={"upload": 1, "download": 2, "total": 3, "expire": 4})
    harness.set_sub("b", sub_b_path)

    result = pipeline.run_full_pipeline(config, store)

    assert result.published is True
    assert result.version == 1
    assert result.node_count == 15          # sub_a 12 真实 + sub_b 3 真实
    assert result.filtered_count == 3       # 2 + 1 个假节点
    assert result.stale_subs == []
    assert result.errors == []

    # 快照：整批替换且含被滤节点
    assert len(store.list_nodes(sub_name="a")) == 14
    assert len(store.list_nodes(sub_name="a", filtered=True)) == 2
    assert len(store.list_nodes(sub_name="b")) == 4
    assert len(store.list_nodes(sub_name="b", filtered=True)) == 1

    # 抓取状态与 userinfo 落库
    assert store.get_subscription(a_id).last_fetch_status == FetchStatus.OK
    assert store.get_subscription(a_id).userinfo == {"upload": 1, "download": 2, "total": 3, "expire": 4}
    assert store.get_subscription(b_id).last_fetch_status == FetchStatus.OK

    # 四产物 + meta.json 落盘，当前版本可查
    vdir = utils.version_dir(config.out_dir, 1)
    for name in pipeline._ARTIFACT_ORDER:
        assert (vdir / name).is_file()
    assert (vdir / "meta.json").is_file()
    current = pipeline.current_version(config)
    assert current is not None and current.version == 1 and current.node_count == 15

    # 消歧后同名节点带 [别名] 后缀，都进了产物
    clash_text = _read_clash(config, 1)
    assert "家庭宽带 01 [a]" in clash_text and "家庭宽带 01 [b]" in clash_text

    # 镜像推送（首次发布内容视为变化）+ 纯净度后台增量扫描已触发
    assert harness.mirror_pushes == 1
    assert harness.purity_event.wait(timeout=5)
    assert harness.purity_calls[0]["full"] is False
    assert len(harness.purity_calls[0]["nodes"]) == 15


def test_content_unchanged_skips_mirror(harness: Harness, config: AppConfig, store: Store,
                                        sub_a_path: Path, sub_b_path: Path) -> None:
    """内容 hash 无变化 → 照常发布新版本但不通知镜像推送。"""
    _add_sub(store, "a")
    _add_sub(store, "b")
    harness.set_sub("a", sub_a_path)
    harness.set_sub("b", sub_b_path)

    assert pipeline.run_full_pipeline(config, store).published is True
    assert harness.mirror_pushes == 1
    second = pipeline.run_full_pipeline(config, store)
    assert second.published is True and second.version == 2
    assert harness.mirror_pushes == 1   # 未再推送


def test_mirror_push_only_on_change(harness: Harness, config: AppConfig, store: Store,
                                    sub_a_path: Path, sub_b_path: Path) -> None:
    """内容变化才推送；镜像未启用时尝试被拒、发布不受影响。"""
    b_id = _add_sub(store, "b")
    harness.set_sub("a", sub_a_path)
    harness.set_sub("b", sub_b_path)
    assert pipeline.run_full_pipeline(config, store).published is True
    assert (harness.mirror_attempts, harness.mirror_pushes) == (1, 1)

    assert pipeline.run_full_pipeline(config, store).published is True  # 内容不变
    assert (harness.mirror_attempts, harness.mirror_pushes) == (1, 1)

    # 内容变化 + 镜像未启用：尝试推送被拒（记日志），发布照常成功
    harness.mirror_enabled = False
    proxies = yaml.safe_load(sub_b_path.read_text(encoding="utf-8"))["proxies"]
    proxies.append({"name": "🇩🇪 德国 柏林 02", "type": "vless",
                    "server": "de-02.example-airport-b.com", "port": 2053,
                    "uuid": "dddddddd-dddd-4ddd-8ddd-ddddddddddd4",
                    "network": "tcp", "udp": True, "tls": True})
    harness.set_sub("b", content=_dump({"proxies": proxies}))
    assert pipeline.run_full_pipeline(config, store, sub_id=b_id).version == 3
    assert (harness.mirror_attempts, harness.mirror_pushes) == (2, 1)

    # 重新启用后，内容无变化的刷新依旧不推
    harness.mirror_enabled = True
    assert pipeline.run_full_pipeline(config, store, sub_id=b_id).published is True
    assert (harness.mirror_attempts, harness.mirror_pushes) == (2, 1)


def test_validation_failure_refuses_and_keeps_previous(harness: Harness, config: AppConfig,
                                                       store: Store, sub_a_path: Path,
                                                       sub_b_path: Path) -> None:
    """渲染/校验失败：拒绝发布、沿用上一版，磁盘产物一字不变。"""
    _add_sub(store, "a")
    _add_sub(store, "b")
    harness.set_sub("a", sub_a_path)
    harness.set_sub("b", sub_b_path)
    assert pipeline.run_full_pipeline(config, store).version == 1
    before = _read_clash(config, 1)

    harness.render_garbage = True
    result = pipeline.run_full_pipeline(config, store)

    assert result.published is False
    assert result.version is None
    assert any("发布被校验拦截" in e for e in result.errors)
    assert any("不是合法 YAML" in e for e in result.errors)
    assert _read_clash(config, 1) == before                 # 上一版产物未被动过
    assert utils.list_versions(config.out_dir) == [1]
    assert _all_dirs_have_meta(config)
    assert pipeline.current_version(config).version == 1

    # 显式 PublishError 也走同一安全网
    harness.render_garbage = False
    harness.publish_errors = ["两格式组数不一致"]
    result2 = pipeline.run_full_pipeline(config, store)
    assert result2.published is False
    assert "两格式组数不一致" in result2.errors
    assert pipeline.current_version(config).version == 1


def test_publish_crash_midway_leaves_no_partial(harness: Harness, config: AppConfig,
                                                store: Store, sub_a_path: Path) -> None:
    """发布中途异常：残留的无 meta 目录被清理，下一版版本号不跳号错乱。"""
    _add_sub(store, "a")
    harness.set_sub("a", sub_a_path)
    assert pipeline.run_full_pipeline(config, store).version == 1

    harness.fail_publish_midway = True
    result = pipeline.run_full_pipeline(config, store)
    assert result.published is False
    assert result.version is None
    assert any("发布过程异常" in e for e in result.errors)
    assert utils.list_versions(config.out_dir) == [1]       # 半成品 v2 已被清理
    assert _all_dirs_have_meta(config)
    assert pipeline.current_version(config).version == 1

    harness.fail_publish_midway = False
    recovered = pipeline.run_full_pipeline(config, store)
    assert recovered.published is True and recovered.version == 2
    assert utils.list_versions(config.out_dir) == [1, 2]


def test_empty_nodes_refused_no_subs(harness: Harness, config: AppConfig, store: Store) -> None:
    """误删全部订阅 + 空库：拒绝发布空配置，不产生任何版本。"""
    result = pipeline.run_full_pipeline(config, store)
    assert result.published is False
    assert result.version is None
    assert any("无启用订阅" in e for e in result.errors)
    assert any("拒绝发布空配置" in e for e in result.errors)
    assert utils.list_versions(config.out_dir) == []
    assert pipeline.current_version(config) is None


def test_empty_nodes_refused_all_fake(harness: Harness, config: AppConfig, store: Store) -> None:
    """抓取成功但清洗后 0 个有效节点：标记订阅失效、沿用旧快照、拒绝发布。"""
    sub_id = _add_sub(store, "a")
    harness.set_sub("a", content=_dump({"proxies": [
        {"name": "剩余流量：1GB", "type": "ss", "server": "x.example", "port": 8388,
         "cipher": "aes-256-gcm", "password": "fake-password-x"},
        {"name": "套餐到期：2026-01-01", "type": "ss", "server": "y.example", "port": 8388,
         "cipher": "aes-256-gcm", "password": "fake-password-y"},
    ]}))

    result = pipeline.run_full_pipeline(config, store)

    assert result.published is False
    assert result.version is None
    assert result.stale_subs == ["a"]
    assert any("订阅失效" in e and "0 个有效节点" in e for e in result.errors)
    assert any("拒绝发布空配置" in e for e in result.errors)
    assert store.get_subscription(sub_id).last_fetch_status == FetchStatus.INVALID
    assert store.list_nodes(sub_name="a") == []             # 未写入半截快照
    assert utils.list_versions(config.out_dir) == []


def test_subscription_invalid_keeps_old_snapshot(harness: Harness, config: AppConfig,
                                                 store: Store, sub_a_path: Path,
                                                 sub_b_path: Path) -> None:
    """订阅失效（401/403 / 超时）：沿用旧快照继续发布，UI 状态为「订阅失效」。"""
    a_id = _add_sub(store, "a")
    _add_sub(store, "b")
    harness.set_sub("a", sub_a_path)
    harness.set_sub("b", sub_b_path)
    assert pipeline.run_full_pipeline(config, store).version == 1
    assert len(store.list_nodes(sub_name="a")) == 14

    # 401/403 → 订阅失效
    harness.set_sub("a", status=FetchStatus.INVALID, error="401/403：订阅 token 无效")
    result = pipeline.run_full_pipeline(config, store)

    assert result.published is True                          # b 新鲜 + a 旧快照，照常分发
    assert result.version == 2
    assert result.node_count == 15
    assert result.stale_subs == ["a"]
    assert any("订阅失效" in e and "401/403" in e for e in result.errors)
    assert store.get_subscription(a_id).last_fetch_status == FetchStatus.INVALID
    assert len(store.list_nodes(sub_name="a")) == 14         # 旧快照未被清空
    clash_text = _read_clash(config, 2)
    assert "🇬🇧 英国 伦敦 01" in clash_text                    # a 的旧节点仍在产物里
    assert "🇯🇵 日本 大阪 家庭宽带 02" in clash_text           # b 的新节点也在（b 独有名不加后缀）

    # 超时 → 同样 stale
    harness.set_sub("a", status=FetchStatus.TIMEOUT, error="连接超时")
    result2 = pipeline.run_full_pipeline(config, store)
    assert result2.published is True and result2.version == 3
    assert result2.stale_subs == ["a"]
    assert store.get_subscription(a_id).last_fetch_status == FetchStatus.TIMEOUT


def test_versions_kept_five(harness: Harness, config: AppConfig, store: Store,
                            sub_a_path: Path) -> None:
    """版本目录保留最近 5 份。"""
    _add_sub(store, "a")
    harness.set_sub("a", sub_a_path)
    for _ in range(7):
        assert pipeline.refresh_all(config, store).published is True
    assert utils.list_versions(config.out_dir) == [3, 4, 5, 6, 7]
    assert _all_dirs_have_meta(config)
    assert pipeline.current_version(config).version == 7


def test_diff_recorded(harness: Harness, config: AppConfig, store: Store,
                       sub_a_path: Path) -> None:
    """节点 diff（新增/消失/改名）写入 meta.json 并可经 current_version 查询。"""
    _add_sub(store, "a")
    harness.set_sub("a", sub_a_path)
    assert pipeline.run_full_pipeline(config, store).version == 1

    proxies = yaml.safe_load(sub_a_path.read_text(encoding="utf-8"))["proxies"]
    proxies = [p for p in proxies if "英国" not in p["name"]]            # 消失 1 个
    for p in proxies:
        if "法兰克福" in p["name"]:
            p["name"] = "🇩🇪 德国 柏林 01"                                # 改名（server/port 不变）
    harness.set_sub("a", content=_dump({"proxies": proxies}))

    result = pipeline.run_full_pipeline(config, store)
    assert result.published is True and result.version == 2

    meta = pipeline.current_version(config)
    assert meta is not None
    assert "🇬🇧 英国 伦敦 01" in meta.diff_summary["removed"]
    assert meta.diff_summary["added"] == []                 # 改名按 renamed 记，不重复计新增
    assert ["🇩🇪 德国 法兰克福 01", "🇩🇪 德国 柏林 01"] in meta.diff_summary["renamed"]
    assert meta.node_count == 11


def test_rollback(harness: Harness, config: AppConfig, store: Store, sub_a_path: Path) -> None:
    """回退：历史产物复制为新版本（note 标注），损坏/缺失版本拒绝回退。"""
    _add_sub(store, "a")
    harness.set_sub("a", sub_a_path)
    assert pipeline.run_full_pipeline(config, store).version == 1

    proxies = [p for p in yaml.safe_load(sub_a_path.read_text(encoding="utf-8"))["proxies"]
               if "英国" not in p["name"]]
    harness.set_sub("a", content=_dump({"proxies": proxies}))
    assert pipeline.run_full_pipeline(config, store).version == 2

    pushes_before = harness.mirror_pushes
    meta = pipeline.rollback_to_version(config, store, 1)

    assert meta.version == 3
    assert meta.note == "回退到 v0001"
    assert meta.node_count == 12                            # 回到 v1 的节点数
    assert _read_clash(config, 3) == _read_clash(config, 1)  # 内容与源版本一致
    assert utils.list_versions(config.out_dir) == [1, 2, 3]
    current = pipeline.current_version(config)
    assert current is not None and current.version == 3 and current.note == "回退到 v0001"
    assert harness.mirror_pushes == pushes_before + 1        # 回退后镜像也拿到旧配置

    with pytest.raises(FileNotFoundError, match="不存在"):
        pipeline.rollback_to_version(config, store, 999)

    # 源产物损坏 → 本地检查拦截，绝不回退到坏产物
    (utils.version_dir(config.out_dir, 1) / "clash.yaml").write_text("proxies: [未闭合",
                                                                     encoding="utf-8")
    with pytest.raises(ValueError, match="YAML"):
        pipeline.rollback_to_version(config, store, 1)
    assert pipeline.current_version(config).version == 3


def test_single_sub_refresh(harness: Harness, config: AppConfig, store: Store,
                            sub_a_path: Path, sub_b_path: Path) -> None:
    """指定 sub_id：只抓取该订阅，其余订阅快照与状态不动，产物仍含全部订阅。"""
    a_id = _add_sub(store, "a")
    b_id = _add_sub(store, "b")
    harness.set_sub("a", sub_a_path)
    harness.set_sub("b", sub_b_path)
    assert pipeline.run_full_pipeline(config, store).version == 1
    harness.fetch_calls.clear()

    proxies = yaml.safe_load(sub_b_path.read_text(encoding="utf-8"))["proxies"]
    proxies.append({"name": "🇩🇪 德国 柏林 02", "type": "vless",
                    "server": "de-02.example-airport-b.com", "port": 2053,
                    "uuid": "dddddddd-dddd-4ddd-8ddd-ddddddddddd4",
                    "network": "tcp", "udp": True, "tls": True})
    harness.set_sub("b", content=_dump({"proxies": proxies}))

    result = pipeline.run_full_pipeline(config, store, sub_id=b_id)

    assert result.published is True and result.version == 2 and result.node_count == 16
    assert harness.fetch_calls == ["b"]
    assert store.get_subscription(a_id).last_fetch_status == FetchStatus.OK
    assert len(store.list_nodes(sub_name="a")) == 14
    assert "🇩🇪 德国 柏林 02" in _read_clash(config, 2)          # b 独有名不加后缀

    # 显式 sub_id 不存在 → 明确状态、不动任何产物
    missing = pipeline.run_full_pipeline(config, store, sub_id=9999)
    assert missing.published is False
    assert any("订阅不存在" in e for e in missing.errors)
    assert pipeline.current_version(config).version == 2


def test_render_receives_latest_purity(harness: Harness, config: AppConfig, store: Store,
                                       sub_a_path: Path) -> None:
    """渲染回灌库内最新纯净度结果（docs/02 §2：Claude 组评分降序排序的数据源）。"""
    _add_sub(store, "a")
    harness.set_sub("a", sub_a_path)
    store.save_purity_result(PurityResult(
        node_name="🇺🇸 美国 洛杉矶 家庭宽带 01", source_sub="a",
        checked_at=utils.now_iso(), ip_type="residential", claude_rank=3,
    ))

    assert pipeline.run_full_pipeline(config, store).published is True

    assert len(harness.render_purity) == 2          # 假 templater 仅 render_clash 记录（主+离线两次）
    for purity in harness.render_purity:
        assert len(purity) == 1
        assert purity[0].node_name == "🇺🇸 美国 洛杉矶 家庭宽带 01"
        assert purity[0].claude_rank == 3


def test_render_receives_stability_summary(harness: Harness, config: AppConfig, store: Store,
                                           sub_a_path: Path) -> None:
    """渲染回灌 24h 稳定性摘要（判死节点标记 hard_down，供 templater 剔除/沉底）。"""
    from datetime import datetime, timedelta

    from app.models import HealthSample

    _add_sub(store, "a")
    harness.set_sub("a", sub_a_path)
    at = (datetime.now() - timedelta(minutes=15)).isoformat()
    store.save_health_samples([
        HealthSample(node_name="🇺🇸 美国 洛杉矶 家庭宽带 01", source_sub="a",
                     checked_at=at, delay_ms=None),   # delay=None = 本轮失败
    ])

    assert pipeline.run_full_pipeline(config, store).published is True

    assert len(harness.render_stability) == 2        # 与 render_purity 同口径（主+离线两次）
    for rows in harness.render_stability:
        assert len(rows) == 1
        assert rows[0]["node_name"] == "🇺🇸 美国 洛杉矶 家庭宽带 01"
        assert rows[0]["down_streak"] == 1           # 仅 1 轮失败：未达判死线但已可见
        assert rows[0]["hard_down"] is False


def test_single_sub_refresh_keeps_disambiguation(harness: Harness, config: AppConfig,
                                                 store: Store, sub_a_path: Path,
                                                 sub_b_path: Path) -> None:
    """单订阅刷新后跨订阅同名节点的「 [别名]」后缀不丢失（docs/02 §1、docs/04 验收 #8）。"""
    _add_sub(store, "a")
    _add_sub(store, "b")
    harness.set_sub("a", sub_a_path)
    harness.set_sub("b", sub_b_path)
    assert pipeline.run_full_pipeline(config, store).version == 1

    result = pipeline.run_full_pipeline(config, store, sub_id=1)   # 单刷 a

    assert result.published is True
    a_names = [n.name for n in store.list_nodes(sub_name="a", filtered=False)]
    b_names = [n.name for n in store.list_nodes(sub_name="b", filtered=False)]
    assert "🇺🇸 美国 洛杉矶 家庭宽带 01 [a]" in a_names
    assert "🇺🇸 美国 洛杉矶 家庭宽带 01" not in a_names      # 后缀不因单刷丢失
    assert "🇺🇸 美国 洛杉矶 家庭宽带 01 [b]" in b_names      # 两侧都带后缀，产物无重名
    clash_text = _read_clash(config, result.version)
    assert "家庭宽带 01 [a]" in clash_text and "家庭宽带 01 [b]" in clash_text


def test_new_sub_with_duplicate_name_publishes(harness: Harness, config: AppConfig,
                                               store: Store, sub_a_path: Path,
                                               sub_b_path: Path) -> None:
    """新增含同名节点的订阅（单刷路径）：消歧后正常发布，验收 #8 成立。"""
    _add_sub(store, "a")
    harness.set_sub("a", sub_a_path)
    assert pipeline.run_full_pipeline(config, store).version == 1

    _add_sub(store, "b")
    harness.set_sub("b", sub_b_path)
    result = pipeline.run_full_pipeline(config, store, sub_id=2)

    assert result.published is True
    assert not any("节点名重复" in e for e in result.errors)
    names = [n.name for n in store.list_nodes(filtered=False)]
    assert len(names) == len(set(names))            # 产物全局无重名
    assert "🇺🇸 美国 洛杉矶 家庭宽带 01 [b]" in names


def test_placeholder_rules_refuse_publish(harness: Harness, config: AppConfig, store: Store,
                                          sub_a_path: Path) -> None:
    """规则集存在空占位文件（上游/缓存/基线全缺）→ 拒绝发布，沿用上一版（docs/01 安全网）。"""
    _add_sub(store, "a")
    harness.set_sub("a", sub_a_path)
    assert pipeline.run_full_pipeline(config, store).version == 1

    placeholder = config.rules_dir / "ChinaMax.yaml"
    placeholder.write_text("# 占位文件：上游拉取失败且无旧缓存/基线\npayload: []\n",
                           encoding="utf-8")
    result = pipeline.run_full_pipeline(config, store)

    assert result.published is False
    assert result.version is None
    assert any("空占位" in e and "ChinaMax.yaml" in e for e in result.errors)
    assert pipeline.current_version(config).version == 1    # 沿用上一版

    placeholder.unlink()
    recovered = pipeline.run_full_pipeline(config, store)
    assert recovered.published is True and recovered.version == 2


def test_disabled_sub_not_fetched_but_snapshots_render(harness: Harness, config: AppConfig,
                                                       store: Store, sub_a_path: Path) -> None:
    """禁用订阅：不抓取，但其已有快照仍参与渲染（停用=停刷新，不是删除）。"""
    _add_sub(store, "a")
    _add_sub(store, "b", enabled=False)
    store.replace_nodes("b", [Node(name="🇯🇵 日本 大阪 家庭宽带 02 [b]", type="vless",
                                   server="jp-osa.example-airport-b.com", port=2053,
                                   source_sub="b", credentials={"uuid": "seed-uuid"})])
    harness.set_sub("a", sub_a_path)   # b 被禁用：即便配了抓取脚本也不应被用到

    result = pipeline.run_full_pipeline(config, store)

    assert result.published is True and result.node_count == 13   # a 12 + b 旧快照 1
    assert harness.fetch_calls == ["a"]
    assert "日本 大阪 家庭宽带 02 [b]" in _read_clash(config, 1)


def test_current_version_empty(config: AppConfig) -> None:
    """从未发布过 → current_version 返回 None。"""
    assert pipeline.current_version(config) is None
