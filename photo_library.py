#!/usr/bin/env python3
"""Photo libraries: self-contained folders that can live on different disks.

A library is a folder holding the photos, their database (inbox.db) and a marker file,
library.json, with a permanent id and a name. The id travels with the disk, so a library
is recognised wherever it gets mounted.

The server keeps a registry of every library it has seen (~/.config/photos/libraries.json):
name, last known path and when it was last seen. A library is "online" while its marker
file can be read - so an unplugged USB disk, or a locked encrypted dataset, is simply
offline and nothing is ever created in its place.

Command line (./libraries.sh):
    list                      every known library, online or not
    create PATH NAME          make a new, empty library in PATH
    inbox NAME|ID             choose where uploads go when the client does not say
                              (the iPhone shortcut; send_camera.py asks instead)
A library on another machine (a laptop that ingests with ingest_camera.py and syncs) is
listed as "remote": this machine knows its name, host, item count and a catalogue of its
photos (photo_catalogue.py), but not the files.
    duplicates allow|refuse   accept or refuse photos that already exist in another library
    rename NAME|ID NEWNAME
    forget NAME|ID            drop an offline library from the registry (files untouched)
    verify NAME|ID [--repair] re-read every original and compare it with its recorded hash;
                              with --repair, damaged files are moved to .quarantine and
                              replaced from a good copy when one is plugged in
    acknowledge NAME|ID       clear the warnings shown for a library; a library that was made
                              read-only as a suspected older copy can be written to again
"""

import argparse
import contextvars
import fcntl
import getpass
import json
import os
import sys
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from photo_config import PHOTO_DIR

MARKER = "library.json"
# An extra, uniquely named file in every library. Backup tools that should leave photo
# libraries alone key on it (tar --exclude-tag-all=.photos-receiver-library), wherever
# the library is.
BACKUP_TAG = ".photos-receiver-library"
BACKUP_TAG_TEXT = ("This folder is a photos_receiver photo library (see library.json).\n"
                   "The server backup skips any folder containing this file.\n")
REGISTRY = Path(os.environ.get("PHOTO_REGISTRY") or Path.home() / ".config" / "photos" / "libraries.json")
SCAN_TTL = 5.0            # seconds a scan of the mount points is reused
SEEN_EVERY = 3600         # how often "last seen" is refreshed in the registry


@dataclass(frozen=True)
class Library:
    id: str
    name: str
    path: Path | None      # where it is (or was last) mounted
    online: bool
    last_seen: str | None = None
    inbox: bool = False
    # What another machine (a laptop running ingest_camera.py) last reported about this
    # library: host, path, items, deleted, synced_at. None for a library only ever seen here.
    remote: dict | None = None
    # Other folders plugged in right now that carry this same library id (copies).
    copies: tuple = ()
    # Set while the library is read-only because it looks like an older copy (see hold()).
    hold: dict | None = None

    @property
    def host(self) -> str | None:
        """The machine that holds this library, when it is not on this one."""
        if self.online or not self.remote:
            return None
        try:   # plugged in here since the other machine last reported it: it moved, and is just unplugged
            if self.last_seen and datetime.fromisoformat(self.last_seen) >= datetime.fromisoformat(self.remote["synced_at"]):
                return None
        except (KeyError, ValueError, TypeError):
            pass
        return self.remote.get("host") or "another machine"

    @property
    def where(self) -> str:
        """Where the library is, for people: a local path, or host:path on another machine."""
        if not self.host:
            return str(self.path) if self.path else "unknown location"
        return f"{self.host}:{self.remote.get('path') or '?'}"

    @property
    def db_path(self) -> Path:
        return self.path / "inbox.db"

    @property
    def trash_dir(self) -> Path:
        return self.path / ".trash"

    @property
    def cache_dir(self) -> Path:
        return self.path / ".cache"


def local_time(value: str | None, seconds: bool = False) -> str:
    """A stored UTC time as this machine's local date (or date and time), for people: the
    command line shows the same day the Libraries page does."""
    try:
        dt = datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return (value or "")[:19 if seconds else 10]
    if dt.tzinfo:
        dt = dt.astimezone()
    return dt.strftime("%Y-%m-%d %H:%M:%S" if seconds else "%Y-%m-%d")


class LibraryError(Exception):
    """User-facing problem: unknown, offline or ambiguous library."""


_current: contextvars.ContextVar[Library | None] = contextvars.ContextVar("photo_library", default=None)


def current() -> Library:
    """The library the running request (or command) works on."""
    lib = _current.get()
    if lib is None:
        raise LibraryError("no photo library selected")
    return lib


def use(lib: Library) -> contextvars.Token:
    return _current.set(lib)


# ---- registry ---------------------------------------------------------------

def _load() -> dict:
    try:
        data = json.loads(REGISTRY.read_text())
    except (OSError, ValueError):
        data = {}
    data.setdefault("libraries", {})
    data.setdefault("inbox", None)
    data.setdefault("allow_duplicates", False)
    return data


def _update(change) -> dict:
    """Read-modify-write the registry under a lock; `change(data)` edits it in place."""
    REGISTRY.parent.mkdir(parents=True, exist_ok=True)
    with open(REGISTRY.with_suffix(".lock"), "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        data = _load()
        change(data)
        tmp = REGISTRY.with_suffix(".tmp")
        with open(os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "w") as f:
            f.write(json.dumps(data, indent=2))
        os.chmod(tmp, 0o600)      # the same whether a service or the command line wrote it
        os.replace(tmp, REGISTRY)
    _scan.cache = None
    return data


def allow_duplicates() -> bool:
    return bool(_load()["allow_duplicates"])


# ---- markers & discovery ----------------------------------------------------

def read_marker(path: Path) -> dict | None:
    try:
        data = json.loads((path / MARKER).read_text())
        return data if isinstance(data.get("id"), str) and data["id"] else None
    except (OSError, ValueError, AttributeError):
        return None


def save_marker(path: Path, marker: dict) -> None:
    """Replace library.json in one step: a crash or a pulled disk leaves the old marker
    or the new one, never half a file (which would make the library unrecognisable)."""
    tmp = path / (MARKER + ".tmp")
    with open(tmp, "w") as f:
        f.write(json.dumps(marker, indent=2) + "\n")
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path / MARKER)
    try:
        fd = os.open(path, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
    except OSError:
        pass          # a filesystem that cannot flush a folder (FAT)


def write_marker(path: Path, name: str) -> dict:
    marker = {"id": uuid.uuid4().hex[:8], "name": name,
              "created": datetime.now(timezone.utc).isoformat(timespec="seconds")}
    save_marker(path, marker)
    ensure_backup_tag(path)
    return marker


def ensure_backup_tag(path: Path) -> None:
    try:
        if not (path / BACKUP_TAG).exists():
            (path / BACKUP_TAG).write_text(BACKUP_TAG_TEXT)
    except OSError:
        pass


def _candidate_dirs(known: list[str]) -> list[Path]:
    """Folders that might hold a library: remembered paths, the default library, and
    every mounted volume (the volume itself and its top-level folders)."""
    user = getpass.getuser()
    volumes: list[Path] = []
    parents = [Path("/media") / user, Path("/run/media") / user, Path("/media"), Path("/mnt"),
               Path("/Volumes")]
    for parent in parents:
        try:
            volumes += [p for p in parent.iterdir() if p.is_dir()]
        except OSError:
            pass
    volumes += [Path(p) for p in os.environ.get("PHOTO_LIBRARY_ROOTS", "").split(":") if p]
    out = [Path(p) for p in known] + [PHOTO_DIR]
    for vol in volumes:
        out.append(vol)
        try:
            out += [p for p in vol.iterdir() if p.is_dir() and not p.name.startswith(".")]
        except OSError:
            pass
    seen, unique = set(), []
    for p in out:
        if p not in seen:
            seen.add(p)
            unique.append(p)
    return unique


def _adopt_default() -> None:
    """The original single library has no marker yet: give it one.

    Only when its database is actually there (or PHOTO_DIR was set explicitly), so a
    locked or unplugged disk never gets an empty library created in its place.
    """
    if read_marker(PHOTO_DIR):
        return
    explicit = "PHOTO_DIR" in os.environ
    if (PHOTO_DIR / "inbox.db").is_file() or explicit:
        try:
            PHOTO_DIR.mkdir(parents=True, exist_ok=True)
            write_marker(PHOTO_DIR, PHOTO_DIR.name.replace("_", " "))
        except OSError:
            pass


def _registry_stamp() -> int:
    try:
        return REGISTRY.stat().st_mtime_ns
    except OSError:
        return 0


def _scan() -> list[Library]:
    now = time.monotonic()
    # Reuse a recent scan unless the registry was edited meanwhile (e.g. `libraries.sh inbox`).
    if _scan.cache and now - _scan.cache[0] < SCAN_TTL and _scan.cache[2] == _registry_stamp():
        return _scan.cache[1]
    _adopt_default()
    data = _load()
    known = data["libraries"]
    found: dict[str, tuple[Path, dict]] = {}
    copies: dict[str, list[str]] = {}
    for folder in _candidate_dirs([e["path"] for e in known.values() if e.get("path")]):
        marker = read_marker(folder)
        if not marker:
            continue
        if marker["id"] not in found:                 # the remembered path is looked at first
            found[marker["id"]] = (folder, marker)
            ensure_backup_tag(folder)                 # libraries made before the tag existed
        elif not _same_folder(folder, found[marker["id"]][0]):
            # A copied library folder: the same id in two places. Only one is used - the
            # one changed most recently (an older copy that was once plugged in alone must
            # not displace the original), else the remembered path. The other is reported,
            # never written to and never merged.
            kept = found[marker["id"]][0]
            if _folder_revision(folder) > _folder_revision(kept):
                found[marker["id"]] = (folder, marker)
                ensure_backup_tag(folder)
                folder = kept
            copies.setdefault(marker["id"], []).append(str(folder))

    stamp = datetime.now(timezone.utc).isoformat(timespec="seconds")

    def stale(entry: dict | None, folder: Path, marker: dict) -> bool:
        if not entry or entry.get("path") != str(folder) or entry.get("name") != marker.get("name"):
            return True
        try:
            seen = datetime.fromisoformat(entry["last_seen"])
            age = (datetime.now(timezone.utc) - seen).total_seconds()
            synced = (entry.get("remote") or {}).get("synced_at")
            if synced and datetime.fromisoformat(synced) > seen:
                return True      # reported from another machine since we last saw it: it is back here now
        except (KeyError, ValueError, TypeError):
            return True
        return age > SEEN_EVERY

    if any(stale(known.get(i), f, m) for i, (f, m) in found.items()) or (found and data["inbox"] not in known):
        def change(d: dict) -> None:
            for lib_id, (folder, marker) in found.items():
                d["libraries"].setdefault(lib_id, {}).update(
                    name=marker.get("name") or lib_id, path=str(folder), last_seen=stamp)
            if d["inbox"] not in d["libraries"] and found:
                d["inbox"] = next(iter(found))
        refreshed = [i for i, (f, m) in found.items() if stale(known.get(i), f, m)]
        try:
            data = _update(change)
        except OSError:
            pass
        known = data["libraries"]
        # Record what each just-found (or hourly re-checked) library holds, so that the
        # duplicate check still knows it after the disk is unplugged.
        import photo_catalogue
        for lib_id in refreshed:
            folder, marker = found[lib_id]
            photo_catalogue.snapshot(Library(id=lib_id, name=marker.get("name") or lib_id, path=folder, online=True))
    else:
        refreshed = []
    # A change whose catalogue update was lost (a crash between the two) shows as a change
    # counter that differs from the recorded one: record the library again.
    _catch_up({i: found[i] for i in found if i not in refreshed})
    known = _load()["libraries"]          # a snapshot above may have put a library on hold
    if any((known.get(i) or {}).get("hold") for i in found):
        _release_holds(found, known)
        known = _load()["libraries"]

    libs = []
    for lib_id in {**known, **found}:
        entry = known.get(lib_id, {})
        folder, marker = found.get(lib_id, (None, {}))
        libs.append(Library(
            id=lib_id, name=marker.get("name") or entry.get("name") or lib_id,
            path=folder or (Path(entry["path"]) if entry.get("path") else None),
            online=lib_id in found, last_seen=stamp if lib_id in found else entry.get("last_seen"),
            inbox=lib_id == data["inbox"], remote=entry.get("remote") or None,
            copies=tuple(copies.get(lib_id, ())), hold=entry.get("hold") or None,
        ))
    libs.sort(key=lambda l: (not l.online, l.name.lower()))
    _scan.cache = (time.monotonic(), libs, _registry_stamp())
    return libs


_scan.cache = None


def _same_folder(a: Path, b: Path) -> bool:
    try:
        return os.path.samefile(a, b)
    except OSError:
        return a == b


def _folder_revision(folder: Path) -> int:
    """The change counter of the library database in `folder`, read without writing; -1 if
    it has none."""
    import sqlite3
    try:
        conn = sqlite3.connect((folder / "inbox.db").resolve().as_uri() + "?mode=ro", uri=True)
        try:
            row = conn.execute("SELECT value FROM library_meta WHERE key = 'revision'").fetchone()
        finally:
            conn.close()
        return int(row[0]) if row and row[0] is not None else -1
    except (sqlite3.Error, OSError, TypeError, ValueError):
        return -1


def _catch_up(found: dict) -> None:
    try:
        import photo_catalogue
        import photo_db
        for lib_id, (folder, marker) in found.items():
            lib = Library(id=lib_id, name=marker.get("name") or lib_id, path=folder, online=True)
            token = use(lib)
            try:
                with photo_db.get_db() as conn:
                    now = photo_db.revision(conn)
            except Exception:
                continue
            finally:
                _current.reset(token)
            if now is not None and now != photo_catalogue.recorded_revision(lib_id):
                photo_catalogue.snapshot(lib)
    except Exception:
        pass          # the catalogue is a convenience; finding libraries must not fail over it


def all_libraries(fresh: bool = False) -> list[Library]:
    if fresh:
        _scan.cache = None
    return _scan()


def online() -> list[Library]:
    return [lib for lib in all_libraries() if lib.online]


def find(ref: str, libs: list[Library] | None = None) -> Library:
    """Look a library up by id or (case-insensitive) name."""
    libs = all_libraries() if libs is None else libs
    ref = (ref or "").strip()
    matches = [l for l in libs if l.id == ref.lower()] or [l for l in libs if l.name.lower() == ref.lower()]
    if not matches:
        raise LibraryError(f"no library called '{ref}'")
    if len(matches) > 1:
        raise LibraryError(f"several libraries are called '{ref}'; use the id")
    return matches[0]


def inbox() -> Library:
    """Where uploads go when the client does not choose."""
    libs = all_libraries()
    for lib in libs:
        if lib.inbox:
            if not lib.online:
                raise LibraryError(f"the upload library '{lib.name}' is not plugged in" + _absent(lib))
            return lib
    raise LibraryError("no photo library is available")


def require_attached(lib: Library) -> None:
    """Registered libraries must retain their marker before any database/file write."""
    if lib.id in _load()['libraries']:
        marker = read_marker(lib.path) if lib.path else None
        if not marker or marker.get('id') != lib.id:
            raise LibraryError(f"library '{lib.name}' is disconnected or its disk has changed")


# ---- read-only until acknowledged ---------------------------------------------
# A library that comes back as an older copy, or without photos it had, is not written to
# until someone confirms it is the copy to use: writing to a stale copy is how two copies of
# one library drift apart. Viewing, verifying and syncing its catalogue still work.

def hold(lib_id: str) -> dict | None:
    return (_load()["libraries"].get(lib_id) or {}).get("hold") or None


def set_hold(lib_id: str, reason: str, expected: int | None) -> None:
    def change(d: dict) -> None:
        if lib_id in d["libraries"]:
            d["libraries"][lib_id]["hold"] = {
                "reason": reason, "expected": expected,
                "at": datetime.now(timezone.utc).isoformat(timespec="seconds")}
    _update(change)


def clear_hold(lib_id: str) -> bool:
    if not hold(lib_id):
        return False
    _update(lambda d: d["libraries"].get(lib_id, {}).pop("hold", None))
    return True


def held_message(lib: Library, held: dict) -> str:
    return (f"library '{lib.name}' is read-only for now: {held.get('reason') or 'it may be an older copy'} "
            f"Nothing is written to it until you confirm this is the copy to use, with "
            f"./libraries.sh acknowledge {lib.id} or the Acknowledge button on the Libraries page.")


def require_writable(lib: Library) -> None:
    require_attached(lib)
    held = hold(lib.id)
    if held:
        raise LibraryError(held_message(lib, held))


def acknowledge(lib_id: str) -> int:
    """Clear a library's warnings and let it be written to again. Returns how many
    warnings were cleared."""
    import photo_catalogue
    cleared = photo_catalogue.acknowledge(lib_id)
    clear_hold(lib_id)
    return cleared


def _release_holds(found: dict, known: dict) -> None:
    """The up-to-date copy is back (its change counter has caught up with the one an older
    copy fell short of): it is the library again, with nothing to confirm."""
    for lib_id, (folder, _) in found.items():
        held = (known.get(lib_id) or {}).get("hold")
        if held and held.get("expected") is not None and _folder_revision(folder) >= held["expected"]:
            try:
                clear_hold(lib_id)
            except OSError:
                pass


def require_online(lib: Library) -> Library:
    require_attached(lib) if lib.online else None
    if not lib.online:
        raise LibraryError(f"library '{lib.name}' is not plugged in" + _absent(lib))
    return lib


def _absent(lib: Library) -> str:
    if lib.host:
        return f" (it is on {lib.host})"
    return f" (last seen {local_time(lib.last_seen)})" if lib.last_seen else ""


def register_remote(lib_id: str, name: str, remote: dict) -> None:
    """Record what another machine reported about one of its libraries.

    Only the name and the "remote" block are written: path, last_seen and the upload
    library describe this machine's own disks and are never changed from outside.
    """
    def change(d: dict) -> None:
        entry = d["libraries"].setdefault(lib_id, {})
        entry["name"] = name
        entry["remote"] = remote
    _update(change)


def create(path: Path, name: str) -> Library:
    path = path.expanduser().resolve()
    if read_marker(path):
        raise LibraryError(f"{path} already is a library")
    if any(l.name.lower() == name.lower() for l in all_libraries(fresh=True)):
        raise LibraryError(f"a library called '{name}' already exists")
    path.mkdir(parents=True, exist_ok=True)
    marker = write_marker(path, name)

    def change(d: dict) -> None:
        d["libraries"].setdefault(marker["id"], {}).update(
            name=name, path=str(path), last_seen=datetime.now(timezone.utc).isoformat(timespec="seconds"))
        d["inbox"] = d["inbox"] if d["inbox"] in d["libraries"] else marker["id"]
    _update(change)
    import photo_catalogue
    import photo_sync
    photo_catalogue.touch(marker['id'])
    photo_sync.changed(marker['id'])
    return find(marker["id"], all_libraries(fresh=True))


# ---- command line -----------------------------------------------------------

def _photo_count(lib: Library) -> str:
    if not lib.online:
        if lib.host and lib.remote.get("items") is not None:
            return str(lib.remote["items"])
        import photo_catalogue
        cached = photo_catalogue.item_counts().get(lib.id)
        return str(cached) if cached is not None else "-"
    import photo_db
    token = use(lib)
    try:
        with photo_db.get_db() as conn:
            return str(conn.execute("SELECT COUNT(*) FROM photos").fetchone()[0])
    except Exception:
        return "?"
    finally:
        _current.reset(token)


def cli_library(ref: str | None) -> Library:
    """For the maintenance scripts: the named library, else the upload library."""
    return require_online(find(ref)) if ref else inbox()


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd")
    sub.add_parser("list")
    p = sub.add_parser("create"); p.add_argument("path", type=Path); p.add_argument("name")
    p = sub.add_parser("inbox"); p.add_argument("library")
    p = sub.add_parser("duplicates"); p.add_argument("mode", choices=["allow", "refuse"])
    p = sub.add_parser("rename"); p.add_argument("library"); p.add_argument("name")
    p = sub.add_parser("forget"); p.add_argument("library")
    p = sub.add_parser("verify"); p.add_argument("library")
    p.add_argument("--repair", action="store_true",
                   help="quarantine damaged originals and recover them from good copies where possible")
    p = sub.add_parser("acknowledge"); p.add_argument("library")
    args = ap.parse_args()
    try:
        if args.cmd == "create":
            lib = create(args.path, args.name)
            import photo_db
            token = use(lib)
            photo_db.get_db().close()
            _current.reset(token)
            print(f"created '{lib.name}' ({lib.id}) in {lib.path}")
        elif args.cmd == "inbox":
            lib = find(args.library, all_libraries(fresh=True))
            if lib.path is None:
                raise LibraryError(f"'{lib.name}' is on {lib.host or 'another machine'} and has never been "
                                   "plugged in here; uploads cannot go to it")
            _update(lambda d: d.update(inbox=lib.id))
            print(f"uploads now go to '{lib.name}'" + ("" if lib.online else "  (not plugged in right now!)"))
        elif args.cmd == "duplicates":
            _update(lambda d: d.update(allow_duplicates=args.mode == "allow"))
            print("photos that exist in another library are now " + ("accepted" if args.mode == "allow" else "refused"))
        elif args.cmd == "rename":
            lib = require_online(find(args.library, all_libraries(fresh=True)))
            require_writable(lib)
            new_name = " ".join(args.name.split())
            if not new_name:
                raise LibraryError("the library name cannot be empty")
            if any(l.id != lib.id and l.name.lower() == new_name.lower() for l in all_libraries(fresh=True)):
                raise LibraryError(f"a library called '{new_name}' already exists")
            marker = read_marker(lib.path)
            marker["name"] = new_name
            save_marker(lib.path, marker)
            _update(lambda d: d["libraries"][lib.id].update(name=new_name))
            import photo_sync
            photo_sync.changed(lib.id)
            print(f"'{lib.name}' is now '{new_name}'")
        elif args.cmd == "forget":
            lib = find(args.library, all_libraries(fresh=True))
            if lib.online:
                raise LibraryError(f"'{lib.name}' is plugged in; only offline libraries can be forgotten")
            _update(lambda d: (d["libraries"].pop(lib.id, None), d.update(inbox=None if d["inbox"] == lib.id else d["inbox"])))
            import photo_catalogue
            photo_catalogue.forget(lib.id)
            print(f"forgot '{lib.name}'" + (f" (it comes back when {lib.host} next syncs)" if lib.host else ""))
        elif args.cmd == "verify":
            import photo_verify
            return photo_verify.run(require_online(find(args.library, all_libraries(fresh=True))), args.repair)
        elif args.cmd == "acknowledge":
            lib = find(args.library, all_libraries(fresh=True))
            was_held = bool(hold(lib.id))
            print(f"cleared {acknowledge(lib.id)} warning(s) for '{lib.name}'"
                  + ("; it can be written to again" if was_held else ""))
        libs = all_libraries(fresh=True)
        if args.cmd in (None, "list"):
            if not libs:
                print("no libraries yet - make one with: create PATH NAME")
            for lib in libs:
                state = "online " if lib.online else "remote " if lib.host else "offline"
                seen = "" if lib.online or not lib.last_seen else f"  last seen {local_time(lib.last_seen)}"
                if lib.host and lib.remote.get("synced_at"):
                    seen += f"  synced {local_time(lib.remote['synced_at'])}"
                print(f"{lib.id}  {state}  {_photo_count(lib):>6} items  {'uploads->' if lib.inbox else '         '} "
                      f"{lib.name}  [{lib.where}]{seen}")
                if lib.hold:
                    print(f"          READ-ONLY until acknowledged: {lib.hold.get('reason')}")
                for copy in lib.copies:
                    print(f"          WARNING: another copy of this library is plugged in at {copy}; "
                          f"only {lib.path} (the one changed most recently) is used. Copies are not merged.")
            import photo_catalogue
            names = {lib.id: lib.name for lib in libs}
            for w in photo_catalogue.warnings():
                if w["library_id"] in names:
                    print(f"WARNING '{names[w['library_id']]}' ({local_time(w['at'])}): {w['detail']}")
            print("photos already in another library: " + ("accepted" if allow_duplicates() else "refused"))
    except LibraryError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    # Run through the importable module, so photo_db shares this module's state.
    import photo_library
    sys.exit(photo_library.main())
