"""SQLite 存储层：三张主表 + Fernet 加解密 + userinfo/纯净度读写 + 属性变化对比。

表结构按 docs/03 §1：
- subscriptions     订阅（URL 以 Fernet 密文落盘）
- node_snapshots    每次刷新的节点快照（地区/属性/所属订阅/是否被滤）
- purity_results    纯净度检测结果（追加式，按 checked_at 取最新/历史对比）

线程安全：check_same_thread=False + 全部操作持 threading.RLock。
密钥：data/secret.key（Fernet key，0600 尽力而为，Windows 下尽力而为）。
安全纪律：任何日志不得输出订阅 URL 明文与节点凭据（打码见 app.utils）。
"""
from __future__ import annotations

import json
import logging
import sqlite3
import threading
from datetime import datetime
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken

from app.models import AttrChange, FetchStatus, HealthSample, Node, PurityResult, Subscription
from app.utils import atomic_write_text, now_iso, restrict_permissions

logger = logging.getLogger("subhub.store")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS subscriptions (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    name               TEXT    NOT NULL UNIQUE,
    url_enc            TEXT    NOT NULL,              -- Fernet 密文
    enabled            INTEGER NOT NULL DEFAULT 1,
    last_fetch_at      TEXT,
    last_fetch_status  TEXT,
    userinfo           TEXT                       -- JSON：{upload, download, total, expire}
);

CREATE TABLE IF NOT EXISTS node_snapshots (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    sub_name       TEXT    NOT NULL,               -- subscriptions.name
    name           TEXT    NOT NULL,               -- 消歧后的最终名
    orig_name      TEXT,                           -- 消歧前的原始名
    type           TEXT    NOT NULL,
    server         TEXT    NOT NULL,
    port           INTEGER NOT NULL,
    credentials    TEXT    NOT NULL DEFAULT '{}',  -- JSON（敏感，不外泄）
    region         TEXT,
    residential    INTEGER NOT NULL DEFAULT 0,
    iplc           INTEGER NOT NULL DEFAULT 0,
    rate           REAL    NOT NULL DEFAULT 1.0,
    filtered       INTEGER NOT NULL DEFAULT 0,
    filter_reason  TEXT,
    fetched_at     TEXT    NOT NULL                -- 该批快照时间（ISO8601）
);
CREATE INDEX IF NOT EXISTS idx_snapshots_sub ON node_snapshots(sub_name);
CREATE INDEX IF NOT EXISTS idx_snapshots_region ON node_snapshots(region);

CREATE TABLE IF NOT EXISTS purity_results (
    node_name    TEXT NOT NULL,
    source_sub   TEXT NOT NULL,
    checked_at   TEXT NOT NULL,
    exit_ip      TEXT,
    country      TEXT,
    asn          TEXT,
    org          TEXT,
    isp          TEXT,
    ip_type      TEXT,                              -- residential|datacenter|mobile|unknown
    hosting      INTEGER,                           -- 0/1/NULL
    proxy        INTEGER,
    mobile       INTEGER,
    claude_rank  INTEGER,                           -- 0..3 / NULL
    raw          TEXT,                              -- provider 原始 JSON
    PRIMARY KEY (node_name, source_sub, checked_at)
);
CREATE INDEX IF NOT EXISTS idx_purity_node ON purity_results(node_name, source_sub);

CREATE TABLE IF NOT EXISTS node_health_samples (
    node_name  TEXT NOT NULL,
    source_sub TEXT NOT NULL,
    checked_at TEXT NOT NULL,                      -- ISO8601（同一轮采样共用）
    delay_ms   INTEGER,                            -- NULL=该轮探活失败
    PRIMARY KEY (node_name, source_sub, checked_at)
);
CREATE INDEX IF NOT EXISTS idx_health_node_time
    ON node_health_samples(node_name, source_sub, checked_at);
"""

_PURITY_COLUMNS = (
    "node_name, source_sub, checked_at, exit_ip, country, asn, org, isp, "
    "ip_type, hosting, proxy, mobile, claude_rank, raw"
)


class Store:
    """订阅/节点快照/纯净度结果的唯一读写入口。"""

    def __init__(self, db_path: str | Path, secret_key_path: str | Path) -> None:
        self.db_path = Path(db_path)
        self.secret_key_path = Path(secret_key_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(str(self.db_path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.executescript(_SCHEMA)
            self._conn.commit()
        self._fernet = self._load_or_create_fernet()

    # ------------------------------------------------------------------ 加密

    def _load_or_create_fernet(self) -> Fernet:
        if self.secret_key_path.exists():
            key = self.secret_key_path.read_text(encoding="utf-8").strip()
            if not key:
                key = Fernet.generate_key().decode("ascii")
                atomic_write_text(self.secret_key_path, key + "\n")
                restrict_permissions(self.secret_key_path)
        else:
            key = Fernet.generate_key().decode("ascii")
            atomic_write_text(self.secret_key_path, key + "\n")
            restrict_permissions(self.secret_key_path)
            logger.info("首次初始化：已生成 Fernet 密钥文件")
        return Fernet(key.encode("ascii"))

    def encrypt(self, plaintext: str) -> str:
        """明文 → Fernet 密文（token 字符串）。"""
        return self._fernet.encrypt(plaintext.encode("utf-8")).decode("ascii")

    def decrypt(self, ciphertext: str) -> str:
        """Fernet 密文 → 明文；密钥不匹配/密文损坏抛 ValueError。"""
        try:
            return self._fernet.decrypt(ciphertext.encode("ascii")).decode("utf-8")
        except (InvalidToken, ValueError) as exc:
            raise ValueError("订阅 URL 解密失败：密钥不匹配或密文损坏") from exc

    # ------------------------------------------------------------------ subscriptions

    def add_subscription(self, name: str, url: str, enabled: bool = True) -> int:
        """新增订阅（URL 密文落盘），返回 id。重名抛 sqlite3.IntegrityError。"""
        with self._lock:
            cur = self._conn.execute(
                "INSERT INTO subscriptions (name, url_enc, enabled) VALUES (?, ?, ?)",
                (name, self.encrypt(url), int(enabled)),
            )
            self._conn.commit()
            return int(cur.lastrowid)

    def get_subscription(self, sub_id: int) -> Subscription | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM subscriptions WHERE id = ?", (sub_id,)
            ).fetchone()
        return self._sub_from_row(row) if row else None

    def find_subscription_by_name(self, name: str) -> Subscription | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM subscriptions WHERE name = ?", (name,)
            ).fetchone()
        return self._sub_from_row(row) if row else None

    def list_subscriptions(self) -> list[Subscription]:
        """全部订阅，按 id 升序；url 为解密后的明文（调用方负责打码展示）。"""
        with self._lock:
            rows = self._conn.execute("SELECT * FROM subscriptions ORDER BY id").fetchall()
        return [self._sub_from_row(r) for r in rows]

    def update_subscription(
        self,
        sub_id: int,
        *,
        name: str | None = None,
        url: str | None = None,
        enabled: bool | None = None,
    ) -> None:
        """按需更新别名/URL/启停；未提供的字段不动。"""
        sets: list[str] = []
        params: list[object] = []
        if name is not None:
            sets.append("name = ?")
            params.append(name)
        if url is not None:
            sets.append("url_enc = ?")
            params.append(self.encrypt(url))
        if enabled is not None:
            sets.append("enabled = ?")
            params.append(int(enabled))
        if not sets:
            return
        params.append(sub_id)
        with self._lock:
            self._conn.execute(f"UPDATE subscriptions SET {', '.join(sets)} WHERE id = ?", params)
            self._conn.commit()

    def delete_subscription(self, sub_id: int) -> None:
        """删除订阅并清空其节点快照（调用方随后重算配置）。"""
        with self._lock:
            self._conn.execute("DELETE FROM node_snapshots WHERE sub_name = (SELECT name FROM subscriptions WHERE id = ?)", (sub_id,))
            self._conn.execute("DELETE FROM subscriptions WHERE id = ?", (sub_id,))
            self._conn.commit()

    def set_fetch_status(
        self, sub_id: int, status: FetchStatus, when: datetime | None = None
    ) -> None:
        """记录最近一次抓取状态与时间（fetcher 调用）。"""
        at = (when or datetime.now()).isoformat(timespec="microseconds")
        with self._lock:
            self._conn.execute(
                "UPDATE subscriptions SET last_fetch_status = ?, last_fetch_at = ? WHERE id = ?",
                (status.value, at, sub_id),
            )
            self._conn.commit()

    def set_userinfo(self, sub_id: int, userinfo: dict | None) -> None:
        """写入 subscription-userinfo 解析结果（fetcher 调用）；None 清空。"""
        payload = None if userinfo is None else json.dumps(userinfo, ensure_ascii=False)
        with self._lock:
            self._conn.execute(
                "UPDATE subscriptions SET userinfo = ? WHERE id = ?", (payload, sub_id)
            )
            self._conn.commit()

    def _sub_from_row(self, row: sqlite3.Row) -> Subscription:
        userinfo = json.loads(row["userinfo"]) if row["userinfo"] else None
        return Subscription(
            id=int(row["id"]),
            name=row["name"],
            url=self.decrypt(row["url_enc"]),
            enabled=bool(row["enabled"]),
            last_fetch_at=row["last_fetch_at"],
            last_fetch_status=FetchStatus(row["last_fetch_status"]) if row["last_fetch_status"] else None,
            userinfo=userinfo,
        )

    # ------------------------------------------------------------------ node_snapshots

    def replace_nodes(self, sub_name: str, nodes: list[Node], *, fetched_at: str | None = None) -> None:
        """整批替换某订阅的节点快照（每次刷新调用；解析/清洗后传入全部节点，
        含被滤节点——filtered=1 + filter_reason）。"""
        at = fetched_at or now_iso()
        rows = [
            (
                n.source_sub or sub_name,
                n.name,
                n.orig_name,
                n.type,
                n.server,
                int(n.port),
                _dump_credentials(n.credentials),
                n.region,
                int(n.residential),
                int(n.iplc),
                float(n.rate),
                int(n.filtered),
                n.filter_reason,
                at,
            )
            for n in nodes
        ]
        with self._lock:
            self._conn.execute("DELETE FROM node_snapshots WHERE sub_name = ?", (sub_name,))
            self._conn.executemany(
                "INSERT INTO node_snapshots (sub_name, name, orig_name, type, server, port, "
                "credentials, region, residential, iplc, rate, filtered, filter_reason, fetched_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                rows,
            )
            self._conn.commit()

    def list_nodes(
        self,
        *,
        sub_name: str | None = None,
        region: str | None = None,
        filtered: bool | None = None,
    ) -> list[Node]:
        """按订阅/地区/是否被滤过滤节点快照（UI 与 pipeline 用）。"""
        sql = "SELECT * FROM node_snapshots"
        conds: list[str] = []
        params: list[object] = []
        if sub_name is not None:
            conds.append("sub_name = ?")
            params.append(sub_name)
        if region is not None:
            conds.append("region = ?")
            params.append(region)
        if filtered is not None:
            conds.append("filtered = ?")
            params.append(int(filtered))
        if conds:
            sql += " WHERE " + " AND ".join(conds)
        sql += " ORDER BY id"
        with self._lock:
            rows = self._conn.execute(sql, params).fetchall()
        return [self._node_from_row(r) for r in rows]

    @staticmethod
    def _node_from_row(row: sqlite3.Row) -> Node:
        return Node(
            name=row["name"],
            type=row["type"],
            server=row["server"],
            port=int(row["port"]),
            source_sub=row["sub_name"],
            credentials=json.loads(row["credentials"] or "{}"),
            region=row["region"],
            residential=bool(row["residential"]),
            iplc=bool(row["iplc"]),
            rate=float(row["rate"]),
            filtered=bool(row["filtered"]),
            filter_reason=row["filter_reason"],
            orig_name=row["orig_name"],
        )

    # ------------------------------------------------------------------ purity_results

    def save_purity_result(self, result: PurityResult) -> None:
        """追加一条检测结果（同节点同时刻则覆盖）。"""
        with self._lock:
            self._conn.execute(
                f"INSERT OR REPLACE INTO purity_results ({_PURITY_COLUMNS}) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    result.node_name,
                    result.source_sub,
                    result.checked_at,
                    result.exit_ip,
                    result.country,
                    result.asn,
                    result.org,
                    result.isp,
                    result.ip_type,
                    _bool_to_int(result.hosting),
                    _bool_to_int(result.proxy),
                    _bool_to_int(result.mobile),
                    result.claude_rank,
                    _dump_json(result.raw),
                ),
            )
            self._conn.commit()

    def latest_purity_results(self) -> list[PurityResult]:
        """每节点（node_name+source_sub）最新一条结果（报告页与 Claude 排序用）。"""
        sql = (
            f"SELECT {_PURITY_COLUMNS} FROM ("
            "  SELECT *, ROW_NUMBER() OVER ("
            "    PARTITION BY node_name, source_sub ORDER BY checked_at DESC"
            "  ) AS rn FROM purity_results"
            ") WHERE rn = 1 ORDER BY node_name"
        )
        with self._lock:
            rows = self._conn.execute(sql).fetchall()
        return [self._purity_from_row(r) for r in rows]

    def purity_history(self, node_name: str, source_sub: str) -> list[PurityResult]:
        """单节点全部历史结果（时间升序）。"""
        with self._lock:
            rows = self._conn.execute(
                f"SELECT {_PURITY_COLUMNS} FROM purity_results "
                "WHERE node_name = ? AND source_sub = ? ORDER BY checked_at ASC",
                (node_name, source_sub),
            ).fetchall()
        return [self._purity_from_row(r) for r in rows]

    def attribute_changes(self) -> list[AttrChange]:
        """取每节点最近两次检测，返回 ip_type 发生变化的对比（家宽→机房告警用）。"""
        sql = (
            f"SELECT {_PURITY_COLUMNS} FROM ("
            "  SELECT *, ROW_NUMBER() OVER ("
            "    PARTITION BY node_name, source_sub ORDER BY checked_at DESC"
            "  ) AS rn FROM purity_results"
            ") WHERE rn <= 2 ORDER BY node_name, source_sub, checked_at"
        )
        with self._lock:
            rows = self._conn.execute(sql).fetchall()
        grouped: dict[tuple[str, str], list[PurityResult]] = {}
        for r in rows:
            grouped.setdefault((r["node_name"], r["source_sub"]), []).append(
                self._purity_from_row(r)
            )
        changes: list[AttrChange] = []
        for (node_name, source_sub), pair in grouped.items():
            if len(pair) == 2 and pair[0].ip_type != pair[1].ip_type:
                changes.append(AttrChange(node_name=node_name, source_sub=source_sub, prev=pair[0], curr=pair[1]))
        return changes

    @staticmethod
    def _purity_from_row(row: sqlite3.Row) -> PurityResult:
        return PurityResult(
            node_name=row["node_name"],
            source_sub=row["source_sub"],
            checked_at=row["checked_at"],
            exit_ip=row["exit_ip"],
            country=row["country"],
            asn=row["asn"],
            org=row["org"],
            isp=row["isp"],
            ip_type=row["ip_type"],
            hosting=_int_to_bool(row["hosting"]),
            proxy=_int_to_bool(row["proxy"]),
            mobile=_int_to_bool(row["mobile"]),
            claude_rank=row["claude_rank"],
            raw=json.loads(row["raw"]) if row["raw"] else None,
        )

    # ------------------------------------------------------------------ node_health_samples

    def save_health_samples(self, samples: list[HealthSample]) -> None:
        """批量写入一轮健康采样（同轮共用 checked_at；失败样本 delay_ms=NULL）。"""
        with self._lock:
            self._conn.executemany(
                "INSERT OR REPLACE INTO node_health_samples"
                " (node_name, source_sub, checked_at, delay_ms) VALUES (?, ?, ?, ?)",
                [(s.node_name, s.source_sub, s.checked_at, s.delay_ms) for s in samples],
            )
            self._conn.commit()

    def list_health_samples(self, *, since: str) -> list[HealthSample]:
        """窗口内（checked_at ≥ since）的全部健康样本，按时间升序。"""
        with self._lock:
            rows = self._conn.execute(
                "SELECT node_name, source_sub, checked_at, delay_ms"
                " FROM node_health_samples WHERE checked_at >= ?"
                " ORDER BY checked_at ASC",
                (since,),
            ).fetchall()
        return [HealthSample(
            node_name=r["node_name"], source_sub=r["source_sub"],
            checked_at=r["checked_at"], delay_ms=r["delay_ms"],
        ) for r in rows]

    def prune_health_samples(self, *, before: str) -> int:
        """清理保留窗口之外的过期样本，返回删除行数（每轮采样后调用）。"""
        with self._lock:
            cur = self._conn.execute(
                "DELETE FROM node_health_samples WHERE checked_at < ?", (before,))
            self._conn.commit()
        return cur.rowcount

    # ------------------------------------------------------------------

    def close(self) -> None:
        with self._lock:
            self._conn.commit()
            self._conn.close()


def _dump_credentials(credentials: dict) -> str:
    return json.dumps(credentials, ensure_ascii=False)


def _dump_json(data: dict | None) -> str | None:
    return None if data is None else json.dumps(data, ensure_ascii=False)


def _bool_to_int(value: bool | None) -> int | None:
    return None if value is None else int(value)


def _int_to_bool(value: object) -> bool | None:
    return None if value is None else bool(value)
