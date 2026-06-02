#!/usr/bin/env bash
# Simple smoke test script for agent checks (dry-run only)
set -euo pipefail
HERE=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
ROOT=$(cd "$HERE/.." && pwd)
LAIA="$ROOT/src/laia.py"
LAIA_LINUX="$ROOT/src/laia_linux.py"

echo "Running dry-run smoke tests..."
sudo -n python3 "$LAIA" share --group bot --path ./testbot --dry-run --verbose || true
sudo -n python3 "$LAIA" noshare --group bot --path ./testbot --dry-run --verbose || true
sudo -n python3 "$LAIA_LINUX" create testbot --dry-run || true
sudo -n python3 "$LAIA_LINUX" destroy testbot --dry-run || true

echo "Smoke tests completed. Inspect outputs for expected [DRY RUN] lines."
