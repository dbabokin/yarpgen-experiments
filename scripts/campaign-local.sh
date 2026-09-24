#!/usr/bin/env bash
# Run a campaign on this machine. Successes are not stored.
#   YARPGEN_DURATION=30s bash scripts/campaign-local.sh
#   bash scripts/campaign-local.sh --tests 5 --profile smoke --sanitize-all
set -euo pipefail
cd "$(dirname "$0")/.."
duration="${YARPGEN_DURATION:-}"
config="${YARPGEN_CONFIG:-campaign/example-config.json}"
out="${YARPGEN_STORE:-results}"
args=(--config "$config" --out "$out")
if [[ -n "$duration" ]]; then
  args+=(--duration "$duration")
fi
exec python3 -m yarpgen.campaign "${args[@]}" "$@"
