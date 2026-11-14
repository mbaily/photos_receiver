#!/usr/bin/env bash
# Send new photos/videos from a mounted card or gphoto2 USB camera to the photo server.
#   ./send_camera.sh                 # auto-detect a mounted card, else a gphoto2 camera
#   ./send_camera.sh --gphoto        # Canon/PTP over USB (requires gphoto2)
#   ./send_camera.sh --dry-run       # show what's new, upload nothing
#   ./send_camera.sh /media/me/CARD  # name the mount point
# The server's certificate is always verified, against nucbox3.crt next to this script
# or PHOTO_SERVER_CACERT (else the system's trusted CAs). Pass --insecure yourself to skip
# the check - the upload token is then sent to a server whose identity is unproven.
# PHOTO_SERVER overrides the server URL.
set -euo pipefail
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

if [[ -z "${PHOTO_SERVER_CACERT:-}" && -f "$DIR/nucbox3.crt" ]]; then
    export PHOTO_SERVER_CACERT="$DIR/nucbox3.crt"
fi

exec python3 "$DIR/send_camera.py" "$@"
