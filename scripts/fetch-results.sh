#!/usr/bin/env bash
# Copy the remote failure store to ./results. Successful runs are not in it.
set -euo pipefail
cd "$(dirname "$0")/.."
: "${YARPGEN_SSH_TARGET:?set YARPGEN_SSH_TARGET to user@host}"
remote_dir="${YARPGEN_REMOTE_DIR:-/var/tmp/yarpgen-campaign}"
store="${YARPGEN_STORE:-results}"
ssh_opts=(-o StrictHostKeyChecking=accept-new)
if [[ -n "${YARPGEN_SSH_KEY:-}" ]]; then
  ssh_opts+=(-i "$YARPGEN_SSH_KEY")
fi
if [[ -n "${YARPGEN_SSH_EXTRA:-}" ]]; then
  # shellcheck disable=SC2206
  extra=( ${YARPGEN_SSH_EXTRA} )
  ssh_opts+=("${extra[@]}")
fi
mkdir -p "$store"
rsync -az -e "ssh ${ssh_opts[*]}" \
  "${YARPGEN_SSH_TARGET}:${remote_dir}/${store}/" "./${store}/"
echo "fetched ${store}/"
