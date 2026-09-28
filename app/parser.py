"""订阅解析：Clash YAML 主路径 + base64 订阅（分享 URI）兜底 → 统一 Node 模型。

职责边界（docs/01 模块表、docs/INTERFACES.md §3.2）：
- 主路径：Clash YAML 的 proxies 列表（复用 Node.from_clash_proxy，credentials
  字段名与 Clash YAML 保持一致：uuid/password/cipher/tls/sni/flow/network/ws-opts…）；
- 兜底：base64 解码后按行解析 ss://（SIP002 userinfo-b64 与整段 b64 两种）、
  vmess://（base64 JSON）、vless://、trojan://、hysteria2://（含 hy2://）、tuic://；
- 单节点解析异常：跳过并 log warning（不输出凭据与完整 URI），不阻塞整批；
  同时逐条记入 ParseAnomaly 清单（parse_payload_detailed 返回），由 pipeline
  落盘、UI「解析异常节点」列表展示（docs/01 安全网）；
- 两条路径都失败 → 返回 []（0 节点判定由上层 pipeline 处理，docs/01 安全网）。

安全纪律：日志只记录协议类型/订阅别名/原因，绝不打印 URI 明文（内含 uuid、
password 等节点凭据字段）。
"""
from __future__ import annotations

import base64
import binascii
import json
import logging
import re
from dataclasses import dataclass
from typing import Any
from urllib.parse import parse_qs, unquote

import yaml

from app.models import Node
from app.utils import now_iso

logger = logging.getLogger("subhub.parser")

_B64_CHARSET = re.compile(r"[A-Za-z0-9+/=\-_]+")
_TRUE_VALUES = frozenset({"1", "true", "yes", "on"})


# ---------------------------------------------------------------- 解析异常记录

@dataclass
class ParseAnomaly:
    """单节点解析异常记录（docs/01 安全网：UI「解析异常节点」列表可见）。

    只含排查所需的最小信息，绝不包含 URI 明文与节点凭据字段。
    """

    source_sub: str          # 订阅别名
    kind: str                # "clash"=YAML proxies 项；"uri"=base64 订阅行
    label: str | None        # 节点名（clash 路径）或 scheme 片段（uri 路径）
    reason: str              # 中文失败原因（与告警日志同源，不含凭据）
    detected_at: str         # now_iso()

    def to_dict(self) -> dict[str, Any]:
        return {
            "source_sub": self.source_sub,
            "kind": self.kind,
            "label": self.label,
            "reason": self.reason,
            "detected_at": self.detected_at,
        }


# ---------------------------------------------------------------- 入口

def parse_payload(content: bytes, source_sub: str) -> list[Node]:
    """解析订阅原始响应体（Clash YAML 或 base64）为统一节点列表。

    主路径 Clash YAML（proxies 列表）；YAML 无 proxies 或解析失败 → 兜底
    base64 解码后按行解析 URI；两者皆失败 → 返回 []（0 节点由上层判定）。
    单节点解析异常清单见 parse_payload_detailed。
    """
    nodes, _ = parse_payload_detailed(content, source_sub)
    return nodes


def parse_payload_detailed(content: bytes, source_sub: str) -> tuple[list[Node], list[ParseAnomaly]]:
    """parse_payload 的增强版：额外返回单节点解析异常清单（docs/01 安全网）。

    节点列表语义与 parse_payload 完全一致；异常清单由 pipeline 落盘
    data/parse_anomalies.json，供 UI「解析异常节点」列表展示。
    """
    if not content:
        return [], []
    if isinstance(content, str):  # 容错：上层误传文本
        content = content.encode("utf-8")

    anomalies: list[ParseAnomaly] = []
    text: str | None = None
    try:
        text = content.decode("utf-8-sig")
    except UnicodeDecodeError:
        logger.info("订阅「%s」内容不是 UTF-8 文本，直接尝试 base64 兜底", source_sub)

    if text is not None and text.strip():
        try:
            nodes = parse_clash_yaml(text, source_sub, anomalies=anomalies)
        except (ValueError, yaml.YAMLError) as exc:
            logger.info("订阅「%s」非 Clash YAML（%s），尝试 base64 兜底", source_sub, exc)
        else:
            if nodes:
                return nodes, anomalies
            logger.info("订阅「%s」Clash YAML 无有效 proxies，尝试 base64 兜底", source_sub)

    return parse_base64_subscription(content, source_sub, anomalies=anomalies), anomalies


def parse_clash_yaml(text: str, source_sub: str, *,
                     anomalies: list[ParseAnomaly] | None = None) -> list[Node]:
    """解析 Clash YAML 的 proxies 列表（主路径）。

    顶层不是映射或缺少 proxies 列表 → 抛 ValueError（由 parse_payload 转
    base64 兜底）；列表内单条节点解析失败 → 跳过并 log warning，不阻塞整批；
    传入 anomalies 时逐条记录解析异常（docs/01 安全网）。
    """
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise ValueError(f"YAML 解析失败（{type(exc).__name__}）") from exc
    if not isinstance(data, dict) or not isinstance(data.get("proxies"), list):
        raise ValueError("YAML 顶层缺少 proxies 列表，不是 Clash 订阅格式")

    nodes: list[Node] = []
    for item in data["proxies"]:
        label = item.get("name") if isinstance(item, dict) else None
        try:
            nodes.append(Node.from_clash_proxy(item, source_sub))
        except Exception as exc:  # 单节点异常不阻塞整批（docs/01 安全网）
            logger.warning(
                "跳过解析失败的节点（订阅=%s，name=%r）：%s", source_sub, label, exc
            )
            if anomalies is not None:
                anomalies.append(ParseAnomaly(
                    source_sub=source_sub, kind="clash",
                    label=str(label) if label else None,
                    reason=str(exc) or type(exc).__name__,
                    detected_at=now_iso(),
                ))
    return nodes


def parse_base64_subscription(content: bytes, source_sub: str, *,
                              anomalies: list[ParseAnomaly] | None = None) -> list[Node]:
    """base64 订阅兜底：整段 base64 解码后按行解析分享 URI。

    单行解析失败/协议不支持 → 跳过并记录，不阻塞整批；base64 解码失败 → []。
    传入 anomalies 时逐条记录解析异常（docs/01 安全网）。
    """
    try:
        raw = content.decode("ascii").strip()
    except UnicodeDecodeError:
        logger.info("订阅「%s」内容非 ASCII，无法按 base64 订阅解析", source_sub)
        return []
    if not raw:
        return []
    try:
        text = _b64_to_text(raw)
    except ValueError as exc:
        logger.info("订阅「%s」base64 解码失败，兜底终止：%s", source_sub, exc)
        return []

    nodes: list[Node] = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            node = parse_proxy_uri(line, source_sub)
        except ValueError as exc:
            logger.warning(
                "跳过解析失败的节点（订阅=%s，scheme=%s）：%s",
                source_sub, _scheme_of(line), exc,
            )
            if anomalies is not None:
                anomalies.append(ParseAnomaly(
                    source_sub=source_sub, kind="uri", label=_scheme_of(line),
                    reason=str(exc) or "格式错误", detected_at=now_iso(),
                ))
            continue
        if node is not None:  # None=不支持的协议，parse_proxy_uri 内已告警
            nodes.append(node)
        elif anomalies is not None:
            anomalies.append(ParseAnomaly(
                source_sub=source_sub, kind="uri", label=_scheme_of(line),
                reason="暂不支持的节点协议或缺少 scheme", detected_at=now_iso(),
            ))
    return nodes


def parse_proxy_uri(uri: str, source_sub: str) -> Node | None:
    """解析单条分享 URI 为 Node。

    支持 ss/vmess/vless/trojan/hysteria2(hy2)/tuic；不认识的 scheme 返回
    None 并 log warning；scheme 认识但格式错误抛 ValueError（批量层捕获跳过）。
    """
    uri = (uri or "").strip()
    if not uri:
        return None
    scheme, sep, rest = uri.partition("://")
    if not sep:
        logger.warning("跳过无法识别的节点行（订阅=%s）：缺少 scheme", source_sub)
        return None
    handler = _URI_HANDLERS.get(scheme.lower())
    if handler is None:
        logger.warning("跳过暂不支持的节点协议（订阅=%s，scheme=%s）", source_sub, scheme.lower())
        return None
    return handler(rest, source_sub)


# ---------------------------------------------------------------- base64 工具

def _b64_decode(data: str) -> bytes:
    """宽容的 base64 解码：容忍 URL-safe 字母表、缺省 padding、内嵌换行。"""
    compact = re.sub(r"\s+", "", data)
    if not compact or not _B64_CHARSET.fullmatch(compact):
        raise ValueError("不是有效的 base64 数据")
    normalized = compact.replace("-", "+").replace("_", "/")
    normalized += "=" * (-len(normalized) % 4)
    try:
        return base64.b64decode(normalized, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise ValueError("base64 解码失败") from exc


def _b64_to_text(data: str) -> str:
    """base64 → UTF-8 文本；失败抛 ValueError。"""
    try:
        return _b64_decode(data).decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError("base64 解码结果不是 UTF-8 文本") from exc


def _scheme_of(line: str) -> str:
    """日志用：只取 URI 的 scheme 片段（绝不含凭据）。"""
    scheme, sep, _ = line.partition("://")
    return (scheme[:24] or "<无scheme>") if sep else "<无scheme>"


# ---------------------------------------------------------------- URI 公共片段

def _param(params: dict[str, list[str]], key: str) -> str:
    """取查询参数首个值（顺带试小写形式），无值返回空串。"""
    values = params.get(key) or params.get(key.lower()) or []
    return values[0].strip() if values else ""


def _query_params(body: str) -> tuple[str, dict[str, list[str]]]:
    """把 scheme:// 之后的部分拆成 (hostport 段, 查询参数)。"""
    parts = body.split("?", 1)
    params = parse_qs(parts[1], keep_blank_values=True) if len(parts) > 1 else {}
    return parts[0], params


def _split_hostport(hostport: str) -> tuple[str, int]:
    """拆 host:port（支持 [IPv6]:port）；缺失/非法抛 ValueError。"""
    hostport = hostport.strip().strip("/")
    if not hostport:
        raise ValueError("server/port 缺失")
    if hostport.startswith("["):  # IPv6 字面量
        host, _, rest = hostport[1:].partition("]")
        port_part = rest[1:] if rest.startswith(":") else ""
    else:
        host, sep, port_part = hostport.rpartition(":")
        if not sep:
            raise ValueError("缺少 port")
    if not host:
        raise ValueError("server 缺失")
    if not port_part.isdigit():
        raise ValueError(f"port 无效：{port_part[:16]!r}")
    port = int(port_part)
    if not 0 < port <= 65535:
        raise ValueError(f"port 超出范围：{port}")
    return host, port


def _node_name(fragment: str, server: str, port: int) -> str:
    """节点名 = URL 解码后的 #fragment；缺省回退 server:port（不丢节点）。"""
    name = unquote(fragment or "").strip()
    return name or f"{server}:{port}"


def _apply_transport_params(credentials: dict[str, Any], params: dict[str, list[str]]) -> None:
    """把 vless/trojan 通用的传输层查询参数映射为 Clash 凭据字段（与 YAML 同名）。"""
    network = _param(params, "type").lower() or "tcp"
    if network != "tcp":
        credentials["network"] = network
    security = _param(params, "security").lower()
    if security in {"tls", "reality"}:
        credentials["tls"] = True
    sni = _param(params, "sni") or _param(params, "peer")
    if sni:
        credentials["servername"] = sni
    fingerprint = _param(params, "fp")
    if fingerprint:
        credentials["client-fingerprint"] = fingerprint
    insecure = (_param(params, "allowInsecure") or _param(params, "insecure")).lower()
    if insecure in _TRUE_VALUES:
        credentials["skip-cert-verify"] = True

    host = _param(params, "host")
    path = _param(params, "path")
    if network == "ws" and (path or host):
        ws_opts: dict[str, Any] = {"path": path or "/"}
        if host:
            ws_opts["headers"] = {"Host": host}
        credentials["ws-opts"] = ws_opts
    elif network == "grpc":
        service = _param(params, "serviceName") or path
        if service:
            credentials["grpc-opts"] = {"grpc-service-name": service}

    if security == "reality" and (_param(params, "pbk") or _param(params, "sid")):
        reality: dict[str, str] = {}
        if _param(params, "pbk"):
            reality["public-key"] = _param(params, "pbk")
        if _param(params, "sid"):
            reality["short-id"] = _param(params, "sid")
        credentials["reality-opts"] = reality


# ---------------------------------------------------------------- 各协议解析

def _parse_ss(rest: str, source_sub: str) -> Node:
    """ss:// — SIP002（userinfo 为 base64(cipher:password)）、明文 userinfo、
    整段 base64 旧格式三种写法。"""
    body, _, fragment = rest.partition("#")
    body = body.split("?", 1)[0].rstrip("/")
    if "@" in body:
        userinfo, _, hostport = body.rpartition("@")
    else:  # 旧格式：整段 base64("cipher:password@server:port")
        decoded = _b64_to_text(body)
        userinfo, sep, hostport = decoded.rpartition("@")
        if not sep:
            raise ValueError("ss URI 缺少 @host:port 段")
    cipher, password = _ss_userinfo(userinfo)
    server, port = _split_hostport(hostport)
    return Node(
        name=_node_name(fragment, server, port),
        type="ss",
        server=server,
        port=port,
        source_sub=source_sub,
        credentials={"cipher": cipher, "password": password, "udp": True},
    )


def _ss_userinfo(userinfo: str) -> tuple[str, str]:
    """ss 的 userinfo → (cipher, password)：含「:」视为明文，否则按 base64 试。"""
    raw = userinfo.strip()
    if ":" in raw:
        plain = unquote(raw)
    else:
        try:
            plain = _b64_to_text(raw)
        except ValueError:
            plain = unquote(raw)
    cipher, sep, password = plain.partition(":")
    if not sep or not cipher or not password:
        raise ValueError("ss userinfo 无法解析出 cipher:password")
    return cipher, password


def _parse_vmess(rest: str, source_sub: str) -> Node:
    """vmess:// — base64(JSON) 载荷（v2 格式：ps/add/port/id/aid/net/tls/host/path/scy）。"""
    body, _, fragment = rest.partition("#")
    try:
        data = json.loads(_b64_to_text(body.strip()))
    except ValueError as exc:
        raise ValueError(f"vmess 载荷 base64/JSON 无效（{exc}）") from exc
    if not isinstance(data, dict):
        raise ValueError("vmess 载荷不是 JSON 对象")

    server = str(data.get("add") or "").strip()
    uuid = str(data.get("id") or "").strip()
    if not server:
        raise ValueError("vmess 缺少 add（server）")
    if not uuid:
        raise ValueError("vmess 缺少 id（uuid）")
    try:
        port = int(str(data.get("port")).strip())
    except (TypeError, ValueError):
        raise ValueError(f"vmess port 无效：{str(data.get('port'))[:16]!r}") from None
    if not 0 < port <= 65535:
        raise ValueError(f"vmess port 超出范围：{port}")

    credentials: dict[str, Any] = {
        "uuid": uuid,
        "alterId": _to_int(data.get("aid"), 0),
        "cipher": str(data.get("scy") or "auto"),
        "udp": True,
    }
    network = str(data.get("net") or "tcp").strip().lower() or "tcp"
    if network != "tcp":
        credentials["network"] = network
    if str(data.get("tls") or "").strip().lower() == "tls":
        credentials["tls"] = True
    host = str(data.get("host") or "").strip()
    if host:
        credentials["servername"] = host
    path = str(data.get("path") or "").strip()
    if network == "ws" and (path or host):
        ws_opts: dict[str, Any] = {"path": path or "/"}
        if host:
            ws_opts["headers"] = {"Host": host}
        credentials["ws-opts"] = ws_opts
    elif network == "grpc" and path:
        credentials["grpc-opts"] = {"grpc-service-name": path}
    if str(data.get("allowInsecure") or "").strip().lower() in _TRUE_VALUES:
        credentials["skip-cert-verify"] = True

    name = str(data.get("ps") or "").strip() or f"{server}:{port}"
    return Node(
        name=name, type="vmess", server=server, port=port,
        source_sub=source_sub, credentials=credentials,
    )


def _parse_vless(rest: str, source_sub: str) -> Node:
    """vless://uuid@host:port?security=&sni=&type=&flow=&fp=&pbk=&sid=…#name"""
    body, _, fragment = rest.partition("#")
    hostport, params = _query_params(body)
    userinfo, sep, hp = hostport.rpartition("@")
    if not sep:
        raise ValueError("vless URI 缺少 @host:port 段")
    uuid = unquote(userinfo).strip()
    if not uuid:
        raise ValueError("vless 缺少 uuid")
    server, port = _split_hostport(hp)
    credentials: dict[str, Any] = {"uuid": uuid, "udp": True}
    _apply_transport_params(credentials, params)
    flow = _param(params, "flow")
    if flow:
        credentials["flow"] = flow
    return Node(
        name=_node_name(fragment, server, port), type="vless",
        server=server, port=port, source_sub=source_sub, credentials=credentials,
    )


def _parse_trojan(rest: str, source_sub: str) -> Node:
    """trojan://password@host:port?sni=&allowInsecure=&type=…#name"""
    body, _, fragment = rest.partition("#")
    hostport, params = _query_params(body)
    userinfo, sep, hp = hostport.rpartition("@")
    if not sep:
        raise ValueError("trojan URI 缺少 @host:port 段")
    password = unquote(userinfo)
    if not password:
        raise ValueError("trojan 缺少 password")
    server, port = _split_hostport(hp)
    credentials: dict[str, Any] = {"password": password, "udp": True}
    _apply_transport_params(credentials, params)
    return Node(
        name=_node_name(fragment, server, port), type="trojan",
        server=server, port=port, source_sub=source_sub, credentials=credentials,
    )


def _parse_hysteria2(rest: str, source_sub: str) -> Node:
    """hysteria2://（hy2://）password@host:port?sni=&insecure=&obfs=…#name"""
    body, _, fragment = rest.partition("#")
    hostport, params = _query_params(body)
    userinfo, sep, hp = hostport.rpartition("@")
    if not sep:
        raise ValueError("hysteria2 URI 缺少 @host:port 段")
    password = unquote(userinfo)
    if not password:
        raise ValueError("hysteria2 缺少 password")
    server, port = _split_hostport(hp)
    credentials: dict[str, Any] = {"password": password, "udp": True}
    sni = _param(params, "sni") or _param(params, "peer")
    if sni:
        credentials["sni"] = sni
    insecure = (_param(params, "insecure") or _param(params, "allowInsecure")).lower()
    if insecure in _TRUE_VALUES:
        credentials["skip-cert-verify"] = True
    obfs = _param(params, "obfs")
    if obfs:
        credentials["obfs"] = obfs
    obfs_password = _param(params, "obfs-password")
    if obfs_password:
        credentials["obfs-password"] = obfs_password
    return Node(
        name=_node_name(fragment, server, port), type="hysteria2",
        server=server, port=port, source_sub=source_sub, credentials=credentials,
    )


def _parse_tuic(rest: str, source_sub: str) -> Node:
    """tuic://uuid:password@host:port?sni=&congestion_control=&alpn=…#name"""
    body, _, fragment = rest.partition("#")
    hostport, params = _query_params(body)
    userinfo, sep, hp = hostport.rpartition("@")
    if not sep:
        raise ValueError("tuic URI 缺少 @host:port 段")
    uuid, _, password = unquote(userinfo).partition(":")
    uuid, password = uuid.strip(), password.strip()
    if not uuid and not password:
        raise ValueError("tuic 缺少 uuid:password")
    server, port = _split_hostport(hp)
    credentials: dict[str, Any] = {"udp": True}
    if uuid:
        credentials["uuid"] = uuid
    if password:
        credentials["password"] = password
    sni = _param(params, "sni") or _param(params, "peer")
    if sni:
        credentials["sni"] = sni
    congestion = _param(params, "congestion_control") or _param(params, "congestion-controller")
    if congestion:
        credentials["congestion-controller"] = congestion
    alpn = _param(params, "alpn")
    if alpn:
        credentials["alpn"] = [item.strip() for item in alpn.split(",") if item.strip()]
    insecure = (
        _param(params, "allow_insecure") or _param(params, "allowInsecure")
        or _param(params, "insecure")
    ).lower()
    if insecure in _TRUE_VALUES:
        credentials["skip-cert-verify"] = True
    return Node(
        name=_node_name(fragment, server, port), type="tuic",
        server=server, port=port, source_sub=source_sub, credentials=credentials,
    )


def _to_int(value: Any, default: int = 0) -> int:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return default


_URI_HANDLERS = {
    "ss": _parse_ss,
    "vmess": _parse_vmess,
    "vless": _parse_vless,
    "trojan": _parse_trojan,
    "hysteria2": _parse_hysteria2,
    "hy2": _parse_hysteria2,
    "tuic": _parse_tuic,
}
