#!/usr/bin/env bash
set -euo pipefail

# Always resolve paths relative to this script's directory (the repo root)
source "$(dirname "${BASH_SOURCE[0]}")/venv.sh"
cd "$REPO_DIR"

exec "$VENV_DIR/bin/uvicorn" receiver:app \
    --host 127.0.0.1 \
    --port 3107 \
    --root-path /iphone_photos
