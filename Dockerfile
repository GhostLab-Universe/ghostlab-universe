# GhostLab on one origin: the built SPA plus the /v1 API, on $PORT (default 7860).
# Replay needs no keys. Live mode needs META_API_KEY_PUBLIC and LIVE_MODE=1 (both set
# in the host's dashboard, never in the image). Writable state lives in /tmp only.

FROM node:24.22.1-slim AS web
WORKDIR /app
# vite inlines approved fixtures from examples/ and schemas/ (outside web/)
COPY examples ./examples
COPY schemas ./schemas
COPY web ./web
RUN cd web && npm ci --no-audit --no-fund \
    && npx tsc -p tsconfig.app.json --noEmit \
    && npx vite build

FROM python:3.13.18-slim
COPY --from=ghcr.io/astral-sh/uv:0.13.6 /uv /bin/uv
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app/src \
    PATH=/app/.venv/bin:$PATH \
    UV_PYTHON_DOWNLOADS=never \
    UV_COMPILE_BYTECODE=0 \
    HOME=/tmp \
    TMPDIR=/tmp \
    GHOSTLAB_REGISTRY_DB=/tmp/ghostlab/env.sqlite \
    GHOSTLAB_STORE_DB=/tmp/ghostlab/experiments.sqlite \
    GHOSTLAB_SPEND_PATH=/tmp/ghostlab/spend.json \
    GHOSTLAB_WEB_DIST=/app/web/dist \
    LIVE_MODE=0
WORKDIR /app
# exact versions from uv.lock; the project runs from src/ (llm_routes reads demo/ and docs/)
COPY pyproject.toml uv.lock .python-version ./
RUN uv sync --frozen --no-dev --extra service --no-install-project --no-cache
COPY . .
COPY --from=web /app/web/dist ./web/dist
USER 1000
EXPOSE 7860
CMD ["sh", "-c", "exec uvicorn ghostlab.api.app:create_app --factory --host 0.0.0.0 --port ${PORT:-7860}"]
