# Open Review

Open Review is an AI-powered GitLab merge request review and assistance bot for general software repositories. It receives GitLab webhooks, serializes work per merge request, runs review or mention workflows in isolated worktrees, and publishes structured feedback back to GitLab across mixed-language projects.

The project is built around a small self-hosted deployment model: FastAPI for webhook intake and the admin console, SQLite for durable queue/control-plane state, a long-running worker for agent execution, optional Docker sandboxes for repository work, and optional Phoenix tracing for observability.

## Core Capabilities

- Reviews merge requests on open, reopen, update, or draft-to-ready events.
- Responds to `@<bot-username>` mentions on merge requests.
- Runs scheduled project-level daily audits.
- Uses a durable SQLite-backed queue so webhook handling stays quick and worker runs survive process restarts.
- Serializes runs for the same `project_id!mr_iid` while allowing different merge requests to run in parallel.
- Runs agents in per-run temporary worktrees, with local or Docker-backed sandbox execution.
- Provides a built-in `/admin` console for first-time setup, runtime configuration, run history, and operational visibility.
- Supports Chinese and English in the admin UI.
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

## Agents

Open Review is built around three main agents.

### Auto Review Agent

The Auto Review Agent runs when a merge request is opened, reopened, updated, or moved out of draft. It builds MR context, prepares a review seed, and runs a Director-led review workflow with specialist lanes for correctness, reliability, contracts, performance/build behavior, and security.

The output is a structured review report with confirmed findings, suspicious findings, open questions, and inline candidates. Publishing is deduplicated with hidden markers so the same issue is not repeatedly posted for the same MR head.

### Mention Agent

The Mention Agent handles GitLab MR comments that mention the resolved bot username. It can answer questions, inspect repository context, explain behavior, and make bounded code changes in a temporary worktree when the user asks for implementation work.

Mention-driven changes are guarded by branch push checks, changed-file limits, and head-SHA revalidation before commit and push.

### Daily Audit Agent

The Daily Audit Agent performs scheduled project-level analysis outside a single MR. It first selects one bounded workflow direction, then analyzes that direction in depth. It can use repository tools, shell commands, prior audit memory, direction history, and transcript context to produce focused findings and continuity records.

Daily audit persistence is raw-record based: transcripts and run records are stored once, and summaries, memories, and self-evolution inputs are derived from that canonical source.

## Self-Evolution

Open Review includes agent-scoped self-evolution for `auto_review`, `mention`, and `daily_audit`.

- Each agent has its own enable flag, interval in days, and fixed local schedule.
- Evolution consumes raw run records and feedback from the control-plane database.
- Candidate changes can target prompts, skills, tool descriptions, and selected code paths.
- Prompt and skill assets are file-backed under each agent's `selfevolution/` tree.
- Shared baseline skills live under `agent/scenes/skills/` and are read-only at runtime.
- Applying evolved assets is separate from the main run path, so normal webhook handling does not depend on evolution succeeding.

The design keeps semantic judgment inside the agent workflow: prompts and tools expose evidence and write surfaces, while deterministic code handles storage, schema validation, scheduling, leases, and safe application.

## Requirements

- Python 3.11+
- `uv`
- Git
- A GitLab project or group you can configure webhooks for
- A dedicated GitLab bot account personal access token with API access
- An OpenAI-compatible or Anthropic-compatible model endpoint
- Docker, only if you want Docker sandbox execution or the bundled stack deployment

## Local `uv` Deployment

Local `uv` deployment is the fastest way to run Open Review on a single host. It uses the same admin console, SQLite control plane, durable queue, runtime worker, and fixed state directory as the Docker stack.

Install dependencies:

```bash
uv sync
```

Prepare the fixed state directory if this is the first local run:

```bash
sudo install -d -o "$(id -un)" -g "$(id -gn)" -m 0750 /var/lib/open-review
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

Local deployment characteristics:

- Best for development, local testing, and small single-host installations.
- Runs web and worker as normal host processes.
- Uses `/var/lib/open-review` for the control-plane database, project cache, local sandboxes, and runtime artifacts.
- Supports `SANDBOX_TYPE=local` for trusted development workflows.
- Supports `SANDBOX_TYPE=docker` when Docker execution isolation is needed.
- Can share the same state directory with Docker stack deployment because the stack runs service containers with the host UID/GID.

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

## Docker Stack Deployment

The bundled Docker stack is the preferred packaged deployment path for test and small production environments that want Docker sandbox execution and optional Phoenix tracing.

Run:

```bash
cd deploy/stack
./deploy.sh
```

The stack starts:

- Open Review `web`
- Open Review `worker`
- Phoenix
- Phoenix Postgres
- the Docker sandbox image used by the worker

Mutable state is stored under `/var/lib/open-review`. `deploy.sh` validates that directory before startup, can repair ownership with `sudo` in an interactive shell, and runs Open Review containers with the host UID/GID to keep local `uv` and Docker deployments compatible.

Before deployment, or when debugging host setup, run:

```bash
cd deploy/stack
./doctor.sh
./doctor.sh --fix
```

The doctor checks Docker access, Docker socket visibility, state directory writability, common port conflicts, and optionally Docker build apt connectivity with `OPEN_REVIEW_DOCTOR_CHECK_APT=1`.

## Deployment Summary

Direct local `uv` deployment:

```bash
uv sync --frozen
uv run python -m uvicorn agent.webapp:app --host 0.0.0.0 --port 8000
uv run python -m agent.runtime.worker
```

Packaged Docker stack:

```bash
cd deploy/stack
./deploy.sh
```

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
