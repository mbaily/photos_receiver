"""Putting one file into a library - shared by the receiver (uploads over HTTPS) and
ingest_camera.py (camera plugged into the machine that holds the library), so both paths
name, record and index files in exactly the same way.

No web framework in here: ingest_camera.py runs without the server.
"""

import hashlib
import fcntl
from functools import wraps
from contextlib import contextmanager
import logging
import os
import shutil
import tempfile
import time
from pathlib import Path

import photo_catalogue
import photo_library
from photo_db import deleted_hashes, existing_file, get_db, insert_photo, original_present
from photo_meta import index_photo

log = logging.getLogger("receiver")


def sha256_file(path: Path) -> str:
    hasher = hashlib.sha256()
    with open(path, "rb") as f:
        while chunk := f.read(4 * 1024 * 1024):
            hasher.update(chunk)
    return hasher.hexdigest()


def fsync_path(path: Path) -> None:
    """Flush a file's contents, or a folder's entries, to the disk. Best effort: some
    filesystems (FAT on a USB stick) cannot flush a folder."""
    try:
        fd = os.open(path, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


def copy_verified(src: Path, folder: Path, digest: str) -> Path | None:
    """Copy `src` into a temporary file in `folder`; None unless what was written has the
    expected hash. The temporary file is flushed and is the caller's to move or remove."""
    hasher = hashlib.sha256()
    fd, tmp = tempfile.mkstemp(dir=folder, prefix=".part_")
    tmp_path = Path(tmp)
    try:
        with os.fdopen(fd, "wb") as out, open(src, "rb") as f:
            while chunk := f.read(4 * 1024 * 1024):
                hasher.update(chunk)
                out.write(chunk)
            out.flush()
            os.fsync(out.fileno())
    except BaseException:
        tmp_path.unlink(missing_ok=True)
        raise
    if hasher.hexdigest() != digest:
        tmp_path.unlink(missing_ok=True)
        return None
    return tmp_path


def sniff_extension(path: Path, content_type: str | None) -> str:
    """Extension for an upload that arrived without one (some phone shortcuts omit it)."""
    try:
        with open(path, "rb") as f:
            head = f.read(16)
    except OSError:
        head = b""
    if head[:3] == b"\xff\xd8\xff":
        return ".jpg"
    if head[:8] == b"\x89PNG\r\n\x1a\n":
        return ".png"
    if head[4:8] == b"ftyp":
        brand = head[8:12]
        if brand in (b"heic", b"heix", b"hevc", b"mif1", b"msf1", b"heim", b"heis"):
            return ".heic"
        if brand == b"qt  ":
            return ".mov"
        return ".mp4"
    if head[:4] in (b"II*\x00", b"MM\x00*"):
        return ".arw" if "sony" in (content_type or "") else ".tif"
    by_type = {"image/jpeg": ".jpg", "image/heic": ".heic", "image/heif": ".heic", "image/png": ".png",
               "video/mp4": ".mp4", "video/quicktime": ".mov"}
    return by_type.get((content_type or "").split(";")[0].strip().lower(), "")


RESERVED_NAMES = {photo_library.MARKER.lower(), "inbox.db"}


def stored_name(name: str) -> str:
    """File name an upload may be stored under. The library's own files - the database and
    its -wal/-shm companions, library.json, and everything hidden (.trash, .cache, .part_*,
    the backup tag) - are never used, even when they do not exist at the moment."""
    name = Path(name).name.strip()
    lower = name.lower()
    if not name or name.startswith(".") or lower in RESERVED_NAMES or lower.startswith("inbox.db"):
        name = "upload_" + name.lstrip(".")
    # Every filesystem a library may live on limits a name to 255 bytes (FAT: characters);
    # leave room for the suffix added when a name is already taken.
    suffix = Path(name).suffix
    suffix = suffix if len(suffix.encode()) <= 20 else ""
    stem = name[:len(name) - len(suffix)] if suffix else name
    while len(stem.encode()) > MAX_STEM_BYTES:
        stem = stem[:-1]
    return (stem or "upload") + suffix


MAX_STEM_BYTES = 180


_swept: dict[str, float] = {}


def sweep_parts(folder: Path, every: float = 3600) -> None:
    """Remove temporary files that a crashed or interrupted upload/ingest left behind more
    than a day ago. Cheap, and at most once per `every` seconds for each library."""
    now = time.time()
    if now - _swept.get(str(folder), 0) < every:
        return
    _swept[str(folder)] = now
    try:
        for part in folder.glob(".part_*"):
            try:
                if part.lstat().st_mtime < now - 86400:
                    shutil.rmtree(part) if part.is_dir() and not part.is_symlink() else part.unlink()
            except OSError:
                pass
    except OSError:
        pass


def _other_libraries(hashes: list[str], skip_id: str | None, table: str) -> tuple[dict[str, str], set[str]]:
    """{hash: library name} for hashes listed in `table` of another library plugged in here,
    and the ids of libraries that could not be read (a disk pulled a moment ago is still
    listed as plugged in): those are answered from the catalogue instead of not at all."""
    found: dict[str, str] = {}
    unreadable: set[str] = set()
    for lib in photo_library.online():
        if lib.id == skip_id:
            continue
        token = photo_library.use(lib)
        try:
            with get_db() as conn:
                for i in range(0, len(hashes), 500):
                    chunk = hashes[i:i + 500]
                    columns = 'sha256,saved_filename,size_bytes' if table == 'photos' else 'sha256'
                    for row in conn.execute(
                            f"SELECT {columns} FROM {table} WHERE sha256 IN ({','.join('?' * len(chunk))})", chunk):
                        if table != 'photos' or original_present(row):
                            found.setdefault(row['sha256'], lib.name)
        except Exception as e:
            log.warning("could not read library %s for the duplicate check (%s); using its catalogue", lib.name, e)
            unreadable.add(lib.id)
        finally:
            photo_library._current.reset(token)
    return found, unreadable


def _catalogued(lookup, hashes: list[str], skip_id: str | None, unreadable=frozenset()) -> dict[str, str]:
    """Same, from the catalogue, for libraries that are away: unplugged disks of this
    machine and libraries on other machines. A library that is plugged in here is
    answered from its own database instead."""
    libs = {lib.id: lib for lib in photo_library.all_libraries()}
    skip = {i for i, lib in libs.items() if lib.online and i not in unreadable} | ({skip_id} if skip_id else set())
    try:
        held = lookup(hashes, skip)
    except Exception:
        log.exception("could not read the catalogue of remote libraries")
        return {}
    out = {}
    for sha, lib_id in held.items():
        lib = libs.get(lib_id)
        if lib:      # rows of a forgotten library are ignored
            out[sha] = (f"{lib.name} (on {lib.host})" if lib.host else
                        f"{lib.name} (as last recorded)" if lib.online else f"{lib.name} (not plugged in)")
    return out


def elsewhere(hashes: list[str], skip_id: str | None) -> dict[str, str]:
    """{hash: library name} for the given hashes that another library already has:
    plugged in here, unplugged (as last recorded), or on another machine that has synced."""
    found, unreadable = _other_libraries(hashes, skip_id, "photos")
    for sha, name in _catalogued(photo_catalogue.holders, [h for h in hashes if h not in found], skip_id,
                                 unreadable).items():
        found.setdefault(sha, name)
    return found


def deleted_elsewhere(hashes: list[str], skip_id: str | None) -> dict[str, str]:
    """{hash: library name} for hashes that were deleted from another library."""
    found, unreadable = _other_libraries(hashes, skip_id, "deleted_photos")
    for sha, name in _catalogued(photo_catalogue.deleted_in, [h for h in hashes if h not in found], skip_id,
                                 unreadable).items():
        found.setdefault(sha, name)
    return found


@contextmanager
def library_lock(lib):
    """Coordinate operations that move a library's originals or trash files."""
    photo_library.require_writable(lib)
    with open(lib.path / ".store.lock", "a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        yield


def _serialize_store(func):
    @wraps(func)
    def locked(lib, *args, **kwargs):
        # Duplicate refusal must be atomic across this machine's libraries.
        photo_library.REGISTRY.parent.mkdir(parents=True, exist_ok=True)
        with library_lock(lib), open(photo_library.REGISTRY.parent / ".store.lock", "a") as machine_lock:
            fcntl.flock(machine_lock, fcntl.LOCK_EX)
            return func(lib, *args, **kwargs)
    return locked


def attach_sidecar(lib, digest: str, xml_text: str, *, missing_only: bool = False) -> bool:
    """Attach metadata to an existing clip and refresh its catalogue immediately."""
    photo_library.require_writable(lib)
    with get_db() as conn:
        condition = " AND (sidecar_xml IS NULL OR sidecar_xml = '')" if missing_only else ""
        changed = conn.execute("UPDATE photos SET sidecar_xml = ? WHERE sha256 = ?" + condition,
                               (xml_text, digest)).rowcount
    if not changed:
        return False
    with get_db() as conn:
        index_photo(conn, digest)
    with get_db() as conn:
        photo_catalogue.refresh(lib.id, conn, [digest])
    return True


@_serialize_store
def _store_original(lib: photo_library.Library, tmp_path: Path, digest: str, size_bytes: int, name: str,
               original_name: str | None = None, meta: dict | None = None, *,
               allow_duplicates: bool = False, content_type: str | None = None,
               sidecar_xml: str | None = None) -> dict:
    """Move a finished temporary file (already inside the library folder) into place and
    record it. `lib` must be the current library (photo_library.use).

    Returns {"status": ...}: saved, or deleted_skipped / duplicate_skipped /
    elsewhere_skipped, in which case the temporary file has been removed.
    """
    folder = lib.path
    safe_name = stored_name(name)
    with get_db() as conn:
        if deleted_hashes(conn, [digest]):
            tmp_path.unlink(missing_ok=True)
            return {"status": "deleted_skipped", "sha256": digest}
        recorded = conn.execute('SELECT * FROM photos WHERE sha256=?', (digest,)).fetchone()
        existing = existing_file(conn, digest)
        if existing:
            tmp_path.unlink(missing_ok=True)
            return {"status": "duplicate_skipped", "sha256": digest, "file": existing}

    if recorded:
        safe_name = stored_name(recorded['saved_filename'])
    if not allow_duplicates and not recorded:
        other = elsewhere([digest], lib.id).get(digest)
        if other:
            tmp_path.unlink(missing_ok=True)
            log.warning("refused %s: already in library '%s'", safe_name, other)
            return {"status": "elsewhere_skipped", "sha256": digest, "library": other}

    if not Path(safe_name).suffix:
        safe_name += sniff_extension(tmp_path, content_type)
    dest = folder / safe_name
    if dest.is_file() and dest.stat().st_size == size_bytes and sha256_file(dest) == digest:
        # Left behind by an upload that stored the file but died before recording it.
        tmp_path.unlink(missing_ok=True)
    else:
        base, number = dest, 0
        while dest.exists() or dest.is_symlink():
            number += 1
            suffix = f"_{digest[:8]}" + (f"_{number}" if number > 1 else "")
            dest = folder / f"{base.stem}{suffix}{base.suffix}"
        fsync_path(tmp_path)          # the bytes are on the disk before the row says so
        os.replace(tmp_path, dest)
    fsync_path(folder)

    with get_db() as conn:
        if recorded:
            conn.execute('UPDATE photos SET saved_filename=?,size_bytes=? WHERE sha256=?', (dest.name, size_bytes, digest))
        else:
            insert_photo(conn, digest, dest.name, Path(original_name or safe_name).name, size_bytes, meta or {})
        if sidecar_xml:
            conn.execute("UPDATE photos SET sidecar_xml = ? WHERE sha256 = ?", (sidecar_xml, digest))

    return {"status": "repaired" if recorded else "saved", "file": dest.name, "sha256": digest}


def store_file(lib: photo_library.Library, tmp_path: Path, digest: str, size_bytes: int, name: str,
               original_name: str | None = None, meta: dict | None = None, *,
               allow_duplicates: bool = False, content_type: str | None = None,
               sidecar_xml: str | None = None) -> dict:
    """Store and index a finished temporary file. `lib` must be the current library.

    Duplicate checks and the original's database commit are serialized on this machine.
    Metadata extraction runs after releasing the locks so other uploads can proceed.
    """
    result = _store_original(lib, tmp_path, digest, size_bytes, name, original_name, meta,
                             allow_duplicates=allow_duplicates, content_type=content_type,
                             sidecar_xml=sidecar_xml)
    if result["status"] not in ('saved', 'repaired'):
        return result
    if result["status"] == "repaired":
        for cached in lib.cache_dir.glob(f"{digest}_*.jpg"):
            cached.unlink(missing_ok=True)
    # Camera metadata; never fail the store over it (refresh_metadata.py can redo it).
    try:
        with get_db() as conn:
            index_photo(conn, digest)
    except Exception:
        log.exception("metadata extraction failed for %s", digest)
    with get_db() as conn:
        photo_catalogue.refresh(lib.id, conn, [digest])
    return result
