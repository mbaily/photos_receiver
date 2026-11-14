import hashlib
import fcntl
from functools import wraps
import hmac
import os
import re
import tempfile
from datetime import datetime, timezone
from pathlib import Path
import logging
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse
from pydantic import BaseModel, Field
import photo_catalogue
import photo_library
from photo_db import deleted_hashes, get_db, original_present
from photo_meta import index_photo
from photo_store import attach_sidecar, deleted_elsewhere, elsewhere as _elsewhere_of, store_file, stored_name, sweep_parts

log = logging.getLogger("receiver")
if not log.handlers:   # one line per sync in the service journal
    _handler = logging.StreamHandler()
    _handler.setFormatter(logging.Formatter("%(levelname)s:  receiver: %(message)s"))
    log.addHandler(_handler)
    log.setLevel(logging.INFO)
    log.propagate = False
MAX_SIDECAR_BYTES = 1_000_000

# Shared upload secret. When PHOTO_TOKEN is set, every request must carry it as
# "Authorization: Bearer <token>" (or an X-Photo-Token header); when unset, anyone
# who can reach the receiver can upload.
TOKEN = os.environ.get("PHOTO_TOKEN", "").strip()


def require_token(request: Request) -> None:
    if not TOKEN:
        return
    auth = request.headers.get("authorization", "")
    given = auth[7:].strip() if auth.lower().startswith("bearer ") else request.headers.get("x-photo-token", "")
    if not hmac.compare_digest(given.encode(), TOKEN.encode()):
        raise HTTPException(401, "missing or wrong upload token")


async def target_library(request: Request) -> photo_library.Library:
    """The library this request uploads to: the one named in X-Photo-Library
    (send_camera.py), else the upload library chosen with `libraries.sh inbox`.

    async on purpose: it runs in the request's own task, so the selection is visible to
    the endpoint and to everything it hands to the thread pool.
    """
    try:
        ref = request.headers.get("x-photo-library")
        lib = photo_library.require_online(photo_library.find(ref)) if ref else photo_library.inbox()
    except photo_library.LibraryError as e:
        raise HTTPException(503, str(e))
    held = photo_library.hold(lib.id)
    if held:      # a suspected older copy takes no uploads until that is acknowledged
        raise HTTPException(409, photo_library.held_message(lib, held))
    photo_library.use(lib)
    return lib


app = FastAPI(dependencies=[Depends(require_token)], docs_url=None, redoc_url=None, openapi_url=None)


@app.exception_handler(photo_library.LibraryError)
async def library_unavailable(request: Request, exc: photo_library.LibraryError):
    """The library went away, or became read-only, while a request was using it."""
    return JSONResponse({"detail": str(exc)}, status_code=503)


if not TOKEN:
    log.warning("PHOTO_TOKEN is not set: uploads are accepted without authentication")


# A separate sub-app keeps Safari downloads behind the proxy's browser login,
# while upload API routes continue to require their bearer token.
def require_shortcut_login(request: Request) -> None:
    if not request.headers.get("x-photo-shortcut-user"):
        raise HTTPException(401, "Sign in through the HTTPS shortcut download page")


shortcut_app = FastAPI(dependencies=[Depends(require_shortcut_login)],
                       docs_url=None, redoc_url=None, openapi_url=None)
SHORTCUT_DIR = Path(__file__).resolve().parent / "shortcut_downloads"
SHORTCUT_FILES = {
    "Send Batch to Server.plist": "application/xml",
    "Send Batch to Server.unsigned.shortcut": "application/octet-stream",
}
DOWNLOAD_HEADERS = {"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"}


@shortcut_app.get("/")
def shortcut_page():
    return FileResponse(Path(__file__).resolve().parent / "templates" / "shortcut_download.html",
                        media_type="text/html", headers=DOWNLOAD_HEADERS)


@shortcut_app.get("/{filename}")
def shortcut_file(filename: str):
    if filename not in SHORTCUT_FILES or not (SHORTCUT_DIR / filename).is_file():
        raise HTTPException(404, "Shortcut file not found; run ./build_shortcut.sh")
    return FileResponse(SHORTCUT_DIR / filename, media_type=SHORTCUT_FILES[filename],
                        filename=filename, headers=DOWNLOAD_HEADERS)


app.mount("/shortcuts", shortcut_app)


def _index(digest: str) -> None:
    """Extract camera metadata; never fail the upload over it (refresh_metadata.py can redo it)."""
    try:
        with get_db() as conn:
            index_photo(conn, digest)
    except Exception:
        log.exception("metadata extraction failed for %s", digest)


def _count(lib: photo_library.Library) -> int | None:
    token = photo_library.use(lib)
    try:
        with get_db() as conn:
            return conn.execute("SELECT COUNT(*) FROM photos").fetchone()[0]
    except Exception:
        return None
    finally:
        photo_library._current.reset(token)


def _elsewhere(hashes: list[str], target: photo_library.Library) -> dict[str, str]:
    """{hash: library name} for the given hashes that another library already has."""
    return _elsewhere_of(hashes, target.id)


def _duplicates_allowed(request: Request, flag: bool = False) -> bool:
    return flag or request.headers.get("x-allow-duplicates") == "1" or photo_library.allow_duplicates()


@app.get("/libraries")
def list_libraries():
    """For send_camera.py's menu: every known library and whether it is plugged in."""
    cached_counts = photo_catalogue.item_counts()
    return {"libraries": [
        {"id": lib.id, "name": lib.name, "online": lib.online, "inbox": lib.inbox,
         "last_seen": lib.last_seen,
         "items": _count(lib) if lib.online else (lib.remote or {}).get("items") if lib.host else cached_counts.get(lib.id),
         # Set for a library that lives on another machine (ingest_camera.py on a laptop).
         "host": lib.host, "synced_at": (lib.remote or {}).get("synced_at") if lib.host else None,
         # Why it takes no uploads for now (a suspected older copy), else None.
         "read_only": (lib.hold or {}).get("reason") if lib.online else None}
        for lib in photo_library.all_libraries()
    ]}


# ---- libraries on other machines --------------------------------------------
# A laptop that ingests with ingest_camera.py keeps the files and tells this server what
# its libraries contain. Nothing here can touch a library's own files or database.

LIBRARY_ID = re.compile(r"[0-9a-f]{8}")
MAX_SYNC_HASHES = 500_000
MAX_SYNC_ROWS = 1000


class RemoteLibrary(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    host: str = Field(default="", max_length=200)
    path: str = Field(default="", max_length=1000)
    created: str | None = Field(default=None, max_length=40)
    items: int = Field(default=0, ge=0)
    deleted: int = Field(default=0, ge=0)
    fingerprint: str = Field(default="", max_length=64)
    revision: int | None = Field(default=None, ge=0)    # the library's change counter


class RemoteHashes(BaseModel):
    sync_id: str | None = Field(default=None, max_length=32)
    photos: dict[str, str]          # sha256 -> row stamp
    deleted: list[str] = []


class RemoteRow(BaseModel):
    sha256: str
    saved_filename: str | None = Field(default=None, max_length=500)
    original_filename: str | None = Field(default=None, max_length=500)
    size_bytes: int | None = None
    media_type: str | None = Field(default=None, max_length=20)
    taken_at: str | None = Field(default=None, max_length=40)
    camera: str | None = Field(default=None, max_length=200)
    lens: str | None = Field(default=None, max_length=200)
    uploaded_at: str | None = Field(default=None, max_length=40)


class RemoteRows(BaseModel):
    sync_id: str | None = Field(default=None, max_length=32)
    rows: list[RemoteRow]


class KnownQuery(BaseModel):
    hashes: list[str]
    # The library the client is about to add these to; it does not count as "elsewhere".
    library: str | None = None


def _remote_library(lib_id: str) -> None:
    """Refuse ids that cannot be a library on another machine."""
    if not LIBRARY_ID.fullmatch(lib_id):
        raise HTTPException(400, "library id must be 8 hex digits")
    here = next((l for l in photo_library.all_libraries(fresh=True) if l.id == lib_id and l.online), None)
    if here:
        # Same id in two places: a library folder was copied rather than moved.
        raise HTTPException(409, f"library '{here.name}' ({lib_id}) is plugged in to the server itself "
                                 f"({here.path}), so it cannot also be synced from another machine. "
                                 "Two copies of one library are not merged.")


def _clean_hashes(hashes) -> list[str]:
    return [h.lower() for h in hashes if HASH_RE.fullmatch(h)]


def _serialized_sync(func):
    @wraps(func)
    def wrapped(*args, **kwargs):
        # Serialize publication through both SQLite and the registry, across workers.
        parent = photo_library.REGISTRY.parent
        parent.mkdir(parents=True, exist_ok=True)
        with open(parent / '.remote-sync.lock', 'a') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            return func(*args, **kwargs)
    return wrapped


@app.put("/remote/libraries/{lib_id}")
@_serialized_sync
def register_remote_library(lib_id: str, info: RemoteLibrary, request: Request):
    """A machine reports one of its libraries. Says whether the catalogue is up to date."""
    _remote_library(lib_id)
    name = " ".join(info.name.split())
    host = info.host.strip() or (request.client.host if request.client else "") or "another machine"
    info_dict = {"name": name, "remote": {
        "host": host, "path": info.path, "created": info.created, "items": info.items,
        "deleted": info.deleted, "revision": info.revision,
    }}
    if not re.fullmatch(r'[0-9a-f]{64}', info.fingerprint):
        raise HTTPException(400, 'A complete SHA-256 catalogue fingerprint is required.')
    try:
        sync_id, published = photo_catalogue.begin_sync(lib_id, info_dict, info.fingerprint)
    except photo_catalogue.SyncConflict as exc:
        raise HTTPException(409, str(exc))
    in_sync = published is not None
    _publish_remote(lib_id, published)
    warnings = list((published or {}).get('warnings') or [])
    clash = [l for l in photo_library.all_libraries(fresh=True)
             if l.id != lib_id and l.name.lower() == name.lower()]
    if clash:
        warnings.append(f"the server already has another library called '{clash[0].name}' "
                        f"({clash[0].id}, {clash[0].where}); rename one of them")
    log.info("remote library '%s' (%s) on %s: %d items, %s", name, lib_id, host, info.items,
             "in sync" if in_sync else "catalogue needs updating")
    return {"in_sync": in_sync, "warnings": warnings, "atomic_sync": True, "sync_id": sync_id}


def _known_remote(lib_id: str) -> None:
    _remote_library(lib_id)
    with photo_catalogue.connect() as conn:
        staged = conn.execute('SELECT 1 FROM sync_sessions WHERE library_id=?', (lib_id,)).fetchone()
    if not staged:
        raise HTTPException(404, "unknown library; register it first")


@app.post("/remote/libraries/{lib_id}/hashes")
@_serialized_sync
def sync_remote_hashes(lib_id: str, body: RemoteHashes):
    """Everything the library holds, as hashes. Answers which rows to send, and which of
    the newly listed photos another library already has."""
    _known_remote(lib_id)
    if len(body.photos) + len(body.deleted) > MAX_SYNC_HASHES:
        raise HTTPException(413, "too many hashes")
    stamps = {h.lower(): str(stamp)[:16] for h, stamp in body.photos.items() if HASH_RE.fullmatch(h)}
    try:
        need, new, published = photo_catalogue.stage_hashes(lib_id, stamps, _clean_hashes(body.deleted), body.sync_id)
    except photo_catalogue.SyncConflict as exc:
        raise HTTPException(409, str(exc))
    _publish_remote(lib_id, published)
    return {"need": need, "elsewhere": _elsewhere_of(new, lib_id), "committed": published is not None,
            "warnings": (published or {}).get('warnings') or []}


@app.post("/remote/libraries/{lib_id}/rows")
@_serialized_sync
def sync_remote_rows(lib_id: str, body: RemoteRows):
    _known_remote(lib_id)
    if len(body.rows) > MAX_SYNC_ROWS:
        raise HTTPException(413, f"at most {MAX_SYNC_ROWS} rows per request")
    rows = [r.model_dump() for r in body.rows if HASH_RE.fullmatch(r.sha256)]
    for row in rows:
        row["sha256"] = row["sha256"].lower()
    try:
        published = photo_catalogue.stage_rows(lib_id, rows, body.sync_id)
    except photo_catalogue.SyncConflict as exc:
        raise HTTPException(409, str(exc))
    _publish_remote(lib_id, published)
    return {"saved": len(rows), "committed": published is not None,
            "warnings": (published or {}).get('warnings') or []}


def _publish_remote(lib_id, published):
    if published is not None:
        photo_library.register_remote(lib_id, published['name'], published['remote'])


class SyncCommit(BaseModel):
    sync_id: str = Field(min_length=32, max_length=32)
    fingerprint: str = Field(min_length=64, max_length=64)


@app.post('/remote/libraries/{lib_id}/commit')
@_serialized_sync
def commit_remote_sync(lib_id: str, body: SyncCommit):
    _known_remote(lib_id)
    try:
        published = photo_catalogue.confirm_sync(lib_id, body.sync_id, body.fingerprint)
    except photo_catalogue.SyncConflict as exc:
        raise HTTPException(409, str(exc))
    _publish_remote(lib_id, published)
    return {'committed': True, 'fingerprint': body.fingerprint, 'warnings': published.get('warnings') or []}


class MovedMeta(BaseModel):
    original_filename: str | None = None
    uploaded_at: str | None = None
    metadata: str | None = None
    note1: str = ""
    note2: str = ""
    notes_updated_at: str | None = None
    hashtags: str = ""


class MoveFail(BaseModel):
    error: str = Field(default="", max_length=500)


@app.put("/meta/{digest}")
def upload_moved_meta(digest: str, body: MovedMeta, lib: photo_library.Library = Depends(target_library)):
    """Apply notes, hashtags and arrival metadata when moving an item from another machine."""
    import photo_move
    digest = digest.lower()
    with get_db() as conn:
        if not conn.execute("SELECT 1 FROM photos WHERE sha256 = ?", (digest,)).fetchone():
            raise HTTPException(404, "unknown sha256")
        photo_move.apply_moved_metadata(conn, digest, body.model_dump())
        photo_catalogue.refresh(lib.id, conn, [digest])
    return {"status": "meta_saved", "sha256": digest}


@app.get("/remote/moves")
def list_remote_moves():
    """Pending Server -> Laptop moves waiting to be pulled."""
    moves = photo_catalogue.pending_moves()
    libs = {l.id: l for l in photo_library.all_libraries(fresh=True) if l.online}
    out = []
    for m in moves:
        src = libs.get(m["source_lib_id"])
        if not src:
            continue
        tok = photo_library.use(src)
        try:
            with get_db() as conn:
                row = conn.execute("SELECT * FROM photos WHERE sha256 = ?", (m["sha256"],)).fetchone()
            if not row:
                photo_catalogue.complete_move(m["id"])
                continue
            out.append({
                **m,
                "saved_filename": Path(row["saved_filename"]).name,
                "original_filename": row["original_filename"],
                "size_bytes": row["size_bytes"],
                "uploaded_at": row["uploaded_at"],
                "metadata": row["metadata"] or "{}",
                "note1": row["note1"] or "",
                "note2": row["note2"] or "",
                "notes_updated_at": row["notes_updated_at"],
                "hashtags": row["hashtags"] or "",
                "sidecar_xml": row["sidecar_xml"],
            })
        finally:
            photo_library._current.reset(tok)
    return {"moves": out}


@app.get("/remote/moves/{move_id}/file")
def download_remote_move(move_id: str):
    m = photo_catalogue.get_move(move_id)
    if not m:
        raise HTTPException(404, "move not found")
    src = photo_library.require_online(photo_library.find(m["source_lib_id"]))
    tok = photo_library.use(src)
    try:
        with get_db() as conn:
            row = conn.execute("SELECT saved_filename FROM photos WHERE sha256 = ?", (m["sha256"],)).fetchone()
        if not row:
            raise HTTPException(404, "source photo no longer exists")
        path = src.path / Path(row["saved_filename"]).name
        if not path.is_file():
            raise HTTPException(404, "source file is missing")
        return FileResponse(path, filename=Path(row["saved_filename"]).name)
    finally:
        photo_library._current.reset(tok)


@app.post("/remote/moves/{move_id}/complete")
@_serialized_sync
def complete_remote_move(move_id: str):
    import photo_move
    m = photo_catalogue.get_move(move_id)
    if not m:
        return {"completed": True}
    src = photo_library.require_online(photo_library.find(m["source_lib_id"]))
    photo_move.remove_from_source(src, m["sha256"])
    photo_catalogue.complete_move(move_id)
    return {"completed": True}


@app.post("/remote/moves/{move_id}/fail")
def fail_remote_move(move_id: str, body: MoveFail):
    photo_catalogue.fail_move(move_id, body.error)
    return {"recorded": True}


@app.post("/known")
def known_hashes(body: KnownQuery):
    """Which of these files does any library the server knows already hold, or has deleted?
    Asked by ingest_camera.py before it copies from a camera into a library of its own."""
    if len(body.hashes) > MAX_SYNC_ROWS:
        raise HTTPException(413, f"at most {MAX_SYNC_ROWS} hashes per request")
    hashes = _clean_hashes(body.hashes)
    return {"known": _elsewhere_of(hashes, body.library), "deleted": deleted_elsewhere(hashes, body.library)}


class HashBatch(BaseModel):
    hashes: list[str]
    # Hashes the client holds a Sony sidecar XML for (send_camera.py); optional.
    have_sidecar: list[str] = []
    # Also ask for files that another library already has.
    allow_elsewhere: bool = False
    # Name the libraries those files are in (send_camera.py). Off by default so a
    # simple client that searches the reply for a hash only ever finds wanted ones.
    detail: bool = False


@app.get("/want/{digest}", response_class=PlainTextResponse)
def want_one(digest: str, request: Request, lib: photo_library.Library = Depends(target_library)):
    """Plain-text yes/no for one hash, for clients that cannot handle JSON reliably
    (the iPhone shortcut): "yes" = upload it, "no" = the server has it or does not want it."""
    batch = HashBatch(hashes=[digest.strip()])
    return "yes" if _wanted(batch, request, lib)["wanted"] else "no"


HASH_RE = re.compile(r"(?<![0-9A-Fa-f])[0-9A-Fa-f]{64}(?![0-9A-Fa-f])")


@app.post("/wanted")
async def check_wanted_batch(request: Request, lib: photo_library.Library = Depends(target_library)):
    """Which of these hashes does the library still want?

    The body should be {"hashes": [...]}. Shortcuts on the phone do not always produce
    clean JSON, so anything else is searched for 64-digit hex hashes instead of rejected.
    """
    raw = (await request.body()).decode("utf-8", "replace")
    try:
        batch = HashBatch.model_validate_json(raw)
    except ValueError:
        batch = HashBatch(hashes=HASH_RE.findall(raw))
        log.warning("wanted: body was not the expected JSON; found %d hash(es) in it. Body starts: %r",
                    len(batch.hashes), raw[:300])
    return await run_in_threadpool(_wanted, batch, request, lib)


def _wanted(batch: HashBatch, request: Request, lib: photo_library.Library) -> dict:
    normalized = [h.lower() for h in batch.hashes if len(h) == 64]
    if not normalized:
        log.info("wanted: %d offered, none usable (lengths %s) -> nothing wanted",
                 len(batch.hashes), sorted({len(h) for h in batch.hashes}))
        return {"wanted": []}

    rows = []
    with get_db() as conn:
        for i in range(0, len(normalized), 500):
            chunk = normalized[i:i + 500]
            placeholders = ",".join("?" for _ in chunk)
            rows.extend(conn.execute(
                f"SELECT sha256, saved_filename, size_bytes, sidecar_xml IS NULL AS no_sidecar FROM photos WHERE sha256 IN ({placeholders})",
                chunk,
            ).fetchall())
        deleted = deleted_hashes(conn, normalized)   # deleted in the browser: don't ask for them again

    tracked = {row['sha256'] for row in rows}
    present = {row['sha256'] for row in rows if original_present(row)}
    existing = present | deleted
    wanted = [h for h in normalized if h not in existing]
    elsewhere = _elsewhere([h for h in wanted if h not in tracked], lib)
    if elsewhere and not _duplicates_allowed(request, batch.allow_elsewhere):
        wanted = [h for h in wanted if h not in elsewhere]
        log.warning("%d of %d offered files already exist in another library (%s); not requested for '%s'",
                    len(elsewhere), len(normalized), ", ".join(sorted(set(elsewhere.values()))), lib.name)
    # A plain request (the iPhone shortcut) gets exactly {"wanted": [...]} and nothing else:
    # the shortcut parses this reply and broke when other keys were added.
    result = {"wanted": wanted}
    log.info("wanted: %d offered, %d usable, %d wanted for '%s' (client %s)", len(batch.hashes), len(normalized),
             len(wanted), lib.name, (request.headers.get("user-agent") or "?")[:60])
    if batch.detail:
        result["library"] = {"id": lib.id, "name": lib.name}
        result["elsewhere"] = elsewhere
        result['repair_wanted'] = [h for h in wanted if h in tracked]
    if batch.have_sidecar:
        # Files we already have whose sidecar never arrived (e.g. the connection dropped
        # between the two uploads), so the client can send just the sidecar.
        offered = {h.lower() for h in batch.have_sidecar}
        result["sidecar_wanted"] = [r["sha256"] for r in rows if r["no_sidecar"] and r["sha256"] in offered and r['sha256'] in present]
    return result


@app.put("/sidecar/{digest}")
async def upload_sidecar(digest: str, request: Request,
                         lib: photo_library.Library = Depends(target_library)):
    """Sony video sidecar XML (C0001M01.XML) for an already-uploaded clip."""
    body = await request.body()
    if len(body) > MAX_SIDECAR_BYTES:
        raise HTTPException(413, "sidecar too large")
    xml_text = body.decode("utf-8", "replace")
    saved = await run_in_threadpool(attach_sidecar, lib, digest.lower(), xml_text)
    if not saved:
        raise HTTPException(404, "unknown sha256")
    return {"status": "sidecar_saved", "sha256": digest.lower()}


@app.put("/{filename}")
async def upload(filename: str, request: Request,
                 lib: photo_library.Library = Depends(target_library)):
    hasher = hashlib.sha256()
    safe_name = stored_name(filename)
    size_bytes = 0
    folder = lib.path
    if not os.access(folder, os.W_OK):
        raise HTTPException(503, f"library '{lib.name}' is read-only or not writable")
    sweep_parts(folder)      # leftovers of uploads that died part-way

    try:
        fd, tmp_path_str = tempfile.mkstemp(dir=folder, prefix=".part_")
    except OSError as exc:
        raise HTTPException(503, f"library '{lib.name}' cannot be written to: {exc}") from exc
    tmp_path = Path(tmp_path_str)
    try:
        with os.fdopen(fd, "wb") as f:
            async for chunk in request.stream():
                hasher.update(chunk)
                f.write(chunk)
                size_bytes += len(chunk)

        digest = hasher.hexdigest()
        expected = request.headers.get('x-photo-sha256')
        if expected is not None:
            if not HASH_RE.fullmatch(expected):
                raise HTTPException(400, 'X-Photo-SHA256 must contain a SHA-256 hash')
            if not hmac.compare_digest(expected.lower(), digest):
                raise HTTPException(409, 'Source bytes changed after hashing; retry the upload')
        meta = {
            "content_type": request.headers.get("content-type"),
            "client_ip": request.client.host if request.client else None,
        }
        # Optional headers sent by send_camera.py (the iPhone shortcut sends none).
        for header, key in (("x-photo-source", "source"), ("x-source-path", "source_path")):
            if request.headers.get(header):
                meta[key] = request.headers[header]
        if request.headers.get("x-photo-source") == "library_move":
            def _was_deleted() -> bool:
                with get_db() as conn:
                    return bool(deleted_hashes(conn, [digest]))
            if await run_in_threadpool(_was_deleted):
                raise HTTPException(409, f"was previously deleted from '{lib.name}'")
        # Same steps as ingest_camera.py on a laptop: see photo_store.store_file.
        return await run_in_threadpool(
            store_file, lib, tmp_path, digest, size_bytes, safe_name,
            request.headers.get("x-original-filename"), meta,
            allow_duplicates=_duplicates_allowed(request), content_type=request.headers.get("content-type"))
    except Exception:
        tmp_path.unlink(missing_ok=True)
        raise
