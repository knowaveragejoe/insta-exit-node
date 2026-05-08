#!/usr/bin/env bash
# Quick-start wrapper for the SSH provisioning flow.
# Edit examples/.env with your settings, then run: ./examples/run.sh
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"

# Load .env if present
ENV_FILE="${SCRIPT_DIR}/.env"
if [ -f "${ENV_FILE}" ]; then
  set -a
  # shellcheck source=/dev/null
  . "${ENV_FILE}"
  set +a
else
  echo "Hint: copy examples/.env.example to examples/.env and fill in your values."
fi

: "${TS_AUTHKEY:?TS_AUTHKEY is required. Set it in examples/.env or in the environment.}"
: "${EXIT_HOST:?EXIT_HOST is required (e.g. root@1.2.3.4). Set it in examples/.env.}"

EXIT_HOSTNAME="${EXIT_HOSTNAME:-insta-exit-1}"
EXIT_TAG="${EXIT_TAG:-tag:exit}"

exec uv run "${REPO_ROOT}/bin/insta-exit-node" ssh \
  --host "${EXIT_HOST}" \
  --hostname "${EXIT_HOSTNAME}" \
  --tag "${EXIT_TAG}"
