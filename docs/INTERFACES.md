# INTERFACES.md — sub-hub 模块契约（实现者必读）

> 骨架阶段产出并已验证的共享层：`app/config.py`、`app/models.py`、`app/store.py`、`app/utils.py`、
> `rules_manifest.yaml`、`tests/conftest.py` + `tests/fixtures/`。本文给出其余模块（fetcher / parser /
> cleaner / templater / validator / rulesync / purity / probe / pipeline / web / scheduler / mirror）
> 必须遵守的公开签名与调用关系。**签名精确到参数名与类型，集成阶段按此核对。**

## 0. 全局纪律（违者集成阶段打回）

1. **日志与代码不得打印完整订阅 URL**（用 `app.utils.mask_url_host` 打码到 host）与**节点凭据字段**
   （`Node.credentials`、Fernet 明文、`secret.key` 内容）。UI 展示订阅 URL 用 `mask_url_tail`（仅尾 6 位）。
2. UI 文案与日志文本用中文，代码标识符用英文。
3. 所有落盘写入用 `app.utils.atomic_write_text/bytes`（先 `.tmp` 再 `os.replace`），绝不直接 `write_text` 覆盖正被分发的文件。
4. 测试一律用 `tests/conftest.py` 的 `data_dir/config/store` fixture（tmp_path 隔离），不得读写仓库根的 `data/`。
5. 时间字符串一律 `app.utils.now_iso()`（ISO8601 含微秒，字符串比较即时序）。
6. 错误体统一 `{code, message}`；分发端点任何后端故障都不 5xx 空响应，永远返回上一份有效产物（docs/01 安全网）。
7. 分组语义、规则链顺序、测速参数以 docs/02 为准；不重排 `rules_manifest.yaml` 的 rules 顺序。

## 1. 调用关系总览

```
scheduler ──定时──▶ pipeline.run_full_pipeline ──▶ fetcher.fetch_subscription（逐启用订阅）
                 （web POST /api/refresh 同样调用）      │ FetchResult.content
                                                        ▼
                                                   parser.parse_payload ──▶ list[Node]
                                                        ▼
                                    cleaner.clean（滤假/分类/消歧，就地补 Node 字段）
                                                        ▼
                       store.replace_nodes（快照落盘）+templater.build_groups/render_*
                                                        ▼
                       validator.validate_* → check_consistency → publish（写 data/out/）
                                                        ▼
                       （内容变化且 mirror 已开启）mirror.push_current（失败不影响主链路）

rulesync.sync_rules ──定时/启动──▶ data/rules/*（永不失效：上游失败沿用旧缓存）
purity.scan ──定时/手动──▶ probe.ProbeInstance（内置 mihomo 探测实例）+ PurityProvider ──▶ store.save_purity_result
web.create_app ──▶ store / pipeline / rulesync / purity / mirror / utils（分发端点读 data/out/ 与 data/rules/）
```

## 2. 已就绪的共享层（已实现，勿改动；确需改动写进集成阶段的 deferred）

### 2.1 app/config.py

```python
@dataclass(frozen=True)
class AppConfig:
    data_dir: Path; host: str; port: int; nas_lan_host: str; token: str
    sub_refresh_minutes: int; rules_mirror_hours: int; purity_scan_hour: int
    mihomo_path: str; skip_anytls: bool
    probe_controller_port: int; probe_mixed_port: int; fetch_user_agent: str
    # 派生属性：cache_dir out_dir rules_dir probe_dir db_path secret_key_path
    #           token_path probe_config_path scheduler_state_path mirror_settings_path
    #           base_url（http://192.168.31.10:8399） sub_url_prefix（.../sub/<token>）

def load_config(env: Mapping[str, str] | None = None) -> AppConfig
```

- `load_config()` 按调用时环境变量构造；副作用：建 `data/{cache,out,rules,probe}`，首次生成 `data/token`（32 hex）。
- 环境变量见 `app/config.py` 模块 docstring（SUBHUB_DATA_DIR / HOST / PORT / NAS_HOST / REFRESH_MINUTES / MIRROR_HOURS / PURITY_HOUR / MIHOMO_PATH / SKIP_ANYTLS / PROBE_* / FETCH_UA）。
- **任何模块不得自行读环境变量**，一律从 `AppConfig` 取。

### 2.2 app/models.py

```python
class FetchStatus(str, Enum):  # OK="ok" INVALID="invalid" TIMEOUT="timeout" FAILED="failed" NEVER="never"
    label: str                 # 中文：成功/订阅失效/超时/失败/从未抓取

@dataclass
class Node:
    name: str; type: str; server: str; port: int; source_sub: str
    credentials: dict[str, Any]                 # 协议专有字段（敏感！）
    region: str | None; residential: bool; iplc: bool; rate: float
    filtered: bool; filter_reason: str | None; orig_name: str | None
    key: tuple[str, str]                        # property：(source_sub, orig_name or name)
    def to_clash_proxy(self) -> dict[str, Any]
    @classmethod
    def from_clash_proxy(cls, proxy: dict[str, Any], source_sub: str) -> Node

@dataclass
class Subscription:
    id: int; name: str; url: str; enabled: bool
    last_fetch_at: str | None; last_fetch_status: FetchStatus | None
    userinfo: dict[str, int] | None             # {upload, download, total, expire}

@dataclass
class PurityResult:
    node_name: str; source_sub: str; checked_at: str
    exit_ip: str | None; country: str | None; asn: str | None; org: str | None; isp: str | None
    ip_type: str | None                         # residential|datacenter|mobile|unknown
    hosting: bool | None; proxy: bool | None; mobile: bool | None
    claude_rank: int | None                     # 3=住宅首选 2=中小机房 1=大厂云 0=已标记代理
    raw: dict[str, Any] | None

@dataclass
class AttrChange:
    node_name: str; source_sub: str; prev: PurityResult; curr: PurityResult
    is_residential_lost: bool                   # property：家宽→机房

@dataclass
class ConfigVersion:
    version: int; created_at: str; node_count: int; content_hash: str
    diff_summary: dict[str, Any]                # {"added": [...], "removed": [...], "renamed": [[old,new], ...]}
    note: str | None
```

### 2.3 app/store.py（`Store(db_path, secret_key_path)`，线程安全：RLock + check_same_thread=False）

```python
    # 加解密
    def encrypt(self, plaintext: str) -> str
    def decrypt(self, ciphertext: str) -> str                    # 失败抛 ValueError
    # subscriptions（模型 url 为明文；落盘密文）
    def add_subscription(self, name: str, url: str, enabled: bool = True) -> int
    def get_subscription(self, sub_id: int) -> Subscription | None
    def find_subscription_by_name(self, name: str) -> Subscription | None
    def list_subscriptions(self) -> list[Subscription]
    def update_subscription(self, sub_id: int, *, name: str | None = None,
                            url: str | None = None, enabled: bool | None = None) -> None
    def delete_subscription(self, sub_id: int) -> None           # 连带清其 node_snapshots
    def set_fetch_status(self, sub_id: int, status: FetchStatus, when: datetime | None = None) -> None
    def set_userinfo(self, sub_id: int, userinfo: dict | None) -> None
    # node_snapshots（整批替换语义：每次刷新先 DELETE 该订阅再插入全部节点，含被滤节点）
    def replace_nodes(self, sub_name: str, nodes: list[Node], *, fetched_at: str | None = None) -> None
    def list_nodes(self, *, sub_name: str | None = None, region: str | None = None,
                   filtered: bool | None = None) -> list[Node]
    # purity_results（追加式）
    def save_purity_result(self, result: PurityResult) -> None
    def latest_purity_results(self) -> list[PurityResult]        # 每节点最新一条
    def purity_history(self, node_name: str, source_sub: str) -> list[PurityResult]
    def attribute_changes(self) -> list[AttrChange]              # 最近两次 ip_type 不同
    def close(self) -> None
```

### 2.4 app/utils.py

```python
def restrict_permissions(path: Path) -> None            # 0600 尽力而为（Windows 容错）
def atomic_write_text(path: Path, text: str, *, encoding: str = "utf-8") -> None
def atomic_write_bytes(path: Path, data: bytes) -> None
def write_json(path: Path, data: Any) -> None
def read_json(path: Path, default: Any = None) -> Any
def now_iso() -> str
def mask_url_host(url: str) -> str                      # https://host:port/…
def mask_url_tail(url: str) -> str                      # …尾6位（UI 展示）
def content_hash(data: str | bytes) -> str              # sha256 hex
# 版本目录：data/out/v<NNNN>（零填充4位）
def new_version_dir(out_dir: Path, version: int) -> Path
def list_versions(out_dir: Path) -> list[int]
def latest_version(out_dir: Path) -> int | None
def next_version(out_dir: Path) -> int
def version_dir(out_dir: Path, version: int) -> Path
def prune_versions(out_dir: Path, *, keep: int = 5) -> list[int]
```

### 2.5 rules_manifest.yaml（仓库根）

- `manifest_version: 1`；`sources`：blackmatrix7 Clash/SR 双格式 URL 模板、Loyalsoldier txt 模板、`builtin`（自维护 claude-extra）。
- `rules`：**列表顺序即规则链顺序**（docs/02 §3）。每项字段：`name/category/source/upstream_file/clash_file/sr_file/policy/behavior`，
  claude-extra 额外带 `domains` 列表（自维护域名钉死）。policy 里 TikTok/PrimeVideo/GitHub 落 `🚀 节点选择`/`📢 谷歌服务`
  （组清单无专属组，已在 manifest 注释说明）。

### 2.6 tests/（已就绪）

- `conftest.py` fixtures：`data_dir`（tmp_path + SUBHUB_DATA_DIR 环境变量）、`config`（load_config()）、
  `store`（临时目录 Store，自动 close）、`fixtures_dir`、`sub_a_path`、`sub_b_path`、`sub_uri_path`。
- `fixtures/sub_a.yaml`：14 条 proxies（真实 12：vless10/ss1/anytls1，其中美国 3=家宽+DMIT+x2、香港 2、
  台湾/日本/新加坡/韩国/英国/德国各 1、无地区特征 1；假节点 2：「剩余流量」「官网」）。
- `fixtures/sub_b.yaml`：4 条（与 sub_a 同名 1、日本家宽 1、anytls 1、「套餐到期」假节点 1）。
- `fixtures/sub_uri_base64.txt`：整文件 base64 → 3 行 URI（ss/vmess/vless 各 1），parser 兜底路径用。

## 3. 待实现模块契约

> 模块文件一律放 `app/<module>.py`；logger 一律 `logging.getLogger("subhub.<module>")`。

### 3.1 fetcher — `app/fetcher.py`（同步实现，httpx.Client；scheduler/pipeline 直接调用）

```python
@dataclass
class FetchResult:
    sub_id: int
    sub_name: str
    status: FetchStatus                # OK / INVALID(401/403或0节点) / TIMEOUT / FAILED
    content: bytes | None              # 原始响应体（Clash YAML 或 base64）
    userinfo: dict[str, int] | None    # subscription-userinfo 头解析结果
    error: str | None                  # 中文错误描述（不含完整 URL，用 mask_url_host）
    fetched_at: str                    # now_iso()

def fetch_subscription(sub: Subscription, *, config: AppConfig,
                       timeout: float = 20.0) -> FetchResult
def parse_userinfo_header(value: str | None) -> dict[str, int] | None
    # "upload=123; download=456; total=789; expire=1790000000" → dict；缺项忽略；无值返回 None

def save_cache(config: AppConfig, sub_name: str, content: bytes) -> Path
    # 写 data/cache/<sub_name>-<ts>.yaml，保留每订阅最近 3 份；返回路径。fetcher 成功后调用
```

- 请求头：`User-Agent: config.fetch_user_agent`（Clash UA，拿 YAML 直出）。
- **成功路径调用方契约**：`store.set_fetch_status(sub.id, FetchStatus.OK)`、`store.set_userinfo(sub.id, userinfo)`、
  `store.replace_nodes(...)` 由 pipeline 统一做；fetcher 只负责抓取+缓存+状态返回。失败也必须让调用方拿到
  FetchResult（不抛异常），HTTP 状态 401/403 → `INVALID`。

### 3.2 parser — `app/parser.py`

```python
def parse_payload(content: bytes, source_sub: str) -> list[Node]
    # 主路径：Clash YAML（proxies 列表，可用 Node.from_clash_proxy）；YAML 无 proxies 或解析失败
    # → 兜底：尝试 base64 解码后按行解析 URI。两者皆失败 → 返回 []（由上层判 0 节点）

def parse_clash_yaml(text: str, source_sub: str) -> list[Node]
def parse_base64_subscription(content: bytes, source_sub: str) -> list[Node]
def parse_proxy_uri(uri: str, source_sub: str) -> Node | None
    # 支持 ss://（SIP002 userinfo-b64 与整段 b64 两种）、vmess://（base64 JSON）、
    # vless:// trojan:// hysteria2:// tuic://；不认识的 scheme 返回 None 并 log warning
```

- 单条 URI 解析失败跳过并记录（不阻塞整批）。credentials 字段名与 Clash YAML 保持一致（uuid/password/cipher/tls/sni/flow/network/ws-opts…）。

### 3.3 cleaner — `app/cleaner.py`

```python
FAKE_NODE_PATTERNS: list[re.Pattern]   # docs/02 §1 黑名单：剩余流量|套餐到期|到期时间|重置|官网|官址|网址|续费|订阅|流量|expire|traffic|电报|频道|群|tg|telegram（大小写/emoji 容错）
REGION_RULES: dict[str, RegionRule]    # code → RegionRule(code, name_zh, pattern)；三级匹配：国旗 emoji→中文→英文缩写；dict 可被 UI 后续扩充

@dataclass
class CleanResult:
    kept: list[Node]                   # 真实节点（已分类+消歧）
    filtered: list[Node]               # 被滤节点（filtered=True + filter_reason=命中关键词）

def clean(nodes: list[Node]) -> CleanResult
    # 依次调 filter_fake_nodes → classify → disambiguate

def filter_fake_nodes(nodes: list[Node]) -> list[Node]   # 命中者置 filtered/filter_reason 后从返回列表剔除
def classify(nodes: list[Node]) -> list[Node]            # 就地填 region/residential/iplc/rate（rate: x2→2.0）
def disambiguate(nodes: list[Node]) -> list[Node]        # 跨订阅同名：name 改为「原名 [source_sub]」，
                                                         # orig_name 记原名；返回含全部订阅的合并列表
```

- `clean` 的输入是**多订阅合并后的全量节点**（pipeline 负责合并）；disambiguate 依赖合并后的上下文。

### 3.4 templater — `app/templater.py`

```python
@dataclass
class RuleEntry:                       # rules_manifest.yaml 单项的内存形态
    name: str; category: str; policy: str; behavior: str
    clash_file: str; sr_file: str

def load_rules_manifest(path: Path | None = None) -> list[RuleEntry]
    # 默认读仓库根 rules_manifest.yaml，按文件顺序返回

def build_groups(nodes: list[Node], *, config: AppConfig,
                 purity: list[PurityResult] | None = None,
                 stability: list[dict] | None = None,
                 region_presence_nodes: list[Node] | None = None) -> list[dict]
    # docs/02 §2 组清单：无节点的地区组不生成；返回 mihomo proxy-groups 原生 dict 结构
    # 测速参数统一：url=http://cp.cloudflare.com/generate_204, interval=120, tolerance=40,
    # max-failed-times=3, timeout=3000；🧷 Claude 备援 interval=90 且 lazy=false
    # purity：Claude 专用/备援准入（claude_rank≥3）与「评分降序、住宅恒在机房前」排序
    # stability：health.stability_index 摘要 dict 列表——判死节点从 url-test 组剔除
    # （组清空回退原成员）、Claude 组同评分内存活优先/判死沉底；无数据时行为不变

def render_clash(nodes: list[Node], *, config: AppConfig, rules: list[RuleEntry],
                 offline: bool = False, purity: list[PurityResult] | None = None,
                 stability: list[dict] | None = None) -> str
    # 主版本 rule-providers 指 http://<nas>:8399/rules/<clash_file>；offline=True 时
    # type: inline + payload 内联（data/rules/ 现有内容）。全局段按 docs/02 §4。

def render_sr_conf(nodes: list[Node], *, config: AppConfig, rules: list[RuleEntry],
                   offline: bool = False, purity: list[PurityResult] | None = None,
                   stability: list[dict] | None = None) -> str
    # [Proxy] [Proxy Group] [Rule] 三段；组与 mihomo 同名同语义；
    # config.skip_anytls=True 时跳过 anytls 节点（参数默认开）。
    # offline=True 时 .list 内容直接展开进 [Rule]。
```

- jinja2 模板放 `app/templates/`（本模块私有）；Claude 专用组静态排序：`美国家宽 → 其他美国 → 香港/新加坡家宽 → 其余`。

### 3.5 validator — `app/validator.py`

```python
def validate_clash_yaml(text: str) -> list[str]        # pyyaml 解析 + proxies/proxy-groups/rules 结构检查；空=通过
def validate_sr_conf(text: str) -> list[str]           # 三段式结构 + [Proxy] 行数抽查
def check_consistency(clash_text: str, sr_text: str) -> list[str]
    # 回读两产物：组数、组名集合、规则条目（name+policy 序列）必须等价；不等价返回差异描述列表
```

**发布契约（唯一写 data/out/ 的入口）**：

```python
def publish(config: AppConfig, store: Store, artifacts: dict[str, str],
            nodes: list[Node], *, note: str | None = None) -> ConfigVersion
    # artifacts 键固定为：
    #   "clash.yaml" | "shadowrocket.conf" | "shadowrocket.yaml" | "clash-offline.yaml" | "shadowrocket-offline.conf"
    # 流程：5 份各自 validate_* → check_consistency(主两份) → 任一失败抛 PublishError(list[str])
    #   → new_version_dir(next_version) 原子写入 5 份 + meta.json（ConfigVersion 序列化）
    #   → content_hash = sha256(5 份按序拼接) → prune_versions(keep=5) → 返回 ConfigVersion

class PublishError(Exception):
    def __init__(self, errors: list[str]) -> None: ...   # .errors 属性
```

- 节点数 diff 摘要：publish 内部对比上一版 meta.json 的节点名集合（added/removed/renamed），写进 `diff_summary`。

### 3.6 rulesync — `app/rulesync.py`

```python
@dataclass
class RulesSyncReport:
    updated: list[str]                 # 本次成功刷新的规则名
    stale: list[str]                   # 沿用旧缓存的规则名（上游失败——永不失效）
    failed_never: list[str]            # 无缓存且上游失败的规则名（仅内置基线兜底）
    last_success_at: str | None
    checked_at: str

def sync_rules(config: AppConfig, *, manifest: list[RuleEntry] | None = None,
               timeout: float = 30.0) -> RulesSyncReport
    # 逐条下载 source 对应双格式 → data/rules/<clash_file>|<sr_file>（原子写）
    # 单条失败：保留旧文件、记入 stale；builtin 条目从 app/baseline_rules/ 复制（离线兜底）
    # 状态写 data/rules/state.json（供 UI「规则源 N 小时未更新」）

def load_sync_state(config: AppConfig) -> RulesSyncReport | None
```

- 内置基线目录 `app/baseline_rules/`（本模块所有）至少含 claude-extra 双格式，其余尽力预置。

### 3.7 probe — `app/probe.py`（内置 mihomo 探测实例）

```python
class ProbeInstance:
    def __init__(self, config: AppConfig, nodes: list[Node]) -> None
    def start(self) -> bool
        # 写 config.probe_config_path（external-controller: 127.0.0.1:<probe_controller_port>，
        # mixed-port 绑 127.0.0.1:<probe_mixed_port>，仅容器/本机内），Popen(config.mihomo_path, "-f", ...)
        # 探活控制 API 直到就绪；失败返回 False（不抛异常，主链路无感）
    def stop(self) -> None
    def is_available(self) -> bool
    def select(self, node_name: str) -> bool
        # PUT /proxies/<selector名> body {"name": node_name}，把全局出口切到该节点
    def local_proxy_url(self) -> str        # http://127.0.0.1:<probe_mixed_port>
    def delay(self, node_name: str) -> int | None
        # GET /proxies/<name>/delay?timeout=3000&url=<cloudflare 204>；不可用返回 None
```

- 端口约定：controller 9095、mixed 9096（均来自 AppConfig，测试可覆盖）。崩溃后由下轮调度重建。

### 3.8 purity — `app/purity.py`

```python
class PurityProvider(Protocol):
    def lookup(self, *, proxy_url: str | None = None) -> dict: ...
        # proxy_url=None 直连；否则经该代理请求并返回原始 JSON dict（含 exit ip 与属性信号）

class IpApiProvider:                    # 默认实现，http://ip-api.com/json/?fields=status,country,as,asname,org,isp,proxy,hosting,mobile
    def __init__(self, *, timeout: float = 10.0, min_interval: float = 1.4) -> None
        # 免费额度 45 req/min → 内置限速（min_interval 秒/次）；429 时退避重试一次
    def lookup(self, *, proxy_url: str | None = None) -> dict

@dataclass
class PurityReport:
    results: list[PurityResult]
    unavailable: bool                   # 探测实例启动失败/中途崩溃 → True，主链路无感
    checked: int
    skipped: int                        # full=False 时未测的已有结果节点数

def claude_rank_result(result: PurityResult, node: Node | None) -> int
    # docs/03 §4：住宅 3 / 中小机房 2 / 大厂云 ASN 集合 1 / proxy=true 0

def scan(nodes: list[Node], *, config: AppConfig, store: Store,
         provider: PurityProvider | None = None, full: bool = False) -> PurityReport
    # full=False 只测「无结果或已失效」节点（增量）；逐节点：probe.select → probe.local_proxy_url
    # 经代理 lookup → 组装 PurityResult → store.save_purity_result；异常节点跳过计数

def claude_recommendations(latest: list[PurityResult], nodes: list[Node]) -> list[PurityResult]
    # Claude 专用组推荐 Top3（claude_rank 降序，住宅恒在机房前；无数据时按 docs/02 静态排序回退）
```

### 3.9 pipeline — `app/pipeline.py`（编排核心，变更类操作统一入口）

```python
@dataclass
class PipelineResult:
    published: bool                     # False=校验拦截/0 节点，沿用上一版
    version: int | None
    node_count: int
    filtered_count: int
    stale_subs: list[str]               # 本次抓取失效/超时但仍用旧快照的订阅别名
    errors: list[str]                   # 中文错误（含 PublishError.errors）

def run_full_pipeline(config: AppConfig, store: Store, *,
                      sub_id: int | None = None) -> PipelineResult
    # 完整链路：抓取（sub_id=None 全部启用订阅）→ parser → 合并 → cleaner.clean
    # → store.replace_nodes（每订阅，含被滤节点）→ templater 渲染 5 份
    # → validator.publish（0 有效节点时拒绝发布，PipelineResult.published=False）
    # → 新节点增量纯净度扫描钩子（try/except 包裹，失败不影响发布）
    # → mirror 已开启则 push_current（同样吞异常）

def rollback_to_version(config: AppConfig, store: Store, version: int) -> ConfigVersion
    # 把 data/out/v<version>/5 份产物复制为新版本目录（meta.note="回退到 v<version>"）
```

### 3.10 web — `app/web.py` + `app/main.py`（装配）

```python
def create_app(config: AppConfig | None = None, store: Store | None = None) -> FastAPI
    # None 时 load_config() / Store(cfg.db_path, cfg.secret_key_path)
    # lifespan：启动 rulesync.sync_rules（best-effort）+ scheduler.create_scheduler().start()
    #           关停时 scheduler.shutdown()
    # app.state.config / app.state.store 挂载；UI 单页模板与静态文件放 app/webui/（本模块私有）
```

端点契约（路径/语义以 docs/03 §2 为准，全部实现）：

| 端点 | 语义要点 |
| --- | --- |
| `GET /api/health` | 存活 + scheduler 三任务上次执行状态（读 config.scheduler_state_path） |
| `GET/POST /api/subs`、`PATCH/DELETE /api/subs/{id}` | 列表 URL 用 mask_url_tail；变更后调 `run_full_pipeline` |
| `POST /api/refresh` | `{sub_id?: int}` → run_full_pipeline → 返回版本号 |
| `GET /api/nodes` | `?region=&tag=&sub_id=`；含属性、纯净度摘要、被滤原因 |
| `GET /api/purity/report`、`POST /api/purity/scan` | 报告按 Claude 适配度排序；attribute_changes 标红 |
| `GET /api/config/preview?fmt=clash\|sr`、`POST /api/config/rollback` | preview 纯文本；rollback → pipeline.rollback_to_version |
| `GET /sub/{token}/clash.yaml\|shadowrocket.conf\|shadowrocket.yaml\|clash-offline.yaml\|shadowrocket-offline.conf` | token 校验；附 `subscription-userinfo` 头（各启用订阅 userinfo 汇总）；ETag=content_hash，304 支持；故障返回上一版产物 |
| `GET /rules/{file}` | data/rules/ 缓存（.yaml/.list），防目录穿越 |
| `GET/POST /api/mirror`、`POST /api/mirror/push` | mirror.json 读写 + push_current |
| `?qr` 任意分发端点 | 返回二维码扫码页（qrcode 库） |

`app/main.py`（thin 装配，不超过 30 行）：

```python
def main() -> None:
    cfg = load_config()
    store = Store(cfg.db_path, cfg.secret_key_path)
    uvicorn.run(create_app(config=cfg, store=store), host=cfg.host, port=cfg.port)

if __name__ == "__main__":
    main()
```

### 3.11 scheduler — `app/scheduler.py`

```python
def create_scheduler(config: AppConfig, store: Store) -> BackgroundScheduler
    # APScheduler BackgroundScheduler（fetcher/pipeline/rulesync 均为同步函数）
    # 四个 job（id 固定，供 /api/health 读取）：
    #   sub_refresh  interval minutes=config.sub_refresh_minutes → pipeline.run_full_pipeline(config, store)
    #   rules_mirror interval hours=config.rules_mirror_hours   → rulesync.sync_rules(config)
    #   purity_scan  cron hour=config.purity_scan_hour, minute=0 → purity.scan(全量)
    #   health_probe interval minutes=config.health_probe_minutes（0=不注册）→ health.sample_once
    # 每个 job 执行完把 {job_id: {last_run, last_status, last_error}} 合并写入
    # config.scheduler_state_path（utils.write_json）；错误不外抛
```

### 3.12 mirror — `app/mirror.py`（默认关闭的可选模块）

```python
@dataclass
class MirrorResult:
    ok: bool
    provider: str | None               # "cf-kv" | "github"
    url: str | None                    # 公网订阅 URL
    error: str | None
    pushed_at: str

def load_mirror_settings(config: AppConfig) -> dict     # data/mirror.json；缺省 {"enabled": False, ...}
def save_mirror_settings(config: AppConfig, settings: dict) -> None
def push_current(config: AppConfig) -> MirrorResult
    # 未启用 → ok=False, error="镜像未启用"；内容 hash 未变不推；
    # 推送失败不影响主链路（绝不抛出到 pipeline 外）
```

## 4. data/ 目录布局与读写权矩阵

```
data/
├── subhub.db              store 独占写
├── secret.key             store 首次生成（0600 尽力而为）
├── token                  config 首次生成（32 hex）
├── cache/<sub>-<ts>.yaml  fetcher 写（每订阅留 3 份），parser 不读（内存直传）
├── out/v<NNNN>/           validator.publish 独占写；web 分发端点与回退读
│   ├── clash.yaml  shadowrocket.conf  shadowrocket.yaml  clash-offline.yaml  shadowrocket-offline.conf
│   └── meta.json          ConfigVersion 序列化（validator 写，web/pipeline 读）
├── rules/<name>.yaml|.list  rulesync 写；web GET /rules/{file} 读；templater 离线内联读
├── rules/state.json       rulesync 写；web 读
├── probe/config.yaml      probe 写/读（探测实例专用，独立于分发产物）
├── scheduler_state.json   scheduler 写；web /api/health 读
└── mirror.json            mirror 读写；web /api/mirror 读
```

| 文件/目录 | 写者 | 读者 |
| --- | --- | --- |
| data/subhub.db | store | 所有模块经 Store 实例 |
| data/out/** | validator.publish / pipeline.rollback_to_version | web（分发/预览/回退）、pipeline |
| data/rules/** | rulesync | web（/rules 分发）、templater（离线内联） |
| data/cache/** | fetcher | 无（审计/排查用） |
| data/probe/** | probe | purity（经 probe 实例间接） |
| data/token、data/secret.key | config/store 启动时 | 相应模块内存持有，不进日志 |

## 5. 已拍板的实现决策（避免各实现者自行分叉）

1. **同步 httpx**：fetcher/rulesync/purity/mirror 全部用 `httpx.Client` 同步实现；FastAPI 端点内经
   `run_in_threadpool` 或直接调用，scheduler 用 `BackgroundScheduler`。不引入 asyncio 复杂度。
2. **探测实例选节点方式**：probe 配置内置一个 selector 组（如 `PROBE`），purity 通过 `ProbeInstance.select()`
   切换出口后经同一 mixed 端口发请求（mihomo 单实例无法按请求指定节点）。
3. **无专属组的规则集落点**：TikTok/PrimeVideo → `🚀 节点选择`；Grok/Perplexity/CursorAI → `🧠 通用 AI`；
   GitHub → `📢 谷歌服务`（均与 rules_manifest.yaml 的 policy 一致）。
4. **发布原子性**：版本目录内单文件用 utils 原子写；整批以「新目录先写完、meta.json 最后写」为发布点。
5. **纯净度结果键**：`(node_name, source_sub)`，node_name 为消歧后最终名；节点改名视为新节点重新检测。
6. **增删订阅后的 userinfo 头**：分发端点把各启用订阅 userinfo 的 upload/download 求和、total/expire 取最小非零值。

## 6. 骨架阶段验证记录（已执行）

- `.venv/Scripts/python.exe -m pip install -r requirements.txt` → 全部安装成功（Python 3.14.0）。
- `.venv/Scripts/python.exe -c "import app.models, app.store, app.config, app.utils"` → 无报错。
- Store/config/utils 冒烟脚本：订阅增改查+加解密+userinfo、节点快照读写、纯净度追加/最新/历史/属性变化、
  URL 打码、版本目录保留 5 版 —— 全部通过。
- fixtures 断言：sub_a 14 条（真实 12+假 2、vless10/ss3/anytls1、美国 3 含家宽/DMIT/x2、地区覆盖齐）、
  sub_b 4 条（同名/日本家宽/anytls/套餐到期）、base64 解码 3 行 ss/vmess/vless —— 全部通过。
