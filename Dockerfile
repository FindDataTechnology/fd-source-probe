# syntax=docker/dockerfile:1
FROM python:3.12-slim AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# 依赖由 uv.lock 钉死（openspec: unify-fastmcp-and-board-five）。
# 构建读锁而不是裸 `pip install .`——后者每次解析到当时最新版，声明与实装会静默漂移。
# 用 uv 镜像导出 requirements 再交给 pip，保持本仓原有 pip 安装形状不变。
FROM ghcr.io/astral-sh/uv:0.9-python3.12-bookworm-slim AS deps
WORKDIR /src
COPY pyproject.toml uv.lock README.md ./
COPY fd_source_probe ./fd_source_probe
RUN uv export --frozen --no-dev --no-emit-project --format requirements-txt -o /tmp/req.txt

FROM python:3.12-slim AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

COPY --from=deps /tmp/req.txt /tmp/req.txt
COPY pyproject.toml README.md ./
COPY fd_source_probe ./fd_source_probe
RUN pip install --no-cache-dir -r /tmp/req.txt && pip install --no-cache-dir --no-deps .

# 审计落盘目录（compose/k8s 挂卷覆盖）
RUN mkdir -p /var/log/fd-source-probe
ENV FD_PROBE_AUDIT_LOG=/var/log/fd-source-probe/audit.jsonl

EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/healthz',timeout=4).status==200 else 1)"

CMD ["fd-source-probe", "serve", "--host", "0.0.0.0", "--port", "8000"]
