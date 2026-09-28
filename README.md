# sub-hub — 机场订阅转换与配置分发枢纽

自研轻量 Python/FastAPI 容器，部署在 NAS（端口 8399）：输入多个机场订阅，自动完成
**抓取 → 解析 → 清洗分类（假节点过滤/地区识别/重名消歧）→ 模板渲染 → 校验 → 版本化发布**，
输出带完整 AI 分流、Claude 纯净分组、快速故障切换的 mihomo 与 Shadowrocket 双格式配置，
并作为规则集的局域网分发源——客户端永远只从 NAS 拉配置和规则，不受上游被墙影响。

核心能力：

- **AI 精细分流**：Claude / OpenAI / Gemini / Copilot / Grok / Perplexity / Cursor 各自独立分组；
  🛑 Claude 专用组为 select 手动锁定 + 🧷 备援 fallback（90s），防 IP 跳变封号；
- **快速切换**：url-test/fallback 统一 interval 120s（关键组 90s）、tolerance 40ms、
  max-failed-times 3、timeout 3000ms、探活 `http://cp.cloudflare.com/generate_204`；
- **多机场合并**：节点合并、跨订阅重名自动追加「 [别名]」后缀、假节点（剩余流量/官网/套餐到期类）过滤；
- **规则自托管**：blackmatrix7 等上游规则定时镜像到容器缓存（永不失效：上游断更沿用旧缓存/内置基线），
  客户端 RULE-SET 全部指向 NAS；另产出规则全内联的离线自包含版，外网本地导入兜底；
- **纯净度检测**：内置 mihomo 探测实例逐节点检测出口 IP 属性（ASN/住宅机房/代理信号），
  驱动 Claude 组推荐排序与「家宽→机房」告警；探测故障不影响分发主链路；
- **版本与回退**：`data/out/v<NNNN>/` 保留最近 5 版，校验不过拒绝发布、可一键回退。

设计文档见 [docs/](docs/)：[架构与数据流](docs/01-架构与数据流.md) ·
[转换引擎与分组模板](docs/02-转换引擎与分组模板.md) ·
[订阅管理与纯净度检测](docs/03-订阅管理与纯净度检测.md) ·
[客户端接入与验收](docs/04-客户端接入与验收.md)。

## 目录结构

```
app/                 应用代码（config/models/store/utils 共享层 + fetcher/parser/cleaner/
                     templater/validator/rulesync/purity/probe/pipeline/web/scheduler/mirror）
app/baseline_rules/  内置规则基线（首次部署离线可用）
app/templates/       mihomo YAML / Shadowrocket conf 渲染模板
app/web_static/      管理单页（无前端框架）
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

:: 3) 启动服务（数据目录默认 ./data，监听默认 127.0.0.1:8399）
set SUBHUB_PORT=18399
.venv\Scripts\python.exe -m app.main

:: 4) 验活后浏览器打开管理页
curl http://127.0.0.1:18399/api/health
```

常用环境变量（全部可缺省，见 `app/config.py` 模块 docstring）：
`SUBHUB_DATA_DIR` / `SUBHUB_HOST` / `SUBHUB_PORT` / `SUBHUB_NAS_HOST` /
`SUBHUB_REFRESH_MINUTES`(30) / `SUBHUB_MIRROR_HOURS`(6) / `SUBHUB_PURITY_HOUR`(4) /
`SUBHUB_SKIP_ANYTLS`(开) / `SUBHUB_PROBE_CONTROLLER_PORT`(9095) / `SUBHUB_PROBE_MIXED_PORT`(9096)。

纯净度扫描需要 `mihomo` 二进制（查 PATH 或 `SUBHUB_MIHOMO_PATH`）；缺失时纯净度报告
标记「不可用」，其余功能不受影响。

## NAS 部署（/share/Container/sub-hub）

```bash
# 1) 代码放 NAS 惯例目录
#    /share/Container/sub-hub/{Dockerfile,docker-compose.yml,app/,rules_manifest.yaml,requirements.txt}

# 2) 构建并启动（mihomo 二进制构建时自动下载：GitHub 直连失败自动走 ghfast.top 加速镜像，
#    也可 --build-arg MIHOMO_URL=<完整地址> 覆盖；ARM 机型加 --build-arg MIHOMO_ARCH=linux-arm64）
docker-compose up -d --build

# 3) 验活（宿主机上；容器内不留 curl/wget）
curl http://127.0.0.1:8399/api/health

# 4) 数据持久化在 /share/Container/sub-hub/data/（compose 已挂载 ./data:/app/data）
```

本机 Windows 构建后也可 `docker save sub-hub:latest | ssh nas docker load` 直传镜像
（沿用既有 movieclaw 流程 B 惯例）。部署前确认宿主 8399 未被占用：
`ss -tlnp | grep 8399`。

## 客户端接入（摘要）

管理页（`http://<nas>:8399/`）首页展示四条分发链接（token 已内嵌，可复制/扫码）：

| 客户端 | 订阅链接 | 说明 |
| --- | --- | --- |
| Clash Verge（Windows 本机） | `http://<nas>:8399/sub/<token>/clash.yaml` | mihomo 主版本；导入后代理页确认组齐全，手动锁定一次 Claude 节点 |
| Shadowrocket（iOS，≥6.3） | `http://<nas>:8399/sub/<token>/shadowrocket.conf` | SR 主版本；anytls 需 6.3+，低版本已按开关自动跳过该类节点 |
| 离线自包含版（外网兜底） | `…/clash-offline.yaml`、`…/shadowrocket-offline.conf` | 规则全内联，本地导入后脱离 NAS 运行 |

规则集分发：`http://<nas>:8399/rules/<name>.yaml|.list`（配置内 RULE-SET 已自动指向）。
接入步骤、端到端验收清单与故障场景预期见 **[docs/04-客户端接入与验收](docs/04-客户端接入与验收.md)**。

## 安全说明

- **订阅凭据加密落盘**：订阅 URL 以 Fernet 密文存 SQLite，密钥 `data/secret.key`
  （0600，首次启动生成）；数据库文件本身不泄露明文 URL。
- **分发链接带随机 token**：`/sub/<32hex>/…`，token 首次启动自动生成（`data/token`），
  UI 可复制；token 校验失败一律 404。请勿外传完整订阅链接。
- **打码展示**：管理 API 中订阅 URL 仅显示尾 6 位；日志一律打码到 host，
  节点凭据字段（uuid/password 等）不进日志、不进管理 API 响应、不进 meta.json。
- **镜像推送默认关闭**：配置产物含全部节点凭证，推送到第三方（CF Workers+KV / GitHub 公库）
  属敏感动作，须在管理页显式开启并知晓凭证告知义务（docs/04 §2）。
- **密钥与凭据不进配置产物**：产物与规则文件中不含任何订阅 URL、token 或节点凭据之外的敏感信息；
  探测实例控制面仅绑定 127.0.0.1。
