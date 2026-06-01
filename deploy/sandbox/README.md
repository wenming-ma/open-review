# Docker Sandbox Image

This directory packages the Open Review Docker sandbox as a buildable, testable deployment unit.

## Quick Start

```bash
cd deploy/sandbox
cp .env.example .env
docker compose build
docker compose run --rm sandbox-smoke
```

## Smoke Checks

The smoke container verifies that the current sandbox toolchain is present:

- `git --version`
- `python3 --version`
- `node --version`
- `npm --version`
- `jq --version`
- `shellcheck --version`
- `clang-tidy --version`
- `cppcheck --version`
- `ctags --version`
- `cscope --version`
- `clangd --version`
- `gtags --version`
- `bear --version`
- `cmake --version`
- `rg --version`

## Scope

This image intentionally matches the bot's current sandbox needs:

- git operations inside the container
- repository inspection helpers such as `ripgrep`, `jq`, `shellcheck`, and `universal-ctags`
- Python and JavaScript/TypeScript project inspection with `python3`, `uv`, `node`, and `npm`
- Compiled-language static evidence support with `clangd`, `clang-tidy`, `cppcheck`, `cscope`, GNU Global (`gtags`), `bear`, `cmake`, `ninja`, `gcc/g++`, and `clang/clang++`
- local benchmark and A/B experiment support with `google-benchmark` where useful
- standard graphics/rendering library experiments with `GLM`, `Cairo`, `Pixman`, `Freetype`, `HarfBuzz`, `Fontconfig`, and Mesa OpenGL headers

It is intended for local benchmark and local experiment workflows, not whole-project builds.
It does **not** try to bundle every language runtime or project-specific system dependency set.
