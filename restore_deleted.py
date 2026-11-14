#!/usr/bin/env python3
"""List photos deleted in the browser, put some back, or empty the trash.

    ./.venv/bin/python restore_deleted.py              # list what is in the trash
    ./.venv/bin/python restore_deleted.py 3c804b ...   # restore by SHA-256 prefix
    ./.venv/bin/python restore_deleted.py --purge 30   # remove trashed files deleted over 30 days ago
    ./.venv/bin/python restore_deleted.py --purge 0    # ... or all of them
    ./.venv/bin/python restore_deleted.py --library NAME ...   # a library other than the upload one

A restored photo comes back with its notes, hashtags, Sony sidecar and album places (albums
deleted meanwhile are skipped). Photos deleted before this was recorded come back bare.

--purge frees the disk space for good: those files cannot be restored afterwards. Their
hashes stay remembered, so the phone and camera syncs still do not bring them back.
"""

import argparse
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import photo_library
from photo_db import get_db
from photo_trash import restore_one, purge_one, TrashError


def human(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} TB"


def purge(conn, trash: Path, days: int) -> int:
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    removed = freed = 0
    for r in conn.execute("SELECT * FROM deleted_photos WHERE deleted_at <= ? ORDER BY deleted_at", (cutoff,)):
        try:
            lib = photo_library.current()
            count, size = purge_one(lib, conn, r['sha256'])
            if not count:
                continue
        except FileNotFoundError:
            continue
        except photo_library.LibraryError as e:
            print(f"error: {e}", file=sys.stderr)
            return 1
        except OSError as e:
            print(f"{r['sha256'][:12]}: {e}", file=sys.stderr)
            return 1
        removed, freed = removed + 1, freed + size
    print(f"removed {removed} file(s) from the trash, {human(freed)} freed")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("prefixes", nargs="*", help="SHA-256 prefixes of the photos to restore")
    ap.add_argument("--library", help="library name or id (default: the upload library)")
    ap.add_argument("--purge", type=int, metavar="DAYS",
                    help="permanently remove trashed files deleted at least DAYS days ago")
    args = ap.parse_args()
    if args.purge is not None and (args.purge < 0 or args.prefixes):
        ap.error("--purge takes a number of days (0 or more) and no prefixes")
    try:
        lib = photo_library.cli_library(args.library)
    except photo_library.LibraryError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    photo_library.use(lib)
    PHOTO_DIR, TRASH_DIR = lib.path, lib.trash_dir
    print(f"Library: {lib.name}  [{lib.path}]")
    prefixes = [a.lower() for a in args.prefixes]
    with get_db() as conn:
        if args.purge is not None:
            return purge(conn, TRASH_DIR, args.purge)
        rows = conn.execute("SELECT * FROM deleted_photos ORDER BY deleted_at").fetchall()
        if not prefixes:
            held = 0
            for r in rows:
                path = TRASH_DIR / Path(r["saved_filename"]).name
                state = "" if path.is_file() else "  (file gone - cannot be restored)"
                held += path.stat().st_size if path.is_file() else 0
                print(f"{r['sha256'][:12]}  {photo_library.local_time(r['deleted_at'], seconds=True)}  {r['original_filename']}{state}")
            print(f"{len(rows)} deleted item(s), {human(held)} in the trash")
            return 0
        failed = 0
        for prefix in prefixes:
            matches = [r for r in rows if r["sha256"].startswith(prefix)]
            if len(matches) != 1:
                print(f"{prefix}: {'no match' if not matches else 'matches several, be more specific'}", file=sys.stderr)
                failed += 1
                continue
            r = matches[0]
            name = Path(r["saved_filename"]).name
            src, dest = TRASH_DIR / name, PHOTO_DIR / name
            if not src.is_file():
                print(f"{prefix}: file is no longer in {TRASH_DIR}", file=sys.stderr)
                failed += 1
                continue
            if dest.exists():
                print(f"{prefix}: {dest} already exists; not overwriting it", file=sys.stderr)
                failed += 1
                continue
            try:
                result = restore_one(lib, conn, r["sha256"])
            except (OSError, TrashError, photo_library.LibraryError) as exc:
                print(f"{prefix}: {exc}", file=sys.stderr)
                failed += 1
                continue
            if result['metadata_warning']:
                print(f"  {result['metadata_warning']}", file=sys.stderr)
            extra = "" if result["full"] else " (without notes, tags or albums: deleted by an older version)"
            if result["albums"]:
                extra += f", back in {result['albums']} album(s)"
            print(f"restored {r['original_filename']}{extra}")
            other = result["elsewhere"]
            if other:
                print(f"  note: library '{other}' also has this photo")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
