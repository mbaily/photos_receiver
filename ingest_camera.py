#!/usr/bin/env python3
"""Copy photos/videos from a USB-connected camera into a photo library on THIS machine.

For a laptop: the library is a folder on the laptop or on a USB drive plugged into it,
made beforehand with ./libraries.sh create PATH NAME. Files are named, recorded and
indexed exactly as if they had been uploaded to the server. No network is needed.

Afterwards the photo server is told what the library holds (name, this machine, item
count and a catalogue of the photos - not the files). If the server cannot be reached
that is not an error: run `sync` later, or let the next ingest do it.

    ./ingest_camera.sh                     # auto-detect a mounted card, else a gphoto2 camera
    ./ingest_camera.sh /media/me/CARD      # or name the card's mount point
    ./ingest_camera.sh --gphoto            # USB PTP camera (gphoto2 required)
    ./ingest_camera.sh --library "Trip"    # skip the library menu
    ./ingest_camera.sh --dry-run           # report what's new, copy nothing
    ./ingest_camera.sh sync                # only send library info to the server

Nothing is ever deleted from the camera. Unlike send_camera.py this needs the whole
repository (and its .venv), not just one file.
"""

import argparse
import hashlib
import json
import os
import shutil
import socket
import sys
import tempfile
import time
import urllib.error
import urllib.request
from contextlib import ExitStack
from pathlib import Path

import photo_catalogue
import photo_library
import photo_meta
import photo_sync
import send_camera as sc
from photo_db import deleted_hashes, get_db, original_present
from photo_store import attach_sidecar, elsewhere, store_file, sweep_parts

HOST = socket.gethostname()
SPARE_BYTES = 64 * 1024 * 1024        # kept free on the library's disk
FAT32_MAX = 4 * 1024 ** 3 - 1
KNOWN_BATCH = 500
ROWS_BATCH = 500


# ---- the server (optional) --------------------------------------------------

class Link:
    """Talks to the photo server when it can; after the first failure it stays offline
    for the rest of the run instead of timing out on every file."""

    def __init__(self, server: sc.Server | None):
        self.server = server
        self.online = server is not None
        self.why = "" if server else "not asked"
        self.untrusted = False      # the server answered, but its certificate could not be verified

    def call(self, method: str, path: str, body=None, timeout: int = 60):
        """JSON request to the server. Raises urllib errors; see ask()."""
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(f"{self.server.base}{path}", data=data, method=method,
                                     headers={"Content-Type": "application/json", **self.server.auth})
        with urllib.request.urlopen(req, context=self.server.ctx, timeout=timeout) as resp:
            return json.load(resp)

    def ask(self, method: str, path: str, body=None, timeout: int = 60):
        """Like call(), but an unreachable server returns None and marks the link offline.
        HTTP errors (the server answered, and said no) are still raised."""
        if not self.online:
            return None
        try:
            return self.call(method, path, body, timeout)
        except urllib.error.HTTPError:
            raise
        except (urllib.error.URLError, OSError, ValueError) as e:
            self.online = False
            self.why = str(getattr(e, "reason", e))
            if isinstance(getattr(e, "reason", e), sc.ssl.SSLCertVerificationError):
                # Not "offline": say what to do, once.
                self.untrusted = True
                print(sc.unreachable(self.server.base, e), file=sys.stderr)
            return None


def http_detail(e: urllib.error.HTTPError) -> str:
    try:
        return json.load(e)["detail"]
    except Exception:
        return str(e)


def make_link(args) -> Link:
    if args.no_sync:
        return Link(None)
    token = args.token.strip()
    if not token:
        try:
            token = sc.TOKEN_FILE.read_text().strip()
        except OSError:
            pass
    return Link(sc.Server(args.server, args.cacert, insecure=args.insecure and not args.cacert, token=token))


# ---- sync: tell the server what the libraries here hold ---------------------

def _sync_library(link: Link, lib: photo_library.Library, quiet: bool = False) -> bool:
    """Returns False only when the server refused or misbehaved (offline is not a failure)."""
    token = photo_library.use(lib)
    try:
        with get_db() as conn:
            conn.execute('BEGIN')
            rows, deleted = photo_catalogue.local_rows(conn)
            revision = photo_catalogue.library_revision(conn)
    finally:
        photo_library._current.reset(token)
    stamps = {sha: row["stamp"] for sha, row in rows.items()}
    marker = photo_library.read_marker(lib.path) or {}
    fingerprint = photo_catalogue.fingerprint(stamps, deleted)
    try:
        reply = link.ask("PUT", f"/remote/libraries/{lib.id}", {
            "name": lib.name, "host": HOST, "path": str(lib.path), "created": marker.get("created"),
            "items": len(rows), "deleted": len(deleted),
            "fingerprint": fingerprint, "revision": revision,
        }, timeout=20)
        if reply is None:
            return True
        for warning in reply.get("warnings", []):
            print(f"  WARNING '{lib.name}': {warning}")
        if reply.get("in_sync"):
            if not quiet:
                print(f"  '{lib.name}': server is up to date ({len(rows)} items)")
            return True
        sync_id = reply.get('sync_id')
        if not reply.get('atomic_sync') or not sync_id:
            print('  Update the receiver: this server does not support verified atomic sync.', file=sys.stderr)
            return False
        reply = link.ask("POST", f"/remote/libraries/{lib.id}/hashes", {"photos": stamps, "deleted": deleted, 'sync_id': sync_id},
                         timeout=300)
        if reply is None:
            return True
        need = [sha for sha in reply.get("need", []) if sha in rows]
        for i in range(0, len(need), ROWS_BATCH):
            batch = [{k: v for k, v in rows[sha].items() if k != "stamp"} for sha in need[i:i + ROWS_BATCH]]
            if link.ask("POST", f"/remote/libraries/{lib.id}/rows", {"rows": batch, 'sync_id': sync_id}, timeout=300) is None:
                return True
        committed = link.ask('POST', f'/remote/libraries/{lib.id}/commit',
                             {'sync_id': sync_id, 'fingerprint': fingerprint}, timeout=60)
        if committed is None:
            return True
        if not committed.get('committed'):
            raise ValueError('Server did not confirm catalogue publication')
        for warning in committed.get("warnings", []):
            print(f"  WARNING '{lib.name}': {warning}")
        print(f"  '{lib.name}': server told about {len(rows)} items ({len(need)} new or changed)")
        if reply.get("elsewhere"):
            print("  WARNING: " + count_by_library(reply["elsewhere"])
                  + " of them also exist in another library the server knows.")
        return True
    except urllib.error.HTTPError as e:
        detail = http_detail(e)
        link.why = f'HTTP {e.code}: {detail}'
        if e.code == 409 and 'plugged in to the server itself' in detail:
            if not quiet:
                print(f"  '{lib.name}': not synced - {detail}")
            return False
        if e.code == 401:
            print(f"  The server rejected the upload token. Put it in {sc.TOKEN_FILE} "
                  "(or pass --token / set PHOTO_TOKEN).", file=sys.stderr)
        elif e.code in (404, 405):
            print("  The server does not know about laptop libraries yet (update and restart "
                  "photos-receiver there).", file=sys.stderr)
        else:
            print(f"  '{lib.name}': the server refused the sync: {detail}", file=sys.stderr)
        return False


def sync_library(link: Link, lib: photo_library.Library, quiet: bool = False) -> bool:
    generation = photo_sync.begin(lib.id)
    try:
        result = _sync_library(link, lib, quiet)
        if result and link.online:
            photo_sync.finish(lib.id, generation, success=True)
        else:
            photo_sync.finish(lib.id, generation, error=link.why or 'The server refused the catalogue sync.')
        return result
    except Exception as exc:
        photo_sync.finish(lib.id, generation, error=str(exc))
        raise


def _sync_all(link: Link, quiet: bool = False) -> int:
    if link.server is None:
        return 0
    libs = photo_library.online()
    if not libs:
        if not quiet:
            print("No photo library on this machine to sync.")
        return 0
    if not quiet:
        print(f"Sending library info to {link.server.base}")
    import photo_move
    photo_move.pull_pending_moves(link, quiet=quiet)
    ok = True
    for lib in libs:
        ok = sync_library(link, lib, quiet) and ok
        if not link.online:
            if link.untrusted:
                print("  Nothing was sent. Once the certificate is in place run:  ./ingest_camera.sh sync")
                return 1
            if not quiet:
                print(f"  Offline ({link.why}). Nothing is lost: the server is told on the next "
                      "ingest, or run:  ./ingest_camera.sh sync")
            break
    return 0 if ok else 1


def sync_all(link: Link, quiet: bool = False) -> int:
    if link.server is None:
        return 0
    with photo_sync.run_lock() as acquired:
        if not acquired:
            if not quiet:
                print('A library sync is already running; pending changes will be retried automatically.')
            return 0
        return _sync_all(link, quiet)


def count_by_library(found: dict[str, str]) -> str:
    counts: dict[str, int] = {}
    for name in found.values():
        counts[name] = counts.get(name, 0) + 1
    return ", ".join(f"{n} in '{name}'" for name, n in sorted(counts.items()))


# ---- ingest -----------------------------------------------------------------

def choose_library(wanted: str | None) -> photo_library.Library:
    libs = photo_library.online()
    if wanted:
        return photo_library.require_online(photo_library.find(wanted))
    if not libs:
        raise photo_library.LibraryError(
            "there is no photo library on this machine. Make one first:\n"
            '    ./libraries.sh create ~/photos/trip "Trip 2026"      (or a folder on a USB drive)')
    default = next((l for l in libs if l.inbox), libs[0])
    if len(libs) == 1 or not sys.stdin.isatty():
        return default
    print("Which library should these go to?")
    for n, lib in enumerate(libs, 1):
        print(f"  {n}. {lib.name}  [{lib.path}]{'  [default]' if lib is default else ''}")
    while True:
        try:
            answer = input(f"Library [1-{len(libs)}, Enter = {default.name}]: ").strip()
        except EOFError:
            raise KeyboardInterrupt
        if not answer:
            return default
        if answer.isdigit() and 1 <= int(answer) <= len(libs):
            return libs[int(answer) - 1]
        named = [l for l in libs if l.name.lower() == answer.lower()]
        if len(named) == 1:
            return named[0]
        print("  Type one of the numbers above.")


def filesystem_type(path: Path) -> str:
    best = ("", "")
    try:
        with open("/proc/mounts") as mounts:
            for line in mounts:
                _, mount, kind = line.split()[:3]
                mount = mount.replace("\\040", " ")
                if (str(path) == mount or str(path).startswith(mount.rstrip("/") + "/")) and len(mount) >= len(best[0]):
                    best = (mount, kind)
    except (OSError, ValueError):
        pass
    return best[1]


def copy_in(src: Path, folder: Path) -> tuple[Path, str, int]:
    """Copy into a temporary file in the library, hashing what is actually written."""
    hasher, size = hashlib.sha256(), 0
    fd, tmp = tempfile.mkstemp(dir=folder, prefix=".part_")
    tmp_path = Path(tmp)
    try:
        with os.fdopen(fd, "wb") as out, open(src, "rb") as f:
            while chunk := f.read(sc.CHUNK):
                hasher.update(chunk)
                out.write(chunk)
                size += len(chunk)
            out.flush()
            os.fsync(out.fileno())
    except BaseException:
        tmp_path.unlink(missing_ok=True)
        raise
    return tmp_path, hasher.hexdigest(), size


def fsync_file(path: Path) -> None:
    with open(path, "rb") as f:
        os.fsync(f.fileno())


class CapacityError(RuntimeError):
    """Stop a run when storage cannot accept its next original."""


def require_attached(lib):
    marker = photo_library.read_marker(lib.path)
    if not marker or marker.get('id') != lib.id:
        raise RuntimeError('The library disk is disconnected or has changed. No files were written in its place.')


class Ingest:
    def __init__(self, lib: photo_library.Library, link: Link, allow_duplicates: bool, dry_run: bool):
        self.lib, self.link = lib, link
        self.allow_duplicates = allow_duplicates or photo_library.allow_duplicates()
        self.dry_run = dry_run
        self.seen: set[str] = set()
        self.recovering: set[str] = set()
        self.saved = self.repaired = self.have = self.deleted = self.repeats = self.errors = 0
        self.elsewhere: dict[str, str] = {}          # skipped (or, with allow, copied anyway)
        self.deleted_on_server: dict[str, str] = {}
        self.fat32 = filesystem_type(lib.path) in ("vfat", "msdos")
        self.unchecked = ""         # why the server could not be asked about duplicates

    def classify(self, digests: list[str]) -> dict[str, str]:
        """{hash: why it is not ingested}; hashes missing from the result are new."""
        skip: dict[str, str] = {}
        fresh = []
        batch_seen = set()
        for digest in digests:
            if digest in self.seen:
                self.repeats += 1
                if digest not in batch_seen:
                    skip[digest] = "repeat"
            else:
                self.seen.add(digest)
                batch_seen.add(digest)
                fresh.append(digest)
        with get_db() as conn:
            gone = deleted_hashes(conn, fresh)
            have, tracked = set(), set()
            for i in range(0, len(fresh), 500):
                chunk = fresh[i:i + 500]
                for row in conn.execute(
                        f"SELECT sha256,saved_filename,size_bytes FROM photos WHERE sha256 IN ({','.join('?' * len(chunk))})", chunk):
                    tracked.add(row['sha256'])
                    if original_present(row):
                        have.add(row['sha256'])
        self.recovering.update(tracked - have - gone)
        for digest in fresh:
            if digest in have:
                self.have += 1
                skip[digest] = "have"
            elif digest in gone:
                self.deleted += 1
                skip[digest] = "deleted"
        # Repairing an existing logical photo is allowed even if another library holds it.
        rest = [d for d in fresh if d not in skip and d not in tracked]
        other = elsewhere(rest, self.lib.id)                     # other libraries on this machine
        for i in range(0, len(rest), KNOWN_BATCH):               # and everything the server knows
            try:
                reply = self.link.ask("POST", "/known", {"hashes": rest[i:i + KNOWN_BATCH], "library": self.lib.id},
                                      timeout=20)
            except urllib.error.HTTPError as e:
                self.unchecked = f"the server refused the duplicate check (HTTP {e.code})"
                reply = None
            if reply is None:
                break
            for sha, name in reply.get("known", {}).items():
                other.setdefault(sha, f"{name} (server)")
            self.deleted_on_server.update(reply.get("deleted", {}))
        self.elsewhere.update(other)
        if not self.allow_duplicates:
            for digest in other:
                skip[digest] = "elsewhere"
        return skip

    def repair_sidecar(self, source: Path, digest: str) -> None:
        if sidecar := sc.find_sidecar(source):
            with get_db() as conn:
                row = conn.execute('SELECT sidecar_xml FROM photos WHERE sha256=?', (digest,)).fetchone()
            if not row or row['sidecar_xml']:
                return
            if self.dry_run:
                print(f'  would repair sidecar: {source.name}')
                return
            if attach_sidecar(self.lib, digest, sidecar.read_text("utf-8", "replace"), missing_only=True):
                print(f"  sidecar repaired: {source.name}")

    def too_big(self, size: int) -> bool:
        return self.fat32 and size > FAT32_MAX

    def store(self, tmp: Path, digest: str, size: int, source: Path, rel: str) -> dict:
        """`source` is where the camera's file (and its sidecar) can be read; may be `tmp`."""
        require_attached(self.lib)
        stamp = sc.capture_time(source).strftime("%Y-%m-%d_%H-%M-%S")
        sidecar_xml = None
        if sidecar := sc.find_sidecar(source):
            try:
                sidecar_xml = sidecar.read_text("utf-8", "replace")
            except OSError:
                pass
        meta = {"source": "usb_camera", "source_path": rel, "ingested_on": HOST}
        result = store_file(self.lib, tmp, digest, size, f"{digest}_{stamp}{source.suffix.lower()}",
                            source.name, meta, allow_duplicates=self.allow_duplicates, sidecar_xml=sidecar_xml)
        if result["status"] == "saved":
            self.saved += 1
        elif result['status'] == 'repaired':
            self.repaired += 1
        elif result["status"] == "elsewhere_skipped":
            self.elsewhere[digest] = result["library"]
        elif result["status"] == "deleted_skipped":
            self.deleted += 1
        else:
            self.have += 1
        if sidecar_xml:
            result["sidecar"] = sidecar.name
        return result


def ingest_card(job: Ingest, root: Path) -> None:
    files = list(sc.iter_media(root))
    print(f"{root}: {len(files)} media files, {sc.human(sum(p.stat().st_size for p in files))}")
    # Always verify source content; persisted timestamps are not a checksum.
    cache = {}
    hashed = []
    for i, path in enumerate(files, 1):
        print(f"\r  hashing {i}/{len(files)}", end="", flush=True)
        hashed.append((path, sc.sha256_of(path, cache)))
    print()
    skip = job.classify([d for _, d in hashed])
    todo, queued = [], set()
    for path, digest in hashed:
        if skip.get(digest) == "have":
            job.repair_sidecar(path, digest)
        if digest not in skip and digest not in queued:
            queued.add(digest)
            todo.append((path, digest))
    oversized = [p.name for p, _ in todo if job.too_big(p.stat().st_size)]
    if oversized:
        raise CapacityError('FAT32 cannot hold files of 4 GB or larger: ' + ', '.join(oversized) + '. Nothing was copied.')
    total = sum(p.stat().st_size for p, _ in todo)
    repair_count = sum(d in job.recovering for _, d in todo)
    print(f"{len(todo) - repair_count} new, {repair_count} original(s) to repair ({sc.human(total)})")
    free = shutil.disk_usage(job.lib.path).free
    if total and total + SPARE_BYTES > free:
        raise RuntimeError(f"not enough room in {job.lib.path}: {sc.human(total)} to copy, {sc.human(free)} free. "
                           "Nothing was copied.")
    for i, (path, digest) in enumerate(todo, 1):
        rel = path.relative_to(root).as_posix()
        if job.dry_run:
            print(f"  {'would repair original' if digest in job.recovering else 'would ingest'} {rel}")
            continue
        tmp = None
        try:
            require_attached(job.lib)
            tmp, copied, size = copy_in(path, job.lib.path)
            if copied != digest:
                sc.invalidate_hash(path, cache)
                tmp.unlink(missing_ok=True)
                raise RuntimeError("the copy does not match the card (read error?); try again")
            result = job.store(tmp, digest, size, path, rel)
            extra = f" (+{result['sidecar']})" if result.get("sidecar") else ""
            print(f"  [{i}/{len(todo)}] {result['status']:17} {rel}{extra}")
        except (OSError, RuntimeError, ValueError, photo_library.LibraryError) as e:
            job.errors += 1
            print(f"  [{i}/{len(todo)}] ERROR {rel}: {e}", file=sys.stderr)
        finally:
            if tmp is not None:
                tmp.unlink(missing_ok=True)


def ingest_gphoto(job: Ingest, camera: sc.GPhotoCamera) -> None:
    """Each file is downloaded once, straight into the library's disk, hashed there, and
    then either kept or removed."""
    files = camera.files
    print(f"USB camera ({camera.port}): {len(files)} media files, {sc.human(sum(camera.size(p) for p in files))}")
    print("Every file has to be read from the camera to recognise it; ones already in the library are discarded.")
    if any(job.too_big(camera.size(path)) for path in files):
        raise CapacityError('The camera contains a file of 4 GB or larger; FAT32 cannot spool it. Nothing was copied.')
    print('Space is checked per file. If a later read fails, earlier completed imports are kept.')
    budget = shutil.disk_usage(job.lib.path).free
    planned = 0
    for i, path in enumerate(files, 1):
        rel = path.relative_to(camera.root).as_posix()
        try:
            require_attached(job.lib)
            size = camera.size(path)
            if size + SPARE_BYTES > shutil.disk_usage(job.lib.path).free:
                raise CapacityError(f"not enough room left in {job.lib.path}; stopping before another download")
            camera.materialize(path)
            digest = sc.sha256_of(path, {})
            why = job.classify([digest]).get(digest)
            if why:
                if why == "have":
                    job.repair_sidecar(path, digest)
                print(f"  [{i}/{len(files)}] {'skipped: ' + why:17} {rel}")
            elif job.dry_run:
                planned += path.stat().st_size
                if planned + SPARE_BYTES > budget:
                    raise CapacityError('The predicted imports would exhaust the library disk; nothing was copied.')
                print(f"  [{i}/{len(files)}] {'would repair original' if digest in job.recovering else 'would ingest':17} {rel}")
            else:
                os.chmod(path, 0o600)
                fsync_file(path)
                result = job.store(path, digest, path.stat().st_size, path, rel)
                extra = f" (+{result['sidecar']})" if result.get("sidecar") else ""
                print(f"  [{i}/{len(files)}] {result['status']:17} {rel}{extra}")
        except CapacityError as e:
            job.errors += 1
            print(f"  [{i}/{len(files)}] ERROR {rel}: {e}", file=sys.stderr)
            break
        except (OSError, RuntimeError, ValueError, photo_library.LibraryError) as e:
            job.errors += 1
            print(f"  [{i}/{len(files)}] ERROR {rel}: {e}", file=sys.stderr)
        finally:
            path.unlink(missing_ok=True)


def run(resources: ExitStack) -> int:
    sync_options = photo_sync.config()
    local_ca = Path(__file__).with_name('nucbox3.crt')
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("source", nargs="?", help="camera/card mount point (auto-detected if omitted), "
                                              "or the word 'sync' to only send library info to the server")
    ap.add_argument("--library", help="library on this machine to ingest into, by name or id (default: ask)")
    ap.add_argument("--allow-duplicates", action="store_true",
                    help="also ingest files that another library (here or on the server) already has")
    ap.add_argument("--dry-run", action="store_true", help="report what's new, copy nothing")
    ap.add_argument("--gphoto", action="store_true", help="use a USB PTP camera via gphoto2 instead of a mounted card")
    ap.add_argument("--camera-port", help="gphoto2 USB port, e.g. usb:001,041 (implies --gphoto)")
    ap.add_argument("--no-sync", action="store_true", help="do not contact the photo server at all")
    ap.add_argument("--quiet", action="store_true", help="with sync: print only changes and problems (for a timer)")
    ap.add_argument("--server", default=os.environ.get("PHOTO_SERVER", sync_options.get("server", sc.DEFAULT_SERVER)),
                    help=f"photo server base URL (default {sc.DEFAULT_SERVER}, or $PHOTO_SERVER)")
    ap.add_argument("--cacert", default=os.environ.get("PHOTO_SERVER_CACERT") or sync_options.get("cacert") or (str(local_ca) if local_ca.is_file() else None),
                    help="CA certificate file to trust for the server (or $PHOTO_SERVER_CACERT)")
    ap.add_argument("--token", default=os.environ.get("PHOTO_TOKEN", ""),
                    help=f"upload token, if the server requires one (or $PHOTO_TOKEN, or {sc.TOKEN_FILE})")
    ap.add_argument("--insecure", action="store_true",
                    help="don't verify the server's TLS certificate (ignored if --cacert is given)")
    args = ap.parse_args()
    # One completed snapshot at the end; --no-sync does not launch background work.
    resources.enter_context(photo_sync.defer())
    link = make_link(args)

    if args.source == "sync":
        return sync_all(link, args.quiet)

    if args.source and (args.gphoto or args.camera_port):
        ap.error("choose either a mounted source folder or --gphoto/--camera-port")
    try:
        lib = choose_library(args.library)
    except photo_library.LibraryError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    photo_library.use(lib)
    if not os.access(lib.path, os.W_OK):
        print(f'ERROR: Library {lib.name} is read-only or not writable. Nothing was copied.', file=sys.stderr)
        return 1
    if held := photo_library.hold(lib.id):
        print(f'ERROR: {photo_library.held_message(lib, held)} Nothing was copied.', file=sys.stderr)
        return 1
    print(f"Library: {lib.name}  [{lib.path}]")
    sweep_parts(lib.path, every=0)

    def gphoto() -> sc.GPhotoCamera:
        # Downloads land on the library's own disk, so keeping a file is a rename, not a copy.
        spool = resources.enter_context(tempfile.TemporaryDirectory(dir=lib.path, prefix=".part_camera_"))
        return sc.GPhotoCamera(Path(spool), args.camera_port)

    camera = root = None
    if args.gphoto or args.camera_port:
        camera = gphoto()
    elif args.source:
        root = Path(args.source)
        if not sc.is_camera_root(root):
            print(f"{root} has no DCIM or PRIVATE/M4ROOT/CLIP folder - is that the camera's mount point?", file=sys.stderr)
            return 1
    else:
        roots = sc.find_camera_roots()
        if len(roots) > 1:
            print("Several camera volumes found; pass one explicitly:", *roots, sep="\n  ", file=sys.stderr)
            return 1
        if roots:
            root = roots[0]
        else:
            camera = gphoto()

    if not photo_meta.EXIFTOOL:
        print("NOTE: exiftool is not installed here, so only basic metadata is recorded. Install it\n"
              "      (sudo apt install libimage-exiftool-perl) and run refresh_metadata.py to upgrade.")
    job = Ingest(lib, link, args.allow_duplicates, args.dry_run)
    start = time.monotonic()
    try:
        if camera:
            ingest_gphoto(job, camera)
        else:
            ingest_card(job, root)
    finally:
        # Fold the write-ahead log into inbox.db, so a USB drive can be unplugged cleanly.
        try:
            conn = get_db()
            conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            conn.close()
        except Exception:
            pass

    verb = "would be ingested" if args.dry_run else "ingested"
    parts = [f"{job.saved} {verb}" if not args.dry_run else "dry run", f"{job.have} already in the library"]
    if job.repaired:
        parts.append(f'{job.repaired} original(s) repaired')
    if job.deleted:
        parts.append(f"{job.deleted} skipped (deleted from this library)")
    if job.repeats:
        parts.append(f"{job.repeats} repeated on the camera")
    if job.errors:
        parts.append(f"{job.errors} failed")
        if job.saved or job.repaired:
            print(f'PARTIAL IMPORT: {job.saved + job.repaired} completed original(s) are safe; retry to resume the remaining files.')
    print(f"Done in {time.monotonic() - start:.0f}s: " + ", ".join(parts))
    if job.elsewhere:
        where = count_by_library(job.elsewhere)
        if job.allow_duplicates:
            print(f"WARNING: {len(job.elsewhere)} file(s) also exist in another library ({where}); "
                  f"duplicate policy allows importing them into '{lib.name}'. See the per-file results.")
        else:
            print(f"WARNING: {len(job.elsewhere)} file(s) already exist in another library ({where}) and were skipped.\n"
                  f"         Use --allow-duplicates to ingest them into '{lib.name}' anyway.")
    if job.deleted_on_server:
        print(f"NOTE: {len(job.deleted_on_server)} of these were deleted from a library on the server "
              f"({count_by_library(job.deleted_on_server)}); those records do not block importing into this library.")
    if job.unchecked:
        print(f"NOTE: {job.unchecked}: duplicates could only be checked against libraries on this machine.")
    elif link.server is not None and not link.online:
        print(f"Server not reachable ({link.why}): duplicates could only be checked against libraries on this machine.")

    status = 1 if job.errors else 0
    if not args.dry_run:
        status = sync_all(link) or status
    return status


def main() -> int:
    try:
        with ExitStack() as resources:
            return run(resources)
    except (RuntimeError, OSError, ValueError, photo_library.LibraryError) as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\nCancelled; temporary files removed. Files already ingested stay in the library.", file=sys.stderr)
        return 130


if __name__ == "__main__":
    sys.exit(main())
