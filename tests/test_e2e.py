"""端到端冒烟（集成阶段）：sub_a + sub_b fixture 走完整真实链路。

链路：抓取（注入 fixture 内容，其余全真实）→ parser → cleaner（滤假/分类/
跨订阅消歧）→ store 快照 → templater 渲染 4 份产物 → validator 校验 →
publish 发布到临时数据目录（pytest 临时目录，绝不触碰仓库根 data/）。

断言清单（集成任务书）：
- 四份产物生成（clash.yaml / shadowrocket.conf / clash-offline.yaml /
  shadowrocket-offline.conf + meta.json）；
- mihomo 主版本可被 pyyaml 解析；
- 组名齐全（🛑 Claude 专用、🧷 Claude 备援、⛳ 美国优质、♻️ 常规自动、
  🐟 漏网之鱼等全部固定组 + 地区组）；
- 测速参数正确（interval 120 / 备援 90、tolerance 40、max-failed-times 3、
  timeout 3000、探活 http://cp.cloudflare.com/generate_204）；
- rule-providers 全指向 NAS /rules/；
- 离线版无外链（全部 inline、无 NAS 地址与上游仓库地址）；
- SR 主版本与 mihomo 组集合一致；
- 假节点未出现在任何产物；
- 同名消歧后缀（原名 [订阅别名]）生效。

另含 web 服务冒烟：真实 create_app 挂载后，分发端点/规则端点/健康检查/
手动刷新全部可用（fetch 边界沿用本模块注入）。

说明：完整链路含大体积离线内联规则（ChinaMax 11 万行级），单次发布约几十秒，
故模块内共享一次发布结果（一环境多断言），不逐用例重跑。
"""
from __future__ import annotations

import shutil
import threading
from pathlib import Path

import pytest
import yaml

import app.fetcher as fetcher_mod
from app import pipeline, utils
from app.config import AppConfig, load_config
from app.fetcher import FetchResult
from app.models import FetchStatus
from app.rulesync import BASELINE_DIR
from app.store import Store
from app.templater import (
    G_AUTO,
    G_CLAUDE,
    G_CLAUDE_BACKUP,
    G_FINAL,
    G_MAIN,
    G_US,
    PROBE_URL,
    load_rules_manifest,
)

FIXTURES_DIR = Path(__file__).resolve().parent / "fixtures"

# ---------------------------------------------------------------- 期望值（由 fixture 内容推导）

SUB_A = "sub_a"
SUB_B = "sub_b"

# fixture 中跨订阅同名的唯一节点（消歧断言用）
SAME_NAME = "🇺🇸 美国 洛杉矶 家庭宽带 01"
SAME_NAME_A = f"{SAME_NAME} [{SUB_A}]"
SAME_NAME_B = f"{SAME_NAME} [{SUB_B}]"

# 假节点判别词（三份 fixture 假节点名特征，任何产物中都不允许出现）
FAKE_MARKERS = ("剩余流量", "套餐到期", "官网")

# 全部期望组名：19 个固定组 + 9 个地区组（fixture 覆盖 8 个地区 + 1 个无地区特征节点）
EXPECTED_GROUPS = {
    G_MAIN, "♻️ 常规自动", G_US, G_CLAUDE, G_CLAUDE_BACKUP,
    "🎁 OpenAI", "🤖 Gemini", "🐙 Copilot", "🧠 通用 AI", "📲 Telegram",
    "🎬 Netflix", "🏰 Disney+", "📺 YouTube", "🎵 Spotify",
    "📢 谷歌服务", "Ⓜ️ 微软服务", "🍎 苹果服务", "🎮 游戏平台", G_FINAL,
    "🇭🇰 香港", "🇹🇼 台湾", "🇯🇵 日本", "🇸🇬 新加坡", "🇺🇸 美国",
    "🇰🇷 韩国", "🇬🇧 英国", "🇩🇪 德国", "🌍 其他",
}

ARTIFACT_NAMES = (
    "clash.yaml",
    "shadowrocket.conf",
    "clash-offline.yaml",
    "shadowrocket-offline.conf",
)

USERINFO = {"upload": 10, "download": 20, "total": 1000, "expire": 1790000000}


# ---------------------------------------------------------------- 共享环境（模块级，一次发布多断言）

@pytest.fixture(scope="module")
def e2e_config(tmp_path_factory: pytest.TempPathFactory) -> AppConfig:
    """模块级临时数据目录配置（等价 conftest.data_dir 的隔离语义，module 作用域）。"""
    data_dir = tmp_path_factory.mktemp("e2e-data")
    return load_config(env={"SUBHUB_DATA_DIR": str(data_dir)})


@pytest.fixture(scope="module")
def e2e_store(e2e_config: AppConfig):
    store = Store(e2e_config.db_path, e2e_config.secret_key_path)
    yield store
    store.close()


@pytest.fixture(scope="module")
def published(e2e_config: AppConfig, e2e_store: Store) -> pipeline.PipelineResult:
    """真实链路跑一次完整发布：仅注入抓取内容，解析/清洗/渲染/校验/发布全真实。

    规则缓存以「rulesync 已执行（上游失败回落内置基线）」的形态预置到
    data/rules/（同步行为本身由 test_rulesync.py 覆盖，这里不复刻网络）。
    """
    for entry in BASELINE_DIR.iterdir():
        if entry.is_file():
            shutil.copy2(entry, e2e_config.rules_dir / entry.name)

    content_map = {
        SUB_A: (FIXTURES_DIR / "sub_a.yaml").read_bytes(),
        SUB_B: (FIXTURES_DIR / "sub_b.yaml").read_bytes(),
    }

    def fake_fetch(sub, *, config, timeout=20.0, transport=None) -> FetchResult:
        return FetchResult(
            sub_id=sub.id, sub_name=sub.name, status=FetchStatus.OK,
            content=content_map[sub.name], userinfo=dict(USERINFO),
            error=None, fetched_at=utils.now_iso(),
        )

    patcher = pytest.MonkeyPatch()
    patcher.setattr(fetcher_mod, "fetch_subscription", fake_fetch)
    try:
        e2e_store.add_subscription(SUB_A, "http://airport-a.example.com/token-a")
        e2e_store.add_subscription(SUB_B, "http://airport-b.example.com/token-b")
        result = pipeline.run_full_pipeline(e2e_config, e2e_store)

        # 等待旁路纯净度扫描线程结束（本机无 mihomo 二进制时立即降级），
        # 避免 store 在线程结束后才关闭
        for t in threading.enumerate():
            if t.name == "subhub-purity-scan":
                t.join(timeout=30)
        yield result
    finally:
        patcher.undo()


def _clash_doc(text: str) -> dict:
    doc = yaml.safe_load(text)
    assert isinstance(doc, dict)
    return doc


def _sr_groups(conf: str) -> list[str]:
    """[Proxy Group] 段的组名列表（行首到第一个「 = 」）。"""
    groups, current = [], None
    for raw in conf.splitlines():
        line = raw.strip()
        if line.startswith("[") and line.endswith("]"):
            current = line[1:-1]
            continue
        if current == "Proxy Group" and line and not line.startswith("#"):
            groups.append(line.split("=", 1)[0].strip())
    return groups


def _sr_section(conf: str, section: str) -> list[str]:
    lines, current = [], None
    for raw in conf.splitlines():
        line = raw.strip()
        if line.startswith("[") and line.endswith("]"):
            current = line[1:-1]
            continue
        if current == section and line and not line.startswith("#"):
            lines.append(line)
    return lines


# ---------------------------------------------------------------- 主链路断言

class TestFullChainPublish:
    def test_pipeline_published(self, published: pipeline.PipelineResult):
        assert published.published is True
        assert published.version == 1
        # 18 = sub_a 14（12 真 + 2 假）+ sub_b 4（3 真 + 1 假）；真实 15、假 3
        assert published.node_count == 15
        assert published.filtered_count == 3
        assert published.stale_subs == []
        assert published.errors == []

    def test_four_artifacts_written(self, e2e_config: AppConfig,
                                    published: pipeline.PipelineResult):
        vdir = utils.version_dir(e2e_config.out_dir, published.version)
        for name in ARTIFACT_NAMES:
            assert (vdir / name).is_file(), name
            assert (vdir / name).stat().st_size > 0, name
        meta = utils.read_json(vdir / "meta.json")
        assert isinstance(meta, dict)
        assert meta["node_count"] == 15
        # content_hash 口径：4 份产物按规范顺序拼接的 sha256
        digest = utils.content_hash(
            "".join((vdir / name).read_text(encoding="utf-8") for name in ARTIFACT_NAMES)
        )
        assert meta["content_hash"] == digest

    def test_clash_yaml_parseable(self, e2e_config: AppConfig,
                                  published: pipeline.PipelineResult):
        vdir = utils.version_dir(e2e_config.out_dir, published.version)
        doc = _clash_doc((vdir / "clash.yaml").read_text(encoding="utf-8"))
        assert len(doc["proxies"]) == 15                      # 15 个真实节点
        assert len(doc["rules"]) == 26 + 4 + 1                # 4 条内建 LAN 护栏 + 26 条 RULE-SET + MATCH
        assert doc["rules"][-1] == f"MATCH,{G_FINAL}"
        assert doc["sniffer"]["enable"] is True       # 裸 IP 连接靠嗅探恢复域名

    def test_group_names_complete(self, e2e_config: AppConfig,
                                  published: pipeline.PipelineResult):
        vdir = utils.version_dir(e2e_config.out_dir, published.version)
        doc = _clash_doc((vdir / "clash.yaml").read_text(encoding="utf-8"))
        group_names = {g["name"] for g in doc["proxy-groups"]}
        missing = EXPECTED_GROUPS - group_names
        assert not missing, f"缺少组：{missing}"
        for group in doc["proxy-groups"]:
            assert group["proxies"], group["name"]

    def test_speed_params(self, e2e_config: AppConfig,
                          published: pipeline.PipelineResult):
        vdir = utils.version_dir(e2e_config.out_dir, published.version)
        doc = _clash_doc((vdir / "clash.yaml").read_text(encoding="utf-8"))
        for group in doc["proxy-groups"]:
            if group["type"] not in ("url-test", "fallback"):
                continue
            assert group["url"] == PROBE_URL, group["name"]
            assert group["tolerance"] == 40, group["name"]
            assert group["max-failed-times"] == 3, group["name"]
            assert group["timeout"] == 3000, group["name"]
            if group["name"] == G_CLAUDE_BACKUP:
                assert group["interval"] == 90, "Claude 备援须 90s"
                assert group["lazy"] is False, "Claude 备援须 lazy=false"
            else:
                assert group["interval"] == 120, group["name"]

    def test_rule_providers_point_to_nas(self, e2e_config: AppConfig,
                                         published: pipeline.PipelineResult):
        vdir = utils.version_dir(e2e_config.out_dir, published.version)
        doc = _clash_doc((vdir / "clash.yaml").read_text(encoding="utf-8"))
        manifest = {e.name: e for e in load_rules_manifest()}
        assert set(doc["rule-providers"]) == set(manifest)
        for name, spec in doc["rule-providers"].items():
            assert spec["type"] == "http", name
            assert spec["url"] == f"{e2e_config.base_url}/rules/{manifest[name].clash_file}", name
            assert spec["interval"] == 21600, name
        # SR 主版本 RULE-SET 同样全部指向 NAS
        sr = (vdir / "shadowrocket.conf").read_text(encoding="utf-8")
        rule_sets = [ln for ln in _sr_section(sr, "Rule") if ln.startswith("RULE-SET,")]
        assert len(rule_sets) == len(manifest)
        for ln in rule_sets:
            assert f"{e2e_config.base_url}/rules/" in ln, ln
        assert _sr_section(sr, "Rule")[-1] == f"FINAL,{G_FINAL}"

    def test_offline_versions_have_no_external_links(self, e2e_config: AppConfig,
                                                     published: pipeline.PipelineResult):
        vdir = utils.version_dir(e2e_config.out_dir, published.version)
        clash_off = _clash_doc((vdir / "clash-offline.yaml").read_text(encoding="utf-8"))
        # 全部 inline 且 payload 非空（基线已预置）；behavior 与 payload 行格式匹配
        for name, spec in clash_off["rule-providers"].items():
            assert spec["type"] == "inline", name
            assert spec["payload"], name
            if spec["behavior"] == "domain":
                assert all("," not in str(p) for p in spec["payload"]), name
            if spec["behavior"] == "classical":
                assert all("," in str(p) for p in spec["payload"]), name
        text = (vdir / "clash-offline.yaml").read_text(encoding="utf-8")
        assert e2e_config.base_url not in text
        assert "raw.githubusercontent.com" not in text
        assert "type: http" not in text
        # 离线版分组与规则链与主版本完全一致
        main_doc = _clash_doc((vdir / "clash.yaml").read_text(encoding="utf-8"))
        assert clash_off["rules"] == main_doc["rules"]
        assert [g["name"] for g in clash_off["proxy-groups"]] == \
            [g["name"] for g in main_doc["proxy-groups"]]
        # SR 离线版：无 RULE-SET 外链、无 NAS 地址，规则内容直接展开
        sr_off = (vdir / "shadowrocket-offline.conf").read_text(encoding="utf-8")
        assert e2e_config.base_url not in sr_off
        assert "raw.githubusercontent.com" not in sr_off
        assert not [ln for ln in _sr_section(sr_off, "Rule") if ln.startswith("RULE-SET,")]
        assert f"DOMAIN,api.anthropic.com,{G_CLAUDE}" in _sr_section(sr_off, "Rule")
        assert _sr_section(sr_off, "Rule")[-1] == f"FINAL,{G_FINAL}"

    def test_sr_group_set_matches_mihomo(self, e2e_config: AppConfig,
                                         published: pipeline.PipelineResult):
        vdir = utils.version_dir(e2e_config.out_dir, published.version)
        doc = _clash_doc((vdir / "clash.yaml").read_text(encoding="utf-8"))
        sr = (vdir / "shadowrocket.conf").read_text(encoding="utf-8")
        clash_groups = [g["name"] for g in doc["proxy-groups"]]
        assert _sr_groups(sr) == clash_groups  # 顺序与集合都一致
        # SR 测速行参数（docs/02 §4：url=…, interval=…, tolerance=… 形式）
        for line in _sr_section(sr, "Proxy Group"):
            if " = url-test" in line or " = fallback" in line:
                assert f"url={PROBE_URL}" in line, line
                assert "tolerance=40" in line, line
                expected = "interval=90" if line.startswith(G_CLAUDE_BACKUP) else "interval=120"
                assert expected in line, line

    def test_fake_nodes_absent_from_all_artifacts(self, e2e_config: AppConfig,
                                                  published: pipeline.PipelineResult):
        vdir = utils.version_dir(e2e_config.out_dir, published.version)
        for name in ARTIFACT_NAMES:
            text = (vdir / name).read_text(encoding="utf-8")
            for marker in FAKE_MARKERS:
                assert marker not in text, f"{name} 出现假节点特征词「{marker}」"

    def test_disambiguation_suffix_applied(self, e2e_config: AppConfig, e2e_store: Store,
                                           published: pipeline.PipelineResult):
        vdir = utils.version_dir(e2e_config.out_dir, published.version)
        doc = _clash_doc((vdir / "clash.yaml").read_text(encoding="utf-8"))
        proxy_names = [p["name"] for p in doc["proxies"]]
        # 同名节点各自带上订阅别名后缀，且两个后缀变体都在
        assert SAME_NAME_A in proxy_names
        assert SAME_NAME_B in proxy_names
        assert proxy_names.count(SAME_NAME) == 0  # 不允许裸同名残留
        assert len(proxy_names) == len(set(proxy_names))  # 全局无重名
        # SR 主版本同样带后缀（该节点是 vless，不被 anytls 开关跳过）
        sr = (vdir / "shadowrocket.conf").read_text(encoding="utf-8")
        sr_proxy_names = [ln.split("=", 1)[0].strip() for ln in _sr_section(sr, "Proxy")]
        assert SAME_NAME_A in sr_proxy_names and SAME_NAME_B in sr_proxy_names
        # 消歧后 Claude 专用组首位为美国家宽（docs/02 静态排序）
        claude = next(g for g in doc["proxy-groups"] if g["name"] == G_CLAUDE)
        assert set(claude["proxies"][:2]) == {SAME_NAME_A, SAME_NAME_B}
        # ♻️ 常规自动 = 低倍率节点（倍率 ≤ max(阈值, 全库最低倍率)；文档见 templater）
        auto = next(g for g in doc["proxy-groups"] if g["name"] == G_AUTO)
        real = [n for n in e2e_store.list_nodes() if not n.filtered]
        cutoff = max(e2e_config.auto_max_rate, min(n.rate for n in real))
        assert sorted(auto["proxies"]) == sorted(n.name for n in real if n.rate <= cutoff)

    def test_snapshots_and_userinfo_persisted(self, e2e_config: AppConfig, e2e_store: Store,
                                              published: pipeline.PipelineResult):
        nodes = e2e_store.list_nodes()
        assert len(nodes) == 18  # 15 真实 + 3 被滤（整批替换语义含被滤节点）
        assert len([n for n in nodes if n.filtered]) == 3
        assert {n.filter_reason for n in nodes if n.filtered} <= set(FAKE_MARKERS) | {"续费"}
        for sub_id, name in ((1, SUB_A), (2, SUB_B)):
            sub = e2e_store.get_subscription(sub_id)
            assert sub is not None and sub.last_fetch_status == FetchStatus.OK
            assert sub.userinfo == USERINFO
        # 重名节点记录了消歧前原名；地区/属性识别正确
        renamed = [n for n in nodes if n.orig_name == SAME_NAME]
        assert {n.source_sub for n in renamed} == {SUB_A, SUB_B}
        us_home = next(n for n in nodes if n.name == SAME_NAME_A)
        assert us_home.region == "US" and us_home.residential
        iplc = next(n for n in nodes if n.name == "🇭🇰 香港 IEPL 专线 02")
        assert iplc.iplc is True
        x2 = next(n for n in nodes if n.name == "🇺🇸 美国 圣何塞 专线 x2")
        assert x2.rate == 2.0


# ---------------------------------------------------------------- web 服务冒烟

@pytest.fixture(scope="module")
def client(e2e_config: AppConfig, e2e_store: Store,
           published: pipeline.PipelineResult, tmp_path_factory: pytest.TempPathFactory):
    """真实 create_app（lifespan：启动规则镜像 + 定时调度器）挂载的测试客户端。

    启动规则镜像断网化（data/rules 已有缓存 → 沿用旧缓存，不发起真实网络请求）。
    """
    from fastapi.testclient import TestClient
    from app import rulesync
    from app.web import create_app

    patcher = pytest.MonkeyPatch()
    patcher.setattr(rulesync, "fetch_bytes", lambda *a, **k: None)
    app = create_app(config=e2e_config, store=e2e_store)
    try:
        with TestClient(app) as c:
            yield c
    finally:
        patcher.undo()


class TestWebServingSmoke:
    def test_health(self, client, e2e_config: AppConfig):
        resp = client.get("/api/health")
        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "ok"
        assert body["current_version"] == 1

    def test_distribute_endpoints(self, client, e2e_config: AppConfig):
        for artifact, media in (
            ("clash.yaml", "text/yaml"),
            ("shadowrocket.conf", "text/plain"),
            ("clash-offline.yaml", "text/yaml"),
            ("shadowrocket-offline.conf", "text/plain"),
        ):
            resp = client.get(f"/sub/{e2e_config.token}/{artifact}")
            assert resp.status_code == 200, artifact
            assert resp.headers["content-type"].startswith(media), artifact
            assert "ETag" in resp.headers
            assert f'"{utils.content_hash(resp.text)}"' == resp.headers["ETag"]
            # 304 协商
            resp304 = client.get(f"/sub/{e2e_config.token}/{artifact}",
                                 headers={"if-none-match": resp.headers["ETag"]})
            assert resp304.status_code == 304, artifact
        # 订阅链接带 userinfo（注入的两个订阅汇总：upload/download 求和、total/expire 取最小）
        resp = client.get(f"/sub/{e2e_config.token}/clash.yaml")
        assert resp.headers["subscription-userinfo"] == \
            "upload=20; download=40; total=1000; expire=1790000000"
        # 错误 token 404；?qr 出扫码页
        assert client.get("/sub/deadbeef/clash.yaml").status_code == 404
        qr = client.get(f"/sub/{e2e_config.token}/clash.yaml", params={"qr": "1"})
        assert qr.status_code == 200 and "扫码导入" in qr.text

    def test_rules_endpoint_serves_cache(self, client):
        resp = client.get("/rules/claude-extra.list")
        assert resp.status_code == 200
        assert "DOMAIN,api.anthropic.com" in resp.text

    def test_nodes_and_preview(self, client):
        resp = client.get("/api/nodes")
        assert resp.status_code == 200
        body = resp.json()
        assert body["total"] == 18
        assert all("password" not in n and "uuid" not in n for n in body["nodes"])
        fake = client.get("/api/nodes", params={"tag": "fake"})
        assert fake.json()["total"] == 3
        preview = client.get("/api/config/preview", params={"fmt": "clash"})
        assert preview.status_code == 200
        assert "mixed-port: 7897" in preview.text

    def test_manual_refresh_via_api(self, client):
        # web → pipeline 全链路（fetch 注入沿用模块级补丁）：版本 +1
        resp = client.post("/api/refresh", json={})
        assert resp.status_code == 200
        body = resp.json()
        assert body["ok"] is True and body["published"] is True
        assert body["version"] == 2
        versions = client.get("/api/config/versions").json()
        assert versions["current"] == 2 and len(versions["versions"]) == 2
