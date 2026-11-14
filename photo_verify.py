"""Check a library against its own records, and repair what can be repaired safely.

Every original is read again and compared with the SHA-256 it was stored under, so damage
that leaves a file's size alone (a failing USB stick, a bad copy) is found. Also checked:
the database itself, originals that are missing or have the wrong size, files lying in the
library folder that no record points to, and the files in the trash.

Repair (--repair) never deletes anything:
- a damaged original is moved to the library's .quarantine folder;
- a missing or quarantined original is put back from a good copy: a stray file in the
  same library with the right hash, or the same photo in another library plugged in to
  this machine. Every byte copied is hashed again before it takes the original's place;
- what cannot be recovered here stays recorded, with its notes, tags and albums, and is
  repaired by the next import of that photo from the camera or phone.

    ./libraries.sh verify NAME|ID [--repair]
"""

import os
import sqlite3
import stat
import sys
from datetime import datetime, timezone
from pathlib import Path

import photo_catalogue
import photo_db
import photo_library
import photo_store

QUARANTINE = ".quarantine"


def _file_state(path: Path, digest: str, size_bytes: int) -> str:
    """ok | missing | wrong-size | corrupt | unreadable"""
    try:
        info = path.lstat()
    except FileNotFoundError:
        return "missing"
    except OSError:
        return "unreadable"
    if not stat.S_ISREG(info.st_mode):
        return "missing"
    if info.st_size != size_bytes:
        return "wrong-size"
    try:
        return "ok" if photo_store.sha256_file(path) == digest else "corrupt"
    except OSError:
        return "unreadable"


def _hash(path: Path) -> str | None:
    try:
        return photo_store.sha256_file(path)
    except OSError:
        return None


def _library_file(name: str) -> bool:
    """Files in a library folder that are the library's own, not originals."""
    lower = name.lower()
    return (name.startswith(".") or lower in photo_store.RESERVED_NAMES or lower.startswith("inbox.db")
            or lower == photo_library.MARKER.lower() + ".tmp")


def _free_name(folder: Path, name: str) -> Path:
    dest, number = folder / name, 0
    while dest.exists() or dest.is_symlink():
        number += 1
        dest = folder / f"{Path(name).stem}_{number}{Path(name).suffix}"
    return dest


def _donors(digest: str, skip_id: str) -> list[tuple[str, Path]]:
    """(library name, file) for the same photo in other libraries plugged in here."""
    out = []
    for other in photo_library.online():
        if other.id == skip_id:
            continue
        token = photo_library.use(other)
        try:
            with photo_db.get_db() as conn:
                row = conn.execute("SELECT saved_filename, size_bytes FROM photos WHERE sha256 = ?",
                                   (digest,)).fetchone()
            if row and photo_db.original_present(row):
                out.append((other.name, other.path / Path(row["saved_filename"]).name))
        except Exception:
            pass
        finally:
            photo_library._current.reset(token)
    return out


def check(lib: photo_library.Library, say=lambda text: None) -> dict:
    """Read-only examination. `lib` must be the current library."""
    report = {"database": [], "checked": 0, "bad": {}, "strays": [], "trash": [], "trash_strays": 0}
    with photo_db.get_db() as conn:
        try:
            problems = [r[0] for r in conn.execute("PRAGMA integrity_check") if r[0] != "ok"]
            problems += [f"broken reference in {r[0]} (row {r[1]})" for r in conn.execute("PRAGMA foreign_key_check")]
        except sqlite3.Error as e:
            problems = [str(e)]
        report["database"] = problems
        rows = [dict(r) for r in conn.execute(
            "SELECT sha256, saved_filename, original_filename, size_bytes FROM photos ORDER BY saved_filename")]
        deleted = {r["sha256"]: dict(r) for r in conn.execute("SELECT sha256, saved_filename FROM deleted_photos")}

    for i, row in enumerate(rows, 1):
        state = _file_state(lib.path / Path(row["saved_filename"]).name, row["sha256"], row["size_bytes"])
        if state != "ok":
            report["bad"][row["sha256"]] = {**row, "state": state}
        if i % 25 == 0 or i == len(rows):
            say(f"\r  {i}/{len(rows)} originals read")
    report["checked"] = len(rows)
    if rows:
        say("\n")

    # Files in the folder that no record points to.
    named = {Path(r["saved_filename"]).name for r in rows}
    by_hash = {r["sha256"] for r in rows}
    try:
        entries = sorted(lib.path.iterdir())
    except OSError:
        entries = []
    for path in entries:
        if _library_file(path.name) or path.name in named or path.is_symlink() or not path.is_file():
            continue
        digest = _hash(path)
        if digest in report["bad"]:
            kind = "good copy of a damaged or missing original"
        elif digest in by_hash:
            kind = "second copy of an original that is intact"
        elif digest in deleted:
            kind = "copy of a deleted photo"
        elif digest is None:
            kind = "unreadable"
        else:
            kind = "not recorded in this library"
        report["strays"].append({"name": path.name, "sha256": digest, "kind": kind})

    # The trash: a damaged file there would be restored damaged.
    trash_named = set()
    for digest, row in deleted.items():
        name = Path(row["saved_filename"]).name
        trash_named.add(name)
        path = lib.trash_dir / name
        if path.is_file() and _hash(path) != digest:
            report["trash"].append({"name": name, "sha256": digest})
    try:
        report["trash_strays"] = sum(1 for p in lib.trash_dir.iterdir() if p.is_file() and p.name not in trash_named)
    except OSError:
        pass
    return report


def repair_one(lib: photo_library.Library, digest: str, strays: list[dict]) -> tuple[str, str | None]:
    """Repair one original. Returns (what happened, stray file used): the text starts with
    ok, recovered or unrecovered. `lib` must be the current library."""
    with photo_store.library_lock(lib):
        with photo_db.get_db() as conn:
            row = conn.execute("SELECT saved_filename, size_bytes FROM photos WHERE sha256 = ?", (digest,)).fetchone()
        if not row:
            return "ok", None            # deleted meanwhile
        name = Path(row["saved_filename"]).name
        dest = lib.path / name
        state = _file_state(dest, digest, row["size_bytes"])
        if state == "ok":
            return "ok", None            # repaired meanwhile (an import ran)
        if state == "wrong-size" and _hash(dest) == digest:
            # The file is the right photo; the record held the wrong size.
            with photo_db.get_db() as conn:
                conn.execute("UPDATE photos SET size_bytes = ? WHERE sha256 = ?", (dest.stat().st_size, digest))
            return "recovered (the recorded size was wrong; the file is intact)", None

        moved = ""
        if dest.exists() or dest.is_symlink():
            quarantine = lib.path / QUARANTINE
            quarantine.mkdir(exist_ok=True)
            kept = _free_name(quarantine, name)
            os.replace(dest, kept)
            photo_store.fsync_path(quarantine)
            moved = f"; the damaged file is kept as {QUARANTINE}/{kept.name}"

        # 1. A stray file in this library with the right content.
        used = None
        for stray in strays:
            path = lib.path / stray["name"]
            if stray["sha256"] == digest and path.is_file() and _hash(path) == digest:
                os.replace(path, dest)
                source, used = f"from {stray['name']} in this library", stray["name"]
                break
        else:
            # 2. The same photo in another library plugged in here, copied and hashed again.
            source = ""
            for other_name, other_path in _donors(digest, lib.id):
                try:
                    tmp = photo_store.copy_verified(other_path, lib.path, digest)
                except OSError:
                    tmp = None
                if tmp is not None:
                    os.replace(tmp, dest)
                    source = f"from library '{other_name}'"
                    break
        photo_store.fsync_path(lib.path)
        # Previews made from the damaged file must not outlive it.
        for cached in lib.cache_dir.glob(f"{digest}_*.jpg"):
            cached.unlink(missing_ok=True)
        if not source:
            return "unrecovered" + moved, None
        size = dest.stat().st_size
        if size != row["size_bytes"]:
            with photo_db.get_db() as conn:
                conn.execute("UPDATE photos SET size_bytes = ? WHERE sha256 = ?", (size, digest))
        return f"recovered {source}{moved}", used


def run(lib: photo_library.Library, repair: bool = False, out=None) -> int:
    """The verify command. 0 = nothing wrong (or everything repaired), 1 = problems remain."""
    out = out or sys.stdout

    def say(text: str) -> None:
        out.write(text)
        out.flush()

    held = photo_library.hold(lib.id)
    writable = os.access(lib.path, os.W_OK) and not held
    if repair and held:
        print(f"error: {photo_library.held_message(lib, held)} It can be verified but not repaired.",
              file=sys.stderr)
        return 1
    if repair and not writable:
        print(f"error: library '{lib.name}' is read-only; it can be verified but not repaired", file=sys.stderr)
        return 1
    token = photo_library.use(lib)
    try:
        say(f"Verifying '{lib.name}'  [{lib.path}]\n")
        report = check(lib, say)
        outcomes: dict[str, str] = {}
        used = set()
        if repair:
            for digest in report["bad"]:
                try:
                    outcomes[digest], stray = repair_one(lib, digest, report["strays"])
                    used.add(stray)
                except (OSError, sqlite3.Error, photo_library.LibraryError) as e:
                    outcomes[digest] = f"unrecovered (repair failed: {e})"
            changed = [d for d, what in outcomes.items() if not what.startswith("unrecovered")]
            if changed:
                with photo_db.get_db() as conn:
                    photo_catalogue.refresh(lib.id, conn, changed)
        else:
            # Nothing is held while reading, so look once more at anything that seemed wrong:
            # an import, delete or restore may have been under way.
            with photo_db.get_db() as conn:
                for digest in list(report["bad"]):
                    row = conn.execute("SELECT saved_filename, size_bytes FROM photos WHERE sha256 = ?",
                                       (digest,)).fetchone()
                    if not row or _file_state(lib.path / Path(row["saved_filename"]).name, digest,
                                              row["size_bytes"]) == "ok":
                        del report["bad"][digest]

        remaining = 0
        for problem in report["database"]:
            remaining += 1
            say(f"  DATABASE: {problem}\n")
        for digest, bad in report["bad"].items():
            what = outcomes.get(digest)
            if what is None or what.startswith("unrecovered"):
                remaining += 1
            label = {"missing": "MISSING", "wrong-size": "WRONG SIZE", "corrupt": "DAMAGED",
                     "unreadable": "UNREADABLE"}[bad["state"]]
            say(f"  {label}: {bad['saved_filename']} ({bad['original_filename']})"
                + (f" -> {what}" if what else "") + "\n")
        # A file that an import had placed but not yet recorded is not a stray: wait for
        # any import in the middle of that step, then look at the records again.
        def recorded_names():
            with photo_db.get_db() as conn:
                return {Path(r[0]).name for r in conn.execute("SELECT saved_filename FROM photos")}
        if writable and report["strays"]:
            with photo_store.library_lock(lib):
                recorded = recorded_names()
        else:
            recorded = recorded_names()
        for stray in report["strays"]:
            if stray["name"] not in used and stray["name"] not in recorded:
                say(f"  STRAY FILE: {stray['name']} - {stray['kind']} (left untouched)\n")
        for item in report["trash"]:
            remaining += 1
            say(f"  DAMAGED IN TRASH: {item['name']} (restoring it would restore a damaged file)\n")
        if report["trash_strays"]:
            say(f"  {report['trash_strays']} file(s) in the trash have no deletion record (left untouched)\n")

        unrecovered = sum(1 for w in outcomes.values() if w.startswith("unrecovered"))
        if remaining == 0:
            repaired = sum(1 for w in outcomes.values() if w.startswith("recovered"))
            summary = f"{report['checked']} originals verified" + (f", {repaired} repaired" if repaired else "")
            say(f"OK: {summary}.\n")
        else:
            summary = f"{remaining} problem(s) among {report['checked']} originals"
            say(f"PROBLEMS: {summary}.\n")
            if not repair and report["bad"]:
                say("  Run again with --repair to quarantine damaged files and recover from good copies.\n")
            if unrecovered:
                say("  What could not be recovered stays recorded with its notes, tags and albums. Import the\n"
                    "  photo again from the camera, card or phone into this library and it is put back.\n")
        try:      # what this library records but does not hold is not "already in another library"
            photo_catalogue.set_absent(lib.id, [d for d in report["bad"]
                                               if not outcomes.get(d, "").startswith(("recovered", "ok"))])
        except Exception:
            pass
        when = datetime.now(timezone.utc).isoformat(timespec="seconds")
        if writable:
            try:
                with photo_db.get_db() as conn:
                    photo_db.set_meta(conn, "verified_at", when)
                    photo_db.set_meta(conn, "verified_result", summary)
            except (sqlite3.Error, photo_library.LibraryError):
                pass
        try:
            photo_catalogue.set_warning(lib.id, "verify",
                                        None if remaining == 0 else f"Integrity check found {summary}. "
                                        "Run ./libraries.sh verify for details.")
        except Exception:
            pass
        return 0 if remaining == 0 else 1
    finally:
        photo_library._current.reset(token)
