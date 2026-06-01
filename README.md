# Open Review

Open Review is an AI-powered GitLab merge request review and assistance bot for C/C++ and EDA-style repositories. It receives GitLab webhooks, serializes work per merge request, runs review or mention workflows in isolated worktrees, and publishes structured feedback back to GitLab.

The project is built around a small self-hosted deployment model: FastAPI for webhook intake and the admin console, SQLite for durable queue/control-plane state, a long-running worker for agent execution, optional Docker sandboxes for repository work, and optional Phoenix tracing for observability.

## What It Does

- Reviews merge requests on open, reopen, update, or draft-to-ready events.
- Responds to `@<bot-username>` mentions on merge requests.
- Uses a durable SQLite-backed queue so webhook handling stays quick and worker runs survive process restarts.
- Serializes runs for the same `project_id!mr_iid` while allowing different merge requests to run in parallel.
- Runs agents in per-run temporary worktrees, with local or Docker-backed sandbox execution.
- Provides a built-in `/admin` console for first-time setup, runtime configuration, run history, and operational visibility.
- Optionally exports spans to Phoenix for debugging agent lanes and runs.

## Architecture

```text
GitLab Webhook -> FastAPI -> Durable Queue -> MR Actor Worker
                                      |
                                      +-> Auto Review workflow
                                      +-> Mention workflow
                                      +-> Daily Audit workflow
                                      +-> Agent self-evolution workflow
```

The webhook server validates and enqueues events only. The worker owns actual execution. Runtime configuration is edited in the admin console and persisted in SQLite under `/var/lib/open-review/controlplane.db`.

## Requirements

- Python 3.11+
- `uv`
- Git
- A GitLab project or group you can configure webhooks for
- A dedicated GitLab bot account personal access token with API access
- An OpenAI-compatible or Anthropic-compatible model endpoint
- Docker, only if you want Docker sandbox execution or the bundled stack deployment

## Quick Start

Install dependencies:

```bash
uv sync
```

Start the webhook/admin server:

```bash
uv run python -m uvicorn agent.webapp:app --host 0.0.0.0 --port 8000 --reload
```

Start the worker in a second terminal:

```bash
uv run python -m agent.runtime.worker
```

Open the admin console:

```text
http://localhost:8000/admin
```

On first boot, create the initial admin password. Then configure GitLab, model provider settings, webhook URL, scheduling, sandbox mode, and optional tracing from the admin UI.

For local GitLab webhook testing, expose the server with a tunnel:

```bash
cloudflared tunnel --url http://localhost:8000
```

## Configuration

Business configuration is admin-first. The application starts with code defaults, then reads runtime overrides from the control-plane database. A repository-root `.env` is not required for normal operation.

The most important settings are:

- `GITLAB_API_URL`: GitLab API and git remote base URL used by the service.
- `GITLAB_EXTERNAL_URL`: browser-facing GitLab URL.
- `GITLAB_TOKEN`: dedicated bot account token.
- `GITLAB_WEBHOOK_SECRET`: shared webhook validation secret.
- `GITLAB_TARGET_PROJECTS`: projects for webhook setup.
- `OPEN_REVIEW_EXTERNAL_URL`: externally reachable URL GitLab uses to call Open Review.
- `LLM_ACTIVE_PROVIDER`: `openai` or `anthropic`.
- `OPENAI_BASE_URL`, `OPENAI_API_KEY`, `OPENAI_MODEL`: OpenAI-compatible provider settings.
- `ANTHROPIC_BASE_URL`, `ANTHROPIC_API_KEY`, `ANTHROPIC_MODEL`: Anthropic-compatible provider settings.
- `SANDBOX_TYPE`: `local` or `docker`.
- `DOCKER_IMAGE`: sandbox image used when Docker sandboxing is enabled.
- `PHOENIX_TRACING_ENABLED` and related Phoenix settings: optional tracing.

See [.env.example](.env.example) for deployment-level examples. Do not commit real tokens or secrets.

## Deployment Options

Run the two core processes directly:

```bash
uv sync --frozen
uv run python -m uvicorn agent.webapp:app --host 0.0.0.0 --port 8000
uv run python -m agent.runtime.worker
```

Or use the bundled local stack:

```bash
cd deploy/stack
./deploy.sh
```

The stack starts the web service, worker, Phoenix, Phoenix Postgres, and the Docker sandbox image. Mutable state is stored under `/var/lib/open-review`.

## Optional Phoenix Tracing

Phoenix is optional and fail-open. If it is unavailable or disabled, webhook processing and worker runs continue normally.

Start Phoenix from the bundled assets:

```bash
cd deploy/phoenix
cp .env.example .env
docker compose up -d --build
```

Then configure these values in the admin console:

```text
PHOENIX_TRACING_ENABLED=true
PHOENIX_COLLECTOR_ENDPOINT=http://localhost:6006/v1/traces
PHOENIX_UI_BASE_URL=http://localhost:6006
PHOENIX_PROJECT_NAME=open-review
```

## Testing

Install development dependencies and run tests:

```bash
uv sync --extra dev
uv run python -m pytest tests/ -v
```

For a quick syntax check:

```bash
uv run python -m compileall agent tests
```

## Repository Layout

- `agent/webapp.py`: FastAPI app and GitLab webhook endpoint.
- `agent/runtime/`: durable queue, stores, run models, and worker loop.
- `agent/scenes/auto_review/`: automatic merge request review workflow.
- `agent/scenes/mention/`: mention-driven assistant workflow.
- `agent/scenes/daily_audit/`: scheduled project-level audit workflow.
- `agent/admin/`: built-in admin console.
- `agent/gitlab/`: GitLab API helpers.
- `agent/sandbox/`: local and Docker sandbox helpers.
- `deploy/`: optional deployment assets.
- `tests/`: unit and integration-style tests.

## Security Notes

- Use a dedicated GitLab bot account with the minimum permissions needed for the target projects.
- Keep API keys, webhook secrets, and admin passwords out of Git history.
- Prefer Docker sandboxing for untrusted or multi-project review workloads.
- Review generated comments and commits before using the bot in repositories with sensitive code.
