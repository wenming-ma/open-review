# AGENTS.md — Open Review Project Guide

## Project Overview

**Open Review** is an AI-powered GitLab MR review and assistance bot for general software projects, including mixed-language repositories. It listens to GitLab webhook events and dispatches AI agents to review code, answer questions, and commit fixes.

## Architecture

```
GitLab Webhook → FastAPI (agent/webapp.py)
                 ↓
      Durable Event Queue (SQLite)
                 ↓
      MR Actor Worker (same MR serialized)
                 ↓
    ┌────────────────────────┐
    │  MR open/update        │ → Auto Review Orchestrator (agent/scenes/auto_review/)
    │  @bot mention          │ → Mention Orchestrator     (agent/scenes/mention/)
    └────────────────────────┘
                 ↓
    Local or Docker sandbox + per-run temporary worktrees
    Director-led deepagents workflows with structured report outputs
```

The webhook server only validates and enqueues events. Actual execution happens inside the durable worker, which guarantees one active run per `project_id!mr_iid` while still allowing different MRs to run in parallel.

The current runtime model is intentionally small-team oriented:
- single-host SQLite-backed durable queue and actor state
- one long-running worker process
- optional Docker sandbox for MR execution isolation
- optional Phoenix tracing stack for observability

### Auto Review Workflow

The webhook runtime uses a staged auto-review pipeline:

1. `intake` — collect MR metadata, diff range, changed files, commit messages, and recent GitLab comments
2. `seed` — build a lightweight review seed context for the current MR head and review mode
3. `director review` — one writable Director agent coordinates five writable specialist agents: `correctness`, `reliability`, `contracts`, `performance-build`, and `security`
4. `specialist investigation` — specialists may freely use repo/file/shell tools and spawn investigation subagents such as `explore`, `task`, `trace-impact`, `counterexample`, and `general-purpose`
5. `report finalize` — the Director merges duplicate candidate findings into a report with `confirmed findings`, `suspicious findings`, and `open questions`
6. `publish` — post a persistent structured summary comment plus inline comments only for high-confidence findings that map cleanly to the current diff

Auto review comments include hidden markers for:
- review run id
- dedupe key
- reviewed head SHA
- diff fingerprint

These markers are used to avoid reposting the same finding and to skip re-reviewing the same MR head.

## Directory Structure

```
agent/
├── config.py              # Code-default runtime settings + SQLite-backed runtime overrides
├── admin/                 # Built-in editorial-tech admin console
│   ├── router.py          # Admin auth, pages, and JSON APIs
│   └── static/            # Control-room CSS and JS assets
├── controlplane/          # SQLite-backed config/auth/run tracking services
│   └── service.py         # Config service + tracking service
├── observability/         # Optional Phoenix tracing integration
│   └── phoenix.py         # OTEL bootstrap, Open Review spans, and Phoenix deep links
├── webapp.py              # FastAPI app, GitLab webhook endpoint (enqueue only)
├── server.py              # LangGraph compatibility entry for auto-review
├── runtime/               # Durable queue + MR actor worker runtime
│   ├── models.py          # EventEnvelope / RunRecord / journal models
│   ├── queue.py           # enqueue_gitlab_event() and queue adapters
│   ├── store.py           # in-memory and SQLite-backed runtime stores
│   └── worker.py          # drain_mr_actor() and standalone SQLite worker loop
├── prompt.py              # Legacy shared prompt helpers; scene-specific prompts live under scenes/*/prompts.py
├── gitlab/                # GitLab API wrappers (python-gitlab)
│   ├── client.py          # Connection factory
│   ├── mr_info.py         # Fetch MR diffs and metadata
│   └── comments.py        # Post comments, inline comments, and read MR activity
├── sandbox/
│   ├── manager.py         # Sandbox lifecycle: create, clone repo, cache per MR
│   ├── docker_backend.py  # Thin Docker-backed execution adapter
│   └── command_runner.py  # Host vs sandbox command execution helper
├── scenes/
│   ├── auto_review/       # Scene 1: automatic MR review on open/update
│   │   ├── graph.py       # Director + specialist/subagent builders and observed wrappers
│   │   ├── models.py      # ReviewContext / ReviewSeedContext / report and finding schemas
│   │   ├── prompts.py     # Director, specialist, and investigation-subagent prompts
│   │   ├── orchestrator.py # Director-led review pipeline entry point
│   │   └── selfevolution/ # File-backed selfevolution tree: engine, prompts, skills, tool metadata, code targets
│   ├── daily_audit/       # Scene 3: scheduled project-level daily audit
│   │   ├── graph.py       # Direction + analysis agent builders
│   │   ├── models.py      # DailyAuditContext / selection / report schemas
│   │   ├── orchestrator.py # Daily audit orchestration entry point
│   │   ├── middleware/    # DeepAgents middleware hooks for session lifecycle and async persistence fan-out
│   │   │   └── session_lifecycle.py
│   │   ├── persistence/   # SQLite-backed business persistence + async persistence scenes
│   │   │   ├── store.py
│   │   │   ├── direction.py
│   │   │   ├── short_term.py
│   │   │   ├── long_term.py
│   │   │   └── skill.py
│   │   ├── runtime/       # Daily-audit runtime infrastructure for DeepAgents/LangGraph
│   │   │   ├── backends.py
│   │   │   └── deepagents.py
│   │   └── selfevolution/ # File-backed selfevolution tree: engine, repo/paths/evaluation, prompts, skills, tool metadata, code targets
│   └── mention/           # Scene 2+4: @mention handler
│       ├── graph.py        # build_mention_agent() + auxiliary subagent builders
│       ├── models.py       # MentionContext / MRSnapshot / result schemas
│       ├── prompts.py      # Classifier and executor prompts
│       ├── orchestrator.py # Mention Agent orchestration entry point
│       └── selfevolution/  # File-backed selfevolution tree: engine, prompts, skills, tool metadata, code targets
├── tools/                 # Custom tools exposed to agents
│   ├── gitlab_comment.py         # Post MR comment
│   └── gitlab_inline_comment.py  # Post inline code comment
├── middleware/
│   └── tool_error_handler.py  # ToolErrorMiddleware (wraps tool exceptions)
└── utils/
    ├── model.py           # make_model() — supports provider:model format
    ├── thread_id.py       # Deterministic MR → thread ID mapping
    └── diff_parser.py     # Parse unified diffs
```

Top-level deployment/runtime assets:
- `Dockerfile` — main Open Review service image
- `deploy/sandbox/` — dedicated Docker sandbox image plus smoke-test deployment assets
- `deploy/phoenix/` — Phoenix/Postgres deployment assets using a pinned upstream Phoenix image
- `deploy/stack/` — combined Open Review web/worker + Phoenix packaging for test environments; it uses the fixed host state path `/var/lib/open-review` so nested sandbox containers can see worker-created repos/worktrees
- `deploy/gitlab/docker-compose.yml` — optional GitLab container deployment kept under `deploy/`

## Key Conventions

### Runtime Config
Business configuration is admin-first:
- the service starts with code defaults
- the built-in admin setup flow creates the first admin account on first boot
- GitLab, LLM, agent scheduling, and tracing settings are edited in `/admin` and persisted to SQLite
- runtime reads business config from the control plane database, not from a repository `.env`

Deployment-level container settings may still exist in Docker/Compose, but they are infrastructure inputs rather than Open Review business configuration. Key runtime config fields are:
- `GITLAB_API_URL` — Open Review 访问 GitLab API 和 git remote 时使用的地址
- `GITLAB_EXTERNAL_URL` — 浏览器访问 GitLab 时使用的外部地址
- `GITLAB_TOKEN` — dedicated bot account personal access token (api scope)
- `GITLAB_WEBHOOK_SECRET` — Shared secret for webhook validation
- `GITLAB_TARGET_PROJECTS` — GitLab project list used for webhook configuration; one or more explicit project paths/IDs
- `OPEN_REVIEW_EXTERNAL_URL` — externally reachable base URL used by GitLab to reach the webhook service
- `GITLAB_TOKEN` also defines the bot's real GitLab username, display name, and avatar; the username is resolved automatically and cached locally for mention matching and self-comment detection
- `LLM_ACTIVE_PROVIDER` — active LLM provider, currently `openai` or `anthropic`
- `OPENAI_BASE_URL` / `OPENAI_API_KEY` / `OPENAI_MODEL` — OpenAI-compatible endpoint settings
- `ANTHROPIC_BASE_URL` / `ANTHROPIC_API_KEY` / `ANTHROPIC_MODEL` — Anthropic-compatible endpoint settings
- `LLM_MODEL_ID` — compatibility field auto-synced from the active provider/model
- `SANDBOX_TYPE` — `local` (default) or `docker`
- `DOCKER_IMAGE` — dedicated Docker image used for MR sandbox execution when `SANDBOX_TYPE=docker`
- `WORKER_CONCURRENCY` — max concurrent MR actors per worker process
- `MR_ACTOR_LEASE_SECONDS` — lease TTL for one MR actor
- `MENTION_BATCH_WINDOW_SECONDS` — batching window for same-discussion `@mention` bursts
- `MENTION_SELF_EVOLUTION_ENABLED` / `MENTION_SELF_EVOLUTION_INTERVAL_DAYS` / `MENTION_SELF_EVOLUTION_TIME_LOCAL` — Mention 自我演进的独立开关与固定日历时间计划
- `MENTION_MAX_CHANGED_FILES` — hard cap on how many files one mention-driven code change may touch before it must stop without pushing
- `AUTO_REVIEW_SELF_EVOLUTION_ENABLED` / `AUTO_REVIEW_SELF_EVOLUTION_INTERVAL_DAYS` / `AUTO_REVIEW_SELF_EVOLUTION_TIME_LOCAL` — Auto Review 自我演进的独立开关与固定日历时间计划
- `PHOENIX_TRACING_ENABLED` — enable optional local Phoenix tracing without making it a business dependency
- `PHOENIX_COLLECTOR_ENDPOINT` / `PHOENIX_API_KEY` / `PHOENIX_PROJECT_NAME` — optional local Phoenix connection settings
- `PHOENIX_UI_BASE_URL` — optional Phoenix browser base URL used by `/admin` to deep-link to traces and sessions
- `AUTO_REVIEW_MAX_PUBLISHED_FINDINGS` — upper bound used when expanding summary and inline findings for one run
- `AUTO_REVIEW_COMMENT_HISTORY_LIMIT` — number of prior bot comments to inspect for dedupe
- `AUTO_REVIEW_HUMAN_COMMENT_LIMIT` — number of recent human comments injected into review context
- `AUTO_REVIEW_FETCH_DEPTH` — fetch depth for target/source branch refs before diffing
- `DAILY_AUDIT_SELF_EVOLUTION_ENABLED` / `DAILY_AUDIT_SELF_EVOLUTION_INTERVAL_DAYS` / `DAILY_AUDIT_SELF_EVOLUTION_TIME_LOCAL` — Daily Audit 自我演进的独立开关与固定日历时间计划

Mutable application state is fixed under `/var/lib/open-review` and is no longer a user-managed setting:
- SQLite DB: `/var/lib/open-review/controlplane.db`
- Project cache: `/var/lib/open-review/project-cache/`
- Local sandboxes: `/var/lib/open-review/sandboxes/`
- Runtime scratch/artifacts: `/var/lib/open-review/runtime/`

Deployment scripts and container entrypoints must keep this path writable by the deployment user. Docker stack services and worker-created sandbox containers run with the host UID/GID so Docker deployment and local `uv` deployment can share the same state directory without root-owned files.

### LLM Model
Use `make_model()` from `agent/utils/model.py`. It resolves the active provider-specific base URL, API key, and model from runtime configuration, while still accepting legacy `provider:model` overrides for compatibility. Under the hood it now follows the same `init_chat_model(...)` initialization pattern used by upstream `deepagents` and `langchain-open-swe`. Never hardcode a model name or provider endpoint.

### Admin Console

The built-in admin console is served from `/admin` and provides:
- authenticated overview of live actor pressure and recent runs
- persistent run history backed by SQLite
- browser-based editing of runtime configuration
- browser-based operational visibility
- first-boot setup flow that creates the initial admin password

The console is intentionally not a generic CRUD backend. It is an operational surface for this bot.

### Optional Phoenix

Phoenix tracing is optional:
- if Phoenix is not deployed or not configured, the bot still works normally
- enabling Phoenix should add tracing only; it must not become a hard dependency for webhook processing
- current business state remains local to the open-review control plane even when Phoenix is enabled
- once Phoenix tracing code is in place, later enablement is configuration-only: deploy Phoenix, create a system API key, set the Phoenix env vars, and restart the server/worker

### Raw Record Principle

When the system persists agent data, the default rule is:
- persist raw source records once
- do not persist multiple semantically-equivalent assembled variants
- any consumer that needs a view of the data must assemble that view at read time from the raw source

This rule exists for three reasons:
- single source of truth: the system defines one canonical origin for each class of data
- simpler evolution: when schemas or consumers change, only the consumer-side assembly changes
- lower drift risk: different downstream features do not silently diverge because they were built on duplicated stored projections

In practice:
- `tracked_runs` is the main run-level raw record container
- `agent_records_json` stores raw agent input/output records
- `published_objects_json` stores raw published GitLab objects
- `feedback_events_json` stores raw external feedback linked back to a run
- if search, recall, memory extraction, self-evolution, analytics, or review tools need a transcript, summary, or feature-specific sample, they must derive it from these raw records instead of reading a separate preassembled store

What not to do:
- do not introduce a second persisted “formatted transcript” source if the raw messages already exist
- do not keep compatibility fallbacks to older duplicate stores once the raw source path is implemented and migrated
- do not persist “artifact samples” or other consumer-specific assembled payloads as an additional truth layer unless there is a clearly justified new canonical source decision

### Agentic Autonomy Principle

This system is intentionally agentic-first. When the system needs to decide whether something is worth preserving as a skill, memory, direction, or other semantic asset, the default rule is:
- let the agent decide through prompts, tools, and its own reasoning
- do not hardcode semantic value judgments in deterministic code paths
- use code to provide durable infrastructure, not to replace agent judgment

In practice:
- prompts should tell the agent what “good” looks like, for example generic, reusable, high-value, low-noise workflow knowledge
- tools should expose the relevant read/write surfaces so the agent can inspect existing state and decide whether to create, patch, or skip
- middleware and persistence layers should handle durability, scheduling, cancellation, tracing, auth, and storage integrity
- self-evolution, evaluation, and feedback loops should improve these behaviors over time instead of adding brittle rule gates

What not to do:
- do not add code-side keyword filters, score thresholds, or allow/deny lists to decide whether a skill or memory is valuable enough to save
- do not add deterministic “review layers” that override the agent’s semantic judgment about whether content is generic, useful, or noisy
- do not solve prompt-quality problems by inserting hardcoded repo-specific heuristics into tool write paths

Allowed deterministic constraints:
- tool input/output schema validation
- file/layout invariants such as `SKILL.md` structure and required arguments
- raw-record storage invariants, idempotency, and transactional integrity
- operational controls such as retries, cancellation, leases, and auth

### Agent Creation

Both scenes now use staged orchestrators.

Auto review:
- orchestration lives in `agent/scenes/auto_review/orchestrator.py`
- the entrypoint is one Director `create_deep_agent()` that manages five specialist agents
- specialists are writable investigation agents working in a shared temporary worktree
- specialists may call investigation subagents: `explore`, `task`, `trace-impact`, `counterexample`, and `general-purpose`
- specialists and subagents produce structured investigation reports and candidate findings
- the Director produces a structured report, not a suppress/publish decision
- raw agent records for the Director and named specialists are persisted by scene middleware, not by the orchestrator or trace wrappers
- `tracked_runs.agent_records_json` is the only raw agent-data source for auto-review runs
- specialist and subagent traces are wrapped in `open_review.*` Phoenix spans for per-lane debugging

Mention:
- orchestration lives in `agent/scenes/mention/orchestrator.py`
- every incoming mention is handled by one writable primary Mention Agent
- the primary Mention Agent may invoke four read-only auxiliary sub-agents: `dialogs`, `review`, `task`, and `explore`
- code changes happen in a temporary worktree and commit/push is still orchestrator-controlled
- mention-driven code changes must pass GitLab branch push checks, changed-file count limits, and head-SHA revalidation before commit/push
- raw agent records for the main author/reviewer agents are persisted by scene middleware, not by the orchestrator
- `tracked_runs.agent_records_json` is the only raw agent-data source for mention runs

Daily audit:
- orchestration lives in `agent/scenes/daily_audit/orchestrator.py`
- the main run is a two-stage agent workflow: `direction` first, then `analysis`
- `direction` is agent-driven: it must use its own tools (`direction_history`, `exploration_memory`, `session_search`, repo/file/shell tools) to explore source code and choose one bounded workflow direction
- `analysis` only follows the `selected_unit` returned by `direction`; it should not reopen direction selection
- transcript archive is the only inline business persistence step during `analysis`; middleware writes transcript synchronously and only then fans out async persistence work
- `tracked_runs` is the single main run record for daily audit raw data
- daily-audit transcript raw source is `tracked_runs.agent_records_json`, specifically the `daily_audit.analysis` record
- short-term summary generation, long-term memory extraction, and skill persistence review all read the same raw transcript source through the store layer
- no compatibility fallback is allowed for daily-audit transcript reads:
  - do not read legacy `daily_audit_run_transcripts`
  - do not read legacy transcript chunk tables
  - do not fallback to legacy thread ids
- daily-audit self-evolution is also raw-only:
  - it reads `tracked_runs.agent_records_json` and `feedback_events_json`
  - it does not read or generate legacy evolution artifact samples
- business persistence is intentionally reduced to four layers:
  - short-term summary
  - long-term memory
  - direction archive
  - transcript archive
- all LLM-backed post-run persistence is durable and asynchronous:
  - `daily_audit_direction_persistence`
  - `daily_audit_short_term_persistence`
  - `daily_audit_long_term_persistence`
  - `daily_audit_skill_persistence`
- daily-audit skills remain file-backed under the selfevolution tree; SQL is used for business persistence, not as the skill content source of truth
- in Docker stack environments, bundled daily-audit skills are mirrored into `/var/lib/open-review/runtime/daily_audit/bundled-skills/` so DeepAgents `SkillsMiddleware` can load them through sandbox-visible paths

Agent self-evolution:
- self-evolution is no longer owned by `daily_audit`; it is a shared agent-scoped runtime capability
- runtime events use the generic `agent_self_evolution` scene and carry `agent_type` in payload
- actor keys are per-project and per-agent, for example `project!self_evolution:mention`
- scheduling is independent from the main agent workflow:
  - each agent has its own `enabled + interval_days + fixed local time`
  - manual self-evolution triggers are also per-agent
- the only coupling between self-evolution and a scene is raw-record consumption:
  - `mention` self-evolution consumes mention raw runs
  - `auto_review` self-evolution consumes auto-review raw runs
  - `daily_audit` self-evolution consumes daily-audit raw runs
- self-evolution prompt assets are file-backed and participate in runtime behavior for `mention`, `auto_review`, and `daily_audit`
- self-evolution skills are also file-backed under each scene's `selfevolution/skills` tree; do not keep a second per-scene `agent/scenes/<agent>/skills` directory outside `selfevolution`
- shared baseline skills live under source-namespaced folders such as `agent/scenes/skills/superpowers/<skill-name>/SKILL.md`
- each immediate child of `agent/scenes/skills/` is treated as one shared skill collection and can be passed to DeepAgents as a native skill source
  - these are loaded before scene-specific skills by DeepAgents main agents and normal subagents
  - shared skills are read-only runtime assets and must not participate in self-evolution
  - self-evolution tools and apply paths must continue to target only `agent/scenes/<agent>/selfevolution/*`

### Sandbox
Each MR gets one sandbox keyed by `thread_id = sha256(project_id + mr_iid)`. The sandbox:
- Is created on first use and cached in `SANDBOX_CACHE`
- Has the MR's source branch cloned at `{sandbox.root_dir}/repo`
- Reuses the clone on subsequent events for the same MR
- Creates per-run detached worktrees under `{sandbox.root_dir}/worktrees/` so independent runs do not share dirty state

Architecturally:
- one MR maps to one reusable sandbox
- one run maps to one temporary worktree
- `local` and `docker` modes keep the same agent capability model; only the execution boundary changes
- in `docker` mode, the worker/orchestrator still runs in the main service, while repo/file/shell/build execution happens inside the sandbox container

### GitLab Tools
- `post_mr_comment(project_id, mr_iid, body)` — regular comment
- `post_inline_comment(project_id, mr_iid, file_path, line, body)` — inline diff comment, falls back to regular if line not in diff
- `list_mr_comments(project_id, mr_iid)` — list top-level MR notes
- `list_mr_discussion_comments(project_id, mr_iid)` — list discussion notes, including inline comments
- `list_mr_activity(project_id, mr_iid)` — merged chronological MR comment history for review context and dedupe
- Agent-facing tools still receive `project_id` / `mr_iid` / `repo_dir` from LangGraph `configurable` context

### Adding a New Tool
1. Create `agent/tools/<tool_name>.py` with a function whose docstring is the tool description
2. Use `get_config()["configurable"]` to access `project_id`, `mr_iid`, `repo_dir`
3. Export from `agent/tools/__init__.py`
4. Add to `tools=[...]` in the relevant agent's `graph.py`

### Adding a New Skill
Place a directory with a `SKILL.md` file (YAML frontmatter + markdown content) in:
- `agent/scenes/skills/<source>/<skill-name>/SKILL.md` — shared read-only baseline skill, not self-evolved; use source folders such as `superpowers` to keep independently supplied skill sets separate
- `agent/scenes/<agent>/selfevolution/skills/<skill-name>/SKILL.md` — bundled agent selfevolution skill
- `<repo>/.agents/skills/<skill-name>/SKILL.md` — per-repository skills

### Review Output Contract

Auto review now works with structured reports before publishing:
- `ReviewContext` carries MR metadata, diff range, changed files, commit messages, previous bot comments, and recent human comments
- `ReviewSeedContext` carries the Director's lightweight starting context for the current MR run
- `CandidateFinding` carries file, line, category, severity, confidence, evidence, recommended fix, and dedupe key
- `SpecialistReviewReport` carries lane status, investigation notes, supporting evidence, candidate findings, and open questions
- the Director finalizes one report with `confirmed findings`, `suspicious findings`, `open questions`, and `inline candidates`
- the summary comment is persistent per MR and includes report counts, lane health, and hidden dedupe markers

### Reference Repositories

`reference/` stores third-party repositories for design and workflow reference only.
- Do not import or depend directly on the checked-in code under `reference/` in production paths
- If a referenced project should be used in production, integrate it through its official or recommended mechanism:
  published Python package, git dependency, editable install, external CLI, sidecar service, or other upstream-documented integration path
- `reference/` is not a vendored runtime dependency directory
- Use it to study patterns, prompts, workflows, and tooling ideas
- When `reference/` code and the installed or upstream integration path differ, the installed or upstream integration path is the source of truth for runtime behavior
- Historical snapshots under `reference/` are not the source of truth for the current implementation; prefer code under `agent/` and this guide when they disagree

## Running Locally

```bash
# Install dependencies
uv sync

# Start the webhook server
uv run python -m uvicorn agent.webapp:app --host 0.0.0.0 --port 8000 --reload

# Start the durable runtime worker
uv run python -m agent.runtime.worker

# Open the built-in admin console
# http://localhost:8000/admin
# First boot goes through the one-time setup page.

# Expose to GitLab via Cloudflare Tunnel
cloudflared tunnel --url http://localhost:8000
```

## Deployment Commands

Run these on the deployment host after cloning the repository.

```bash
# Install locked dependencies
uv sync --frozen
```

```bash
# Start the webhook server in production mode
uv run python -m uvicorn agent.webapp:app --host 0.0.0.0 --port 8000
```

```bash
# Start the durable worker in a separate process
uv run python -m agent.runtime.worker
```

```bash
# Optional: expose the webhook endpoint publicly for GitLab
cloudflared tunnel --url http://localhost:8000
```

## Optional Local Phoenix

If you want optional tracing, start Phoenix from the bundled deployment assets:

```bash
cd deploy/phoenix
cp .env.example .env
# edit .env with real secrets before the first start
docker compose up -d
```

Then create a Phoenix system API key in the Phoenix UI and configure these values from the Open Review admin console:

```bash
PHOENIX_TRACING_ENABLED=true
PHOENIX_COLLECTOR_ENDPOINT=http://localhost:6006/v1/traces
PHOENIX_UI_BASE_URL=http://localhost:6006
PHOENIX_API_KEY=your-system-api-key
PHOENIX_PROJECT_NAME=open-review
```

After updating the bot config, restart both processes:

```bash
uv run python -m uvicorn agent.webapp:app --host 0.0.0.0 --port 8000
uv run python -m agent.runtime.worker
```

At minimum, production needs two long-running processes or services:
- `uvicorn` for `agent.webapp:app`
- worker for `agent.runtime.worker`

The built-in admin console is served by the same `uvicorn` process at `/admin`.

On the first visit to `/admin`, the service shows a one-time setup page for creating the initial admin password. After that, configure GitLab, LLM, agent scheduling, and tracing from the admin UI; those settings are persisted in `/var/lib/open-review/controlplane.db`.

For a packaged Docker-based test environment, prefer `deploy/stack/`, which composes:
- Open Review `web`
- Open Review `worker`
- Phoenix
- Phoenix Postgres
- the pinned Docker sandbox image used by the worker

Use `deploy/stack/doctor.sh` for host preflight checks. `deploy/stack/deploy.sh` can repair `/var/lib/open-review` ownership with `sudo` when run interactively.

## Deployment Lessons

- Phoenix collector endpoint must be the OTLP traces path, not the UI root:
  - correct: `PHOENIX_COLLECTOR_ENDPOINT=http://localhost:6006/v1/traces`
  - wrong: `PHOENIX_COLLECTOR_ENDPOINT=http://localhost:6006`
  - if the endpoint is wrong, worker logs will show `Failed to export span batch code: 405, reason: Method Not Allowed`
- `PHOENIX_UI_BASE_URL` should stay at the browser root, for example `http://localhost:6006`
- After enabling Phoenix or rotating its API key, restart both long-running bot processes:
  - `uv run python -m uvicorn agent.webapp:app ...`
  - `uv run python -m agent.runtime.worker`
- The fastest way to confirm tracing is working:
  - trigger a new `mention` or `auto_review` run
  - check `/admin/runs` or `tracked_runs.trace_id` / `tracked_runs.trace_url`
  - older runs will not backfill into Phoenix after tracing is enabled
- Replaying the exact same `merge_request update` webhook for the same MR head SHA will usually be deduped by `event_id`
  - for repeat manual testing, use a new head SHA, a different action such as `reopen`, or a fresh `note_id` for `mention`
- Cached sandboxes may print a non-blocking clone warning after process restarts:
  - `destination path .../repo already exists and is not an empty directory`
  - current behavior falls back and continues, but the warning is expected until sandbox bootstrap is cleaned up further

## Running Tests

```bash
uv sync --extra dev
python -m pytest tests/ -v
```

## Webhook Events Handled

| Event | Condition | Action |
|-------|-----------|--------|
| `merge_request` open/reopen | Not draft | Auto Review Orchestrator |
| `merge_request` update + push | Not draft | Auto Review Orchestrator |
| `merge_request` draft → ready | — | Auto Review Orchestrator |
| `note` on MR | Contains `@<resolved-bot-username>` | Mention Orchestrator |
