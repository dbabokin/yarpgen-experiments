#!/usr/bin/env bash
# Generate a few programs, compile them with sanitizers, and exercise reduction.
set -euo pipefail
cd "$(dirname "$0")/.."
python3 -m unittest discover -s tests -v
echo "smoke ok"
