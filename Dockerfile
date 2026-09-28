# ==============================================================
# sub-hub — 机场订阅转换与配置分发枢纽（NAS 容器镜像）
# 构建：docker build -t sub-hub:latest .
# 运行：见 docker-compose.yml（/share/Container/sub-hub，端口 8399）
# ==============================================================
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    # 服务监听（容器内必须 0.0.0.0 才能被端口映射转发）
    SUBHUB_HOST=0.0.0.0 \
    SUBHUB_PORT=8399 \
    SUBHUB_DATA_DIR=/app/data

WORKDIR /app

# ---- 依赖层（单独 COPY 以利用层缓存）----
COPY requirements.txt ./requirements.txt
# PIP_INDEX_URL 默认留空 = 官方 PyPI；国内/NAS 直连构建可覆盖，例如：
#   --build-arg PIP_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple
ARG PIP_INDEX_URL=
RUN set -eux; \
    if [ -n "$PIP_INDEX_URL" ]; then \
        python -m pip install --no-cache-dir --index-url "$PIP_INDEX_URL" -r requirements.txt; \
    else \
        python -m pip install --no-cache-dir -r requirements.txt; \
    fi

# ---- mihomo 二进制（纯净度探测实例专用，docs/03 §4）----
# 下载源依序尝试：加速镜像（MIHOMO_MIRRORS，空格分隔）→ GitHub Releases 直连垫底。
# 实测直连在本机网络（Docker Desktop 内部代理）下会出现下载截断
# （gzip: unexpected end of file），故默认镜像优先；每个源拿到内容后当场
# gzip.decompress 全量校验（CRC/截断/HTML 错误页都会抛异常），坏源自动换下一个。
# 全部可覆盖：MIHOMO_VERSION / MIHOMO_ARCH / MIHOMO_BASE_URL / MIHOMO_MIRRORS，
# 或直接用 MIHOMO_URL 指定完整下载地址（指定后不再走镜像链）。
ARG MIHOMO_VERSION=v1.19.31
ARG MIHOMO_ARCH=linux-amd64
ARG MIHOMO_BASE_URL=https://github.com/MetaCubeX/mihomo/releases/download
ARG MIHOMO_MIRRORS="https://ghfast.top/ https://gh-proxy.com/ https://ghproxy.net/"
ARG MIHOMO_URL=
RUN set -eux; \
    asset="mihomo-${MIHOMO_ARCH}-${MIHOMO_VERSION}.gz"; \
    direct="${MIHOMO_BASE_URL}/${MIHOMO_VERSION}/${asset}"; \
    if [ -n "$MIHOMO_URL" ]; then urls="$MIHOMO_URL"; else \
        urls=""; \
        for m in $MIHOMO_MIRRORS; do urls="$urls ${m}${direct}"; done; \
        urls="$urls $direct"; \
    fi; \
    ok=""; \
    for u in $urls; do \
        echo ">> mihomo 下载源：$u"; \
        if python -c "import gzip,sys,urllib.request; d=urllib.request.urlopen(sys.argv[1], timeout=180).read(); open('/usr/local/bin/mihomo','wb').write(gzip.decompress(d))" "$u"; then \
            ok=1; break; \
        fi; \
        rm -f /usr/local/bin/mihomo; \
        echo ">> 该源不可达或内容不完整（截断/非 gzip），切换下一个源" >&2; \
    done; \
    [ -n "$ok" ] || { echo "mihomo 下载失败：直连与全部加速镜像均不可达，可用 --build-arg MIHOMO_URL=… 指定完整地址" >&2; exit 1; }; \
    chmod 0755 /usr/local/bin/mihomo; \
    mihomo -v

# ---- 应用代码（app/ + rules_manifest.yaml 为运行必需）----
COPY app ./app
COPY rules_manifest.yaml ./rules_manifest.yaml

# data/ 由卷挂载持久化（compose：./data:/app/data），镜像内不预建内容
VOLUME ["/app/data"]

EXPOSE 8399

ENTRYPOINT ["python", "-m", "app.main"]
