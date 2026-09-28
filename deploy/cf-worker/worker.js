/**
 * sub-hub 公网镜像读取 Worker（极简，无依赖）
 *
 * 路由：GET|HEAD /<url_token>/<文件名>  →  KV 读取 key "<url_token>/<文件名>"
 * - url_token 必须与 Worker 环境变量 MIRROR_TOKEN 完全一致，否则一律 404
 *   （错误 token 与不存在文件返回相同响应，不泄露存在性）。
 * - KV 绑定名固定为 SUBHUB_MIRROR（见 wrangler.toml）。
 * - 文件名白名单 [A-Za-z0-9._-]，防目录穿越。
 *
 * sub-hub 侧经 Cloudflare API v4 写入的 key 即 "<url_token>/<文件名>"
 * （app/mirror.py _push_cf_kv），两侧约定一致。
 *
 * 部署步骤见同目录 README.md。
 */

const CONTENT_TYPES = {
  ".yaml": "text/yaml; charset=utf-8",
  ".conf": "text/plain; charset=utf-8",
  ".txt": "text/plain; charset=utf-8",
};

function notFound() {
  return new Response("Not Found", { status: 404 });
}

export default {
  async fetch(request, env) {
    if (request.method !== "GET" && request.method !== "HEAD") {
      return new Response("Method Not Allowed", { status: 405 });
    }
    const url = new URL(request.url);
    const parts = url.pathname.split("/").filter((p) => p.length > 0);
    if (parts.length !== 2) {
      return notFound();
    }
    const [token, name] = parts;
    if (!env.MIRROR_TOKEN || token !== env.MIRROR_TOKEN) {
      return notFound();
    }
    if (!/^[A-Za-z0-9._-]+$/.test(name)) {
      return notFound();
    }
    const value = await env.SUBHUB_MIRROR.get(`${token}/${name}`);
    if (value === null) {
      return notFound();
    }
    const dot = name.lastIndexOf(".");
    const ext = dot >= 0 ? name.slice(dot).toLowerCase() : "";
    const headers = {
      "content-type": CONTENT_TYPES[ext] || "application/octet-stream",
      "cache-control": "no-cache",
    };
    if (request.method === "HEAD") {
      return new Response(null, { status: 200, headers });
    }
    return new Response(value, { status: 200, headers });
  },
};
