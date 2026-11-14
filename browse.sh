#!/usr/bin/env bash
set -euo pipefail
source "$(dirname "${BASH_SOURCE[0]}")/venv.sh"
exec "$VENV_DIR/bin/python" "$REPO_DIR/browse_photos.py" --port 8095 "$@"
