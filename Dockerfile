# syntax=docker/dockerfile:1.7
FROM python:3.12.14-slim-bookworm@sha256:0f5b26b9518d002b6173fd61daad821fa340635ebfec5bba471013f9ca114579 AS builder

ARG UV_VERSION=0.12.1
ARG MODEL_REVISION=1110a243fdf4706b3f48f1d95db1a4f5529b4d41
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    HF_HOME=/opt/huggingface
WORKDIR /app

RUN python -m pip install --no-cache-dir "uv==${UV_VERSION}"
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project
COPY src ./src
RUN uv sync --frozen --no-dev && \
    .venv/bin/python -c "from sentence_transformers import SentenceTransformer; SentenceTransformer('sentence-transformers/all-MiniLM-L6-v2', revision='${MODEL_REVISION}')"

FROM python:3.12.14-slim-bookworm@sha256:0f5b26b9518d002b6173fd61daad821fa340635ebfec5bba471013f9ca114579 AS runtime

ARG BUILD_SHA=development
ARG APP_VERSION=0.1.0
LABEL org.opencontainers.image.title="RetrievalOps" \
      org.opencontainers.image.version="${APP_VERSION}" \
      org.opencontainers.image.revision="${BUILD_SHA}" \
      org.opencontainers.image.source="https://github.com/amulyagupta1278/rag-retrieval-dissertation"

ENV PATH=/app/.venv/bin:$PATH \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    HF_HOME=/opt/huggingface \
    HF_HUB_OFFLINE=1 \
    TRANSFORMERS_OFFLINE=1 \
    RETRIEVALOPS_BUILD_SHA=${BUILD_SHA} \
    RETRIEVALOPS_STORAGE_ROOT=/data/artifacts \
    RETRIEVALOPS_DATABASE_URL=sqlite:////data/retrievalops.db

RUN groupadd --system --gid 10001 retrievalops && \
    useradd --system --uid 10001 --gid retrievalops --home-dir /app retrievalops && \
    mkdir -p /app /data /opt/huggingface && \
    chown -R retrievalops:retrievalops /app /data /opt/huggingface
WORKDIR /app
COPY --from=builder --chown=retrievalops:retrievalops /app/.venv ./.venv
COPY --from=builder --chown=retrievalops:retrievalops /opt/huggingface /opt/huggingface

USER 10001:10001
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=3s --start-period=10s --retries=3 \
  CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/healthz', timeout=2)"]
CMD ["retrievalops-api"]
