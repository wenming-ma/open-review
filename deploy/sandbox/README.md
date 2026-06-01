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
- symbol lookup and navigation with `universal-ctags`, `cscope`, `clangd`, and GNU Global (`gtags`)
- compile database capture with `bear`
- static analysis with `clang-tidy` and `cppcheck`
- local C/C++ build verification with `cmake`, `ninja`, `gcc/g++`, and `clang/clang++`
- local benchmark and A/B experiment support with `google-benchmark`
- standard graphics/rendering library experiments with `GLM`, `Cairo`, `Pixman`, `Freetype`, `HarfBuzz`, `Fontconfig`, and Mesa OpenGL headers
- shell/file inspection helpers such as `ripgrep`

It is intended for local benchmark and local experiment workflows, not whole-project builds.
It does **not** try to bundle a full KiCad or project-specific EDA system dependency set.
