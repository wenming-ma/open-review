#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
cd "${SCRIPT_DIR}"

pick_compose_cmd() {
  if docker compose version >/dev/null 2>&1; then
    COMPOSE_CMD=(docker compose)
    COMPOSE_FLAVOR="v2"
    return
  fi

  if docker-compose version >/dev/null 2>&1; then
    COMPOSE_CMD=(docker-compose)
    COMPOSE_FLAVOR="v1"
    return
  fi

  echo "Neither 'docker compose' nor 'docker-compose' is available." >&2
  exit 1
}

port_in_use() {
  local port="$1"

  if command -v ss >/dev/null 2>&1; then
    ss -ltn 2>/dev/null | awk '{print $4}' | grep -Eq "(^|:|\\])${port}$"
    return
  fi

  if command -v netstat >/dev/null 2>&1; then
    netstat -ltn 2>/dev/null | awk '{print $4}' | grep -Eq "(^|:|\\])${port}$"
    return
  fi

  return 1
}

port_in_use_by_other_project() {
  local port="$1"
  local containers=()
  local container
  local project

  if ! port_in_use "${port}"; then
    return 1
  fi

  while IFS= read -r container; do
    [[ -n "${container}" ]] && containers+=("${container}")
  done < <(docker ps --filter "publish=${port}" --format '{{.ID}}')

  if (( ${#containers[@]} == 0 )); then
    return 0
  fi

  for container in "${containers[@]}"; do
    project="$(docker inspect --format '{{ index .Config.Labels "com.docker.compose.project" }}' "${container}" 2>/dev/null || true)"
    if [[ "${project}" != "${COMPOSE_PROJECT_NAME}" ]]; then
      return 0
    fi
  done

  return 1
}

pick_port() {
  local requested="$1"
  local port="${requested}"

  while port_in_use_by_other_project "${port}"; do
    port=$((port + 1))
  done

  printf '%s\n' "${port}"
}

compose_down_selected_project() {
  echo "Stopping existing compose project if present: ${COMPOSE_PROJECT_NAME}"
  env "COMPOSE_PROJECT_NAME=${COMPOSE_PROJECT_NAME}" "${COMPOSE_CMD[@]}" down --remove-orphans || true
}

is_legacy_open_review_project() {
  local project="$1"
  [[ "${project}" =~ ^open-review-stack-[0-9]{8}t[0-9]{6}z$ ]]
}

remove_containers() {
  local label="$1"
  shift
  local containers=("$@")
  local unique=()
  local seen=" "
  local container

  if (( ${#containers[@]} == 0 )); then
    return
  fi

  for container in "${containers[@]}"; do
    if [[ " ${seen} " == *" ${container} "* ]]; then
      continue
    fi
    seen+="${container} "
    unique+=("${container}")
  done

  echo "Removing ${label}: ${unique[*]}"
  docker rm -f "${unique[@]}" >/dev/null
}

remove_legacy_open_review_stack_containers() {
  local containers=()
  local container
  local project

  while IFS= read -r container; do
    [[ -n "${container}" ]] || continue
    project="$(docker inspect --format '{{ index .Config.Labels "com.docker.compose.project" }}' "${container}" 2>/dev/null || true)"
    if is_legacy_open_review_project "${project}" && [[ "${project}" != "${COMPOSE_PROJECT_NAME}" ]]; then
      containers+=("${container}")
    fi
  done < <(docker ps -aq --filter "label=com.docker.compose.project")

  remove_containers "legacy Open Review stack containers" "${containers[@]}"
}

remove_open_review_image_containers() {
  local images=(
    "${OPEN_REVIEW_IMAGE:-open-review:0.1.0}"
    "${OPEN_REVIEW_SANDBOX_IMAGE:-open-review/sandbox:0.1.0}"
    "${OPEN_REVIEW_PHOENIX_IMAGE:-open-review/phoenix:14.2.1}"
  )
  local image
  local container
  local containers=()

  for image in "${images[@]}"; do
    while IFS= read -r container; do
      [[ -n "${container}" ]] && containers+=("${container}")
    done < <(docker ps -aq --filter "ancestor=${image}")
  done

  while IFS= read -r container; do
    [[ -n "${container}" ]] && containers+=("${container}")
  done < <(docker ps -aq --filter "name=open-review-sandbox-")

  remove_containers "old Open Review containers" "${containers[@]}"
}

stop_existing_open_review_stacks() {
  compose_down_selected_project
  remove_legacy_open_review_stack_containers
  remove_open_review_image_containers
}

remove_old_open_review_images() {
  local images=(
    "${OPEN_REVIEW_IMAGE:-open-review:0.1.0}"
    "${OPEN_REVIEW_SANDBOX_IMAGE:-open-review/sandbox:0.1.0}"
    "${OPEN_REVIEW_PHOENIX_IMAGE:-open-review/phoenix:14.2.1}"
  )
  local image

  echo "Removing old Open Review service images if present."
  for image in "${images[@]}"; do
    if ! docker image inspect "${image}" >/dev/null 2>&1; then
      continue
    fi
    if ! docker rmi "${image}" >/dev/null; then
      echo "Image ${image} is still in use; continuing with bundled image load."
    fi
  done
}

ensure_docker_ready() {
  if ! docker info >/dev/null 2>&1; then
    echo "Docker daemon is not reachable. Start Docker first." >&2
    exit 1
  fi
}

print_state_dir_fix_commands() {
  local state_dir="$1"

  echo "Run these commands on the host, then rerun deploy.sh:" >&2
  printf '  sudo install -d -o %q -g %q -m 0750 %q\n' "$(id -un)" "$(id -gn)" "${state_dir}" >&2
  printf '  sudo chown -R %q %q\n' "$(id -u):$(id -g)" "${state_dir}" >&2
}

repair_state_dir_with_sudo() {
  local state_dir="$1"

  if [[ "${OPEN_REVIEW_DEPLOY_AUTO_FIX_STATE_DIR:-1}" == "0" ]]; then
    return 1
  fi
  if ! command -v sudo >/dev/null 2>&1; then
    return 1
  fi
  if [[ ! -t 0 ]]; then
    return 1
  fi

  echo "Attempting to prepare ${state_dir} with sudo."
  sudo install -d -o "$(id -un)" -g "$(id -gn)" -m 0750 "${state_dir}" &&
    sudo chown -R "$(id -u):$(id -g)" "${state_dir}"
}

state_dir_is_writable() {
  local state_dir="$1"
  local test_file="${state_dir}/.open-review-write-test.$$"

  if ! (umask 077 && : > "${test_file}") 2>/dev/null; then
    return 1
  fi
  rm -f "${test_file}"
}

ensure_state_dir_ready() {
  local state_dir="/var/lib/open-review"

  if ! mkdir -p "${state_dir}" 2>/dev/null; then
    if ! repair_state_dir_with_sudo "${state_dir}" || ! mkdir -p "${state_dir}" 2>/dev/null; then
      echo "Cannot create ${state_dir} as deployment user $(id -un) (uid=$(id -u))." >&2
      print_state_dir_fix_commands "${state_dir}"
      exit 1
    fi
  fi

  if ! state_dir_is_writable "${state_dir}"; then
    if ! repair_state_dir_with_sudo "${state_dir}" || ! state_dir_is_writable "${state_dir}"; then
      echo "${state_dir} is not writable by deployment user $(id -un) (uid=$(id -u))." >&2
      print_state_dir_fix_commands "${state_dir}"
      exit 1
    fi
  fi
}

detect_docker_socket_gid() {
  if [[ -S /var/run/docker.sock ]] && command -v stat >/dev/null 2>&1; then
    stat -c '%g' /var/run/docker.sock 2>/dev/null && return
  fi
  id -g
}

find_image_bundle() {
  local matches=()

  while IFS= read -r match; do
    matches+=("${match}")
  done < <(find "${SCRIPT_DIR}" -maxdepth 1 -type f -name 'open-review-stack-images-*.tar' | sort)

  if (( ${#matches[@]} == 0 )); then
    return 1
  fi

  printf '%s\n' "${matches[-1]}"
}

maybe_load_local_images() {
  local image_tar
  if ! image_tar="$(find_image_bundle)"; then
    return 1
  fi

  echo "Loading local image bundle: ${image_tar}"
  docker load -i "${image_tar}"
}

compose_up_from_loaded_images() {
  if [[ "${COMPOSE_FLAVOR}" == "v2" ]]; then
    env "${COMPOSE_ENV[@]}" "${COMPOSE_CMD[@]}" up -d --pull never
    return
  fi

  env "${COMPOSE_ENV[@]}" "${COMPOSE_CMD[@]}" up -d --no-build
}

cleanup_historical_sandboxes() {
  if [[ "${OPEN_REVIEW_DEPLOY_CLEANUP_HISTORICAL_SANDBOXES:-1}" == "0" ]]; then
    echo "Skipping historical sandbox cleanup because OPEN_REVIEW_DEPLOY_CLEANUP_HISTORICAL_SANDBOXES=0."
    return
  fi

  echo "Running deployment-time historical sandbox cleanup."
  if ! env "${COMPOSE_ENV[@]}" "${COMPOSE_CMD[@]}" exec -T worker \
    python -m agent.maintenance.sandbox_cleanup; then
    echo "Historical sandbox cleanup failed; deployment will continue." >&2
  fi
}

ensure_docker_ready
ensure_state_dir_ready
pick_compose_cmd

COMPOSE_PROJECT_NAME="${COMPOSE_PROJECT_NAME:-open_review_stack}"

stop_existing_open_review_stacks
remove_old_open_review_images

WEB_PORT="$(pick_port "${OPEN_REVIEW_WEB_PORT:-8000}")"
PHOENIX_PORT_SELECTED="$(pick_port "${PHOENIX_PORT:-6006}")"
PHOENIX_GRPC_PORT_SELECTED="$(pick_port "${PHOENIX_OTLP_GRPC_PORT:-4317}")"
OPEN_REVIEW_UID_SELECTED="${OPEN_REVIEW_UID:-$(id -u)}"
OPEN_REVIEW_GID_SELECTED="${OPEN_REVIEW_GID:-$(id -g)}"
OPEN_REVIEW_DOCKER_GID_SELECTED="${OPEN_REVIEW_DOCKER_GID:-$(detect_docker_socket_gid)}"

echo "Using compose command: ${COMPOSE_CMD[*]}"
echo "Using project name: ${COMPOSE_PROJECT_NAME}"
echo "Using container user: ${OPEN_REVIEW_UID_SELECTED}:${OPEN_REVIEW_GID_SELECTED}"
echo "Using ports:"
echo "  Open Review: ${WEB_PORT}"
echo "  Phoenix UI: ${PHOENIX_PORT_SELECTED}"
echo "  Phoenix OTLP gRPC: ${PHOENIX_GRPC_PORT_SELECTED}"

COMPOSE_ENV=(
  "COMPOSE_PROJECT_NAME=${COMPOSE_PROJECT_NAME}"
  "OPEN_REVIEW_WEB_PORT=${WEB_PORT}"
  "OPEN_REVIEW_UID=${OPEN_REVIEW_UID_SELECTED}"
  "OPEN_REVIEW_GID=${OPEN_REVIEW_GID_SELECTED}"
  "OPEN_REVIEW_DOCKER_GID=${OPEN_REVIEW_DOCKER_GID_SELECTED}"
  "PHOENIX_PORT=${PHOENIX_PORT_SELECTED}"
  "PHOENIX_OTLP_GRPC_PORT=${PHOENIX_GRPC_PORT_SELECTED}"
)

if maybe_load_local_images; then
  compose_up_from_loaded_images
else
  env "${COMPOSE_ENV[@]}" "${COMPOSE_CMD[@]}" up -d --build
fi

cleanup_historical_sandboxes

echo
echo "Stack started."
echo "Open Review:  http://localhost:${WEB_PORT}/admin"
echo "Phoenix:  http://localhost:${PHOENIX_PORT_SELECTED}"
