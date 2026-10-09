"""纯净度检测模块测试（app/purity.py + app/probe.py）。

纪律：控制 API 与 ip-api 全部用假实现（本地线程 HTTP 假服务 / MockTransport / FakeProbe），
绝不真跑 mihomo、绝不访问外网；数据目录走 conftest 的 tmp_path 隔离。
覆盖：评分各分支、限速间隔、增量逻辑、unavailable 降级、属性变化告警、
探测配置生成与控制 API 封装、ipinfo widget 增强源（客户端解析 / 合流纪律 / 权威分类 / privacy 降档）。
"""
from __future__ import annotations

import json
import logging
import re
import socket
import threading
import time
from dataclasses import replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import unquote, urlsplit

import httpx
import pytest
import yaml

from app import purity as purity_mod
from app.models import Node, PurityResult
from app.probe import PROBE_GROUP, ProbeInstance
from app.purity import (
    IpApiProvider,
    IpInfoWidgetClient,
    IpInfoWidgetError,
    PurityProviderError,
    _enhance_raw,
    build_purity_result,
    claude_rank_result,
    classify_ip_type,
)
from app.utils import now_iso


# ---------------------------------------------------------------------- 造数工具

def _node(name: str, *, source_sub: str = "subA", region: str | None = None,
          residential: bool = False, filtered: bool = False) -> Node:
    return Node(
        name=name, type="vless", server=f"{name}.example.com", port=443,
        source_sub=source_sub, region=region, residential=residential, filtered=filtered,
    )


def _raw(**overrides):
    """ip-api 成功响应样本（默认 Comcast 住宅宽带）。"""
    data = {
        "status": "success",
        "query": "203.0.113.1",
        "country": "United States",
        "as": "AS7922 Comcast Cable",
        "asname": "Comcast Cable",
        "org": "Comcast Cable",
        "isp": "Comcast Cable",
        "proxy": False,
        "hosting": False,
        "mobile": False,
    }
    data.update(overrides)
    return data


def _purity_result(node: Node, *, ip_type: str | None, rank: int | None,
                   exit_ip: str | None = "198.51.100.1") -> PurityResult:
    return PurityResult(
        node_name=node.name, source_sub=node.source_sub, checked_at=now_iso(),
        exit_ip=exit_ip, country="United States", ip_type=ip_type, claude_rank=rank,
        hosting=ip_type == "datacenter",
    )


# ---------------------------------------------------------------------- 假探测实例

class FakeProbe:
    """ProbeInstance 假实现：记录调用、按场景配置失败行为（子类用类属性定制）。"""

    instances: list["FakeProbe"] = []

    start_ok: bool = True
    start_error: Exception | None = None
    dead_names: frozenset[str] = frozenset()       # delay 返回 None（节点不可达）
    reject_names: frozenset[str] = frozenset()     # select 返回 False（切换失败）
    crash_after: int | None = None                 # 第 N 次 select 后实例「崩溃」

    def __init__(self, config, nodes) -> None:
        self.config = config
        self.nodes = list(nodes)
        self.select_calls: list[str] = []
        self.delay_calls: list[str] = []
        self.stopped = False
        FakeProbe.instances.append(self)

    def start(self) -> bool:
        if self.start_error is not None:
            raise self.start_error
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
        return f"http://127.0.0.1:{self.config.probe_mixed_port}"

    def delay(self, node_name: str) -> int | None:
        self.delay_calls.append(node_name)
        return None if node_name in self.dead_names else 88


class FailingProbe(FakeProbe):
    start_ok = False


class RaisingProbe(FakeProbe):
    start_error = RuntimeError("mihomo 拉起爆炸")


class CrashingProbe(FakeProbe):
    crash_after = 2


class UnreachableNodeProbe(FakeProbe):
    dead_names = frozenset({"节点二"})


class UnselectableNodeProbe(FakeProbe):
    reject_names = frozenset({"节点二"})


@pytest.fixture()
def fake_probe_cls(monkeypatch: pytest.MonkeyPatch):
    """把 purity.scan 内部的 ProbeInstance 换成假实现，并按用例隔离实例记录。"""
    FakeProbe.instances = []
    monkeypatch.setattr(purity_mod, "ProbeInstance", FakeProbe)
    return FakeProbe


class FakeProvider:
    """PurityProvider 假实现：按调用顺序消费 responses，超出后返回 default。"""

    def __init__(self, responses=None, default=None) -> None:
        self.responses: list = list(responses or [])
        self.default = default if default is not None else _raw()
        self.calls: list[str | None] = []

    def lookup(self, *, proxy_url: str | None = None) -> dict:
        self.calls.append(proxy_url)
        if self.responses:
            item = self.responses.pop(0)
            if isinstance(item, Exception):
                raise item
            return item
        return dict(self.default)


class NoopEnhancer:
    """PurityEnhancer no-op 假实现：返回空 dict（不合并任何增强数据），仅记录调用。"""

    def __init__(self, *args, **kwargs) -> None:
        self.calls: list[dict] = []

    def lookup(self, *, ip: str, proxy_url: str | None = None) -> dict:
        self.calls.append({"ip": ip, "proxy_url": proxy_url})
        return {}


class RecordingEnhancer(NoopEnhancer):
    """按构造参数返回固定 widget 数据 / 抛错的假增强源。"""

    def __init__(self, result: dict | None = None, error: Exception | None = None) -> None:
        super().__init__()
        self.result = result if result is not None else {}
        self.error = error

    def lookup(self, *, ip: str, proxy_url: str | None = None) -> dict:
        self.calls.append({"ip": ip, "proxy_url": proxy_url})
        if self.error is not None:
            raise self.error
        return self.result


@pytest.fixture(autouse=True)
def noop_default_enhancer(monkeypatch: pytest.MonkeyPatch):
    """默认把增强源构造类换成 no-op：scan 全部用例不发起真实 ipinfo 查询。

    增强行为用例显式传入 RecordingEnhancer 验证；默认构造行为另有打桩用例覆盖。
    """
    monkeypatch.setattr(purity_mod, "IpInfoWidgetClient", NoopEnhancer)


# ---------------------------------------------------------------------- 假控制 API / 假代理服务器

class _FakeMihomoHandler(BaseHTTPRequestHandler):
    """mihomo 控制 API 假实现：/version、/proxies/<name>/delay、PUT /proxies/PROBE。"""

    def log_message(self, *args):  # noqa: N802 —— 静默
        pass

    def _send_json(self, payload: dict, code: int = 200) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802
        path = urlsplit(self.path).path
        if path == "/version":
            self._send_json({"version": "fake-mihomo-1.0"})
            return
        match = re.fullmatch(r"/proxies/(.+)/delay", path)
        if match:
            name = unquote(match.group(1))
            if name in getattr(self.server, "dead_names", set()):
                self._send_json({"message": "An error occurred"}, code=504)
            else:
                self._send_json({"delay": 233, "mean_delay": 100})
            return
        self._send_json({"message": "not found"}, code=404)

    def do_PUT(self) -> None:  # noqa: N802
        path = urlsplit(self.path).path
        if path == f"/proxies/{PROBE_GROUP}":
            length = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(length) or b"{}") if length else {}
            name = body.get("name")
            if name in getattr(self.server, "reject_names", set()):
                self._send_json({"message": "proxy not found"}, code=400)
                return
            self.server.selected.append(name)
            self.send_response(204)
            self.end_headers()
            return
        self._send_json({"message": "not found"}, code=404)


class _FakeProxyHandler(BaseHTTPRequestHandler):
    """把收到的请求路径（代理场景下是绝对 URL）记下来，返回固定 ip-api 风格响应。"""

    def log_message(self, *args):  # noqa: N802
        pass

    def do_GET(self) -> None:  # noqa: N802
        self.server.seen_paths.append(self.path)
        body = json.dumps({
            "status": "success", "query": "203.0.113.7", "country": "United States",
            "proxy": False, "hosting": False, "mobile": False,
        }).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def _start_http_server(handler_cls) -> ThreadingHTTPServer:
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler_cls)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


@pytest.fixture()
def fake_controller():
    server = _start_http_server(_FakeMihomoHandler)
    server.selected = []
    server.dead_names = {"dead-node"}
    server.reject_names = set()
    yield server
    server.shutdown()
    server.server_close()


@pytest.fixture()
def fake_proxy_server():
    server = _start_http_server(_FakeProxyHandler)
    server.seen_paths = []
    yield server
    server.shutdown()
    server.server_close()


# ---------------------------------------------------------------------- probe：配置生成

class TestProbeConfig:
    def test_generated_config_default_ports(self, config):
        probe = ProbeInstance(config, [_node("节点一"), _node("节点二")])
        path = probe.write_config()
        assert path == config.probe_config_path
        assert path == config.data_dir / "probe" / "config.yaml"
        doc = yaml.safe_load(path.read_text(encoding="utf-8"))
        assert doc["external-controller"] == "127.0.0.1:9095"
        assert doc["mixed-port"] == 9096
        assert doc["bind-address"] == "127.0.0.1"          # 混合端口仅绑 127.0.0.1
        assert doc["allow-lan"] is False
        assert doc["rules"] == ["MATCH,PROBE"]
        assert [p["name"] for p in doc["proxies"]] == ["节点一", "节点二"]
        assert doc["proxies"][0]["type"] == "vless"
        assert doc["proxy-groups"] == [
            {"name": PROBE_GROUP, "type": "select", "proxies": ["节点一", "节点二", "DIRECT"]},
        ]

    def test_custom_ports_and_name_dedupe(self, config):
        cfg = replace(config, probe_controller_port=19095, probe_mixed_port=19096)
        nodes = [_node("重复"), _node("重复"), _node("唯一")]
        probe = ProbeInstance(cfg, nodes)
        doc = yaml.safe_load(probe.write_config().read_text(encoding="utf-8"))
        assert doc["external-controller"] == "127.0.0.1:19095"
        assert doc["mixed-port"] == 19096
        assert [p["name"] for p in doc["proxies"]] == ["重复", "唯一"]     # 重名防御性去重
        assert doc["proxy-groups"][0]["proxies"] == ["重复", "唯一", "DIRECT"]
        assert probe.nodes == [nodes[0], nodes[2]]

    def test_empty_nodes_group_falls_back_to_direct(self, config):
        probe = ProbeInstance(config, [])
        doc = yaml.safe_load(probe.write_config().read_text(encoding="utf-8"))
        assert doc["proxies"] == []
        assert doc["proxy-groups"][0]["proxies"] == ["DIRECT"]


# ---------------------------------------------------------------------- probe：子进程生命周期

class TestProbeLifecycle:
    def test_start_missing_binary_returns_false(self, config, tmp_path):
        cfg = replace(config, mihomo_path=str(tmp_path / "no-such-mihomo.exe"))
        probe = ProbeInstance(cfg, [_node("节点一")])
        assert probe.start() is False                      # 不抛异常
        assert probe.is_available() is False
        assert cfg.probe_config_path.exists()              # 配置已落盘便于排查
        probe.stop()

    def test_start_broken_binary_returns_false(self, config, tmp_path):
        binary = tmp_path / "mihomo-broken"
        binary.write_text("this is not an executable", encoding="utf-8")
        cfg = replace(config, mihomo_path=str(binary))
        probe = ProbeInstance(cfg, [_node("节点一")])
        assert probe.start() is False
        assert probe.is_available() is False
        probe.stop()

    def test_start_binary_not_on_path_returns_false(self, config):
        cfg = replace(config, mihomo_path="definitely-no-mihomo-xyz")
        probe = ProbeInstance(cfg, [])
        assert probe.start() is False

    def test_stop_is_idempotent(self, config):
        probe = ProbeInstance(config, [])
        probe.stop()
        probe.stop()  # 重复调用不抛错


# ---------------------------------------------------------------------- probe：控制 API

class TestProbeControlApi:
    def test_client_ignores_system_proxy(self, config):
        """控制 API 仅 127.0.0.1 可达：必须绕过系统/环境代理，否则被劫持 502。"""
        probe = ProbeInstance(config, [])
        assert probe._client.trust_env is False
        probe.stop()

    def test_select_delay_local_proxy_url(self, config, fake_controller):
        cfg = replace(config, probe_controller_port=fake_controller.server_address[1])
        probe = ProbeInstance(cfg, [_node("🇺🇸 美国 洛杉矶 家庭宽带 01")])
        assert probe.local_proxy_url() == f"http://127.0.0.1:{cfg.probe_mixed_port}"
        assert probe.is_available() is False   # 未 start（无子进程）

        us_name = "🇺🇸 美国 洛杉矶 家庭宽带 01"
        assert probe.select(us_name) is True   # emoji 名需正确编码
        assert fake_controller.selected == [us_name]

        fake_controller.reject_names.add("不存在")
        assert probe.select("不存在") is False

        jp_name = "🇯🇵 日本 家庭宽带 02"
        assert probe.delay(jp_name) == 233
        assert probe.delay("dead-node") is None
        probe.stop()

    def test_control_api_unreachable(self, config):
        sock = socket.socket()
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
        sock.close()  # 拿一个当前无人监听的端口
        cfg = replace(config, probe_controller_port=port)
        probe = ProbeInstance(cfg, [])
        assert probe.select("任意节点") is False
        assert probe.delay("任意节点") is None


# ---------------------------------------------------------------------- IpApiProvider

class TestIpApiProvider:
    def test_rate_limit_min_interval_between_requests(self):
        """默认 min_interval=1.4s：相邻请求间隔必须 ≥1.4 秒（45 req/min）。"""
        times: list[float] = []

        def handler(request: httpx.Request) -> httpx.Response:
            times.append(time.monotonic())
            assert request.url.host == "ip-api.com"
            assert "fields=status,country,as,asname,org,isp,proxy,hosting,mobile" in str(request.url)
            return httpx.Response(200, json=_raw())

        provider = IpApiProvider(transport=httpx.MockTransport(handler))
        started = time.monotonic()
        for _ in range(3):
            provider.lookup()   # 限速门闩与是否走代理无关；代理路径由本地假代理用例覆盖
        gaps = [later - earlier for earlier, later in zip(times, times[1:])]
        assert len(times) == 3
        # Windows 计时器粒度 ~15.6ms：sleep(1.4) 的实测间隔可能提前一个 tick 醒，
        # 容差放宽到 100ms（≈6 tick）——校验「不快于 45 req/min 的量级」足矣
        assert gaps and all(gap >= 1.30 for gap in gaps)
        assert time.monotonic() - started < 20

    def test_retries_once_on_429_then_succeeds(self):
        calls: list[int] = []

        def handler(request: httpx.Request) -> httpx.Response:
            calls.append(1)
            if len(calls) == 1:
                return httpx.Response(429)
            return httpx.Response(200, json=_raw(query="198.51.100.9"))

        provider = IpApiProvider(min_interval=0.01, backoff=0.01, transport=httpx.MockTransport(handler))
        data = provider.lookup()
        assert len(calls) == 2
        assert data["query"] == "198.51.100.9"

    def test_gives_up_after_second_429(self):
        calls: list[int] = []

        def handler(request: httpx.Request) -> httpx.Response:
            calls.append(1)
            return httpx.Response(429)

        provider = IpApiProvider(min_interval=0.01, backoff=0.01, transport=httpx.MockTransport(handler))
        with pytest.raises(PurityProviderError):
            provider.lookup()
        assert len(calls) == 2   # 只重试一次

    def test_raises_on_status_fail(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"status": "fail", "message": "reserved range"})

        provider = IpApiProvider(min_interval=0.01, transport=httpx.MockTransport(handler))
        with pytest.raises(PurityProviderError, match="reserved"):
            provider.lookup()

    def test_direct_lookup_without_proxy(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json=_raw(query="198.51.100.5"))

        provider = IpApiProvider(min_interval=0.01, transport=httpx.MockTransport(handler))
        assert provider.lookup()["query"] == "198.51.100.5"

    def test_lookup_routes_via_local_proxy(self, fake_proxy_server):
        """真实走一遍本地 HTTP 代理：确认请求以绝对 URL 发往代理且带上 fields 参数。"""
        provider = IpApiProvider(min_interval=0.01)
        proxy_url = f"http://127.0.0.1:{fake_proxy_server.server_address[1]}"
        data = provider.lookup(proxy_url=proxy_url)
        assert data["query"] == "203.0.113.7"
        assert fake_proxy_server.seen_paths, "代理服务器未收到任何请求"
        sent = fake_proxy_server.seen_paths[0]
        assert sent.startswith("http://ip-api.com/json/?fields=status,country,as,asname,org,isp,proxy,hosting,mobile")


# ---------------------------------------------------------------------- IpInfoWidgetClient（增强源）

_WIDGET_JSON = {
    "ip": "203.0.113.1",
    "country": "US",
    "asn": {"asn": "AS7018", "name": "AT&T Services, Inc.", "domain": "att.com", "type": "isp"},
    "company": {"name": "AT&T Services, Inc.", "domain": "att.com", "type": "isp"},
    "privacy": {"vpn": False, "proxy": False, "tor": False, "relay": False, "hosting": False},
    "is_hosting": False,
    "is_mobile": False,
}


class TestIpInfoWidgetClient:
    def test_parses_widget_response(self):
        seen: dict = {}

        def handler(request: httpx.Request) -> httpx.Response:
            seen["path"] = request.url.path
            seen["ua"] = request.headers.get("user-agent", "")
            return httpx.Response(200, json=_WIDGET_JSON)

        client = IpInfoWidgetClient(transport=httpx.MockTransport(handler))
        data = client.lookup(ip="203.0.113.1")
        assert data == _WIDGET_JSON
        assert seen["path"] == "/widget/demo/203.0.113.1"
        assert "Mozilla/5.0" in seen["ua"]

    @pytest.mark.parametrize(("status_code", "body"),
                             [(503, b"service unavailable"), (200, b"<html>not json</html>")])
    def test_bad_response_raises(self, status_code, body):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(status_code, content=body)

        client = IpInfoWidgetClient(transport=httpx.MockTransport(handler))
        with pytest.raises(IpInfoWidgetError):
            client.lookup(ip="203.0.113.1")

    def test_transport_error_raises(self):
        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("boom")

        client = IpInfoWidgetClient(transport=httpx.MockTransport(handler))
        with pytest.raises(IpInfoWidgetError):
            client.lookup(ip="203.0.113.1")


class TestEnhanceRaw:
    def test_merges_and_reports(self):
        raw = _raw()
        enhancer = RecordingEnhancer(result=_WIDGET_JSON)
        merged, did = _enhance_raw(raw, enhancer, proxy_url="http://127.0.0.1:9", node_name="节点一")
        assert did is True
        assert merged["ipinfo"] == _WIDGET_JSON
        assert "ipinfo" not in raw                       # 原 raw 不被就地修改
        assert enhancer.calls == [{"ip": "203.0.113.1", "proxy_url": "http://127.0.0.1:9"}]

    def test_failure_degrades_to_base_data(self):
        raw = _raw()
        merged, did = _enhance_raw(raw, RecordingEnhancer(error=IpInfoWidgetError("HTTP 503")),
                                   proxy_url=None, node_name="节点一")
        assert did is False
        assert merged == raw

    def test_no_exit_ip_skips_lookup(self):
        enhancer = RecordingEnhancer(result=_WIDGET_JSON)
        merged, did = _enhance_raw({"status": "success"}, enhancer,
                                   proxy_url=None, node_name="节点一")
        assert did is False
        assert enhancer.calls == []
        assert merged == {"status": "success"}

    def test_empty_widget_not_merged(self):
        merged, did = _enhance_raw(_raw(), RecordingEnhancer(result={}),
                                   proxy_url=None, node_name="节点一")
        assert did is False
        assert "ipinfo" not in merged


# ---------------------------------------------------------------------- 评分与分类

class TestClassificationAndRank:
    def test_residential_isp_keyword(self):
        result = build_purity_result(_node("美国家宽"), _raw())
        assert result.ip_type == "residential"
        assert claude_rank_result(result, None) == 3

    def test_residential_by_node_name_flag(self):
        """hosting=false 且无 ISP 关键词，但节点名标记家宽（node.residential）→ 住宅。"""
        node = _node("美国家宽", residential=True)
        result = build_purity_result(node, _raw(org="North State Company",
                                                **{"as": "AS64512 Example Networks"},
                                                asname="Example Networks",
                                                isp="Example Networks"))
        assert result.ip_type == "residential"
        assert result.claude_rank == 3
        # 同样的信号、无节点名加持 → unknown（2 分，可用但不优先）
        plain = build_purity_result(_node("普通节点"), _raw(org="North State Company",
                                                            **{"as": "AS64512 Example Networks"},
                                                            asname="Example Networks",
                                                            isp="Example Networks"))
        assert plain.ip_type == "unknown"
        assert plain.claude_rank == 2

    def test_datacenter_small_idc(self):
        node = _node("美国 DMIT")
        result = build_purity_result(node, _raw(hosting=True,
                                                **{"as": "AS906 DMIT Cloud Services"},
                                                asname="DMIT Cloud Services", org="DMIT",
                                                isp="DMIT Cloud Services"))
        assert result.ip_type == "datacenter"
        assert result.claude_rank == 2

    def test_datacenter_big_cloud_asn(self):
        result = build_purity_result(_node("美国 AWS"), _raw(
            hosting=True, **{"as": "AS16509 Amazon.com, Inc."}, asname="AMAZON-02",
            org="Amazon.com, Inc.", isp="Amazon.com"))
        assert result.ip_type == "datacenter"
        assert result.claude_rank == 1

    def test_marked_proxy_downweighted(self):
        result = build_purity_result(_node("被标记节点"), _raw(proxy=True))
        assert result.claude_rank == 0   # 一票降权，即使其他信号是住宅

    def test_mobile_treated_like_residential(self):
        result = build_purity_result(_node("美国移动"), _raw(
            mobile=True, isp="T-Mobile USA", **{"as": "AS21928 T-MOBILE-6"}, org="T-Mobile USA"))
        assert result.ip_type == "mobile"
        assert result.claude_rank == 3

    def test_classify_ip_type_direct_call(self):
        node = _node("x")
        result = PurityResult(node_name="x", source_sub="s", checked_at=now_iso())
        assert classify_ip_type(result, node) == "unknown"
        result.hosting = True
        assert classify_ip_type(result, node) == "datacenter"

    # ---------------- ipinfo 增强：权威分类与 privacy 降档

    def test_authoritative_isp_overrides_keyword_miss(self):
        """org 无 ISP 关键词，ipinfo asn.type=isp 权威判住宅 3 分。"""
        raw = _raw(org="North State Company", **{"as": "AS64512 Example Networks"},
                   asname="Example Networks", isp="North State Company",
                   ipinfo={"asn": {"type": "isp", "name": "AT&T Services"}})
        result = build_purity_result(_node("普通节点"), raw)
        assert result.ip_type == "residential"
        assert result.claude_rank == 3

    def test_authoritative_hosting_fixes_ipapi_miss(self):
        """ip-api 漏标 hosting，ipinfo asn.type=hosting 权威判机房。"""
        raw = _raw(ipinfo={"asn": {"type": "hosting", "name": "Some IDC"}})
        result = build_purity_result(_node("普通节点"), raw)
        assert result.ip_type == "datacenter"
        assert result.claude_rank == 2

    def test_top_level_is_hosting_authoritative(self):
        """顶层 is_hosting=true（2026-10-09 实测 schema）同样权威判机房。"""
        raw = _raw(ipinfo={"is_hosting": True})
        result = build_purity_result(_node("普通节点"), raw)
        assert result.ip_type == "datacenter"
        assert result.claude_rank == 2

    def test_top_level_is_mobile_authoritative(self):
        """顶层 is_mobile=true 权威判移动，与住宅同档 3 分。"""
        raw = _raw(ipinfo={"is_mobile": True})
        result = build_purity_result(_node("普通节点"), raw)
        assert result.ip_type == "mobile"
        assert result.claude_rank == 3

    def test_privacy_flag_caps_residential_rank(self):
        """关键词住宅 + privacy.vpn=true → 封顶 2 分；无标记则 3 分。"""
        base = {"org": "Comcast Cable", "isp": "Comcast Cable"}
        flagged = build_purity_result(_node("节点一"), _raw(
            **base, ipinfo={"privacy": {"vpn": True, "proxy": False, "tor": False}}))
        assert flagged.ip_type == "residential"
        assert flagged.claude_rank == 2
        clean = build_purity_result(_node("节点一"), _raw(
            **base, ipinfo={"privacy": {"vpn": False, "proxy": False, "tor": False}}))
        assert clean.claude_rank == 3

    def test_privacy_cap_tor_counts_and_small_idc_unchanged(self):
        """privacy.tor 同样触发封顶；中小机房 2 分封顶后不变。"""
        tor = build_purity_result(_node("节点一"), _raw(
            org="North State Company", isp="North State Company",
            **{"as": "AS64512 Example Networks"}, asname="Example Networks",
            ipinfo={"privacy": {"vpn": False, "proxy": False, "tor": True}}))
        assert claude_rank_result(tor, None) == 2

        idc = build_purity_result(_node("节点二"), _raw(
            hosting=True, ipinfo={"privacy": {"vpn": True, "proxy": False, "tor": False}}))
        assert claude_rank_result(idc, None) == 2

    def test_big_cloud_matched_via_ipinfo_name(self):
        """ip-api org/isp 缺失时，ipinfo company.name 命中大厂云关键词 → 机房 1 分。"""
        raw = _raw(org=None, isp=None, **{"as": None}, asname=None, hosting=True,
                   ipinfo={"asn": {"type": "hosting"},
                           "company": {"name": "Amazon.com, Inc."}})
        result = build_purity_result(_node("节点一"), raw)
        assert result.ip_type == "datacenter"
        assert result.claude_rank == 1

    def test_proxy_one_vote_unaffected_by_enhancement(self):
        """ip-api proxy=true 仍一票 0 分，ipinfo isp 信号不能救回。"""
        result = build_purity_result(_node("节点一"), _raw(proxy=True,
                                                           ipinfo={"asn": {"type": "isp"}}))
        assert result.claude_rank == 0

    def test_no_enhancement_keeps_legacy_behavior(self):
        """无 ipinfo 增强时分类/评分与既有逻辑完全一致（unknown 2 分兜底）。"""
        result = build_purity_result(_node("普通节点"), _raw(org="North State Company",
                                                             **{"as": "AS64512 Example Networks"},
                                                             asname="Example Networks",
                                                             isp="North State Company"))
        assert result.ip_type == "unknown"
        assert result.claude_rank == 2


# ---------------------------------------------------------------------- scan 主流程

class TestScan:
    def test_full_mode_checks_all_and_persists(self, config, store, fake_probe_cls):
        nodes = [_node("节点一"), _node("节点二"), _node("节点三")]
        provider = FakeProvider()
        report = purity_mod.scan(nodes, config=config, store=store, provider=provider, full=True)
        assert (report.checked, report.skipped, report.unavailable) == (3, 0, False)
        probe = fake_probe_cls.instances[0]
        assert [n.name for n in probe.nodes] == ["节点一", "节点二", "节点三"]
        assert probe.select_calls == ["节点一", "节点二", "节点三"]
        assert probe.delay_calls == ["节点一", "节点二", "节点三"]
        assert probe.stopped is True                       # finally 里必然 stop
        assert provider.calls == ["http://127.0.0.1:9096"] * 3   # 全部经混合端口

        latest = {r.node_name: r for r in store.latest_purity_results()}
        assert set(latest) == {"节点一", "节点二", "节点三"}
        first = latest["节点一"]
        assert first.exit_ip == "203.0.113.1"
        assert first.ip_type == "residential"
        assert first.claude_rank == 3
        assert first.checked_at
        assert first.raw["status"] == "success"

    def test_fake_nodes_excluded_from_probe_and_scan(self, config, store, fake_probe_cls):
        nodes = [_node("节点一"), _node("剩余流量", filtered=True), _node("节点三")]
        report = purity_mod.scan(nodes, config=config, store=store, provider=FakeProvider(), full=True)
        assert report.checked == 2
        probe = fake_probe_cls.instances[0]
        assert [n.name for n in probe.nodes] == ["节点一", "节点三"]
        assert "剩余流量" not in probe.select_calls

    def test_incremental_mode_only_new_nodes(self, config, store, fake_probe_cls):
        nodes = [_node("节点一"), _node("节点二"), _node("节点三")]
        store.save_purity_result(_purity_result(nodes[0], ip_type="residential", rank=3))
        provider = FakeProvider()
        report = purity_mod.scan(nodes, config=config, store=store, provider=provider, full=False)
        assert (report.checked, report.skipped) == (2, 1)
        assert fake_probe_cls.instances[0].select_calls == ["节点二", "节点三"]

        # full=True 全量重测，包括已有结果的节点
        report_full = purity_mod.scan(nodes, config=config, store=store, provider=FakeProvider(), full=True)
        assert (report_full.checked, report_full.skipped) == (3, 0)

    def test_incremental_rechecks_result_without_valid_data(self, config, store, fake_probe_cls):
        """上次检测无有效数据（无出口 IP 且未分类）=「已失效」，增量时重测。"""
        nodes = [_node("节点一"), _node("节点二")]
        store.save_purity_result(_purity_result(nodes[0], ip_type=None, rank=None, exit_ip=None))
        store.save_purity_result(_purity_result(nodes[1], ip_type="datacenter", rank=2))
        report = purity_mod.scan(nodes, config=config, store=store, provider=FakeProvider(), full=False)
        assert (report.checked, report.skipped) == (1, 1)
        assert fake_probe_cls.instances[0].select_calls == ["节点一"]

    def test_no_targets_skips_probe_entirely(self, config, store, fake_probe_cls):
        nodes = [_node("节点一"), _node("节点二")]
        for node in nodes:
            store.save_purity_result(_purity_result(node, ip_type="residential", rank=3))
        report = purity_mod.scan(nodes, config=config, store=store, provider=FakeProvider(), full=False)
        assert (report.checked, report.skipped, report.unavailable) == (0, 0, False)
        assert fake_probe_cls.instances == []              # 未实例化探测实例

    def test_empty_nodes_noop(self, config, store, fake_probe_cls):
        report = purity_mod.scan([], config=config, store=store, provider=FakeProvider(), full=True)
        assert (report.checked, report.skipped, report.unavailable) == (0, 0, False)
        assert fake_probe_cls.instances == []

    def test_unavailable_when_probe_start_fails(self, config, store, fake_probe_cls,
                                                monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setattr(purity_mod, "ProbeInstance", FailingProbe)
        provider = FakeProvider()
        nodes = [_node("节点一"), _node("节点二"), _node("节点三")]
        report = purity_mod.scan(nodes, config=config, store=store, provider=provider, full=True)
        assert report.unavailable is True
        assert (report.checked, report.skipped) == (0, 3)
        assert report.results == []
        assert provider.calls == []                    # 实例不可用时不发起任何查询
        assert store.latest_purity_results() == []

    def test_unavailable_when_probe_start_raises(self, config, store, fake_probe_cls,
                                                 monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setattr(purity_mod, "ProbeInstance", RaisingProbe)
        report = purity_mod.scan([_node("节点一")], config=config, store=store,
                                 provider=FakeProvider(), full=True)
        assert report.unavailable is True
        assert (report.checked, report.skipped) == (0, 1)

    def test_mid_scan_crash_marks_unavailable(self, config, store, fake_probe_cls,
                                              monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setattr(purity_mod, "ProbeInstance", CrashingProbe)
        nodes = [_node("节点一"), _node("节点二"), _node("节点三")]
        report = purity_mod.scan(nodes, config=config, store=store,
                                 provider=FakeProvider(), full=True)
        assert report.unavailable is True
        assert report.checked == 2                     # 崩溃前完成的节点保留
        assert report.skipped == 1                     # 崩溃后剩余跳过
        assert len(store.latest_purity_results()) == 2

    def test_unreachable_node_skipped(self, config, store, fake_probe_cls,
                                      monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setattr(purity_mod, "ProbeInstance", UnreachableNodeProbe)
        provider = FakeProvider()
        report = purity_mod.scan([_node("节点一"), _node("节点二"), _node("节点三")],
                                 config=config, store=store, provider=provider, full=True)
        assert (report.checked, report.skipped, report.unavailable) == (2, 1, False)
        assert provider.calls == ["http://127.0.0.1:9096"] * 2

    def test_select_failure_skipped(self, config, store, fake_probe_cls,
                                    monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setattr(purity_mod, "ProbeInstance", UnselectableNodeProbe)
        report = purity_mod.scan([_node("节点一"), _node("节点二"), _node("节点三")],
                                 config=config, store=store,
                                 provider=FakeProvider(), full=True)
        assert (report.checked, report.skipped, report.unavailable) == (2, 1, False)

    def test_provider_error_skips_single_node(self, config, store, fake_probe_cls):
        provider = FakeProvider(responses=[_raw(), RuntimeError("网络异常"), _raw(query="203.0.113.2")])
        report = purity_mod.scan([_node("节点一"), _node("节点二"), _node("节点三")],
                                 config=config, store=store, provider=provider, full=True)
        assert (report.checked, report.skipped, report.unavailable) == (2, 1, False)
        names = {r.node_name for r in store.latest_purity_results()}
        assert names == {"节点一", "节点三"}

    def test_attribute_change_residential_lost_warns(self, config, store, fake_probe_cls, caplog):
        nodes = [_node("节点一"), _node("节点二")]
        # 种子固定在过去时刻：Windows 时钟粒度粗，now_iso() 可能与扫描首条同刻度，
        # 同 (节点, checked_at) 主键会被 INSERT OR REPLACE 覆盖，测不出「变化」
        prev = replace(_purity_result(nodes[0], ip_type="residential", rank=3,
                                      exit_ip="198.51.100.1"),
                       checked_at="2026-01-01T00:00:00.000000")
        store.save_purity_result(prev)
        provider = FakeProvider(default=_raw(hosting=True,
                                             **{"as": "AS906 DMIT Cloud Services"},
                                             asname="DMIT Cloud Services", org="DMIT",
                                             isp="DMIT Cloud Services"))
        with caplog.at_level(logging.WARNING, logger="subhub.purity"):
            report = purity_mod.scan(nodes, config=config, store=store,
                                     provider=provider, full=True)
        assert report.checked == 2
        assert any(
            "属性变化告警" in record.message and "住宅" in record.message
            and "机房" in record.message and "节点一" in record.message
            for record in caplog.records
        )
        changes = store.attribute_changes()
        assert len(changes) == 1
        assert changes[0].node_name == "节点一"
        assert changes[0].is_residential_lost is True     # 住宅→机房：UI 标红场景

    def test_default_provider_created_when_none_given(self, config, store, fake_probe_cls,
                                                      monkeypatch):
        """provider 缺省时构造 IpApiProvider（打桩确认，不发真实网络请求）。"""
        created = []

        class SpyProvider(IpApiProvider):
            def __init__(self, **kwargs):
                super().__init__(**kwargs)
                created.append(self)

            def lookup(self, *, proxy_url=None):
                return _raw()

        monkeypatch.setattr(purity_mod, "IpApiProvider", SpyProvider)
        report = purity_mod.scan([_node("节点一")], config=config, store=store, full=True)
        assert report.checked == 1
        assert len(created) == 1

    def test_enhancer_merges_ipinfo_and_counts(self, config, store, fake_probe_cls):
        """增强成功：widget 数据合入 raw 落库，report.enhanced 计数，查询经混合端口发往出口 IP。"""
        enhancer = RecordingEnhancer(result={"asn": {"type": "isp"}, "privacy": {"vpn": True}})
        report = purity_mod.scan([_node("节点一")], config=config, store=store,
                                 provider=FakeProvider(), enhancer=enhancer, full=True)
        assert (report.checked, report.enhanced) == (1, 1)
        result = store.latest_purity_results()[0]
        assert result.raw["ipinfo"]["asn"]["type"] == "isp"
        assert result.claude_rank == 2                   # isp 权威住宅被 privacy.vpn 封顶
        assert enhancer.calls[0]["ip"] == "203.0.113.1"
        assert enhancer.calls[0]["proxy_url"] == "http://127.0.0.1:9096"

    def test_enhancer_failure_keeps_base_data(self, config, store, fake_probe_cls):
        """增强失败：只忽略该次增强，不影响该节点主数据落库。"""
        enhancer = RecordingEnhancer(error=IpInfoWidgetError("HTTP 503"))
        report = purity_mod.scan([_node("节点一")], config=config, store=store,
                                 provider=FakeProvider(), enhancer=enhancer, full=True)
        assert (report.checked, report.enhanced) == (1, 0)
        result = store.latest_purity_results()[0]
        assert result.ip_type == "residential"
        assert result.claude_rank == 3
        assert "ipinfo" not in result.raw

    def test_default_enhancer_created_when_none_given(self, config, store, fake_probe_cls,
                                                      monkeypatch):
        """enhancer 缺省时构造 IpInfoWidgetClient（打桩确认，不发真实网络请求）。"""
        created: list[int] = []

        def spy_enhancer():
            created.append(1)
            return NoopEnhancer()

        monkeypatch.setattr(purity_mod, "IpInfoWidgetClient", spy_enhancer)
        report = purity_mod.scan([_node("节点一")], config=config, store=store,
                                 provider=FakeProvider(), full=True)
        assert report.checked == 1
        assert created == [1]


# ---------------------------------------------------------------------- Claude 推荐

class TestClaudeRecommendations:
    def _latest(self, node: Node, rank: int) -> PurityResult:
        return _purity_result(node, ip_type="residential" if rank >= 3 else "datacenter", rank=rank)

    def test_top3_order_rank_desc_then_static_tier(self):
        us_res = _node("美国家宽", region="US", residential=True)
        hk_res = _node("香港家宽", region="HK", residential=True)
        dmit = _node("美国 DMIT", region="US")
        aws = _node("美国 AWS", region="US")
        marked = _node("被标代理", region="US")
        nodes = [us_res, hk_res, dmit, aws, marked]
        latest = [
            self._latest(dmit, 2),
            self._latest(us_res, 3),
            self._latest(aws, 1),
            self._latest(hk_res, 3),
            self._latest(marked, 0),
            self._latest(_node("已下线节点"), 3),    # 节点已不存在 → 剔除
        ]
        top = purity_mod.claude_recommendations(latest, nodes)
        # rank 降序；同为 3 分时静态排序美国家宽 → 港/新家宽；已下线节点不进推荐
        assert [r.node_name for r in top] == ["美国家宽", "香港家宽", "美国 DMIT"]

    def test_filtered_nodes_not_recommended(self):
        live = _node("正常节点")
        filtered = _node("剩余流量", filtered=True)
        latest = [self._latest(filtered, 3), self._latest(live, 2)]
        top = purity_mod.claude_recommendations(latest, [live, filtered])
        assert [r.node_name for r in top] == ["正常节点"]

    def test_empty_returns_empty(self):
        assert purity_mod.claude_recommendations([], [_node("节点一")]) == []
        assert purity_mod.claude_recommendations([self._latest(_node("幽灵"), 3)], []) == []
