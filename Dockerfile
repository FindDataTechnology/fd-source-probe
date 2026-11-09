# syntax=docker/dockerfile:1
FROM python:3.12-slim AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

COPY pyproject.toml README.md ./
COPY fd_source_probe ./fd_source_probe
RUN pip install --no-cache-dir .

# 审计落盘目录（compose/k8s 挂卷覆盖）
RUN mkdir -p /var/log/fd-source-probe
ENV FD_PROBE_AUDIT_LOG=/var/log/fd-source-probe/audit.jsonl

EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/healthz',timeout=4).status==200 else 1)"

CMD ["fd-source-probe", "serve", "--host", "0.0.0.0", "--port", "8000"]
