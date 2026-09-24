#!/usr/bin/env bash
# Sync this tree to a remote host and start a long-running campaign there.
# Credentials come from the environment, never from the repo.
#
#   export YARPGEN_SSH_TARGET=user@host
#   export YARPGEN_SSH_KEY=$HOME/.ssh/id_ed25519   # optional
#   export YARPGEN_REMOTE_DIR=/var/tmp/yarpgen
#   export YARPGEN_DURATION=24h
#   bash scripts/campaign-ssh.sh
set -euo pipefail
cd "$(dirname "$0")/.."

: "${YARPGEN_SSH_TARGET:?set YARPGEN_SSH_TARGET to user@host}"
remote_dir="${YARPGEN_REMOTE_DIR:-/var/tmp/yarpgen-campaign}"
duration="${YARPGEN_DURATION:-24h}"
config="${YARPGEN_CONFIG:-campaign/example-config.json}"
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

rsync -az --delete \
  --exclude '.git/' \
  --exclude 'results/' \
  --exclude '__pycache__/' \
  --exclude '.work/' \
  -e "ssh ${ssh_opts[*]}" \
  ./ "${YARPGEN_SSH_TARGET}:${remote_dir}/"

ssh "${ssh_opts[@]}" "$YARPGEN_SSH_TARGET" bash -s -- \
  "$remote_dir" "$config" "$duration" "$store" <<'EOF'
set -euo pipefail
remote_dir=$1
config=$2
duration=$3
store=$4
cd "$remote_dir"
mkdir -p "$store"
cmd=(python3 -m yarpgen.campaign --config "$config" --duration "$duration" --out "$store")
if command -v tmux >/dev/null 2>&1; then
  tmux has-session -t yarpgen 2>/dev/null && tmux kill-session -t yarpgen || true
  # tmux does not run a shell unless asked, so redirects need bash -c.
  tmux new-session -d -s yarpgen bash -c "$(printf '%q ' "${cmd[@]}") > campaign.log 2>&1"
  echo "started tmux session yarpgen"
else
  nohup "${cmd[@]}" > campaign.log 2>&1 &
  echo $! > campaign.pid
  echo "started pid $(cat campaign.pid)"
fi
EOF
echo "results stay on the remote under ${remote_dir}/${store}"
echo "fetch them with: bash scripts/fetch-results.sh"
