#!/usr/bin/env bash
set -euo pipefail

ROLE="${OPEN_REVIEW_RUNTIME_ROLE:-web}"
STATE_ROOT="/var/lib/open-review"

mkdir -p \
  "${STATE_ROOT}" \
  "${STATE_ROOT}/project-cache" \
  "${STATE_ROOT}/sandboxes" \
  "${STATE_ROOT}/runtime"

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
