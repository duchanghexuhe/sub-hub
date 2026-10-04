"""rulesync 测试：claude-extra 内容断言、上游失败沿用旧缓存（mock）、manifest URL 拼装等。

纪律：
- 数据目录一律用 conftest 的 config fixture（tmp_path 隔离），绝不读写仓库根的 data/；
- 上游拉取一律 mock（monkeypatch app.rulesync.fetch_bytes），真实网络用例默认 skip，
  设 SUBHUB_TEST_NET=1 才启用。
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

import pytest
import yaml

from app import rulesync
from app.rulesync import (
    BASELINE_DIR,
    RuleStatus,
    RulesSyncReport,
    hours_since,
    load_sync_state,
    payload_yaml_to_list,
    placeholder_rule_files,
    read_manifest,
    resolve_entries,
    sync,
    sync_rules,
    upstream_urls,
)
from app.utils import atomic_write_text

# docs/02 §3 所列 claude-extra 必须覆盖的域名
DOCS_CLAUDE_DOMAINS = [
    "api.anthropic.com",
    "claude.ai",
    "claude.com",
    "statsig.anthropic.com",
    "console.anthropic.com",
    "anthropic.com",
]

# docs/02 §3 规则链顺序（manifest 列表顺序不得重排）
EXPECTED_CHAIN = [
    "claude-extra", "futu-extra",
    "steam-download", "steam-extra", "mteam-tracker", "mteam-web",
    "Claude", "OpenAI", "Gemini", "Copilot",
    "Grok", "Perplexity", "CursorAI", "AI",
    "telegram-extra", "Telegram", "Twitter",
    "Netflix", "Disney", "YouTube", "Spotify", "TikTok", "PrimeVideo",
    "GitHub", "Google", "Microsoft", "Apple",
    "Global", "ProxyGFWlist", "ChinaMax", "CNCIDR", "Lan", "Download",
]


def _builtin_entries() -> list[dict]:
    return [e for e in resolve_entries() if str(e.get("source")) == "builtin"]


def _entries_by_name() -> dict[str, dict]:
    return {str(e["name"]): e for e in resolve_entries()}


def _fail_all(url: str, **_kw) -> None:
    return None


# ---------------------------------------------------------------- claude-extra 内容断言

class TestClaudeExtraBaseline:
    def test_manifest_domains_cover_docs(self):
        """docs/02 §3 所列域名必须全部在 manifest 的 claude-extra.domains 中。"""
        domains = [str(d) for d in _entries_by_name()["claude-extra"]["domains"]]
        missing = [d for d in DOCS_CLAUDE_DOMAINS if d not in domains]
        assert missing == [], f"manifest 缺少文档要求的域名: {missing}"

    @pytest.mark.parametrize("fname", ["claude-extra.yaml", "claude-extra.list"])
    def test_baseline_files_exist_and_cover_domains(self, fname: str):
        """内置基线双格式存在，且覆盖 manifest domains 全集。"""
        text = (BASELINE_DIR / fname).read_text(encoding="utf-8")
        domains = [str(d) for d in _entries_by_name()["claude-extra"]["domains"]]
        if fname.endswith(".yaml"):
            doc = yaml.safe_load(text)
            assert isinstance(doc, dict) and isinstance(doc.get("payload"), list)
            lines = [str(x).strip() for x in doc["payload"] if str(x).strip()]
        else:
            lines = [
                l.strip() for l in text.splitlines()
                if l.strip() and not l.strip().startswith("#")
            ]
        assert lines, f"{fname} 规则行为空"
        for line in lines:
            assert line.startswith(("DOMAIN,", "DOMAIN-SUFFIX,")), f"{fname} 非法行: {line}"
        covered = {line.split(",", 1)[1] for line in lines}
        for d in domains:
            assert d in covered, f"{fname} 缺少域名 {d}"

    def test_yaml_and_list_domains_identical(self):
        """两格式覆盖的域名集合必须一致（docs/02 产物一致性精神）。"""
        ydoc = yaml.safe_load((BASELINE_DIR / "claude-extra.yaml").read_text(encoding="utf-8"))
        ydomains = {str(x).split(",", 1)[1] for x in ydoc["payload"]}
        llines = [
            l.strip() for l in (BASELINE_DIR / "claude-extra.list").read_text(encoding="utf-8")
            .splitlines()
            if l.strip() and not l.strip().startswith("#")
        ]
        ldomains = {l.split(",", 1)[1] for l in llines}
        assert ydomains == ldomains


class TestBuiltinBaselinesGeneric:
    """全部 builtin 条目的基线一致性：改了 manifest domains 忘跑 build_baselines() 即测试失败。"""

    @pytest.mark.parametrize("entry", _builtin_entries(), ids=lambda e: str(e["name"]))
    def test_baseline_covers_manifest_domains(self, entry: dict):
        name = str(entry["name"])
        domains = {str(d).strip() for d in (entry.get("domains") or []) if str(d).strip()}
        for fname in (str(entry["clash_file"]), str(entry["sr_file"])):
            path = BASELINE_DIR / fname
            assert path.exists(), f"{name} 缺少内置基线文件 {fname}（跑 build_baselines()）"
            text = path.read_text(encoding="utf-8")
            if fname.endswith(".yaml"):
                doc = yaml.safe_load(text)
                lines = [str(x).strip() for x in doc.get("payload", [])]
            else:
                lines = [
                    l.strip() for l in text.splitlines()
                    if l.strip() and not l.strip().startswith("#")
                ]
            assert lines, f"{name}/{fname} 规则行为空"
            covered = {l.split(",", 1)[1] for l in lines if "," in l}
            missing = domains - covered
            assert missing == set(), f"{name}/{fname} 基线缺少域名: {sorted(missing)}"

    def test_builtin_sync_offline_writes_baseline(self, config, monkeypatch):
        """完全断网（mock 全部上游失败）时，builtin 条目仍从基线复制到 data/rules/。"""
        monkeypatch.setattr(rulesync, "fetch_bytes", _fail_all)
        report = sync_rules(config)
        assert "claude-extra" in report.updated
        st = report.details["claude-extra"]
        assert st.ok is True and st.source == "builtin"
        assert st.updated_at == report.checked_at
        assert (config.rules_dir / "claude-extra.yaml").read_text(encoding="utf-8") == \
            (BASELINE_DIR / "claude-extra.yaml").read_text(encoding="utf-8")
        assert (config.rules_dir / "claude-extra.list").read_text(encoding="utf-8") == \
            (BASELINE_DIR / "claude-extra.list").read_text(encoding="utf-8")
        # 同为 builtin 的自维护清单（基线缺失时按 domains 生成）
        grok_text = (config.rules_dir / "Grok.yaml").read_text(encoding="utf-8")
        assert "DOMAIN-SUFFIX,x.ai" in grok_text

    def test_render_builtin_rule_with_ips(self):
        """ips 混排：DOMAIN-SUFFIX 与 IP-CIDR 行共存（富途行情网关裸 IP 段场景）。"""
        yaml_text, list_text = rulesync._render_builtin_rule(["a.com"], ["1.2.3.0/24"])
        assert "  - DOMAIN-SUFFIX,a.com" in yaml_text
        assert "  - IP-CIDR,1.2.3.0/24" in yaml_text
        assert "IP-CIDR,1.2.3.0/24" in list_text
        # SR 渲染按「含逗号行原样拼策略」处理，IP 行不得带 no-resolve（否则策略错位）
        assert "no-resolve" not in list_text

    def test_builtin_content_ips_only_entry(self):
        """仅 ips（无 domains）的条目也能生成，且走「基线优先」之外的下级分支。"""
        content = rulesync._builtin_content("no-such-rule.list", [], ["5.6.7.0/24"])
        assert content is not None and b"IP-CIDR,5.6.7.0/24" in content


# ---------------------------------------------------------------- 失败路径：沿用旧缓存 / 基线兜底

class TestFailurePaths:
    def _seed(self, rules_dir: Path, name: str, yaml_text: str, list_text: str) -> None:
        rules_dir.mkdir(parents=True, exist_ok=True)
        atomic_write_text(rules_dir / f"{name}.yaml", yaml_text)
        atomic_write_text(rules_dir / f"{name}.list", list_text)

    def test_upstream_failure_keeps_old_cache(self, config, monkeypatch):
        """上游全部失败：已有缓存的规则原样保留、记入 stale、状态 source=cache。"""
        rules_dir = config.rules_dir
        old_yaml, old_list = "payload:\n  - DOMAIN,old.example\n", "DOMAIN,old.example\n"
        self._seed(rules_dir, "Claude", old_yaml, old_list)
        monkeypatch.setattr(rulesync, "fetch_bytes", _fail_all)

        report = sync_rules(config)

        assert "Claude" in report.stale and "Claude" not in report.updated
        assert (rules_dir / "Claude.yaml").read_text(encoding="utf-8") == old_yaml
        assert (rules_dir / "Claude.list").read_text(encoding="utf-8") == old_list
        st = report.details["Claude"]
        assert st.ok is False and st.source == "cache"
        assert st.files == {"Claude.yaml": "cache", "Claude.list": "cache"}
        assert st.updated_at is not None          # 沿用旧文件的鲜度（mtime）
        assert (rules_dir / "state.json").exists()

    def test_repeated_failure_freshness_not_refreshed(self, config, monkeypatch):
        """连续失败：stale 规则的 updated_at 保持首次成功时间，不随失败刷新。"""
        rules_dir = config.rules_dir
        self._seed(rules_dir, "Claude", "payload:\n  - DOMAIN,old.example\n", "DOMAIN,old.example\n")
        monkeypatch.setattr(rulesync, "fetch_bytes", _fail_all)
        first = sync_rules(config)
        first_at = first.details["Claude"].updated_at
        second = sync_rules(config)
        assert second.details["Claude"].updated_at == first_at

    def test_partial_failure_per_format(self, config, monkeypatch):
        """单格式失败：成功的格式照常刷新，失败的格式沿用旧缓存，规则记 stale。"""
        rules_dir = config.rules_dir
        old_list = "DOMAIN,old.example\n"
        rules_dir.mkdir(parents=True, exist_ok=True)
        atomic_write_text(rules_dir / "Claude.list", old_list)

        def fake_fetch(url: str, **_kw):
            if url.endswith("/rule/Clash/Claude/Claude.yaml"):
                return b"payload:\n  - DOMAIN,upstream.example\n"
            return None                                  # SR 格式拉取失败

        monkeypatch.setattr(rulesync, "fetch_bytes", fake_fetch)
        report = sync_rules(config)

        assert "Claude" in report.stale
        st = report.details["Claude"]
        assert st.files == {"Claude.yaml": "fresh", "Claude.list": "cache"}
        assert st.ok is False and st.source == "cache"
        assert "upstream.example" in (rules_dir / "Claude.yaml").read_text(encoding="utf-8")
        assert (rules_dir / "Claude.list").read_text(encoding="utf-8") == old_list

    def test_failed_never_falls_back_to_baseline(self, config, monkeypatch):
        """无缓存且上游失败：回退内置基线内容，记入 failed_never、source=baseline。"""
        monkeypatch.setattr(rulesync, "fetch_bytes", _fail_all)
        report = sync_rules(config)

        assert "Claude" in report.failed_never
        st = report.details["Claude"]
        assert st.ok is False and st.source == "baseline"
        assert st.updated_at is not None
        assert (config.rules_dir / "Claude.yaml").read_text(encoding="utf-8") == \
            (BASELINE_DIR / "Claude.yaml").read_text(encoding="utf-8")

    def test_failed_never_without_any_fallback_writes_placeholder(self, config, monkeypatch):
        """缓存/基线全缺：写空规则占位（合法空 payload），source=none，避免端点 404。"""
        rules_dir = config.rules_dir
        monkeypatch.setattr(rulesync, "fetch_bytes", _fail_all)
        entry = {
            "name": "Nope", "category": "测试", "source": "blackmatrix7",
            "upstream_file": "Nope", "clash_file": "Nope.yaml", "sr_file": "Nope.list",
            "policy": "DIRECT", "behavior": "classical",
        }
        report = sync_rules(config, manifest=[entry])

        assert "Nope" in report.failed_never
        st = report.details["Nope"]
        assert st.source == "none" and st.files == {"Nope.yaml": "missing", "Nope.list": "missing"}
        assert "payload: []" in (rules_dir / "Nope.yaml").read_text(encoding="utf-8")

    def test_placeholder_rule_files_detection(self, config):
        """占位文件探测（pipeline 拒绝发布的安全网数据源）：只认 yaml/list 占位内容。"""
        rules_dir = config.rules_dir
        assert placeholder_rule_files(config) == []
        atomic_write_text(rules_dir / "Nope.yaml", rulesync._placeholder_text("Nope.yaml"))
        atomic_write_text(rules_dir / "Nope.list", rulesync._placeholder_text("Nope.list"))
        atomic_write_text(rules_dir / "Real.list", "DOMAIN,real.example\n")
        atomic_write_text(rules_dir / "state.json", "{}")
        assert placeholder_rule_files(config) == ["Nope.list", "Nope.yaml"]

    def test_sync_never_raises_on_manifest_error(self, config, monkeypatch):
        """清单不可读等意外：不抛异常，返回空报告（docs/01 安全网）。"""
        def boom(*_a, **_kw):
            raise RuntimeError("清单损坏")

        monkeypatch.setattr(rulesync, "read_manifest", boom)
        report = sync_rules(config)
        assert isinstance(report, RulesSyncReport)
        assert report.updated == [] and report.checked_at

    def test_upstream_success_writes_files_and_state(self, config, monkeypatch):
        """上游成功：双格式原子落盘（文件名与 manifest 一致）、状态 ok/source/updated_at 正确。"""
        def fake_fetch(url: str, **_kw):
            if url.endswith("/rule/Clash/Claude/Claude.yaml"):
                return b"payload:\n  - DOMAIN,upstream.example\n"
            if url.endswith("/rule/Shadowrocket/Claude/Claude.list"):
                return b"# upstream\nDOMAIN,upstream.example\n"
            return None

        monkeypatch.setattr(rulesync, "fetch_bytes", fake_fetch)
        report = sync_rules(config)

        entries = _entries_by_name()
        assert "Claude" in report.updated
        assert (config.rules_dir / entries["Claude"]["clash_file"]).read_text(encoding="utf-8") \
            == "payload:\n  - DOMAIN,upstream.example\n"
        assert (config.rules_dir / entries["Claude"]["sr_file"]).read_text(encoding="utf-8") \
            == "# upstream\nDOMAIN,upstream.example\n"
        st = report.details["Claude"]
        assert st.ok is True and st.source == "upstream" and st.updated_at == report.checked_at
        assert report.last_success_at == report.checked_at
        # 失败的其余规则归入 stale/failed_never，三类列表互斥且覆盖全部条目
        all_names = set(entries)
        listed = set(report.updated) | set(report.stale) | set(report.failed_never)
        assert listed == all_names
        assert not (set(report.updated) & set(report.stale))
        assert not (set(report.updated) & set(report.failed_never))


# ---------------------------------------------------------------- manifest 与 URL 拼装

class TestManifestAndUrls:
    def test_rule_chain_order_preserved(self):
        """默认清单的规则顺序 = docs/02 §3 规则链顺序（不得重排）。"""
        names = [str(e["name"]) for e in resolve_entries()]
        assert names == EXPECTED_CHAIN

    def test_blackmatrix7_default_urls(self):
        sources = read_manifest()["sources"]
        clash, sr = upstream_urls({"source": "blackmatrix7", "upstream_file": "Claude"}, sources)
        assert clash == "https://raw.githubusercontent.com/blackmatrix7/ios_rule_script/master/rule/Clash/Claude/Claude.yaml"
        assert sr == "https://raw.githubusercontent.com/blackmatrix7/ios_rule_script/master/rule/Shadowrocket/Claude/Claude.list"

    def test_blackmatrix7_variant_override_urls(self):
        """残缺桩变体校准：Netflix 的 Clash 端取 _Classical，SR 端取默认 .list。"""
        sources = read_manifest()["sources"]
        entry = {
            "source": "blackmatrix7", "upstream_file": "Netflix",
            "upstream_clash": "Netflix_Classical",
        }
        clash, sr = upstream_urls(entry, sources)
        assert clash.endswith("/rule/Clash/Netflix/Netflix_Classical.yaml")
        assert sr.endswith("/rule/Shadowrocket/Netflix/Netflix.list")

    def test_loyalsoldier_and_builtin_urls(self):
        sources = read_manifest()["sources"]
        clash, sr = upstream_urls({"source": "loyalsoldier", "upstream_file": "cncidr.txt"}, sources)
        assert clash == "https://raw.githubusercontent.com/Loyalsoldier/clash-rules/release/cncidr.txt"
        assert sr is None                      # 单 txt 来源，rulesync 转换出双格式
        assert upstream_urls({"source": "builtin"}, sources) == (None, None)

    def test_real_manifest_entry_urls(self):
        """用真实 manifest 条目拼装：ChinaMax 应指到完整的 _Domain 变体。"""
        sources = read_manifest()["sources"]
        entry = _entries_by_name()["ChinaMax"]
        clash, sr = upstream_urls(entry, sources)
        assert clash.endswith("/rule/Clash/ChinaMax/ChinaMax_Domain.yaml")
        assert sr.endswith("/rule/Shadowrocket/ChinaMax/ChinaMax_Domain.list")

    def test_resolve_entries_backfills_from_manifest(self):
        """templater.RuleEntry 形态（无 source/upstream_file）按 name 从清单回填。"""
        @dataclass
        class FakeRuleEntry:
            name: str
            category: str
            policy: str
            behavior: str
            clash_file: str
            sr_file: str

        known = FakeRuleEntry("Claude", "Claude（上游）", "🛑 Claude 专用", "classical",
                              "Claude.yaml", "Claude.list")
        resolved = resolve_entries([known])[0]
        assert resolved["source"] == "blackmatrix7"          # 回填自仓库清单
        assert resolved["upstream_file"] == "Claude"
        assert resolved["clash_file"] == "Claude.yaml"       # 传入字段优先保留

        unknown = FakeRuleEntry("Unknown", "t", "p", "classical", "Unknown.yaml", "Unknown.list")
        resolved2 = resolve_entries([unknown])[0]
        assert resolved2["source"] == "blackmatrix7"         # 清单外条目给默认值
        assert resolved2["upstream_file"] == "Unknown"


# ---------------------------------------------------------------- 状态读取与换算

class TestStateAndHelpers:
    def test_load_sync_state_roundtrip(self, config, monkeypatch):
        assert load_sync_state(config) is None                  # 从未同步
        monkeypatch.setattr(rulesync, "fetch_bytes", _fail_all)
        report = sync_rules(config)
        state = load_sync_state(config)
        assert state is not None
        assert state.checked_at == report.checked_at
        assert state.updated == report.updated
        assert state.stale == report.stale
        assert state.failed_never == report.failed_never
        assert state.details["claude-extra"].source == "builtin"
        assert isinstance(state.details["claude-extra"], RuleStatus)
        # 损坏的状态文件 → None 而非抛错
        atomic_write_text(config.rules_dir / "state.json", "{broken json")
        assert load_sync_state(config) is None

    def test_hours_since(self):
        assert hours_since(None) is None
        assert hours_since("not-a-date") is None
        two_hours_ago = (datetime.now() - timedelta(hours=2)).isoformat(timespec="microseconds")
        h = hours_since(two_hours_ago)
        assert h is not None and 1.9 < h < 2.1

    def test_payload_yaml_to_list_conversion(self):
        raw = b"payload:\n  - '1.0.1.0/24'\n  - '10.0.0.0/8'\n  - '::/127'\n"
        text = payload_yaml_to_list(raw)
        lines = [l for l in text.splitlines() if l and not l.startswith("#")]
        assert lines == ["IP-CIDR,1.0.1.0/24", "IP-CIDR,10.0.0.0/8", "IP-CIDR6,::/127"]

    def test_sync_alias_matches_sync_rules(self, config, monkeypatch):
        monkeypatch.setattr(rulesync, "fetch_bytes", _fail_all)
        report = sync(config)
        assert isinstance(report, RulesSyncReport)
        assert "claude-extra" in report.updated          # builtin 离线可用


# ---------------------------------------------------------------- 真实网络（默认 skip）

@pytest.mark.skipif(
    os.environ.get("SUBHUB_TEST_NET", "") != "1",
    reason="真实网络拉取用例默认 skip；设 SUBHUB_TEST_NET=1 启用",
)
class TestRealNetwork:
    def test_real_sync(self, config, capsys):
        """真实拉取：claude-extra 必成；直连可达时至少一条上游规则刷新成功。"""
        report = sync_rules(config, timeout=30.0)
        assert report.details["claude-extra"].ok is True
        upstream_ok = sorted(n for n, s in report.details.items() if s.source == "upstream")
        with capsys.disabled():
            print(f"\n真实网络刷新成功 {len(upstream_ok)} 条：{upstream_ok}")
            print(f"沿用缓存 {report.stale}；基线兜底 {report.failed_never}")
        assert upstream_ok, "真实网络下应至少有一条上游规则刷新成功"
        state = load_sync_state(config)
        assert state is not None and state.updated
