#!/usr/bin/env bash
set -euo pipefail

STATE_DIR="/var/lib/open-review"
FIX=0

if [[ "${1:-}" == "--fix" ]]; then
  FIX=1
fi

ok() {
  echo "ok: $*"
}

warn() {
  echo "warn: $*" >&2
}

fail() {
  echo "fail: $*" >&2
  FAILED=1
}

fix_state_dir() {
  if (( FIX == 0 )); then
    warn "state directory repair is available with: ./doctor.sh --fix"
    printf '  sudo install -d -o %q -g %q -m 0750 %q\n' "$(id -un)" "$(id -gn)" "${STATE_DIR}" >&2
    printf '  sudo chown -R %q %q\n' "$(id -u):$(id -g)" "${STATE_DIR}" >&2
    return 1
  fi
  if ! command -v sudo >/dev/null 2>&1; then
    fail "sudo is not available; cannot repair ${STATE_DIR}"
    return 1
  fi
  sudo install -d -o "$(id -un)" -g "$(id -gn)" -m 0750 "${STATE_DIR}"
  sudo chown -R "$(id -u):$(id -g)" "${STATE_DIR}"
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

FAILED=0

echo "Open Review stack doctor"
echo "user: $(id -un) uid=$(id -u) gid=$(id -g)"

if docker compose version >/dev/null 2>&1; then
  ok "docker compose is available"
elif docker-compose version >/dev/null 2>&1; then
  ok "docker-compose is available"
else
  fail "neither docker compose nor docker-compose is available"
fi

if docker info >/dev/null 2>&1; then
  ok "Docker daemon is reachable"
else
  fail "Docker daemon is not reachable by this user"
fi

if [[ -S /var/run/docker.sock ]]; then
  socket_gid="$(stat -c '%g' /var/run/docker.sock 2>/dev/null || true)"
  ok "Docker socket exists at /var/run/docker.sock group=${socket_gid:-unknown}"
else
  fail "Docker socket is missing at /var/run/docker.sock"
fi

if [[ ! -d "${STATE_DIR}" ]]; then
  warn "${STATE_DIR} does not exist"
  fix_state_dir || true
fi

if [[ -d "${STATE_DIR}" ]]; then
  state_owner="$(stat -c '%U:%G %a' "${STATE_DIR}" 2>/dev/null || true)"
  test_file="${STATE_DIR}/.open-review-write-test.$$"
  if (umask 077 && : > "${test_file}") 2>/dev/null; then
    rm -f "${test_file}"
    ok "${STATE_DIR} is writable (${state_owner})"
  else
    fail "${STATE_DIR} is not writable by uid=$(id -u)"
    fix_state_dir || true
  fi
fi

for port in "${OPEN_REVIEW_WEB_PORT:-8000}" "${PHOENIX_PORT:-6006}" "${PHOENIX_OTLP_GRPC_PORT:-4317}"; do
  if port_in_use "${port}"; then
    warn "port ${port} is already in use; deploy.sh will choose the next free port when needed"
  else
    ok "port ${port} is free"
  fi
done

if [[ "${OPEN_REVIEW_DOCTOR_CHECK_APT:-0}" == "1" ]]; then
  if docker run --rm python:3.12-slim bash -lc 'apt-get update >/tmp/apt.log && tail -5 /tmp/apt.log' >/dev/null; then
    ok "Docker build apt network check passed"
  else
    fail "Docker build apt network check failed; check proxy, DNS, or Debian mirror access"
  fi
else
  warn "skipping Docker apt network check; set OPEN_REVIEW_DOCTOR_CHECK_APT=1 to run it"
fi

if (( FAILED != 0 )); then
  exit 1
fi
