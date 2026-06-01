# Open Review Stack Deployment

This directory assembles the testable Open Review stack:

- `web` — FastAPI webhook server and admin console
- `worker` — durable runtime worker
- `phoenix` — observability UI and OTLP collector
- `phoenix-db` — Postgres for Phoenix
- `sandbox-image` — build-only service used to package the Docker sandbox image

## Quick Start

1. Start the stack directly:

```bash
cd deploy/stack
./deploy.sh
```

`deploy.sh` automatically:
- prefers `docker compose`, then falls back to `docker-compose`
- checks whether the default ports are already occupied
- increments ports when needed
- uses a stable default `COMPOSE_PROJECT_NAME` of `open_review_stack`
- stops any existing Open Review stack containers before loading bundled images
- removes old Open Review service images so redeploys use the bundled image set
- verifies `/var/lib/open-review` exists and is writable before starting containers, and can repair it with `sudo` in an interactive shell
- runs Open Review web, worker, and worker-created sandbox containers with the host UID/GID so Docker and local `uv` deployments do not fight over file ownership
- if a local `open-review-stack-images-*.tar` bundle is present, loads it first and starts with `--pull never`
- otherwise falls back to the source-repo path and runs `up -d --build`
- after the worker starts, runs a one-shot historical MR sandbox cleanup for GitLab MRs that are already `closed` or `merged`

2. Open:

- Open Review: `http://localhost:8000/admin`
- Phoenix: `http://localhost:6006`

Before deployment, or when diagnosing host permissions and Docker access, run:

```bash
./doctor.sh
# optional repair for /var/lib/open-review ownership/mode
./doctor.sh --fix
```

3. On the first visit to `/admin`, complete the one-time setup:

- set the initial admin password
- then configure GitLab, LLM, agent scheduling, and tracing from the management UI
- all business config is persisted under `/var/lib/open-review/controlplane.db`

## Notes

- `worker` is pinned to `SANDBOX_TYPE=docker` and receives `DOCKER_IMAGE=${OPEN_REVIEW_SANDBOX_IMAGE}` from this stack.
- Only `worker` mounts `/var/run/docker.sock`, so the web service cannot create sandbox containers.
- `/var/lib/open-review` is mounted as the same absolute path on both the host and the service containers, so worker-created sandboxes and worktrees are visible to nested sandbox containers created through `/var/run/docker.sock`.
- `deploy.sh` injects `OPEN_REVIEW_UID`, `OPEN_REVIEW_GID`, and the Docker socket group id into Compose. If you run `docker compose` directly, set those values yourself or use the defaults in `.env`.
- Self-evolution assets are bootstrapped into a local persistent git repo under `/var/lib/open-review/service-repo/open-review/`.
- Runtime always reads and applies self-evolution assets from that local service-repo copy; the image-bundled files only act as the initial seed.
- `daily_audit` uses a run-scoped sandbox experiment directory for targeted local tests, scripts, builds, and benchmarks; this stack is intended for bounded experiments across common software projects, not whole-project builds.
- This stack no longer depends on a repository-root `.env`; business config is managed from the admin UI after the containers are up.
- `.env.example` is optional. The compose file has defaults; use `.env` only when you want to pin image tags, ports, or Postgres values.
- For local stack testing, Phoenix auth is disabled by default and the stack injects a non-empty placeholder `PHOENIX_API_KEY` so Open Review tracing bootstrap can run end-to-end without manual Phoenix API-key provisioning.
- Redeploy cleanup does not remove `/var/lib/open-review` or Compose volumes, so admin config, run history, project cache, and Phoenix data are preserved unless you delete them manually.
- The deployment-time historical sandbox cleanup only targets per-MR directories under `/var/lib/open-review/sandboxes/` when the corresponding GitLab MR is already `closed` or `merged` and no local actor is active. Set `OPEN_REVIEW_DEPLOY_CLEANUP_HISTORICAL_SANDBOXES=0` to skip that deployment step.
