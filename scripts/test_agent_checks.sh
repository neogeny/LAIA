#!/usr/bin/env bash
# Simple smoke test script for agent checks (dry-run only)
set -euo pipefail
HERE=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
ROOT=$(cd "$HERE/.." && pwd)
LAIA="$ROOT/src/laia.py"

echo "Running dry-run smoke tests..."
python3 "$LAIA" share --group bot --path ./testbot --dry-run --verbose || true
python3 "$LAIA" noshare --group bot --path ./testbot --dry-run --verbose || true
python3 "$LAIA" create testbot --dry-run || true
python3 "$LAIA" destroy testbot --dry-run || true

echo "Smoke tests completed. Inspect outputs for expected [DRY RUN] lines."
