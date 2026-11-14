#!/usr/bin/env python3
"""Install this user's one-minute retry timer and enable automatic catalogue sync.

Run on the laptop with .venv/bin/python configure_laptop_sync.py [--server URL] [--cacert PATH].
The existing ~/.config/photo_token is used; no token is stored in these settings or units.
"""
import argparse
import subprocess
from pathlib import Path

import photo_library
import photo_sync
import send_camera


def main():
    root = Path(__file__).resolve().parent
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--server', default=send_camera.DEFAULT_SERVER)
    ap.add_argument('--cacert', type=Path, default=root / 'nucbox3.crt')
    ap.add_argument('--disable', action='store_true')
    args = ap.parse_args()
    if args.disable:
        subprocess.run(['systemctl', '--user', 'disable', '--now', 'photos-laptop-sync.timer'], check=True)
        subprocess.run(['systemctl', '--user', 'stop', 'photos-laptop-sync.service'], check=True)
        photo_sync._edit('sync-config.json', lambda d: d.update(enabled=False))
        return
    if not args.cacert.is_file():
        ap.error('A trusted server CA certificate is required; pass --cacert PATH.')
    unit_dir = Path.home() / '.config/systemd/user'
    unit_dir.mkdir(parents=True, exist_ok=True)
    service = (root / 'systemd/photos-laptop-sync.service').read_text()
    executable = str(root / 'ingest_camera.sh').replace('\\', '\\\\').replace('"', '\\"').replace('%', '%%')
    service = service.replace('ExecStart=%h/photos_receiver/ingest_camera.sh sync --quiet',
                              f'ExecStart="{executable}" sync --quiet')
    (unit_dir / 'photos-laptop-sync.service').write_text(service)
    (unit_dir / 'photos-laptop-sync.timer').write_text((root / 'systemd/photos-laptop-sync.timer').read_text())
    subprocess.run(['systemctl', '--user', 'daemon-reload'], check=True)
    photo_sync.configure(args.server, str(args.cacert.resolve()))
    for lib in photo_library.online():
        photo_sync.changed(lib.id, launch=False)
    subprocess.run(['systemctl', '--user', 'enable', '--now', 'photos-laptop-sync.timer'], check=True)
    subprocess.run(['systemctl', '--user', 'start', '--no-block', 'photos-laptop-sync.service'], check=True)
    print('Automatic sync enabled: changes request a sync immediately; the timer retries every minute.')


if __name__ == '__main__':
    main()
