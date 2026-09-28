"""parser 测试：Clash YAML 主路径字段正确、base64 兜底解析正确、坏节点跳过且有记录。

纪律：数据目录一律用 conftest fixture（tmp_path 隔离）；日志断言不打印凭据。
"""
from __future__ import annotations

import base64
import json
import logging

import pytest
import yaml

from app.parser import (
    parse_base64_subscription,
    parse_clash_yaml,
    parse_payload,
    parse_payload_detailed,
    parse_proxy_uri,
)

SOURCE = "kuai"


# ---------------------------------------------------------------- 主路径：Clash YAML

def test_parse_clash_yaml_fixture_sub_a(sub_a_path):
    """fixture YAML 解析节点数与字段正确（14 条：vless/ss/anytls + 假节点 2）。"""
    nodes = parse_clash_yaml(sub_a_path.read_text(encoding="utf-8"), SOURCE)
    assert len(nodes) == 14
    assert all(n.source_sub == SOURCE for n in nodes)
    by_name = {n.name: n for n in nodes}

    # vless 美国家宽：凭据字段名与 Clash YAML 一致
    us = by_name["🇺🇸 美国 洛杉矶 家庭宽带 01"]
    assert us.type == "vless"
    assert us.server == "us-lax-01.example-airport-a.com"
    assert us.port == 443
    assert us.credentials["uuid"] == "11111111-1111-4111-8111-111111111101"
    assert us.credentials["flow"] == "xtls-rprx-vision"
    assert us.credentials["tls"] is True
    assert us.credentials["network"] == "tcp"
    assert us.credentials["servername"] == "us-lax-01.example-airport-a.com"
    # to_clash_proxy 还原后凭据齐全（templater 序列化依赖）
    proxy = us.to_clash_proxy()
    assert proxy["name"] == us.name and proxy["uuid"] == us.credentials["uuid"]

    # ss 韩国：cipher/password 进 credentials
    ss = by_name["🇰🇷 韩国 首尔 01"]
    assert ss.type == "ss" and ss.port == 8388
    assert ss.credentials["cipher"] == "aes-256-gcm"
    assert ss.credentials["password"] == "fake-ss-password-kr-01"

    # anytls 新加坡：alpn 列表保留
    sg = by_name["🇸🇬 新加坡 01"]
    assert sg.type == "anytls"
    assert sg.credentials["password"] == "fake-anytls-password-sg-01"
    assert sg.credentials["alpn"] == ["h2", "http/1.1"]

    # ws 传输的 vless：ws-opts 原样保留
    ws = by_name["🇺🇸 美国 圣何塞 专线 x2"]
    assert ws.credentials["network"] == "ws"
    assert ws.credentials["ws-opts"] == {
        "path": "/ws", "headers": {"Host": "us-sjc-02.example-airport-a.com"},
    }


def test_parse_clash_yaml_sub_b(sub_b_path):
    nodes = parse_clash_yaml(sub_b_path.read_text(encoding="utf-8"), "miffy")
    assert len(nodes) == 4
    assert {n.type for n in nodes} == {"vless", "anytls", "ss"}


def test_parse_clash_yaml_skips_bad_items_and_logs(caplog):
    """单条 proxies 项缺字段/类型异常 → 跳过该条并记录，不阻塞整批。"""
    text = yaml.safe_dump({"proxies": [
        {"name": "good-node", "type": "ss", "server": "a.example.com",
         "port": 8388, "cipher": "aes-256-gcm", "password": "p"},
        {"name": "no-port", "type": "ss", "server": "b.example.com",
         "cipher": "aes-256-gcm", "password": "p"},   # 缺 port
        "not-a-dict",                                  # 类型异常
    ]}, allow_unicode=True)
    with caplog.at_level(logging.WARNING, logger="subhub.parser"):
        nodes = parse_clash_yaml(text, SOURCE)
    assert [n.name for n in nodes] == ["good-node"]
    assert "no-port" in caplog.text
    assert any(r.levelno == logging.WARNING for r in caplog.records)


def test_parse_clash_yaml_rejects_non_clash():
    """没有 proxies 列表 → ValueError（由 parse_payload 转 base64 兜底）。"""
    with pytest.raises(ValueError):
        parse_clash_yaml("port: 9090\nmode: rule\n", SOURCE)
    with pytest.raises(ValueError):
        parse_clash_yaml("- just\n- a\n- list\n", SOURCE)


# ---------------------------------------------------------------- 兜底：base64 订阅

def test_parse_base64_subscription_fixture(sub_uri_path):
    """base64 兜底解析正确：ss/vmess/vless 各 1，凭据字段与 Clash YAML 同名。"""
    nodes = parse_base64_subscription(sub_uri_path.read_bytes(), SOURCE)
    assert len(nodes) == 3
    by_type = {n.type: n for n in nodes}

    ss = by_type["ss"]
    assert ss.name == "🇺🇸 美国 SS URI 01"
    assert ss.server == "us-ss-01.example-airport-b.com"
    assert ss.port == 8388
    assert ss.credentials["cipher"] == "aes-256-gcm"
    assert ss.credentials["password"] == "ss-password-uri-01"

    vm = by_type["vmess"]
    assert vm.name == "🇯🇵 日本 VM URI 01"
    assert vm.server == "jp-vm-01.example-airport-b.com"
    assert vm.port == 443
    assert vm.credentials["uuid"] == "dddddddd-dddd-4ddd-8ddd-dddddddddddd4"
    assert vm.credentials["alterId"] == 0
    assert vm.credentials["cipher"] == "auto"
    assert vm.credentials["tls"] is True
    assert vm.credentials["network"] == "ws"
    assert vm.credentials["ws-opts"]["path"] == "/ws"
    assert vm.credentials["ws-opts"]["headers"]["Host"] == "jp-vm-01.example-airport-b.com"

    vl = by_type["vless"]
    assert vl.name == "🇹🇼 台湾 VLESS URI 01"
    assert vl.server == "tw-vless-01.example-airport-b.com"
    assert vl.port == 443
    assert vl.credentials["uuid"] == "eeeeeeee-eeee-4eee-8eee-eeeeeeeeeeee5"
    assert vl.credentials["flow"] == "xtls-rprx-vision"
    assert vl.credentials["tls"] is True
    assert vl.credentials["servername"] == "tw-vless-01.example-airport-b.com"
    assert vl.credentials.get("network", "tcp") == "tcp"


def test_parse_base64_subscription_bad_b64_returns_empty():
    assert parse_base64_subscription(b"!!!not base64!!!", SOURCE) == []
    assert parse_base64_subscription(b"", SOURCE) == []
    # 非 ASCII（二进制）内容不可能来自 base64 订阅
    assert parse_base64_subscription(b"\x00\x01\x02", SOURCE) == []


# ---------------------------------------------------------------- 单条 URI 解析

def test_parse_proxy_uri_ss_variants():
    # SIP002 标准 b64 userinfo + 百分号编码节点名
    node = parse_proxy_uri(
        "ss://YWVzLTI1Ni1nY206cHdk@1.2.3.4:8388#%E6%B5%8B%E8%AF%95", SOURCE
    )
    assert node.type == "ss"
    assert node.name == "测试"
    assert node.server == "1.2.3.4" and node.port == 8388
    assert node.credentials["cipher"] == "aes-256-gcm"
    assert node.credentials["password"] == "pwd"

    # URL-safe b64 且缺省 padding
    raw = base64.urlsafe_b64encode(b"aes-256-gcm:pwd2").decode("ascii").rstrip("=")
    node2 = parse_proxy_uri(f"ss://{raw}@1.2.3.4:8388#n2", SOURCE)
    assert node2.credentials["password"] == "pwd2"

    # 明文 userinfo（含「:」）
    node3 = parse_proxy_uri("ss://aes-128-gcm:plainpwd@5.6.7.8:443#n3", SOURCE)
    assert node3.credentials["cipher"] == "aes-128-gcm"
    assert node3.credentials["password"] == "plainpwd"

    # 旧格式：整段 base64("cipher:password@host:port")
    whole = base64.b64encode(b"aes-256-gcm:oldpwd@9.9.9.9:8388").decode("ascii")
    node4 = parse_proxy_uri(f"ss://{whole}#old", SOURCE)
    assert node4.server == "9.9.9.9" and node4.port == 8388
    assert node4.credentials["password"] == "oldpwd"

    # 缺 fragment → 回退 server:port 命名，不丢节点
    node5 = parse_proxy_uri("ss://YWVzLTI1Ni1nY206cHdk@1.2.3.4:8388", SOURCE)
    assert node5.name == "1.2.3.4:8388"


def test_parse_proxy_uri_vmess():
    payload = {
        "v": "2", "ps": "vm-node", "add": "vm.example.com", "port": "443",
        "id": "uuid-1", "aid": "0", "net": "ws", "type": "none",
        "host": "vm.example.com", "path": "/wspath", "tls": "tls", "scy": "auto",
    }
    uri = "vmess://" + base64.b64encode(
        json.dumps(payload).encode("utf-8")
    ).decode("ascii")
    node = parse_proxy_uri(uri, SOURCE)
    assert node.type == "vmess"
    assert node.name == "vm-node"
    assert node.server == "vm.example.com" and node.port == 443
    assert node.credentials["uuid"] == "uuid-1"
    assert node.credentials["alterId"] == 0
    assert node.credentials["cipher"] == "auto"
    assert node.credentials["tls"] is True
    assert node.credentials["ws-opts"]["path"] == "/wspath"


def test_parse_proxy_uri_vmess_bad_payload():
    with pytest.raises(ValueError):
        parse_proxy_uri("vmess://!!!invalid-b64!!!", SOURCE)
    bad_json = base64.b64encode(b"not json at all").decode("ascii")
    with pytest.raises(ValueError):
        parse_proxy_uri(f"vmess://{bad_json}", SOURCE)


def test_parse_proxy_uri_trojan():
    uri = ("trojan://pass%40word@tj.example.com:443?sni=sni.example.com"
           "&allowInsecure=1&type=ws&host=h.example.com&path=%2Fws#trojan%E8%8A%82%E7%82%B9")
    node = parse_proxy_uri(uri, SOURCE)
    assert node.type == "trojan"
    assert node.name == "trojan节点"
    assert node.server == "tj.example.com" and node.port == 443
    assert node.credentials["password"] == "pass@word"
    assert node.credentials["servername"] == "sni.example.com"
    assert node.credentials["skip-cert-verify"] is True
    assert node.credentials["network"] == "ws"
    assert node.credentials["ws-opts"]["path"] == "/ws"
    assert node.credentials["ws-opts"]["headers"]["Host"] == "h.example.com"


def test_parse_proxy_uri_vless_reality():
    uri = ("vless://uuid-r@vl.example.com:443?encryption=none&security=reality"
           "&sni=www.example.com&fp=chrome&pbk=pubkey123&sid=abcd"
           "&type=tcp&flow=xtls-rprx-vision#reality")
    node = parse_proxy_uri(uri, SOURCE)
    assert node.type == "vless"
    assert node.credentials["uuid"] == "uuid-r"
    assert node.credentials["tls"] is True
    assert node.credentials["client-fingerprint"] == "chrome"
    assert node.credentials["reality-opts"] == {"public-key": "pubkey123", "short-id": "abcd"}
    assert node.credentials["flow"] == "xtls-rprx-vision"


def test_parse_proxy_uri_hysteria2_and_hy2_alias():
    uri = ("hysteria2://pw123@hy.example.com:8443?sni=sni.example.com&insecure=1"
           "&obfs=salamander&obfs-password=obfspw#hy2")
    node = parse_proxy_uri(uri, SOURCE)
    assert node.type == "hysteria2"
    assert node.port == 8443
    assert node.credentials["password"] == "pw123"
    assert node.credentials["sni"] == "sni.example.com"
    assert node.credentials["skip-cert-verify"] is True
    assert node.credentials["obfs"] == "salamander"
    assert node.credentials["obfs-password"] == "obfspw"
    # hy2:// 是同一协议别名
    alias = parse_proxy_uri("hy2://pw123@hy.example.com:8443#alias", SOURCE)
    assert alias.type == "hysteria2"
    assert alias.credentials["password"] == "pw123"


def test_parse_proxy_uri_tuic():
    uri = ("tuic://uuid-9:tupass@tu.example.com:443?sni=sni.example.com"
           "&congestion_control=bbr&alpn=h3,h1&udp=1&allow_insecure=1#tuic")
    node = parse_proxy_uri(uri, SOURCE)
    assert node.type == "tuic"
    assert node.credentials["uuid"] == "uuid-9"
    assert node.credentials["password"] == "tupass"
    assert node.credentials["sni"] == "sni.example.com"
    assert node.credentials["congestion-controller"] == "bbr"
    assert node.credentials["alpn"] == ["h3", "h1"]
    assert node.credentials["skip-cert-verify"] is True


def test_parse_proxy_uri_unknown_scheme_returns_none(caplog):
    """不认识的 scheme → None + warning（不抛异常）。"""
    with caplog.at_level(logging.WARNING, logger="subhub.parser"):
        assert parse_proxy_uri("foo://bar@example.com:1#x", SOURCE) is None
    assert "foo" in caplog.text


def test_parse_proxy_uri_bad_port_raises():
    with pytest.raises(ValueError):
        parse_proxy_uri("ss://YWVzLTI1Ni1nY206cHdk@1.2.3.4:notaport#x", SOURCE)
    with pytest.raises(ValueError):
        parse_proxy_uri("ss://YWVzLTI1Ni1nY206cHdk@1.2.3.4:70000#x", SOURCE)


def test_parse_proxy_uri_empty_returns_none():
    assert parse_proxy_uri("", SOURCE) is None
    assert parse_proxy_uri("   ", SOURCE) is None
    assert parse_proxy_uri("no-scheme-line", SOURCE) is None


# ---------------------------------------------------------------- parse_payload 编排

def test_parse_payload_clash_yaml_path(sub_a_path):
    nodes = parse_payload(sub_a_path.read_bytes(), SOURCE)
    assert len(nodes) == 14


def test_parse_payload_base64_fallback(sub_uri_path):
    """非 YAML 内容自动落 base64 兜底路径。"""
    nodes = parse_payload(sub_uri_path.read_bytes(), SOURCE)
    assert len(nodes) == 3


def test_parse_payload_garbage_returns_empty():
    """两条路径都失败 → []（0 节点判定交给上层）。"""
    assert parse_payload(b"", SOURCE) == []
    assert parse_payload(b"\x00\x01\x02binary-noise", SOURCE) == []
    assert parse_payload("port: 9090\nmode: rule\n".encode("utf-8"), SOURCE) == []


def test_parse_payload_skips_bad_lines_with_record(caplog):
    """坏 URI 行被跳过且日志有记录；凭据值绝不进日志。"""
    good_ss = "ss://" + base64.b64encode(b"aes-256-gcm:goodpwd@1.1.1.1:8388").decode("ascii") + "#good"
    bad_vmess = "vmess://!!!broken-payload!!!"
    good_vless = "vless://uuid-g@g.example.com:443?security=tls#gv"
    blob = base64.b64encode("\n".join([good_ss, bad_vmess, good_vless]).encode("utf-8")).decode("ascii")
    with caplog.at_level(logging.WARNING, logger="subhub.parser"):
        nodes = parse_payload(blob.encode("ascii"), SOURCE)
    assert [n.name for n in nodes] == ["good", "gv"]
    assert "vmess" in caplog.text           # 记录了失败行（只到 scheme 粒度）
    assert "goodpwd" not in caplog.text     # 凭据不进日志
    assert "!!!broken-payload!!!" not in caplog.text  # 原始 URI 不进日志


# ---------------------------------------------------------------- 解析异常清单（docs/01 安全网）

def test_parse_payload_detailed_reports_clash_anomalies():
    """Clash 路径：坏 proxies 项跳过且逐条进入异常清单；凭据不进记录。"""
    text = yaml.safe_dump({"proxies": [
        {"name": "good-node", "type": "ss", "server": "a.example.com",
         "port": 8388, "cipher": "aes-256-gcm", "password": "p"},
        {"name": "no-port", "type": "ss", "server": "b.example.com",
         "cipher": "aes-256-gcm", "password": "hidden-pw"},   # 缺 port
    ]}, allow_unicode=True)
    nodes, anomalies = parse_payload_detailed(text.encode("utf-8"), SOURCE)
    assert [n.name for n in nodes] == ["good-node"]
    assert len(anomalies) == 1
    a = anomalies[0]
    assert a.source_sub == SOURCE
    assert a.kind == "clash"
    assert a.label == "no-port"
    assert a.reason and a.detected_at
    assert a.to_dict()["label"] == "no-port"
    assert "hidden-pw" not in a.reason          # 凭据不进异常记录


def test_parse_payload_detailed_reports_uri_anomalies():
    """base64 路径：坏行与不支持协议逐条进入异常清单；原始 URI 不进记录。"""
    good_ss = "ss://" + base64.b64encode(b"aes-256-gcm:goodpwd@1.1.1.1:8388").decode("ascii") + "#good"
    bad_vmess = "vmess://!!!broken-payload!!!"
    unknown = "foo://bar@example.com:1#x"
    blob = base64.b64encode(
        "\n".join([good_ss, bad_vmess, unknown]).encode("utf-8")
    ).decode("ascii")
    nodes, anomalies = parse_payload_detailed(blob.encode("ascii"), SOURCE)
    assert [n.name for n in nodes] == ["good"]
    assert {(a.kind, a.label) for a in anomalies} == {("uri", "vmess"), ("uri", "foo")}
    assert all("!!!" not in a.reason for a in anomalies)


def test_parse_payload_detailed_clean_subscription_has_no_anomalies(sub_a_path):
    """正常订阅：节点齐全、异常清单为空。"""
    nodes, anomalies = parse_payload_detailed(sub_a_path.read_bytes(), SOURCE)
    assert len(nodes) == 14
    assert anomalies == []
