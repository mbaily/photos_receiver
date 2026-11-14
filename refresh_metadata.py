#!/usr/bin/env python3
"""Fill in camera metadata for photos already in the library.

By default processes rows with no metadata yet, or only 'basic' (Pillow) metadata
when exiftool is now available. --all re-extracts everything.
"""

import argparse
import sys

import photo_library
import photo_catalogue
from photo_db import get_db
from photo_meta import EXIFTOOL, index_photo


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--all", action="store_true", help="re-extract every row")
    ap.add_argument("--library", help="library name or id (default: the upload library)")
    args = ap.parse_args()
    try:
        lib = photo_library.cli_library(args.library)
    except photo_library.LibraryError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    try:
        photo_library.require_writable(lib)
    except photo_library.LibraryError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    photo_library.use(lib)
    print(f"Library: {lib.name}  [{lib.path}]")

    if not EXIFTOOL:
        print("exiftool not found - using basic Pillow EXIF (install libimage-exiftool-perl for full metadata)")
    with get_db() as conn:
        if args.all:
            where = ""
        elif EXIFTOOL:
            where = " WHERE exif IS NULL OR extractor != 'exiftool'"
        else:
            where = " WHERE exif IS NULL"
        digests = [r[0] for r in conn.execute(f"SELECT sha256 FROM photos{where}")]
        failed = 0
        for i, digest in enumerate(digests, 1):
            try:
                index_photo(conn, digest)
                conn.commit()
                photo_catalogue.refresh(lib.id, conn, [digest])
            except Exception as e:
                failed += 1
                print(f"  {digest[:12]}: {e}", file=sys.stderr)
            print(f"\r{i}/{len(digests)}", end="", flush=True)
    print(f"\nDone: {len(digests) - failed} updated, {failed} failed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
