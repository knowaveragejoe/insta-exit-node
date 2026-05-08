#!/usr/bin/env bash
# Example: create a DigitalOcean droplet using cloud-init user-data.
# Requires: doctl (https://docs.digitalocean.com/reference/doctl/how-to/install/)
# Edit examples/.env with your settings before running.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"

ENV_FILE="${SCRIPT_DIR}/.env"
if [ -f "${ENV_FILE}" ]; then
  set -a
  # shellcheck source=/dev/null
  . "${ENV_FILE}"
  set +a
fi

: "${TS_AUTHKEY:?TS_AUTHKEY is required.}"
: "${DO_SSH_KEY:?DO_SSH_KEY is required (doctl compute ssh-key list to find yours).}"

EXIT_HOSTNAME="${EXIT_HOSTNAME:-insta-exit-1}"
EXIT_TAG="${EXIT_TAG:-tag:exit}"
DO_REGION="${DO_REGION:-nyc3}"
DO_SIZE="${DO_SIZE:-s-1vcpu-512mb-10gb}"
DO_IMAGE="${DO_IMAGE:-ubuntu-24-04-x64}"

USERDATA_FILE="$(mktemp /tmp/exit-node-userdata.XXXXXX.yaml)"
trap 'rm -f "${USERDATA_FILE}"' EXIT

echo "[cloud-init-do] Generating user-data..."
uv run "${REPO_ROOT}/bin/insta-exit-node" cloud-init \
  --hostname "${EXIT_HOSTNAME}" \
  --tag "${EXIT_TAG}" \
  > "${USERDATA_FILE}"

echo "[cloud-init-do] Creating droplet ${EXIT_HOSTNAME} in ${DO_REGION}..."
doctl compute droplet create "${EXIT_HOSTNAME}" \
  --image "${DO_IMAGE}" \
  --size "${DO_SIZE}" \
  --region "${DO_REGION}" \
  --ssh-keys "${DO_SSH_KEY}" \
  --user-data-file "${USERDATA_FILE}" \
  --wait \
  --no-header \
  --format ID,Name,PublicIPv4,Status

echo ""
echo "[cloud-init-do] Droplet created. Cloud-init will run in the background."
echo "  Wait ~60-90s, then check your Tailscale admin console for ${EXIT_HOSTNAME}."
echo "  To verify from the VM: ssh root@<ip> 'journalctl -u tailscaled -n 50'"
