# sub-hub — 机场订阅转换与配置分发枢纽

自研轻量 Python/FastAPI 容器，部署在 NAS（端口 8399）：输入多个机场订阅，自动完成
**抓取 → 解析 → 清洗分类（假节点过滤/地区识别/重名消歧）→ 模板渲染 → 校验 → 版本化发布**，
输出带完整 AI 分流、Claude 纯净分组、快速故障切换的 mihomo 与 SR 双端配置（五份产物），
并作为规则集的局域网分发源——客户端永远只从 NAS 拉配置和规则，不受上游被墙影响。

核心能力：

- **AI 精细分流**：Claude / OpenAI / Gemini / Copilot / Grok / Perplexity / Cursor 各自独立分组；
  🛑 Claude 专用组为 select 手动锁定 + 🧷 备援 fallback（90s），防 IP 跳变封号；
  两组仅收三星纯净度节点（claude_rank = 3，住宅/家宽/mobile 级出口；机房/代理标记/未检测一律排除；无数据时全量池兜底）；
- **GitHub 吞吐量优选**：🐱 GitHub 专属组 + 🏆 GitHub 优选组。Fastly 对机场出口做吞吐限速且按出口
  区分，延迟选路会精准把 GitHub 流量送进被限速出口（实测差 270 倍）——容器内置探测实例逐节点实测
  GitHub CDN 下载速度（每节点 5s，默认 6h 一轮，流量约 0.3~1GB），最快 top-N 存活节点自动组成
  fallback 优选组并置为默认出口，死亡自动顺延次快；NAS 测速、随配置下发，客户机零依赖；
- **游戏平台与下载链路校准**：🎮 游戏平台组复活（默认 DIRECT）：游戏九集规则（Epic/Riot/Blizzard/
  EA/Origin/Ubisoft/PlayStation/Xbox/Nintendo）+ Helldivers 2 联机域与进程钉组（进程规则仅 mihomo
  端生效）；Steam 官网系走代理、下载 CDN 钉直连；M-Team 站点走代理、tracker/announce 钉直连
  （保做种联通性）；UDP 联机建议客户端开 TUN；
- **直连修正集（direct-fix）**：NVIDIA / JetBrains / Adobe / Chrome 组件等厂商中国站
  「检测 + 下载」成对钉直连（只钉一半会断链），只收 NAS 直连实测可达者；配套
  `scripts/audit_dead_rules.py` 全链模拟排死条目（首轮清出 157 条被前位规则抢先而失效的条目）；
- **快速切换**：url-test/fallback 统一 interval 120s（关键组 90s）、tolerance 40ms、
  max-failed-times 3、timeout 3000ms、探活 `http://cp.cloudflare.com/generate_204`；
- **多机场合并**：节点合并、跨订阅重名自动追加「 [别名]」后缀、假节点（剩余流量/官网/套餐到期/
  推广下载类）过滤；
- **低倍率省流**：♻️ 常规自动只收低倍率节点（≤ max(`SUBHUB_AUTO_MAX_RATE`，默认 1.0，
  全库最低倍率)），常规流量不烧高价档；
- **规则自托管**：46 组规则集（blackmatrix7 等上游镜像 + 自维护补丁 claude-extra/futu-extra/
  steam-extra/mteam-*/telegram-extra/gemini-extra/snssdk-direct/direct-fix/helldivers-extra）
  定时镜像到容器缓存，永不失效——上游断更沿用旧缓存/内置基线；客户端 RULE-SET 全部指向 NAS，
  清单文件化于 `rules_manifest.yaml`，顺序即规则链顺序；
- **纯净度检测**：内置 mihomo 探测实例逐节点检测出口 IP 属性（ASN/住宅机房/代理信号），
  驱动 Claude 组推荐排序与「家宽→机房」告警；探测故障不影响分发主链路；
- **节点稳定性**：定时（默认 15 分钟）采样全部节点延迟/可达性并落库（保留 7 天），
  管理台「节点」页逐节点融合 24h 时间块条（绿=通畅 黄=通但慢 红=失败）与纯净度徽章，
  Claude 适配推荐 Top3 与采样汇总在表格上方，无需在多张卡片间人工比对；
- **采样自动驱动策略（免人工挑选）**：每次发布把 24h 稳定性摘要连同纯净度一起回灌渲染——
  连续失败判死的节点自动移出 url-test 自动选路组（组清空回退原成员），Claude 专用/备援组
  同评分内存活优先、判死沉底；GitHub 组按实测速度降序；无采样数据时行为不变；
- **外网更新（mirror 推送，默认关闭）**：产物变化自动推送到公网静态镜像（推荐 CF Workers+KV，
  带 URL 读取 token；连接层失败自动直连兜底重试，推送超时 180s），外网订阅以镜像为主、
  离线包应急；读取端部署说明见 [deploy/cf-worker](deploy/cf-worker/README.md)；
- **版本与回退**：`data/out/v<NNNN>/` 保留最近 5 版，校验不过拒绝发布、可一键回退。

设计文档见 [docs/](docs/)：[架构与数据流](docs/01-架构与数据流.md) ·
[转换引擎与分组模板](docs/02-转换引擎与分组模板.md) ·
[订阅管理与纯净度检测](docs/03-订阅管理与纯净度检测.md) ·
[客户端接入与验收](docs/04-客户端接入与验收.md) ·
[模块契约（实现者必读）](docs/INTERFACES.md)。

## 管理台

单页管理台（`http://<nas>:8399/`，无前端框架）按页签组织：

| 页签 | 内容 |
| --- | --- |
| 订阅 | 机场增删改/启停、流量与到期进度条、最近抓取状态、手动刷新 |
| 节点 | 节点总表（纯净度徽章 + 24h 稳定性时间块条 + Claude 适配推荐 Top3）、已过滤/解析异常折叠区、纯净度扫描与 GitHub 测速手动触发 |
| 配置 | 当前版本与节点数 diff 时间线、五份产物链接复制/二维码、双格式预览、离线包下载、一键回退 |
| 设置 | 镜像推送凭据分字段表单与推送状态概览、规则源健康状态 |

## 目录结构

```
app/                 应用代码（config/models/store/utils 共享层 + fetcher/parser/cleaner/
                     templater/validator/rulesync/purity/probe/ghspeed/pipeline/web/scheduler/mirror）
app/baseline_rules/  内置规则基线（首次部署离线可用）
app/templates/       mihomo YAML / SR 系渲染模板
app/web_static/      管理单页（无前端框架，页签式布局）
scripts/             运维工具：gh_probe.py（GitHub 优选应急重钉）、audit_dead_rules.py（规则死条目审计）等
deploy/              生产 compose 范例（compose.nas.yml）+ CF Worker 镜像读取端
rules_manifest.yaml  规则集清单（顺序即规则链顺序）
tests/               pytest 测试（临时数据目录隔离，不读写仓库根 data/）
data/                运行期数据（首次启动自动创建：SQLite/密钥/token/产物/规则缓存）
```

## 本地开发（Windows）

```bat
:: 1) 建虚拟环境并装依赖（Python 3.12+；本项目在 3.14 上验证）
python -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirements.txt

:: 2) 跑全量测试（自动用 pytest 临时目录，绝不写仓库根 data/）
.venv\Scripts\python.exe -m pytest tests -q

:: 3) 启动服务（数据目录默认 ./data，监听默认 127.0.0.1:8399；也可直接跑 start.bat）
set SUBHUB_PORT=18399
.venv\Scripts\python.exe -m app.main

:: 4) 验活后浏览器打开管理台
curl http://127.0.0.1:18399/api/health
```

常用环境变量（全部可缺省，见 `app/config.py` 模块 docstring）：
`SUBHUB_DATA_DIR` / `SUBHUB_HOST` / `SUBHUB_PORT` / `SUBHUB_NAS_HOST` /
`SUBHUB_REFRESH_MINUTES`(30) / `SUBHUB_MIRROR_HOURS`(6) / `SUBHUB_PURITY_HOUR`(4) /
`SUBHUB_HEALTH_MINUTES`(15，节点健康采样间隔，0=关闭) /
`SUBHUB_GHPROBE_HOURS`(6，GitHub 吞吐扫描间隔，0=关闭) / `SUBHUB_GHPROBE_SECONDS`(5) /
`SUBHUB_GHPROBE_TOP_N`(5，🏆 优选组收录数) / `SUBHUB_AUTO_MAX_RATE`(1.0，常规自动组倍率上限) /
`SUBHUB_RULES_PROXY`(规则上游直连失败时的代理回落) /
`SUBHUB_SKIP_ANYTLS`(开) / `SUBHUB_PROBE_CONTROLLER_PORT`(9095) / `SUBHUB_PROBE_MIXED_PORT`(9096)。

纯净度扫描、健康采样与 GitHub 吞吐测速共用内置 mihomo 探测实例（需 `mihomo` 二进制，
查 PATH 或 `SUBHUB_MIHOMO_PATH`）；缺失时扫描/采样标记「不可用」（节点总表显示未测/无采样），
策略回退为不融合稳定度、GitHub 组退回基础形状，其余功能不受影响。

## NAS 部署（/share/Container/sub-hub）

```bash
# 1) 代码放 NAS 惯例目录
#    /share/Container/sub-hub/{Dockerfile,docker-compose.yml,app/,rules_manifest.yaml,requirements.txt}

# 2) 构建并启动（mihomo 二进制构建时自动下载：加速镜像链优先、GitHub 直连垫底，
#    也可 --build-arg MIHOMO_URL=<完整地址> 覆盖；默认 amd64-compatible 构建，
#    ARM 机型加 --build-arg MIHOMO_ARCH=linux-arm64）
docker-compose up -d --build

# 3) 验活（宿主机上；容器内不留 curl/wget）
curl http://127.0.0.1:8399/api/health

# 4) 数据持久化在 /share/Container/sub-hub/data/（compose 已挂载 ./data:/app/data）
```

- 生产 compose 范例见 [deploy/compose.nas.yml](deploy/compose.nas.yml)：固定 `TZ: Asia/Shanghai`
  （容器默认 UTC，不设 TZ 落库时间戳与每日扫描时刻会漂移）；`HTTPS_PROXY` 仅供镜像推送使用
  （绕过 CF API 的 IPv4 DNS 污染，订阅抓取/探测/规则同步不受影响）。
- 本机 Windows 构建后也可 `docker save sub-hub:latest | ssh nas docker load` 直传镜像
  （沿用既有 movieclaw 流程 B 惯例）。部署前确认宿主 8399 未被占用：
  `ss -tlnp | grep 8399`。

## 客户端接入（摘要）

管理台「配置」页展示五条分发链接（token 已内嵌，可复制/扫码）：

| 客户端 | 订阅链接 | 说明 |
| --- | --- | --- |
| Clash Verge（Windows 本机） | `http://<nas>:8399/sub/<token>/clash.yaml` | mihomo 主版本，规则走 NAS 的 RULE-SET（6h 自动更新）；导入后代理页确认组齐全，手动锁定一次 Claude 节点 |
| SR（iOS，主推） | `http://<nas>:8399/sub/<token>/shadowrocket.yaml` | SR 主入口：Clash 兼容格式、规则全内联（原生 conf 无法表达 VLESS REALITY，已弃为主推）；anytls 需 SR ≥6.3，低版本已按开关自动跳过该类节点 |
| SR（iOS，兼容保留） | `…/shadowrocket.conf` | 原生 conf 产物，仅为兼容老习惯保留；REALITY 机场下不可用 |
| 离线自包含版（外网兜底） | `…/clash-offline.yaml`、`…/shadowrocket-offline.conf` | 规则全内联，本地导入后脱离 NAS 运行 |

规则集分发：`http://<nas>:8399/rules/<name>.yaml|.list`（配置内 RULE-SET 已自动指向）。
外网使用走「公网镜像推送（主）+ 离线包（保底）」；接入步骤、端到端验收清单与故障场景预期见
**[docs/04-客户端接入与验收](docs/04-客户端接入与验收.md)**。

## 安全说明

- **订阅凭据加密落盘**：订阅 URL 以 Fernet 密文存 SQLite，密钥 `data/secret.key`
  （0600，首次启动生成）；数据库文件本身不泄露明文 URL。
- **分发链接带随机 token**：`/sub/<32hex>/…`，token 首次启动自动生成（`data/token`），
  UI 可复制；token 校验失败一律 404。请勿外传完整订阅链接。
- **打码展示**：管理 API 中订阅 URL 仅显示尾 6 位；日志一律打码到 host，
  节点凭据字段（uuid/password 等）不进日志、不进管理 API 响应、不进 meta.json。
- **镜像推送默认关闭**：配置产物含全部节点凭证，推送到第三方（CF Workers+KV / GitHub 公库）
  属敏感动作，须在管理台「设置」页显式开启并知晓凭证告知义务（docs/04 §2）；
  推送凭据只存 NAS 本地 `data/mirror.json`，不进仓库、不进产物。
- **密钥与凭据不进配置产物**：产物与规则文件中不含任何订阅 URL、token 或节点凭据之外的敏感信息；
  探测实例控制面仅绑定 127.0.0.1。
