from __future__ import annotations

import os
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]


def _read(relative_path: str) -> str:
    return (REPO_ROOT / relative_path).read_text(encoding="utf-8")


def test_phoenix_deploy_uses_pinned_upstream_image_and_docs_match_runtime() -> None:
    dockerfile = REPO_ROOT / "deploy/phoenix/Dockerfile"
    compose_text = _read("deploy/phoenix/docker-compose.yml")
    env_text = _read("deploy/phoenix/.env.example")
    readme_text = _read("deploy/phoenix/README.md")

    assert not dockerfile.exists()
    assert "build:" not in compose_text
    assert "image: ${OPEN_REVIEW_PHOENIX_IMAGE}" in compose_text
    assert "OPEN_REVIEW_PHOENIX_IMAGE=arizephoenix/phoenix:14.2.1" in env_text
    assert "arizephoenix/phoenix:latest" not in env_text
    assert "/v1/traces" in readme_text
    assert "does not build or publish a Phoenix image" in readme_text


def test_sandbox_deploy_has_buildable_bundle() -> None:
    compose_path = REPO_ROOT / "deploy/sandbox/docker-compose.yml"
    env_example_path = REPO_ROOT / "deploy/sandbox/.env.example"
    readme_path = REPO_ROOT / "deploy/sandbox/README.md"

    assert compose_path.exists()
    assert env_example_path.exists()
    assert readme_path.exists()

    compose_text = compose_path.read_text(encoding="utf-8")
    readme_text = readme_path.read_text(encoding="utf-8")
    dockerfile_text = (REPO_ROOT / "deploy/sandbox/Dockerfile").read_text(encoding="utf-8")

    assert "build:" in compose_text
    assert "dockerfile: deploy/sandbox/Dockerfile" in compose_text
    assert "image: ${OPEN_REVIEW_SANDBOX_IMAGE}" in compose_text
    assert "pip install --timeout=600 --retries=20 uv" in dockerfile_text
    for needle in [
        "bear",
        "clang",
        "clangd",
        "libbenchmark-dev",
        "libfmt-dev",
        "nlohmann-json3-dev",
        "libboost-locale-dev",
        "libglm-dev",
        "libcairo2-dev",
        "libpixman-1-dev",
        "libfreetype-dev",
        "libharfbuzz-dev",
        "libfontconfig1-dev",
        "libgl1-mesa-dev",
        "cscope",
        "global",
        "universal-ctags",
    ]:
        assert needle in dockerfile_text
    assert "git --version" in readme_text
    assert "clang-tidy --version" in readme_text
    assert "ctags --version" in readme_text
    assert "clangd --version" in readme_text
    assert "bear --version" in readme_text
    assert "local benchmark" in readme_text.lower()


def test_legacy_static_analysis_wrappers_are_removed() -> None:
    tools_dir = REPO_ROOT / "agent" / "tools"

    assert not (tools_dir / "compile_check.py").exists()
    assert not (tools_dir / "static_analysis.py").exists()

    init_text = (tools_dir / "__init__.py").read_text(encoding="utf-8")
    assert "compile_check" not in init_text
    assert "run_static_analysis" not in init_text


def test_shared_superpower_skills_are_bundled() -> None:
    shared_root = REPO_ROOT / "agent" / "scenes" / "skills" / "superpowers"

    for skill_name in [
        "brainstorming",
        "test-driven-development",
        "using-superpowers",
        "verification-before-completion",
        "writing-plans",
    ]:
        assert (shared_root / skill_name / "SKILL.md").exists()

    for relative in [
        "brainstorming/scripts/server.cjs",
        "using-superpowers/references/codex-tools.md",
        "writing-skills/examples/CLAUDE_MD_TESTING.md",
    ]:
        assert (shared_root / relative).exists()


def test_stack_deploy_bundles_open_review_services_and_phoenix() -> None:
    compose_path = REPO_ROOT / "deploy/stack/docker-compose.yml"
    deploy_script_path = REPO_ROOT / "deploy/stack/deploy.sh"
    doctor_script_path = REPO_ROOT / "deploy/stack/doctor.sh"
    env_example_path = REPO_ROOT / "deploy/stack/.env.example"
    readme_path = REPO_ROOT / "deploy/stack/README.md"

    assert compose_path.exists()
    assert deploy_script_path.exists()
    assert doctor_script_path.exists()
    assert os.access(doctor_script_path, os.X_OK)
    assert env_example_path.exists()
    assert readme_path.exists()

    compose_text = compose_path.read_text(encoding="utf-8")
    deploy_script_text = deploy_script_path.read_text(encoding="utf-8")
    doctor_script_text = doctor_script_path.read_text(encoding="utf-8")
    env_text = env_example_path.read_text(encoding="utf-8")
    readme_text = readme_path.read_text(encoding="utf-8")

    for needle in [
        "web:",
        "worker:",
        "phoenix:",
        "phoenix-db:",
        "OPEN_REVIEW_RUNTIME_ROLE=worker",
        "DOCKER_IMAGE=${OPEN_REVIEW_SANDBOX_IMAGE:-open-review/sandbox:0.1.0}",
        "/var/run/docker.sock:/var/run/docker.sock",
        "/var/lib/open-review:/var/lib/open-review",
        'user: "${OPEN_REVIEW_UID:-1000}:${OPEN_REVIEW_GID:-1000}"',
        "group_add:",
        "${OPEN_REVIEW_DOCKER_GID:-0}",
        "OPEN_REVIEW_UID=${OPEN_REVIEW_UID:-1000}",
        "OPEN_REVIEW_GID=${OPEN_REVIEW_GID:-1000}",
    ]:
        assert needle in compose_text

    assert "env_file:" not in compose_text
    assert "open_review_state:/var/lib/open-review" not in compose_text
    assert "${OPEN_REVIEW_STATE_DIR}:/var/lib/open-review" not in compose_text
    assert "OPEN_REVIEW_IMAGE=" in env_text
    assert "OPEN_REVIEW_SANDBOX_IMAGE=" in env_text
    assert "OPEN_REVIEW_PHOENIX_IMAGE=" in env_text
    assert "OPEN_REVIEW_PHOENIX_IMAGE=arizephoenix/phoenix:14.2.1" in env_text
    assert "OPEN_REVIEW_UID=" in env_text
    assert "OPEN_REVIEW_GID=" in env_text
    assert "OPEN_REVIEW_DOCKER_GID=" in env_text
    assert "PHOENIX_TRACING_ENABLED=" in env_text
    assert "PHOENIX_API_KEY=" in env_text
    assert "PHOENIX_UI_BASE_URL=" in env_text
    assert "./deploy.sh" in readme_text
    assert "docker compose version" in deploy_script_text
    assert "docker-compose version" in deploy_script_text
    assert "pick_port" in deploy_script_text
    assert "port_in_use_by_other_project" in deploy_script_text
    assert "com.docker.compose.project" in deploy_script_text
    assert "ensure_state_dir_ready" in deploy_script_text
    assert "repair_state_dir_with_sudo" in deploy_script_text
    assert "OPEN_REVIEW_DEPLOY_AUTO_FIX_STATE_DIR" in deploy_script_text
    assert "detect_docker_socket_gid" in deploy_script_text
    assert "Using container user" in deploy_script_text
    assert "/var/lib/open-review" in deploy_script_text
    assert ".open-review-write-test" in deploy_script_text
    assert "compose_up_from_loaded_images" in deploy_script_text
    assert "stop_existing_open_review_stacks" in deploy_script_text
    assert "remove_old_open_review_images" in deploy_script_text
    assert '"${OPEN_REVIEW_PHOENIX_IMAGE' not in deploy_script_text
    assert "docker rm -f" in deploy_script_text
    assert "docker rmi" in deploy_script_text
    assert "--volumes" not in deploy_script_text
    assert "COMPOSE_PROJECT_NAME" in deploy_script_text
    assert "open_review_stack_${WEB_PORT}" not in deploy_script_text
    assert "COMPOSE_PROJECT_NAME=\"${COMPOSE_PROJECT_NAME:-open_review_stack}\"" in deploy_script_text
    assert "docker load -i" in deploy_script_text
    assert "--pull never" in deploy_script_text
    assert "--no-build" in deploy_script_text
    assert "up -d --build" in deploy_script_text
    assert "Open Review stack doctor" in doctor_script_text
    assert "OPEN_REVIEW_DOCTOR_CHECK_APT" in doctor_script_text
    assert "./doctor.sh --fix" in doctor_script_text
    assert "service-repo" in readme_text
    assert "run-scoped sandbox experiment directory" in readme_text
    assert "host UID/GID" in readme_text
    assert "stops any existing Open Review stack containers before loading bundled images" in readme_text
    assert "removes old Open Review service images" in readme_text
    assert "stable default `COMPOSE_PROJECT_NAME`" in readme_text
    assert "derives a safe default `COMPOSE_PROJECT_NAME` from the chosen web port" not in readme_text


def test_docs_do_not_reference_removed_open_review_state_dir() -> None:
    agents_text = _read("AGENTS.md")

    assert "OPEN_REVIEW_STATE_DIR" not in agents_text


def test_root_dockerfile_copies_readme_before_uv_sync() -> None:
    dockerfile_text = _read("Dockerfile")

    assert "COPY pyproject.toml uv.lock README.md ./" in dockerfile_text
    assert "COPY .env.example" not in dockerfile_text
    assert "docker-cli" in dockerfile_text
    assert "docker.io" not in dockerfile_text
    assert "RUN UV_HTTP_TIMEOUT=180 uv sync --frozen --no-dev" in dockerfile_text
