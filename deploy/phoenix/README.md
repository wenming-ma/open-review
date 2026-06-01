# Phoenix Deployment

This directory packages Phoenix as a pinned, repo-managed deployment unit for Open Review tracing.

## Quick Start

```bash
cd deploy/phoenix
cp .env.example .env
# edit .env before first start
docker compose up -d --build
```

Phoenix will be available at `http://localhost:6006` by default.

## First-Time Setup

1. Open the Phoenix UI and sign in with the bootstrap admin account.
2. Change the admin password.
3. Create a Phoenix system API key from the Phoenix settings page.
4. Put the Phoenix values into the bot `.env`:

```bash
PHOENIX_TRACING_ENABLED=true
PHOENIX_COLLECTOR_ENDPOINT=http://localhost:6006/v1/traces
PHOENIX_UI_BASE_URL=http://localhost:6006
PHOENIX_API_KEY=your-system-api-key
PHOENIX_PROJECT_NAME=open-review
```

5. Restart both Open Review processes:

```bash
uv run python -m uvicorn agent.webapp:app --host 0.0.0.0 --port 8000
uv run python -m agent.runtime.worker
```

## Notes

- The Phoenix image is built from `deploy/phoenix/Dockerfile`, which wraps a pinned official Phoenix base image.
- `PHOENIX_COLLECTOR_ENDPOINT` must point to the OTLP traces path, not the UI root.
- If Phoenix is unavailable, Open Review keeps working; tracing is fail-open.
