#!/usr/bin/env bash
# List and manage photo libraries on this server. Run without arguments to list them.
#   ./libraries.sh create /media/me/DISK/photos "Sony 2026"
#   ./libraries.sh inbox "Sony 2026"        # where the iPhone shortcut uploads to
#   ./libraries.sh duplicates allow|refuse
set -euo pipefail
source "$(dirname "${BASH_SOURCE[0]}")/venv.sh"
exec "$VENV_DIR/bin/python" "$REPO_DIR/photo_library.py" "$@"
