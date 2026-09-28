# deploy/cf-worker — Cloudflare Workers + KV 镜像部署说明

sub-hub「公网静态镜像」的读取端（docs/04 §2 主方案，推荐档）。sub-hub 容器把渲染好的
配置**推**到 Workers KV，外网客户端订阅本 Worker 的 URL——NAS 不需要任何公网条件。

## 0. 前置与凭证告知

- 需要 Cloudflare 账号（免费版额度：每天 10 万次请求，个人使用绰绰有余）。
- **凭证告知义务（docs/04 §2）**：配置文件内含全部节点凭证，推到 Cloudflare 即视为
  托管给该平台。Worker 侧有 URL token 读取保护，平台可见内容。请知晓并接受后再开启
  sub-hub 的 mirror 模块。
- 安装 Node.js ≥18 与 wrangler：`npm install -g wrangler`，然后 `wrangler login`。

## 1. 想清楚两个 token

| 名称 | 谁用 | 作用 |
| --- | --- | --- |
| `MIRROR_TOKEN`（= sub-hub 配置里的 `url_token`） | 出现在订阅 URL 路径里，Worker 强制校验 | 没有它读不到任何文件（404） |
| Cloudflare API Token（= sub-hub 配置里的 `api_token`） | 仅 sub-hub 推送时用（写 KV） | 不进 URL，只存 NAS 本地 `data/mirror.json` |

两个 token 建议都用随机值，例如 `openssl rand -hex 16`。

## 2. 创建 KV namespace

```bash
wrangler kv namespace create SUBHUB_MIRROR
```

记下输出的 `id`，填进本目录 `wrangler.toml` 的 `[[kv_namespaces]]`。

## 3. 部署 Worker 并设置 MIRROR_TOKEN

```bash
cd deploy/cf-worker
wrangler secret put MIRROR_TOKEN     # 粘贴第 1 步生成的随机值
wrangler deploy
```

部署完成后 Worker 地址形如 `https://sub-hub-mirror.<你的子域>.workers.dev`，记为
`worker_base`。

## 4. 创建推送用的 API Token

Dashboard → 右上角头像 → My Profile → API Tokens → Create Token → 自定义：

- 权限：`Account` → `Workers KV Storage` → `Edit`；
- 账户资源限定到你的账户（可选：限定到该 KV namespace）。

记下生成的 token（`api_token`）与账户页可见的 `account_id`、第 2 步的 `namespace_id`。

## 5. 在 sub-hub 侧配置

UI「镜像推送」或 `data/mirror.json`（字段与 `app/mirror.py` 一致）：

```json
{
  "enabled": true,
  "provider": "cf-kv",
  "cf_kv": {
    "api_token": "<第 4 步的 API Token>",
    "account_id": "<账户 ID>",
    "namespace_id": "<第 2 步的 namespace id>",
    "url_token": "<第 1 步的 MIRROR_TOKEN>",
    "worker_base": "https://sub-hub-mirror.<你的子域>.workers.dev"
  }
}
```

然后在 UI 点「立即推送」（或 `POST /api/mirror/push`）。推送的 key 为
`<url_token>/<文件名>`（4 份产物：clash.yaml / shadowrocket.conf /
clash-offline.yaml / shadowrocket-offline.conf），与 worker.js 的读取约定一致；
内容 hash 无变化时不会重复推送。

## 6. 验收

1. 浏览器访问 `https://<worker_base>/<url_token>/clash.yaml` → 返回 YAML 配置。
2. 把 URL 里的 token 改错 → 404（与访问不存在文件的表现一致）。
3. 外网客户端（蜂窝网络）把订阅地址指向该 URL，可正常自动更新。

## 7. 维护说明

- 轮换 `MIRROR_TOKEN`：`wrangler secret put MIRROR_TOKEN` 换新值后，同步修改
  sub-hub 的 `url_token` 并手动推送一次（key 前缀变化，旧 key 可在 Dashboard 删掉）。
- 下线：`wrangler delete` + 删除 KV namespace + 在 sub-hub 关闭 mirror。
- 国内直连 Workers 时好时坏：客户端走代理稳定拉取；完全不通时用离线自包含配置兜底
  （docs/04 §2「死结物理边界」）。
