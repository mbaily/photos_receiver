#!/usr/bin/env python3
"""Photo library browser (FastAPI + htmx + daisyUI).

Shows everything in inbox.db: HEIC/JPG photos, Sony ARW raws and MP4 videos, with two
notes fields per item, #hashtag extraction, a hashtag page and search.
"""

import argparse
import io
import json
import mimetypes
import os
import re
import shutil
import subprocess
import threading
from collections import defaultdict
from datetime import datetime
from pathlib import Path

from fastapi import FastAPI, Form, HTTPException, Query, Request
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from PIL import Image, ImageOps
import pillow_heif

import photo_catalogue
import photo_sync
import photo_library
import photo_db as db
from photo_meta import lookup
import photo_collections as coll
from web_templates import format_field, proxy_prefix, qs, root_of, templates
import browse_albums
import browse_trash
import browse_catalogue
from photo_store import library_lock

pillow_heif.register_heif_opener()

BASE_DIR = Path(__file__).resolve().parent
FFMPEG = os.environ.get("FFMPEG") or shutil.which("ffmpeg")
THUMB_SIZE = 480
VIEW_SIZE = 2400
PAGE_SIZE = 60
IMMUTABLE = {"Cache-Control": "public, max-age=31536000, immutable"}  # URLs are content hashes
CONTENT_TYPES = {".heic": "image/heic", ".heif": "image/heif", ".arw": "image/x-sony-arw",
                 ".mp4": "video/mp4", ".mov": "video/quicktime", ".mts": "video/mp2t"}

# Highlighted in the detail panel (looked up by tag name, ignoring exiftool's group).
SHOOTING_FIELDS = [
    ("Exposure", ["ExposureTime"]), ("Aperture", ["FNumber", "Aperture"]), ("ISO", ["ISO", "ISOSpeedRatings", "PhotographicSensitivity"]),
    ("Focal length", ["FocalLength"]), ("Program", ["ExposureProgram"]),
    ("Exp. comp.", ["ExposureCompensation"]), ("Metering", ["MeteringMode"]),
    ("White balance", ["WhiteBalance"]), ("Focus", ["FocusMode"]), ("Flash", ["Flash"]),
    ("Creative look", ["CreativeStyle", "CreativeLook"]), ("Picture profile", ["PictureProfile"]),
    ("Frame rate", ["VideoFrameRate", "VideoFrame:captureFps"]), ("Bitrate", ["AvgBitrate"]),
    ("Codec", ["CompressorName", "VideoFrame:videoCodec"]),
    ("Gamma", ["Item:CaptureGammaEquation"]), ("Serial", ["InternalSerialNumber", "SerialNumber", "Device:serialNo"]),
    ("GPS", ["GPSPosition"]),
]

app = FastAPI()
# Vendored daisyUI / Tailwind (browser build) / htmx, so no CDN is needed.
app.mount("/static", StaticFiles(directory=BASE_DIR / "static"), name="static")
app.include_router(browse_albums.router)
app.include_router(browse_trash.router)
app.include_router(browse_catalogue.router)
_render_locks: defaultdict[str, threading.Lock] = defaultdict(threading.Lock)


# ---- libraries --------------------------------------------------------------

LIB_PATH = re.compile(r"/lib/([0-9a-fA-F]{8})(/.*)?")
NO_LIBRARY_PATHS = ("/libraries", "/static/")


class LibraryRouter:
    """Every page lives under /lib/<id>/...: strip that part, select the library for the
    request, and send bare URLs (old bookmarks, '/') to the default library."""

    def __init__(self, inner):
        self.inner = inner

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.inner(scope, receive, send)
        path = scope["path"]
        if path in ("/libraries", "/sync") or path.startswith(("/libraries/", "/static/")):
            return await self.inner(scope, receive, send)
        headers = {k.decode("latin-1"): v.decode("latin-1") for k, v in scope["headers"]}
        proxy = proxy_prefix(headers.get("x-forwarded-prefix", ""))
        match = LIB_PATH.fullmatch(path)
        if not match:
            try:
                target = f"{proxy}/lib/{photo_library.inbox().id}{path}"
            except photo_library.LibraryError:
                first = photo_library.online()
                target = f"{proxy}/lib/{first[0].id}{path}" if first else f"{proxy}/libraries"
            if scope["method"] not in ("GET", "HEAD"):
                return await Response("no library in the URL", status_code=404)(scope, receive, send)
            query = scope.get("query_string", b"").decode("latin-1")
            response = RedirectResponse(target + (f"?{query}" if query and "/lib/" in target else ""), status_code=302)
            return await response(scope, receive, send)
        try:
            lib = photo_library.require_online(photo_library.find(match[1]))
        except photo_library.LibraryError as e:
            response = _library_gone(scope["method"], "hx-request" in headers, proxy, str(e))
            return await response(scope, receive, send)
        held = photo_library.hold(lib.id)
        if held and scope["method"] not in ("GET", "HEAD"):
            # A suspected older copy can be looked at, not changed.
            response = Response(photo_library.held_message(lib, held), status_code=409, media_type="text/plain")
            return await response(scope, receive, send)
        inner_path = match[2] or "/"
        scope = dict(scope, path=inner_path, raw_path=inner_path.encode(), photo_library=lib)
        token = photo_library.use(lib)
        try:
            await self.inner(scope, receive, send)
        finally:
            photo_library._current.reset(token)


@app.exception_handler(OverflowError)
async def number_out_of_range(request: Request, exc: OverflowError):
    """An album or page number too large to exist."""
    return Response("not found", status_code=404, media_type="text/plain")


def _library_gone(method: str, from_page_script: bool, proxy: str, why: str) -> Response:
    """Answer for a library that cannot be used (unplugged, or read-only for a change).
    A page load goes to the Libraries page, which says why. A change is refused with the
    reason, so the page shows it instead of looking as if it worked. A part of a page being
    fetched sends the whole page to the Libraries page rather than swapping it in."""
    target = f"{proxy}/libraries?{qs(missing=why)}"
    if method not in ("GET", "HEAD"):
        return Response(why, status_code=409, media_type="text/plain")
    if from_page_script:
        return Response(status_code=200, headers={"HX-Redirect": target})
    return RedirectResponse(target, status_code=302)


@app.exception_handler(photo_library.LibraryError)
async def library_unavailable(request: Request, exc: photo_library.LibraryError):
    """The disk was pulled (or the library became read-only) while a page was using it."""
    return _library_gone(request.method, "hx-request" in request.headers,
                         proxy_prefix(request.headers.get('x-forwarded-prefix', '')), str(exc))


def _warnings_by_library() -> dict:
    out: dict[str, list] = {}
    for w in photo_catalogue.warnings():
        out.setdefault(w['library_id'], []).append(w)
    return out


@app.get("/libraries", response_class=HTMLResponse)
def libraries_page(request: Request, missing: str = "", sync_requested: bool = False):
    libs = photo_library.all_libraries(fresh=True)
    counts = photo_catalogue.item_counts()
    for lib in libs:
        if lib.online:
            token = photo_library.use(lib)
            try:
                with db.get_db() as conn:
                    counts[lib.id] = conn.execute("SELECT COUNT(*) FROM photos").fetchone()[0]
            except Exception:
                counts[lib.id] = None
            finally:
                photo_library._current.reset(token)
    return templates.TemplateResponse(request, "libraries.html", {
        "all_libraries": libs, "counts": counts, "missing": missing,
        "duplicates_allowed": photo_library.allow_duplicates(),
        'background_sync': photo_sync.config().get('enabled', False),
        'sync_statuses': {lib.id: photo_sync.view(lib) for lib in libs},
        'pending_remote': photo_catalogue.pending_syncs(), 'sync_requested': sync_requested,
        'pending_moves': photo_catalogue.pending_moves(),
        'warnings': _warnings_by_library(),
    })


@app.post('/libraries/{library_id}/acknowledge')
def acknowledge_library(request: Request, library_id: str):
    """Clear a library's warnings; a suspected older copy becomes writable again."""
    try:
        lib = photo_library.find(library_id, photo_library.all_libraries(fresh=True))
    except photo_library.LibraryError as exc:
        raise HTTPException(404, str(exc))
    photo_library.acknowledge(lib.id)
    return RedirectResponse(proxy_prefix(request.headers.get('x-forwarded-prefix', '')) + '/libraries',
                            status_code=303)


@app.post('/sync')
def request_catalogue_sync(request: Request):
    if not photo_sync.config().get('enabled'):
        raise HTTPException(409, 'Automatic sync is not configured on this machine.')
    for lib in photo_library.online():
        photo_sync.changed(lib.id, launch=False)
    photo_sync.schedule()
    return RedirectResponse(proxy_prefix(request.headers.get('x-forwarded-prefix', '')) +
                            '/libraries?sync_requested=1', status_code=303)


# ---- data helpers -----------------------------------------------------------

def get_row(digest: str):
    with db.get_db() as conn:
        row = conn.execute("SELECT * FROM photos WHERE sha256 = ?", (digest,)).fetchone()
    if not row:
        raise HTTPException(404, "not found")
    return row


def file_path(row) -> Path:
    path = photo_library.current().path / Path(row["saved_filename"]).name
    if not path.is_file():
        raise HTTPException(404, "file missing")
    return path


def media_type(row) -> str:
    return row["media_type"] or db.media_type_for(row["saved_filename"])


# ---- rendering (thumbnails / web-viewable JPEGs) ----------------------------

def load_image(row, size: int) -> Image.Image | None:
    path = file_path(row)
    kind = media_type(row)
    if kind == "video":
        return video_frame(path, size)
    if kind == "raw":
        import rawpy
        with rawpy.imread(str(path)) as raw:
            if size <= THUMB_SIZE:
                # The embedded preview JPEG is fast; apply the raw's orientation to it.
                try:
                    thumb = raw.extract_thumb()
                    if thumb.format == rawpy.ThumbFormat.JPEG:
                        img = Image.open(io.BytesIO(thumb.data))
                        img.load()
                        rot = {3: Image.Transpose.ROTATE_180, 5: Image.Transpose.ROTATE_90,
                               6: Image.Transpose.ROTATE_270}.get(raw.sizes.flip)
                        return img.transpose(rot) if rot else img
                except (rawpy.LibRawNoThumbnailError, rawpy.LibRawUnsupportedThumbnailError):
                    pass
            rgb = raw.postprocess(use_camera_wb=True, half_size=True, no_auto_bright=False)
            return Image.fromarray(rgb)
    with Image.open(path) as img:
        img = ImageOps.exif_transpose(img)
        img.load()
        return img


def video_frame(path: Path, size: int) -> Image.Image | None:
    if not FFMPEG:
        return None
    for seek in ("1", "0"):
        proc = subprocess.run(
            [FFMPEG, "-v", "error", "-ss", seek, "-i", str(path), "-frames:v", "1",
             "-vf", f"scale='min({size},iw)':-2", "-f", "image2pipe", "-vcodec", "mjpeg", "-"],
            capture_output=True, timeout=60,
        )
        if proc.stdout:
            return Image.open(io.BytesIO(proc.stdout))
    return None


def rendered_jpeg(row, size: int) -> Path | None:
    cache = photo_library.current().cache_dir
    out = cache / f"{row['sha256']}_{size}.jpg"
    if out.exists():
        return out
    cache.mkdir(exist_ok=True)
    with _render_locks[str(out)]:
        if out.exists():
            return out
        try:
            img = load_image(row, size)
        except HTTPException:
            raise
        except Exception:      # corrupt or unsupported file: show the placeholder, not an error
            img = None
        if img is None:
            return None
        if img.mode not in ("RGB", "L"):
            img = img.convert("RGB")
        img.thumbnail((size, size), Image.Resampling.LANCZOS)
        tmp = out.with_suffix(".tmp")
        img.save(tmp, format="JPEG", quality=82 if size <= THUMB_SIZE else 90, optimize=True)
        os.replace(tmp, out)
        return out


def placeholder(label: str) -> Response:
    svg = f"""<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 400 300">
<rect width="400" height="300" fill="#2a2a2e"/>
<path d="M175 115v70l60-35z" fill="#888"/>
<text x="200" y="240" fill="#aaa" font-family="sans-serif" font-size="22" text-anchor="middle">{label}</text></svg>"""
    return Response(svg, media_type="image/svg+xml", headers={"Cache-Control": "no-cache"})


# ---- pages ------------------------------------------------------------------

@app.get("/", response_class=HTMLResponse)
def index(request: Request, q: str = "", type: str = "", camera: str = ""):
    with db.get_db() as conn:
        cameras = db.all_cameras(conn)
    return templates.TemplateResponse(request, "index.html", {
        "q": q, "type": type, "camera": camera, "cameras": cameras, "page": "library", "album": None,
    })


def parse_album(album: str) -> int | None:
    return int(album) if album.isdigit() else None


@app.get("/grid", response_class=HTMLResponse)
def grid(request: Request, q: str = "", type: str = "", album: str = "", camera: str = "", page: int = 1):
    """Photo grid page (infinite scroll). `album` limits it to one album's photos."""
    page = max(page, 1)
    album_id = parse_album(album)
    with db.get_db() as conn:
        rows = db.search(conn, q, type, limit=PAGE_SIZE, offset=(page - 1) * PAGE_SIZE, album=album_id, camera=camera)
        tags = db.tags_for(conn, [r["sha256"] for r in rows])
        total = db.search_count(conn, q, type, album_id, camera=camera) if page == 1 else None
        # Searching the whole library also finds matching albums and folders.
        collections = coll.search(conn, q) if page == 1 and q and album_id is None else []
    resp = templates.TemplateResponse(request, "_grid.html", {
        "rows": rows, "tags": tags, "q": q, "type": type, "album": album_id, "camera": camera, "page": page,
        "total": total, "more": len(rows) == PAGE_SIZE, "collections": collections,
    })
    if page == 1 and request.headers.get("HX-Request"):
        base = root_of(request) + (f"/albums/{album_id}" if album_id else "/")
        query = qs(q=q, type=type, camera=camera)
        resp.headers["HX-Replace-Url"] = base + (f"?{query}" if query else "")
    return resp


@app.get("/photo/{digest}", response_class=HTMLResponse)
def detail(request: Request, digest: str, q: str = "", type: str = "", album: str = "", camera: str = ""):
    row = get_row(digest)
    album_id = parse_album(album)
    with db.get_db() as conn:
        ids = db.search_ids(conn, q, type, album_id, camera=camera)
        tags = db.tags_for(conn, [digest])[digest]
        albums = coll.albums_for_photo(conn, digest)
        album_crumbs = coll.breadcrumbs(conn, album_id) if album_id else []
    pos = ids.index(digest) if digest in ids else None
    prev_id = ids[pos - 1] if pos is not None and pos > 0 else None
    next_id = ids[pos + 1] if pos is not None and pos + 1 < len(ids) else None

    exif = json.loads(row["exif"]) if row["exif"] else {}
    shooting = [(label, format_field(label, v)) for label, names in SHOOTING_FIELDS
                if (v := lookup(exif, *names)) is not None]
    upload = json.loads(row["metadata"] or "{}")
    return templates.TemplateResponse(request, "_detail.html", {
        "p": row, "kind": media_type(row), "tags": tags, "q": q, "type": type, "album": album_id, "camera": camera,
        "prev_id": prev_id, "next_id": next_id, "pos": pos, "count": len(ids),
        "shooting": shooting, "exif": sorted(exif.items()), "upload": upload, "albums": albums,
        "album_crumbs": album_crumbs,
    })


@app.post("/photo/{digest}/notes", response_class=HTMLResponse)
def save_notes(request: Request, digest: str, note1: str = Form(""), note2: str = Form("")):
    get_row(digest)
    with db.get_db() as conn:
        tags = db.set_notes(conn, digest, note1, note2)
    return templates.TemplateResponse(request, "_notes_saved.html", {
        "digest": digest, "tags": tags, "note1": note1, "note2": note2, "saved_at": datetime.now().strftime("%H:%M:%S"),
    })


@app.get('/photos/tags', response_class=HTMLResponse)
def hashtag_form(request: Request, sha: list[str] = Query(default=[]), mode: str = 'add'):
    if mode not in ('add', 'remove'):
        raise HTTPException(400, 'Choose add or remove')
    with db.get_db() as conn:
        selected = [digest for digest in dict.fromkeys(sha) if db.existing_file(conn, digest)]
        existing = sorted({tag for tags in db.tags_for(conn, selected).values() for tag in tags})
    return templates.TemplateResponse(request, '_hashtag_form.html', {
        'shas': selected, 'mode': mode, 'existing': existing,
    })


@app.post('/photos/tags', response_class=HTMLResponse)
def edit_photo_hashtags(sha: list[str] = Form(default=[]), hashtags: str = Form(''),
                        mode: str = Form('add')):
    tokens = re.split(r'[\s,]+', hashtags.strip())
    if mode not in ('add', 'remove') or not any(tokens) or any(
            not re.fullmatch(r'#?\w[\w-]*', token) or not token.lstrip('#').rstrip('-_')
            for token in tokens if token):
        return browse_albums.events(HTMLResponse(''),
            toast=browse_albums.toast('Enter hashtags separated by spaces or commas (for example #holiday #family).', 'error'))
    tags = db.extract_hashtags(' '.join('#' + token.lstrip('#') for token in tokens if token))
    with db.get_db() as conn:
        changed = db.edit_hashtags(conn, sha, tags, mode)
    return browse_albums.events(HTMLResponse(''), closeTagEditor=True, gridChanged=True,
        toast=browse_albums.toast(f"Hashtags {'added to' if mode == 'add' else 'removed from'} {changed} photo(s)"))


@app.post("/photos/delete", response_class=HTMLResponse)
def delete_photos(sha: list[str] = Form(default=[])):
    """Delete from the library: the row goes, the file moves to the trash folder, and the
    hash is remembered so the phone/camera sync does not bring it back."""
    lib = photo_library.current()
    lib.trash_dir.mkdir(exist_ok=True)
    deleted, deleted_shas, failed = [], [], 0
    conn = db.get_db()
    try:
        for digest in dict.fromkeys(sha):
            # One photo per transaction: if its file cannot be moved, its row stays.
            with library_lock(lib):
                names = db.delete_photos(conn, [digest])
                try:
                    for name in names:
                        os.replace(lib.path / Path(name).name, lib.trash_dir / Path(name).name)
                except FileNotFoundError:
                    pass  # file already missing; nothing to keep the row for
                except OSError:
                    conn.rollback()
                    failed += 1
                    continue
                conn.commit()
                deleted += names
                if names:
                    deleted_shas.append(digest)
        photo_catalogue.refresh(lib.id, conn, deleted_shas)
    finally:
        conn.close()
    filenames = deleted
    for digest in deleted_shas:
        if not re.fullmatch(r"[0-9a-fA-F]{1,64}", digest):
            continue   # the value goes into a file pattern: anything else could reach other folders
        for cached in lib.cache_dir.glob(f"{digest}_*.jpg"):
            cached.unlink(missing_ok=True)
    n = len(filenames)
    message = browse_albums.toast(f"Deleted {browse_albums.plural(n, 'item')}")
    if failed:
        message = browse_albums.toast(f"Deleted {browse_albums.plural(n, 'item')}; {failed} could not be moved "
                                      "to the trash and were kept", "error")
    return browse_albums.events(
        HTMLResponse(""), toast=message,
        photosDeleted=True, gridChanged=True, collectionsChanged=True, albumsChanged=True)


@app.get("/move", response_class=HTMLResponse)
def move_picker(request: Request, sha: list[str] = Query(default=[])):
    import photo_move
    current_lib = photo_library.current()
    with db.get_db() as conn:
        selected = [d for d in dict.fromkeys(sha) if db.existing_file(conn, d)]
    all_libs = photo_library.all_libraries(fresh=True)
    known_ids = {l.id for l in all_libs}
    targets = []
    for l in all_libs:
        if l.id == current_lib.id:
            continue
        if l.online:
            writable = not l.hold and os.access(l.path, os.W_OK)
            reason = ("read-only (held)" if l.hold else "read-only" if not os.access(l.path, os.W_OK) else "")
            targets.append({"id": l.id, "name": l.name, "where": "this machine",
                            "enabled": writable, "reason": reason, "queued": False})
        elif l.host:
            targets.append({"id": l.id, "name": l.name, "where": f"on {l.host} (transfers on next sync)",
                            "enabled": True, "reason": "", "queued": True})
        else:
            targets.append({"id": l.id, "name": l.name, "where": "not plugged in",
                            "enabled": False, "reason": "plug in disk to move", "queued": False})
    for r in photo_move.remote_server_libraries():
        if r["id"] not in known_ids:
            targets.append({"id": r["id"], "name": r["name"], "where": "on server (over network)",
                            "enabled": not r.get("read_only"), "reason": r.get("read_only") or "", "queued": False})
    return templates.TemplateResponse(request, "_move_picker.html", {
        "shas": selected, "targets": targets,
    })


@app.post("/photos/move", response_class=HTMLResponse)
def move_photos(target: str = Form(...), sha: list[str] = Form(default=[])):
    import photo_move
    src = photo_library.current()
    photo_library.require_writable(src)
    selected = list(dict.fromkeys(sha))
    if not selected:
        return browse_albums.events(HTMLResponse(""), toast=browse_albums.toast("Select at least one item.", "error"))
    all_libs = photo_library.all_libraries(fresh=True)
    target_lib = next((l for l in all_libs if l.id == target.lower()), None)
    if target_lib is not None:
        if target_lib.online:
            try:
                res = photo_move.move_local(src, target_lib, selected)
            except (photo_move.MoveError, photo_library.LibraryError) as exc:
                raise HTTPException(409, str(exc))
            msg = f"Moved {browse_albums.plural(res['moved'], 'item')} to “{target_lib.name}”"
            level = "success"
            if res["errors"]:
                msg += "; " + "; ".join(res["errors"][:3])
                level = "error"
            return browse_albums.events(
                HTMLResponse(""), toast=browse_albums.toast(msg, level), closePicker=True,
                photosDeleted=res["moved"] > 0, gridChanged=res["moved"] > 0,
                collectionsChanged=res["moved"] > 0, albumsChanged=res["moved"] > 0,
            )
        if target_lib.host:
            with db.get_db() as conn:
                items = []
                for d in selected:
                    row = conn.execute("SELECT original_filename FROM photos WHERE sha256 = ?", (d,)).fetchone()
                    if row:
                        items.append((d, row["original_filename"]))
            queued = photo_catalogue.queue_moves(src.id, target_lib.id, items)
            return browse_albums.events(
                HTMLResponse(""), closePicker=True,
                toast=browse_albums.toast(
                    f"Queued {browse_albums.plural(queued, 'item')} to move to “{target_lib.name}” "
                    f"(transfers when {target_lib.host} next syncs).", "info"),
            )
        raise HTTPException(409, f"library '{target_lib.name}' is not plugged in")

    server_libs = {r["id"]: r for r in photo_move.remote_server_libraries()}
    if target.lower() in server_libs:
        try:
            res = photo_move.push_to_server(src, target.lower(), selected)
        except (photo_move.MoveError, photo_library.LibraryError) as exc:
            raise HTTPException(409, str(exc))
        name = server_libs[target.lower()]["name"]
        msg = f"Moved {browse_albums.plural(res['moved'], 'item')} to “{name}”"
        level = "success"
        if res["errors"]:
            msg += "; " + "; ".join(res["errors"][:3])
            level = "error"
        return browse_albums.events(
            HTMLResponse(""), toast=browse_albums.toast(msg, level), closePicker=True,
            photosDeleted=res["moved"] > 0, gridChanged=res["moved"] > 0,
            collectionsChanged=res["moved"] > 0, albumsChanged=res["moved"] > 0,
        )
    raise HTTPException(404, f"unknown library '{target}'")


@app.get("/tags", response_class=HTMLResponse)
def tags_page(request: Request, sort: str = "count", pills: bool = False):
    with db.get_db() as conn:
        tags = db.all_tags(conn, sort)
    return templates.TemplateResponse(request, "tags.html", {
        "tags": tags, "sort": sort, "pills": pills, "page": "tags", "q": "",
    })


@app.get("/sources", response_class=HTMLResponse)
def sources_page(request: Request):
    with db.get_db() as conn:
        sources = db.all_cameras(conn)
    return templates.TemplateResponse(request, "sources.html", {
        "sources": sources, "page": "sources", "q": "",
    })


# ---- media ------------------------------------------------------------------

@app.get("/thumb/{digest}")
def thumb(digest: str):
    row = get_row(digest)
    out = rendered_jpeg(row, THUMB_SIZE)
    if out is None:
        return placeholder("video" if media_type(row) == "video" else "no preview")
    return FileResponse(out, media_type="image/jpeg", headers=IMMUTABLE)


@app.get("/view/{digest}")
def view(digest: str):
    row = get_row(digest)
    out = rendered_jpeg(row, VIEW_SIZE)
    if out is None:
        return placeholder("no preview")
    return FileResponse(out, media_type="image/jpeg", headers=IMMUTABLE)


@app.get("/file/{digest}")
def original(digest: str, download: bool = False):
    row = get_row(digest)
    path = file_path(row)
    ctype = CONTENT_TYPES.get(path.suffix.lower()) or mimetypes.guess_type(path.name)[0] or "application/octet-stream"
    if download:
        return FileResponse(path, media_type=ctype, filename=row["original_filename"], headers=IMMUTABLE)
    return FileResponse(path, media_type=ctype, headers=IMMUTABLE)  # supports Range for video seeking


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Photo library browser for every plugged-in library (see libraries.sh).")
    parser.add_argument("--host", default="0.0.0.0", help="Bind address (default: 0.0.0.0)")
    parser.add_argument("--port", type=int, default=8095, help="Port number (default: 8095)")
    args = parser.parse_args()
    import uvicorn
    uvicorn.run(LibraryRouter(app), host=args.host, port=args.port)


if __name__ == "__main__":
    main()
