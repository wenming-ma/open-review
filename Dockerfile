FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

RUN apt-get update \
    && apt-get install -y --no-install-recommends curl git docker-cli ca-certificates \
    && rm -rf /var/lib/apt/lists/*

RUN pip install --timeout=600 --retries=20 uv

WORKDIR /app

COPY pyproject.toml uv.lock README.md ./
RUN UV_HTTP_TIMEOUT=180 uv sync --frozen --no-dev

COPY agent ./agent
COPY scripts ./scripts

RUN chmod +x /app/scripts/container-entrypoint.sh

VOLUME ["/var/lib/open-review"]

ENV PATH="/app/.venv/bin:${PATH}" \
    OPEN_REVIEW_RUNTIME_ROLE=web

ENTRYPOINT ["/app/scripts/container-entrypoint.sh"]
