# AGENTS.md — reference Directory Guide

## Purpose

This directory stores third-party projects for reference only.

- Do not treat code under `reference/` as part of the `open-review` runtime.
- Do not import from these projects in production code.
- Do not rely on these projects as direct dependencies of this repository.
- Use them only to study implementation ideas, prompts, workflows, or project structure.

## Notes

- Many entries keep their own `.git` history because they were collected as standalone upstream repositories.
- Changes under this directory should be rare and should only improve the local reference archive or its documentation.
- If you need reusable functionality, reimplement it inside this repository instead of calling code from `reference/`.
- Upstream repositories under `reference/` may contain their own `AGENTS.md` files. Treat those as upstream reference material, not as Open Review-owned documentation to keep in sync.

## Upstream Repositories

- `OpenHands` — `https://github.com/OpenHands/OpenHands.git`
- `aider` — `https://github.com/Aider-AI/aider.git`
- `deepagents` — `https://github.com/langchain-ai/deepagents.git`
- `langchain-open-swe` — `https://github.com/langchain-ai/open-swe.git`
- `mini-swe-agent` — `https://github.com/SWE-agent/mini-swe-agent.git`
- `opencode` — `https://github.com/opencode-ai/opencode.git`
- `openreview` — `https://github.com/vercel-labs/openreview.git`
- `pr-agent` — `https://github.com/Codium-ai/pr-agent.git`
