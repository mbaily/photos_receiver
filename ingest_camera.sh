#!/usr/bin/env bash
# Copy new photos/videos from a USB camera into a photo library on THIS machine (a laptop),
# then tell the photo server what the library holds. Works offline; the server is told later.
#   ./libraries.sh create ~/photos/trip "Trip 2026"   # once: make a library here (or on a USB drive)
#   ./ingest_camera.sh                 # auto-detect a mounted card, else a gphoto2 camera
#   ./ingest_camera.sh --gphoto        # Canon/PTP over USB (requires gphoto2)
#   ./ingest_camera.sh --dry-run       # show what's new, copy nothing
#   ./ingest_camera.sh sync            # only send library info to the server (e.g. back online)
# Server certificate (nucbox3.crt next to this script, or PHOTO_SERVER_CACERT), PHOTO_SERVER
# and the token work as in send_camera.sh; --insecure must be asked for explicitly.
set -euo pipefail
source "$(dirname "${BASH_SOURCE[0]}")/venv.sh"

exec "$VENV_DIR/bin/python" "$REPO_DIR/ingest_camera.py" "$@"
