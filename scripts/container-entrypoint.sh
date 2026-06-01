#!/usr/bin/env bash
set -euo pipefail

ROLE="${OPEN_REVIEW_RUNTIME_ROLE:-web}"
STATE_ROOT="/var/lib/open-review"
CONTAINER_UID="${OPEN_REVIEW_UID:-$(id -u)}"
CONTAINER_GID="${OPEN_REVIEW_GID:-$(id -g)}"

print_state_fix() {
  echo "Open Review cannot write ${STATE_ROOT} as uid=${CONTAINER_UID} gid=${CONTAINER_GID}." >&2
  echo "Run this on the host, then restart the container:" >&2
  echo "  sudo install -d -o ${CONTAINER_UID} -g ${CONTAINER_GID} -m 0750 ${STATE_ROOT}" >&2
  echo "  sudo chown -R ${CONTAINER_UID}:${CONTAINER_GID} ${STATE_ROOT}" >&2
}

ensure_dir_writable() {
  local path="$1"
  local test_file

  if ! mkdir -p "${path}" 2>/dev/null; then
    print_state_fix
    exit 1
  fi

  test_file="${path}/.open-review-write-test.$$"
  if ! (umask 077 && : > "${test_file}") 2>/dev/null; then
    print_state_fix
    exit 1
  fi
  rm -f "${test_file}"
}

ensure_dir_writable "${STATE_ROOT}"
ensure_dir_writable "${STATE_ROOT}/project-cache"
ensure_dir_writable "${STATE_ROOT}/sandboxes"
ensure_dir_writable "${STATE_ROOT}/runtime"

export HOME="${OPEN_REVIEW_HOME:-${STATE_ROOT}/runtime/home}"
ensure_dir_writable "${HOME}"

python -c "from agent.selfevolution.repo import ensure_self_repo_checkout; ensure_self_repo_checkout()"

case "$ROLE" in
  web)
    exec python -m uvicorn agent.webapp:app --host 0.0.0.0 --port "${PORT:-8000}"
    ;;
  worker)
    exec python -m agent.runtime.worker
    ;;
  *)
    echo "unsupported OPEN_REVIEW_RUNTIME_ROLE: $ROLE" >&2
    exit 1
    ;;
esac
