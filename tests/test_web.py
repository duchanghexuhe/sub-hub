"""web 模块测试：API 契约、分发回退、打码、ETag/userinfo、UI 注入、镜像端点。

- 全部走 tests/conftest.py 的 data_dir/config/store fixture（tmp_path 隔离）。
- 变更类操作依赖的并行模块（pipeline/purity/mirror）通过 FastAPI dependency_overrides
  注入替身，并记录调用次数验证「变更后触发完整链路」。
"""
from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app import web
from app.models import FetchStatus, Node, PurityResult
from app.utils import (
    atomic_write_bytes,
    atomic_write_text,
    content_hash,
    new_version_dir,
    now_iso,
    read_json,
    write_json,
)

SECRET_URL = "https://secret-airport.example.com/link/9f8e7d6c"
SECRET_URL_TAIL = SECRET_URL[-6:]


# --------------------------------------------------------------------- 替身与工具


def _result(**kw: Any) -> SimpleNamespace:
    base = dict(
        published=True, version=1, node_count=12, filtered_count=2,
        stale_subs=[], errors=[],
    )
    base.update(kw)
    return SimpleNamespace(**base)


def _sample_nodes(sub: str) -> list[Node]:
    return [
        Node(
            name="🇺🇸 美国 洛杉矶 家庭宽带 01", type="vless",
            server="srv-us-01.example.com", port=443, source_sub=sub,
            credentials={"uuid": "secret-uuid-aaaa"}, region="US", residential=True,
        ),
        Node(
            name="🇭🇰 香港 01", type="vless",
            server="srv-hk-01.example.com", port=443, source_sub=sub,
            credentials={"uuid": "secret-uuid-bbbb"}, region="HK", iplc=True,
        ),
        Node(
            name="剩余流量：100GB", type="ss",
            server="srv-fake.example.com", port=443, source_sub=sub,
            credentials={"password": "secret-pw-cccc"}, filtered=True, filter_reason="流量",
        ),
    ]


def _fake_pipeline(*, invalid_ids: frozenset[int] = frozenset(), version: int = 1) -> tuple[SimpleNamespace, dict[str, list]]:
    """pipeline 替身：记录调用；对指定 sub_id 模拟「订阅失效」，否则写快照并发布。"""
    calls: dict[str, list] = {"run": [], "rollback": []}

    def run_full_pipeline(cfg, st, *, sub_id=None):
        calls["run"].append(sub_id)
        if sub_id is not None and sub_id in invalid_ids:
            st.set_fetch_status(sub_id, FetchStatus.INVALID)
            return _result(published=False, version=None, node_count=0, filtered_count=0,
                           stale_subs=["new-sub"], errors=["订阅失效"])
        if sub_id is not None:
            sub = st.get_subscription(sub_id)
            st.set_fetch_status(sub_id, FetchStatus.OK)
            st.replace_nodes(sub.name, _sample_nodes(sub.name))
        return _result(version=version)

    def rollback_to_version(cfg, st, v):
        calls["rollback"].append(v)
        return SimpleNamespace(version=v + 10, created_at=now_iso(), node_count=12,
                               content_hash="x" * 64, diff_summary={}, note=f"回退到 v{v:04d}")

    return SimpleNamespace(run_full_pipeline=run_full_pipeline, rollback_to_version=rollback_to_version), calls


def _fake_purity() -> tuple[SimpleNamespace, dict[str, list]]:
    calls: dict[str, list] = {"full": [], "rec": 0}

    def scan(nodes, *, config, store, full=False):
        calls["full"].append(full)
        result = PurityResult(
            node_name="🇺🇸 美国 洛杉矶 家庭宽带 01", source_sub="a", checked_at=now_iso(),
            exit_ip="23.45.67.89", country="United States", asn="AS5650 Frontier",
            org="Frontier Communications", isp="Frontier", ip_type="residential",
            hosting=False, proxy=False, mobile=False, claude_rank=3,
        )
        store.save_purity_result(result)
        return SimpleNamespace(results=[result], unavailable=False, checked=1, skipped=max(0, len(nodes) - 1))

    return SimpleNamespace(scan=scan), calls


def _fake_mirror() -> tuple[SimpleNamespace, dict[str, int]]:
    from app.utils import read_json as _rj, write_json as _wj

    calls = {"push": 0}

    def load(cfg):
        return _rj(cfg.mirror_settings_path, {"enabled": False})

    def save(cfg, settings):
        _wj(cfg.mirror_settings_path, settings)

    def push(cfg):
        calls["push"] += 1
        s = load(cfg)
        if not s.get("enabled"):
            return SimpleNamespace(ok=False, provider=None, url=None, error="镜像未启用", pushed_at=now_iso())
        return SimpleNamespace(ok=True, provider=s.get("provider"),
                               url="https://mirror.example.com/p/abc123", error=None, pushed_at=now_iso())

    return SimpleNamespace(load_mirror_settings=load, save_mirror_settings=save, push_current=push), calls


def _publish(config, version: int, *, node_count: int = 12, clash: str | None = None) -> str:
    """模拟 validator.publish 的落盘形态（5 份产物 + meta.json）。"""
    d = new_version_dir(config.out_dir, version)
    clash_text = clash if clash is not None else f"# clash config v{version}\n"
    atomic_write_text(d / "clash.yaml", clash_text)
    atomic_write_text(d / "shadowrocket.conf", f"# sr config v{version}\n")
    atomic_write_text(d / "shadowrocket.yaml", f"proxies:\n# sr yaml v{version}\n")
    atomic_write_text(d / "clash-offline.yaml", f"# offline clash v{version}\n")
    atomic_write_text(d / "shadowrocket-offline.conf", f"# offline sr v{version}\n")
    write_json(d / "meta.json", {
        "version": version, "created_at": now_iso(), "node_count": node_count,
        "content_hash": content_hash(clash_text), "diff_summary": {}, "note": None,
    })
    return clash_text


@pytest.fixture()
def client(config, store):
    """TestClient（不进入 lifespan，避免拉起调度器/真实镜像）。"""
    application = web.create_app(config=config, store=store)
    c = TestClient(application)
    yield c
    application.dependency_overrides.clear()


def _override_pipeline(client: TestClient, invalid_ids: frozenset[int] = frozenset(), version: int = 1):
    mod, calls = _fake_pipeline(invalid_ids=invalid_ids, version=version)
    client.app.dependency_overrides[web.get_pipeline] = lambda: mod
    return calls


# --------------------------------------------------------------------- 基础与健康


class TestHealthAndErrors:
    def test_health_ok(self, config, client):
        resp = client.get("/api/health")
        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "ok"
        assert body["scheduler"] == {}
        assert body["current_version"] is None
        assert "time" in body

    def test_health_reflects_versions(self, config, client):
        _publish(config, 1)
        _publish(config, 2)
        body = client.get("/api/health").json()
        assert body["current_version"] == 2

    def test_unknown_route_error_body(self, client):
        resp = client.get("/api/definitely-not-here")
        assert resp.status_code == 404
        assert resp.json()["code"] == "not_found"


# --------------------------------------------------------------------- 订阅管理


class TestSubs:
    def test_list_masks_url(self, store, client):
        store.add_subscription("a", SECRET_URL)
        resp = client.get("/api/subs")
        assert resp.status_code == 200
        body = resp.json()
        assert body["total"] == 1
        sub = body["subs"][0]
        assert sub["url_masked"] == "…" + SECRET_URL_TAIL
        assert "secret-airport" not in resp.text  # 明文绝不出现
        assert sub["last_fetch_status"] is None
        assert sub["last_fetch_status_label"] == "从未抓取"

    def test_create_success_runs_pipeline_for_new_sub(self, store, client):
        calls = _override_pipeline(client, version=3)
        resp = client.post("/api/subs", json={"name": "kuai", "url": SECRET_URL})
        assert resp.status_code == 200
        body = resp.json()
        assert body["subscription"]["name"] == "kuai"
        assert body["result"]["version"] == 3
        assert calls["run"] == [1]  # 新增订阅只对该订阅抓取验证
        assert store.get_subscription(1).name == "kuai"
        assert "secret-airport" not in resp.text

    def test_create_invalid_rejected_and_rolled_back(self, store, client):
        calls = _override_pipeline(client, invalid_ids=frozenset({1}))
        resp = client.post("/api/subs", json={"name": "bad", "url": SECRET_URL})
        assert resp.status_code == 400
        body = resp.json()
        assert body["code"] == "invalid_subscription"
        assert "验证失败" in body["message"]
        assert store.list_subscriptions() == []  # 已回滚
        assert calls["run"] == [1]

    def test_create_bad_url(self, client):
        calls = _override_pipeline(client)
        resp = client.post("/api/subs", json={"name": "x", "url": "ftp://nope"})
        assert resp.status_code == 400
        assert resp.json()["code"] == "bad_url"
        assert calls["run"] == []

    def test_create_empty_name(self, client):
        _override_pipeline(client)
        resp = client.post("/api/subs", json={"name": "  ", "url": SECRET_URL})
        assert resp.status_code == 400
        assert resp.json()["code"] == "bad_request"

    def test_create_duplicate_name_conflict(self, store, client):
        store.add_subscription("dupe", SECRET_URL)
        _override_pipeline(client)
        resp = client.post("/api/subs", json={"name": "dupe", "url": SECRET_URL})
        assert resp.status_code == 409
        assert resp.json()["code"] == "name_conflict"

    def test_create_missing_fields_maps_to_400(self, client):
        _override_pipeline(client)
        resp = client.post("/api/subs", json={"url": SECRET_URL})  # 缺 name
        assert resp.status_code == 400
        assert resp.json()["code"] == "bad_request"

    def test_patch_toggle_triggers_full_pipeline(self, store, client):
        store.add_subscription("a", SECRET_URL)
        calls = _override_pipeline(client)
        resp = client.patch("/api/subs/1", json={"enabled": False})
        assert resp.status_code == 200
        assert store.get_subscription(1).enabled is False
        assert calls["run"] == [None]  # 完整链路
        assert resp.json()["subscription"]["enabled"] is False

    def test_patch_rename(self, store, client):
        store.add_subscription("a", SECRET_URL)
        _override_pipeline(client)
        resp = client.patch("/api/subs/1", json={"name": "b"})
        assert resp.status_code == 200
        assert store.get_subscription(1).name == "b"

    def test_patch_rename_conflict(self, store, client):
        store.add_subscription("a", SECRET_URL)
        store.add_subscription("b", SECRET_URL)
        _override_pipeline(client)
        resp = client.patch("/api/subs/1", json={"name": "b"})
        assert resp.status_code == 409

    def test_patch_empty_body(self, store, client):
        store.add_subscription("a", SECRET_URL)
        _override_pipeline(client)
        resp = client.patch("/api/subs/1", json={})
        assert resp.status_code == 400
        assert resp.json()["code"] == "bad_request"

    def test_patch_missing_sub_404(self, client):
        _override_pipeline(client)
        resp = client.patch("/api/subs/99", json={"enabled": True})
        assert resp.status_code == 404
        assert resp.json()["code"] == "not_found"

    def test_delete_triggers_full_pipeline(self, store, client):
        store.add_subscription("a", SECRET_URL)
        calls = _override_pipeline(client)
        resp = client.delete("/api/subs/1")
        assert resp.status_code == 200
        assert resp.json()["ok"] is True
        assert store.list_subscriptions() == []
        assert calls["run"] == [None]

    def test_delete_missing_sub_404(self, client):
        _override_pipeline(client)
        assert client.delete("/api/subs/99").status_code == 404


class TestRefresh:
    def test_refresh_all_default(self, store, client):
        calls = _override_pipeline(client, version=7)
        resp = client.post("/api/refresh", json={})
        assert resp.status_code == 200
        body = resp.json()
        assert body["ok"] is True
        assert body["version"] == 7
        assert calls["run"] == [None]

    def test_refresh_single_sub(self, store, client):
        store.add_subscription("a", SECRET_URL)
        calls = _override_pipeline(client)
        resp = client.post("/api/refresh", json={"sub_id": 1})
        assert resp.status_code == 200
        assert calls["run"] == [1]

    def test_refresh_unknown_sub_404(self, client):
        _override_pipeline(client)
        resp = client.post("/api/refresh", json={"sub_id": 42})
        assert resp.status_code == 404


# --------------------------------------------------------------------- 节点与纯净度


def _seed_nodes(store) -> None:
    store.add_subscription("a", SECRET_URL)  # id=1
    store.add_subscription("b", SECRET_URL)  # id=2
    store.replace_nodes("a", [
        Node(name="🇺🇸 美国 家庭 01", type="vless", server="us1.a.com", port=443,
             source_sub="a", credentials={"uuid": "u1"}, region="US", residential=True),
        Node(name="🇭🇰 香港 01", type="vless", server="hk1.a.com", port=443,
             source_sub="a", credentials={"uuid": "u2"}, region="HK", iplc=True),
        Node(name="🇺🇸 美国 DMIT 02", type="ss", server="us2.a.com", port=443,
             source_sub="a", credentials={"password": "p1"}, region="US", rate=2.0),
        Node(name="官网：续费地址", type="ss", server="ad.a.com", port=443,
             source_sub="a", credentials={"password": "p2"}, filtered=True, filter_reason="官网"),
    ])
    store.replace_nodes("b", [
        Node(name="🇺🇸 美国 洛杉矶 家庭宽带 01", type="vless", server="us1.b.com", port=443,
             source_sub="b", credentials={"uuid": "u3"}, region="US", residential=True),
    ])


class TestNodes:
    def test_list_all_with_purity_summary(self, store, client):
        _seed_nodes(store)
        store.save_purity_result(PurityResult(
            node_name="🇺🇸 美国 家庭 01", source_sub="a", checked_at=now_iso(),
            exit_ip="23.45.67.89", ip_type="residential", claude_rank=3,
        ))
        resp = client.get("/api/nodes")
        assert resp.status_code == 200
        body = resp.json()
        assert body["total"] == 5  # 含被滤节点
        by_name = {n["name"]: n for n in body["nodes"]}
        us = by_name["🇺🇸 美国 家庭 01"]
        assert us["purity"]["claude_rank"] == 3
        assert us["purity"]["exit_ip"] == "23.45.67.89"
        assert by_name["🇭🇰 香港 01"]["purity"] is None
        fake = by_name["官网：续费地址"]
        assert fake["filtered"] is True
        assert fake["filter_reason"] == "官网"
        # 凭据与服务器地址绝不外泄
        assert "secret-uuid" not in resp.text
        assert "secret-pw" not in resp.text
        assert "us1.a.com" not in resp.text
        assert "uuid" not in resp.text

    def test_filter_region(self, store, client):
        _seed_nodes(store)
        body = client.get("/api/nodes", params={"region": "US"}).json()
        assert body["total"] == 3
        assert all(n["region"] == "US" for n in body["nodes"])

    def test_filter_tags(self, store, client):
        _seed_nodes(store)
        assert client.get("/api/nodes", params={"tag": "residential"}).json()["total"] == 2
        assert client.get("/api/nodes", params={"tag": "iplc"}).json()["total"] == 1
        fake = client.get("/api/nodes", params={"tag": "fake"}).json()
        assert fake["total"] == 1
        assert fake["nodes"][0]["filter_reason"] == "官网"

    def test_filter_sub_id(self, store, client):
        _seed_nodes(store)
        body = client.get("/api/nodes", params={"sub_id": 2}).json()
        assert body["total"] == 1
        assert body["nodes"][0]["source_sub"] == "b"

    def test_filter_unknown_sub_404(self, store, client):
        _seed_nodes(store)
        resp = client.get("/api/nodes", params={"sub_id": 99})
        assert resp.status_code == 404

    def test_bad_tag_400(self, store, client):
        _seed_nodes(store)
        resp = client.get("/api/nodes", params={"tag": "nope"})
        assert resp.status_code == 400
        assert resp.json()["code"] == "bad_tag"


class TestParseAnomalies:
    """解析异常节点端点（docs/01 安全网：UI「解析异常节点」列表可见）。"""

    def _seed(self, config) -> None:
        write_json(config.data_dir / "parse_anomalies.json", {
            "updated_at": now_iso(),
            "subs": {
                "kuai": {"checked_at": now_iso(), "anomalies": [
                    {"source_sub": "kuai", "kind": "clash", "label": "bad-node",
                     "reason": "port 无效：abc", "detected_at": now_iso()},
                ]},
                "mengliu": {"checked_at": now_iso(), "anomalies": []},
            },
        })

    def test_empty_when_no_file(self, config, client):
        body = client.get("/api/parse-anomalies").json()
        assert body == {"total": 0, "updated_at": None, "anomalies": []}

    def test_lists_anomalies_from_pipeline_file(self, config, client):
        self._seed(config)
        body = client.get("/api/parse-anomalies").json()
        assert body["total"] == 1
        row = body["anomalies"][0]
        assert row["label"] == "bad-node"
        assert row["source_sub"] == "kuai"
        assert row["kind"] == "clash"
        assert body["updated_at"] is not None


def _seed_purity(store) -> None:
    """n_us_home：住宅→机房（触发标红）；n_any_dc：机房；n_us_home2：住宅（推荐 Top1）。"""
    early, late = "2026-09-26T04:00:00.000000", "2026-09-27T04:00:00.000000"
    store.save_purity_result(PurityResult(node_name="n-us-home", source_sub="a", checked_at=early,
                                          exit_ip="1.1.1.1", ip_type="residential", claude_rank=3))
    store.save_purity_result(PurityResult(node_name="n-any-dc", source_sub="a", checked_at=early,
                                          exit_ip="2.2.2.2", ip_type="datacenter", claude_rank=2))
    store.save_purity_result(PurityResult(node_name="n-us-home", source_sub="a", checked_at=late,
                                          exit_ip="3.3.3.3", ip_type="datacenter", claude_rank=2))
    store.save_purity_result(PurityResult(node_name="n-us-home2", source_sub="b", checked_at=late,
                                          exit_ip="4.4.4.4", ip_type="residential", claude_rank=3))


class TestPurity:
    def test_report_sorted_with_changes_and_recommendations(self, store, client):
        _seed_purity(store)
        resp = client.get("/api/purity/report")
        assert resp.status_code == 200
        body = resp.json()
        names = [r["node_name"] for r in body["results"]]
        assert names[0] == "n-us-home2"  # 住宅 rank3 排最前
        assert set(names[1:]) == {"n-us-home", "n-any-dc"}
        assert body["checked"] == 3
        changes = body["changes"]
        assert len(changes) == 1
        assert changes[0]["node_name"] == "n-us-home"
        assert changes[0]["is_residential_lost"] is True
        lost_rows = [r for r in body["results"] if r["residential_lost"]]
        assert [r["node_name"] for r in lost_rows] == ["n-us-home"]
        recs = body["recommendations"]
        assert 1 <= len(recs) <= 3
        assert recs[0]["node_name"] == "n-us-home2"

    def test_report_empty(self, client):
        body = client.get("/api/purity/report").json()
        assert body == {"checked": 0, "results": [], "changes": [], "recommendations": []}

    def test_scan_via_dependency(self, store, client):
        _seed_nodes(store)
        mod, calls = _fake_purity()
        client.app.dependency_overrides[web.get_purity] = lambda: mod
        resp = client.post("/api/purity/scan", json={})
        assert resp.status_code == 200
        body = resp.json()
        assert body["ok"] is True
        assert body["checked"] == 1
        assert calls["full"] == [False]
        # 扫描结果确实落库
        assert any(r.exit_ip == "23.45.67.89" for r in store.latest_purity_results())
        resp = client.post("/api/purity/scan", json={"full": True})
        assert calls["full"][-1] is True
        assert resp.json()["full"] is True


# --------------------------------------------------------------------- 配置产物


class TestConfig:
    def test_preview_empty_404(self, config, client):
        resp = client.get("/api/config/preview", params={"fmt": "clash"})
        assert resp.status_code == 404
        assert resp.json()["code"] == "no_artifact"

    def test_preview_bad_fmt(self, config, client):
        _publish(config, 1)
        resp = client.get("/api/config/preview", params={"fmt": "bogus"})
        assert resp.status_code == 400
        assert resp.json()["code"] == "bad_fmt"

    def test_preview_missing_fmt_400(self, config, client):
        _publish(config, 1)
        assert client.get("/api/config/preview").status_code == 400

    def test_preview_both_formats(self, config, client):
        _publish(config, 1)
        r1 = client.get("/api/config/preview", params={"fmt": "clash"})
        r2 = client.get("/api/config/preview", params={"fmt": "sr"})
        assert r1.status_code == 200 and r2.status_code == 200
        assert "text/plain" in r1.headers["content-type"]
        assert r1.text.startswith("# clash config v1")
        assert "sr yaml v1" in r2.text

    def test_versions_timeline(self, config, client):
        _publish(config, 1, node_count=12)
        _publish(config, 2, node_count=13)
        body = client.get("/api/config/versions").json()
        assert body["current"] == 2
        assert [v["version"] for v in body["versions"]] == [2, 1]
        assert body["versions"][0]["node_count"] == 13

    def test_rollback_via_dependency(self, config, client):
        _publish(config, 1)
        _publish(config, 2)
        calls = _override_pipeline(client)
        resp = client.post("/api/config/rollback", json={"version": 1})
        assert resp.status_code == 200
        body = resp.json()
        assert body["ok"] is True
        assert body["current_version"] == 11  # 替身返回 version+10
        assert calls["rollback"] == [1]

    def test_rollback_unknown_version_404(self, config, client):
        _publish(config, 1)
        calls = _override_pipeline(client)
        resp = client.post("/api/config/rollback", json={"version": 99})
        assert resp.status_code == 404
        assert resp.json()["code"] == "version_not_found"
        assert calls["rollback"] == []  # 不存在的版本不触发链路


# --------------------------------------------------------------------- 分发端点


class TestDistribution:
    def _add_sub_with_userinfo(self, store) -> None:
        store.add_subscription("a", SECRET_URL)
        store.set_userinfo(1, {"upload": 100, "download": 200, "total": 1000, "expire": 1790000000})

    def test_basic_headers_etag_304(self, config, store, client):
        self._add_sub_with_userinfo(store)
        clash = _publish(config, 1)
        resp = client.get(f"/sub/{config.token}/clash.yaml")
        assert resp.status_code == 200
        assert resp.text == clash
        assert resp.headers["etag"] == f'"{content_hash(clash)}"'
        assert resp.headers["subscription-userinfo"] == (
            "upload=100; download=200; total=1000; expire=1790000000"
        )
        assert "yaml" in resp.headers["content-type"]
        # 304 协商
        resp304 = client.get(f"/sub/{config.token}/clash.yaml",
                             headers={"if-none-match": f'"{content_hash(clash)}"'})
        assert resp304.status_code == 304

    def test_userinfo_aggregation_sum_and_min(self, config, store, client):
        self._add_sub_with_userinfo(store)
        store.add_subscription("b", SECRET_URL)
        store.set_userinfo(2, {"upload": 50, "download": 0, "total": 500, "expire": 1780000000})
        resp = client.get(f"/sub/{config.token}/shadowrocket.conf")
        assert resp.headers["subscription-userinfo"] == (
            "upload=150; download=200; total=500; expire=1780000000"
        )

    def test_userinfo_absent_when_no_data(self, config, store, client):
        _publish(config, 1)
        resp = client.get(f"/sub/{config.token}/clash.yaml")
        assert resp.status_code == 200
        assert "subscription-userinfo" not in resp.headers

    def test_disabled_sub_excluded_from_userinfo(self, config, store, client):
        store.add_subscription("a", SECRET_URL, enabled=False)
        store.set_userinfo(1, {"upload": 100, "download": 200, "total": 1000, "expire": 1790000000})
        _publish(config, 1)
        resp = client.get(f"/sub/{config.token}/clash.yaml")
        assert "subscription-userinfo" not in resp.headers

    def test_wrong_token_404(self, config, store, client):
        self._add_sub_with_userinfo(store)
        _publish(config, 1)
        resp = client.get("/sub/00000000000000000000000000000000/clash.yaml")
        assert resp.status_code == 404
        assert resp.json()["code"] == "not_found"

    def test_unknown_artifact_404(self, config, store, client):
        _publish(config, 1)
        resp = client.get(f"/sub/{config.token}/other.yaml")
        assert resp.status_code == 404

    def test_fallback_to_previous_version(self, config, store, client):
        clash1 = _publish(config, 1)
        # 最新版本损坏：目录存在但 clash.yaml 缺失（meta 残留）
        new_version_dir(config.out_dir, 2)
        write_json(config.out_dir / "v0002" / "meta.json",
                   {"version": 2, "created_at": now_iso(), "node_count": 0,
                    "content_hash": "x", "diff_summary": {}, "note": None})
        resp = client.get(f"/sub/{config.token}/clash.yaml")
        assert resp.status_code == 200  # 绝不 5xx 空响应
        assert resp.text == clash1
        # 空文件同样回退
        atomic_write_text(config.out_dir / "v0002" / "shadowrocket.conf", "")
        resp = client.get(f"/sub/{config.token}/shadowrocket.conf")
        assert resp.status_code == 200
        assert resp.text == "# sr config v1\n"

    def test_placeholder_when_no_artifacts_ever(self, config, client):
        resp = client.get(f"/sub/{config.token}/clash.yaml")
        assert resp.status_code == 200
        assert "暂无可用配置产物" in resp.text

    def test_qr_page(self, config, client):
        _publish(config, 1)
        resp = client.get(f"/sub/{config.token}/clash.yaml", params={"qr": "1"})
        assert resp.status_code == 200
        assert "text/html" in resp.headers["content-type"]
        assert "<svg" in resp.text
        assert config.token in resp.text
        assert "扫码导入订阅" in resp.text


class TestRules:
    def test_serve_yaml_and_304(self, config, client):
        payload = "payload:\n  - '+.example.com'\n"
        atomic_write_bytes(config.rules_dir / "Claude.yaml", payload.encode("utf-8"))
        resp = client.get("/rules/Claude.yaml")
        assert resp.status_code == 200
        assert resp.text == payload
        assert resp.headers["etag"] == f'"{content_hash(payload)}"'
        resp304 = client.get("/rules/Claude.yaml", headers={"if-none-match": resp.headers["etag"]})
        assert resp304.status_code == 304

    def test_missing_file_404(self, config, client):
        resp = client.get("/rules/Claude.list")
        assert resp.status_code == 404

    def test_reject_non_rule_files(self, config, client):
        atomic_write_text(config.rules_dir / "state.json", "{}")
        atomic_write_text(config.rules_dir / "token", "x")
        assert client.get("/rules/state.json").status_code == 404
        assert client.get("/rules/token").status_code == 404

    def test_reject_path_traversal(self, config, client):
        for path in ("/rules/%2e%2e%2fsecret.key", "/rules/..%2Ftoken", "/rules/a/b.yaml"):
            resp = client.get(path)
            assert resp.status_code == 404, path


# --------------------------------------------------------------------- 镜像端点


class TestMirror:
    def _override(self, client: TestClient):
        mod, calls = _fake_mirror()
        client.app.dependency_overrides[web.get_mirror] = lambda: mod
        return calls

    def test_get_default_disabled(self, config, client):
        self._override(client)
        body = client.get("/api/mirror").json()
        assert body["settings"]["enabled"] is False
        assert body["settings"]["credentials_configured"] is False

    def test_post_saves_and_trial_push(self, config, client):
        self._override(client)
        resp = client.post("/api/mirror", json={
            "provider": "cf-kv",
            "credentials": {"token": "secret-cf-token", "kv": "kv-ns"},
            "enabled": True,
        })
        assert resp.status_code == 200
        body = resp.json()
        assert body["push"]["ok"] is True
        assert body["push"]["url"] == "https://mirror.example.com/p/abc123"
        assert body["settings"]["credentials_configured"] is True
        assert "secret-cf-token" not in resp.text  # 凭据不回显
        # 落盘归 mirror 模块管，这里只确认保存路径被调用
        assert read_json(config.mirror_settings_path, {}).get("provider") == "cf-kv"

    def test_post_bad_provider(self, config, client):
        self._override(client)
        resp = client.post("/api/mirror", json={"provider": "aliyun", "enabled": True})
        assert resp.status_code == 400
        assert resp.json()["code"] == "bad_provider"

    def test_push_disabled_reports_not_enabled(self, config, client):
        self._override(client)
        resp = client.post("/api/mirror/push")
        assert resp.status_code == 200
        body = resp.json()
        assert body["ok"] is False
        assert body["error"] == "镜像未启用"

    def test_push_enabled(self, config, client):
        self._override(client)
        client.post("/api/mirror", json={"provider": "github", "credentials": "t", "enabled": True})
        resp = client.post("/api/mirror/push")
        assert resp.status_code == 200
        assert resp.json()["ok"] is True
        assert resp.json()["provider"] == "github"


# --------------------------------------------------------------------- 单页 UI


class TestUi:
    def test_index_injects_bootstrap(self, config, client):
        resp = client.get("/")
        assert resp.status_code == 200
        assert "text/html" in resp.headers["content-type"]
        assert "__BOOTSTRAP_JSON__" not in resp.text  # 占位符已被替换
        assert config.token in resp.text
        assert f"{config.base_url}/sub/{config.token}/clash.yaml" in resp.text
        # 五区块关键文案
        for marker in ("订阅", "节点总表", "纯净度报告", "配置产物", "操作"):
            assert marker in resp.text
