# -*- coding: utf-8 -*-
r"""GitHub 流量吞吐量优选（客户端应急工具）：逐节点实测 GitHub release 下载速度，把 🐱 GitHub 组钉到最快节点。

**主机制已上移 NAS（2026-10-07）**：sub-hub 的 ghspeed 定时任务在 NAS 侧测速落库，
渲染时自动生成 🏆 GitHub 优选组并重排 🐱 GitHub 成员，随订阅下发——客户机导入
即得优选，本脚本不再是必跑项。仅当「NAS 刚测完但客户端还没重新导入订阅、又想
立刻生效」时用本脚本对运行中的 Clash 直接钉组；钉选会被 Verge 的
store-selected 缓存记住，下次导入前都有效。

背景（2026-10-07 实测）：Fastly CDN（objects / pkg-containers.githubusercontent.com）
会对机场出口 IP 做吞吐量限速，且按出口区分；mihomo 的
url-test 组只测延迟（HTTP RTT），对这种限速完全失明——延迟 45ms 的节点照常只有
0.1 MB/s。本脚本用真实下载测速补上这个盲区：限速放开时自动抓住快节点，全灭时
至少挑出最不烂的。测速路径与 ghcr 镜像层/release 资产的实际下载路径一致（同走
Clash 混合端口、同落 Fastly），测谁选谁，不猜代理。

用法（Windows 本机，Clash Verge 需在跑且 external-controller 开启）：
    python scripts/gh_probe.py                    # 全节点测速并钉选最快
    python scripts/gh_probe.py --seconds 10       # 每节点测 10 秒（默认 6）
    python scripts/gh_probe.py --restore          # 恢复 🐱 GitHub 组到测速前的选中值
    python scripts/gh_probe.py --group "🚀 节点选择"   # 钉其他含裸节点的组

定时示例（每天 9 点自动优选一次，按需调整频率；测速期间 GitHub 流量会逐节点切换，
约 32 节点 × 6s ≈ 3 分钟，建议避开正在 docker pull / 大下载时运行）：
    schtasks /Create /TN "subhub-gh-probe" /SC DAILY /ST 09:00 ^
        /TR "python G:\Github\sub-hub\scripts\gh_probe.py"

API secret 自动从 Clash Verge 配置读取（%APPDATA%/io.github.clash-verge-rev.clash-verge-rev/
config.yaml 的 secret 字段），也可用 --secret 或环境变量 CLASH_API_SECRET 覆盖。
仅依赖标准库；每个节点的切换都会回读组选中值验证生效，切换失败的节点跳过并标注。
"""

from __future__ import annotations

import argparse
import json
import os
import pathlib
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

sys.stdout.reconfigure(encoding="utf-8")

DEFAULT_API = "http://127.0.0.1:9097"
DEFAULT_PROXY = "http://127.0.0.1:7897"
DEFAULT_GROUP = "🐱 GitHub"
DEFAULT_SECONDS = 6.0
# 与 app/templater.py 的 GitHub 组设计呼应：组内必须含裸节点，否则无从钉选
PROBE_URL = ("https://github.com/prometheus/prometheus/releases/download/"
             "v2.53.0/prometheus-2.53.0.windows-amd64.zip")
REAL_NODE_TYPES = {"Shadowsocks", "ShadowsocksR", "Vmess", "Vless", "Trojan",
                   "Hysteria", "Hysteria2", "Tuic", "Socks5", "Http", "Snell",
                   "WireGuard", "AnyTLS"}
VERGE_CONFIGS = [
    pathlib.Path(os.environ.get("APPDATA", "")) /
    "io.github.clash-verge-rev.clash-verge-rev" / "config.yaml",
]


def detect_secret() -> str:
    env = os.environ.get("CLASH_API_SECRET", "").strip()
    if env:
        return env
    for path in VERGE_CONFIGS:
        try:
            m = re.search(r"^secret:\s*(\S+)", path.read_text(encoding="utf-8"), re.M)
            if m and m.group(1) not in ("\"", "'"):
                return m.group(1).strip("\"'")
        except OSError:
            continue
    return ""


def api_get(api: str, secret: str, path: str) -> dict:
    headers = {"Authorization": f"Bearer {secret}"} if secret else {}
    with urllib.request.urlopen(urllib.request.Request(api + path, headers=headers),
                                timeout=8) as resp:
        return json.load(resp)


def api_put_group(api: str, secret: str, group: str, name: str) -> bool:
    headers = {"Content-Type": "application/json"}
    if secret:
        headers["Authorization"] = f"Bearer {secret}"
    body = json.dumps({"name": name}).encode()
    req = urllib.request.Request(
        f"{api}/proxies/{urllib.parse.quote(group)}",
        data=body, headers=headers, method="PUT")
    try:
        with urllib.request.urlopen(req, timeout=8) as resp:
            return resp.status in (200, 204)
    except urllib.error.HTTPError:
        return False


def group_now(api: str, secret: str, group: str) -> str | None:
    try:
        return api_get(api, secret, f"/proxies/{urllib.parse.quote(group)}").get("now")
    except urllib.error.HTTPError:
        return None


def probe_speed(proxy: str, seconds: float, url: str) -> float:
    """经混合端口实拍下载速度（MB/s）；读满 seconds 秒或流结束为止。"""
    opener = urllib.request.build_opener(urllib.request.ProxyHandler(
        {"http": proxy, "https": proxy}))
    start = time.monotonic()
    total = 0
    try:
        with opener.open(url, timeout=seconds + 3) as resp:
            while time.monotonic() - start < seconds:
                chunk = resp.read(65536)
                if not chunk:
                    break
                total += len(chunk)
    except (urllib.error.URLError, OSError, TimeoutError):
        pass
    elapsed = max(time.monotonic() - start, 0.001)
    return total / elapsed / 1048576.0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--api", default=DEFAULT_API)
    parser.add_argument("--secret", default="")
    parser.add_argument("--proxy", default=DEFAULT_PROXY,
                        help="Clash 混合端口（测速走它）")
    parser.add_argument("--group", default=DEFAULT_GROUP,
                        help=f"被钉选的组（默认 {DEFAULT_GROUP}，需含裸节点成员）")
    parser.add_argument("--seconds", type=float, default=DEFAULT_SECONDS,
                        help="每节点测速时长（秒）")
    parser.add_argument("--url", default=None, help="自定义测速 URL（默认 GitHub release 资产）")
    parser.add_argument("--restore", action="store_true",
                        help="只测速不钉选，结束后恢复原选中值")
    args = parser.parse_args()
    probe_url = args.url or PROBE_URL

    secret = args.secret or detect_secret()
    try:
        proxies = api_get(args.api, secret, "/proxies")["proxies"]
    except (urllib.error.URLError, OSError) as exc:
        print(f"[错误] 连不上 Clash API（{args.api}）：{exc}")
        print("确认 Clash Verge 在跑、external-controller 已开启、secret 正确。")
        return 2

    if args.group not in proxies:
        print(f"[错误] 组不存在：{args.group}。当前配置里的组：")
        for name, info in proxies.items():
            if info.get("type") in ("Selector", "URLTest", "Fallback", "LoadBalance"):
                print(f"  {name}  (now={info.get('now')})")
        return 2

    group = proxies[args.group]
    members = group.get("all", [])
    raw_nodes = [m for m in members
                 if proxies.get(m, {}).get("type") in REAL_NODE_TYPES]
    if not raw_nodes:
        print(f"[错误] {args.group} 的成员里没有裸节点，无从钉选。")
        print("sub-hub 的 🐱 GitHub 组自带全量裸节点——请先用新版 sub-hub 渲染并导入配置。")
        return 2

    original = group.get("now")
    print(f"组: {args.group} | 原选中: {original} | 候选 {len(raw_nodes)} 节点 × {args.seconds}s\n")

    results: list[tuple[str, float]] = []
    for i, node in enumerate(raw_nodes, 1):
        if not api_put_group(args.api, secret, args.group, node):
            print(f"[{i:>2}/{len(raw_nodes)}] {node}  切换被拒（非组成员？）—— 跳过")
            continue
        now = group_now(args.api, secret, args.group)
        if now != node:
            print(f"[{i:>2}/{len(raw_nodes)}] {node}  回读不符（now={now}）—— 跳过")
            continue
        mb = probe_speed(args.proxy, args.seconds, probe_url)
        results.append((node, mb))
        print(f"[{i:>2}/{len(raw_nodes)}] {node}  {mb:6.2f} MB/s")

    results.sort(key=lambda x: -x[1])
    print("\n=== 按速度排序 ===")
    for node, mb in results:
        print(f"{mb:6.2f} MB/s  {'#' * int(min(mb, 30) * 2):<30} {node}")

    if args.restore or not results:
        api_put_group(args.api, secret, args.group, original or "")
        print(f"\n（已恢复 {args.group} → {original}）")
        return 0 if results else 1

    best_node, best_mb = results[0]
    api_put_group(args.api, secret, args.group, best_node)
    pinned = group_now(args.api, secret, args.group)
    if pinned != best_node:
        print(f"\n[错误] 钉选 {best_node} 失败（now={pinned}）")
        return 1
    print(f"\n✅ 已把 {args.group} 钉到最快节点：{best_node}（{best_mb:.2f} MB/s）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
