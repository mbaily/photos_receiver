"""Move photos and videos between libraries (locally on disk, or across machines via HTTPS)."""

import hashlib
import json
import os
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

import photo_catalogue
import photo_db as db
import photo_library
import photo_store
import photo_sync
import send_camera as sc


class MoveError(ValueError):
    """User-facing failure while moving items between libraries."""


def _token_and_server():
    cfg = photo_sync.config()
    if not cfg.get("enabled") or not cfg.get("server"):
        return None
    token = os.environ.get("PHOTO_TOKEN", "").strip()
    if not token:
        try:
            token = sc.TOKEN_FILE.read_text().strip()
        except OSError:
            token = ""
    return sc.Server(cfg["server"], cfg.get("cacert"), token=token)


def remote_server_libraries() -> list[dict]:
    """Online libraries on the paired server (for moving from a laptop to the server)."""
    server = _token_and_server()
    if server is None:
        return []
    req = urllib.request.Request(f"{server.base}/libraries", headers=server.auth)
    try:
        with urllib.request.urlopen(req, context=server.ctx, timeout=4) as resp:
            libs = json.load(resp).get("libraries", [])
        return [l for l in libs if l.get("online") and not l.get("host")]
    except Exception:
        return []


def _photo_meta_payload(row) -> dict:
    return {
        "original_filename": row["original_filename"],
        "uploaded_at": row["uploaded_at"],
        "metadata": row["metadata"] or "{}",
        "note1": row["note1"] or "",
        "note2": row["note2"] or "",
        "notes_updated_at": row["notes_updated_at"],
        "hashtags": row["hashtags"] or "",
    }


def apply_moved_metadata(conn, digest: str, payload: dict) -> None:
    """Apply notes, hashtags, and original arrival metadata to an item in the target library."""
    row = conn.execute("SELECT note1, note2, hashtags, uploaded_at, metadata FROM photos WHERE sha256 = ?",
                       (digest,)).fetchone()
    if not row:
        return
    note1 = row["note1"] or payload.get("note1") or ""
    note2 = row["note2"] or payload.get("note2") or ""
    manual_in = db.extract_hashtags(payload.get("hashtags") or "")
    manual_existing = db.extract_hashtags(row["hashtags"] or "")
    hashtags = " ".join("#" + t for t in sorted(set(manual_existing) | set(manual_in)))
    notes_updated = payload.get("notes_updated_at") if (note1 or note2) else None
    updates = {"note1": note1, "note2": note2, "hashtags": hashtags}
    if notes_updated and not row["note1"] and not row["note2"]:
        updates["notes_updated_at"] = notes_updated
    if payload.get("uploaded_at"):
        updates["uploaded_at"] = payload["uploaded_at"]
    assignments = ", ".join(f"{k} = ?" for k in updates)
    conn.execute(f"UPDATE photos SET {assignments} WHERE sha256 = ?", [*updates.values(), digest])
    tags = db.extract_hashtags(note1, note2, hashtags)
    conn.execute("DELETE FROM photo_tags WHERE sha256 = ?", (digest,))
    conn.executemany("INSERT INTO photo_tags (sha256, tag) VALUES (?, ?)", [(digest, t) for t in tags])


def remove_from_source(lib: photo_library.Library, digest: str) -> bool:
    """Remove `digest` from `lib` after it has been verified in the target library.

    Records the hash in `deleted_photos` (without keeping a duplicate file in `.trash`)
    so camera/phone syncs do not re-import it into `lib`.
    """
    token = photo_library.use(lib)
    try:
        with photo_store.library_lock(lib):
            with db.get_db() as conn:
                names = db.delete_photos(conn, [digest])
                if not names:
                    return False
                for name in names:
                    (lib.path / Path(name).name).unlink(missing_ok=True)
                conn.commit()
                photo_catalogue.refresh(lib.id, conn, [digest])
        for cached in lib.cache_dir.glob(f"{digest}_*.jpg"):
            cached.unlink(missing_ok=True)
        return True
    finally:
        photo_library._current.reset(token)


def move_local(source_lib: photo_library.Library, target_lib: photo_library.Library,
               digests: list[str]) -> dict:
    """Move `digests` from `source_lib` to `target_lib` on the same machine."""
    if source_lib.id == target_lib.id:
        raise MoveError("Choose a different library.")
    photo_library.require_writable(source_lib)
    photo_library.require_writable(target_lib)

    moved, errors = 0, []
    for digest in dict.fromkeys(digests):
        tok_src = photo_library.use(source_lib)
        try:
            with db.get_db() as conn:
                row = conn.execute("SELECT * FROM photos WHERE sha256 = ?", (digest,)).fetchone()
            if not row:
                errors.append(f"{digest[:12]}: not found in {source_lib.name}")
                continue
            src_path = source_lib.path / Path(row["saved_filename"]).name
            if not src_path.is_file():
                errors.append(f"{row['original_filename']}: original file is missing")
                continue
            payload = _photo_meta_payload(row)
            sidecar_xml = row["sidecar_xml"]
            meta = json.loads(row["metadata"] or "{}")
            saved_name = Path(row["saved_filename"]).name
            orig_name = row["original_filename"]
            size_bytes = row["size_bytes"]
        finally:
            photo_library._current.reset(tok_src)

        tok_dst = photo_library.use(target_lib)
        try:
            with db.get_db() as conn:
                if db.deleted_hashes(conn, [digest]):
                    errors.append(f"{orig_name}: was previously deleted from {target_lib.name}")
                    continue
                existing = db.existing_file(conn, digest)
            if existing:
                with db.get_db() as conn:
                    apply_moved_metadata(conn, digest, payload)
                    photo_catalogue.refresh(target_lib.id, conn, [digest])
            else:
                tmp = photo_store.copy_verified(src_path, target_lib.path, digest)
                if tmp is None:
                    errors.append(f"{orig_name}: copy checksum mismatch")
                    continue
                try:
                    res = photo_store.store_file(
                        target_lib, tmp, digest, size_bytes, saved_name, orig_name, meta,
                        allow_duplicates=True, sidecar_xml=sidecar_xml,
                    )
                except Exception:
                    tmp.unlink(missing_ok=True)
                    raise
                if res["status"] not in ("saved", "repaired", "duplicate_skipped"):
                    errors.append(f"{orig_name}: refused by {target_lib.name} ({res['status']})")
                    continue
                with db.get_db() as conn:
                    apply_moved_metadata(conn, digest, payload)
                    photo_catalogue.refresh(target_lib.id, conn, [digest])
        finally:
            photo_library._current.reset(tok_dst)

        if remove_from_source(source_lib, digest):
            moved += 1
        else:
            errors.append(f"{orig_name}: copied to {target_lib.name}, but could not remove from {source_lib.name}")

    return {"moved": moved, "errors": errors}


def push_to_server(source_lib: photo_library.Library, target_id: str, digests: list[str],
                   server: sc.Server | None = None) -> dict:
    """Push `digests` from a local library to a library on the paired server, then remove locally."""
    photo_library.require_writable(source_lib)
    server = server or _token_and_server()
    if server is None:
        raise MoveError("Server sync is not configured on this machine.")
    server.use_library(target_id)
    server.allow_duplicates = True

    moved, errors = 0, []
    for digest in dict.fromkeys(digests):
        tok_src = photo_library.use(source_lib)
        try:
            with db.get_db() as conn:
                row = conn.execute("SELECT * FROM photos WHERE sha256 = ?", (digest,)).fetchone()
            if not row:
                errors.append(f"{digest[:12]}: not found in {source_lib.name}")
                continue
            src_path = source_lib.path / Path(row["saved_filename"]).name
            if not src_path.is_file():
                errors.append(f"{row['original_filename']}: original file is missing")
                continue
            payload = _photo_meta_payload(row)
            sidecar_xml = row["sidecar_xml"]
            orig_name = row["original_filename"]
            saved_name = Path(row["saved_filename"]).name
        finally:
            photo_library._current.reset(tok_src)

        try:
            size = src_path.stat().st_size
            quoted = urllib.parse.quote(saved_name)
            with open(src_path, "rb") as f:
                req = urllib.request.Request(
                    f"{server.base}/{quoted}", data=f, method="PUT",
                    headers={
                        "Content-Type": "application/octet-stream",
                        "Content-Length": str(size),
                        "X-Photo-Source": "library_move",
                        "X-Photo-SHA256": digest,
                        "X-Original-Filename": orig_name,
                        "X-Allow-Duplicates": "1",
                        **server.auth,
                    },
                )
                with urllib.request.urlopen(req, context=server.ctx, timeout=600) as resp:
                    res = json.load(resp)
            if res.get("sha256") != digest:
                errors.append(f"{orig_name}: server hash mismatch")
                continue
            if res.get("status") not in ("saved", "repaired", "duplicate_skipped"):
                errors.append(f"{orig_name}: server refused ({res.get('status')})")
                continue
            if sidecar_xml:
                sreq = urllib.request.Request(
                    f"{server.base}/sidecar/{digest}", data=sidecar_xml.encode("utf-8"), method="PUT",
                    headers={"Content-Type": "application/xml", **server.auth},
                )
                with urllib.request.urlopen(sreq, context=server.ctx, timeout=60) as resp:
                    resp.read()
            mreq = urllib.request.Request(
                f"{server.base}/meta/{digest}", data=json.dumps(payload).encode("utf-8"), method="PUT",
                headers={"Content-Type": "application/json", **server.auth},
            )
            with urllib.request.urlopen(mreq, context=server.ctx, timeout=60) as resp:
                resp.read()
        except urllib.error.HTTPError as e:
            errors.append(f"{orig_name}: {sc.refusal(e)}")
            continue
        except (urllib.error.URLError, OSError, ValueError) as e:
            errors.append(f"{orig_name}: {e}")
            continue

        if remove_from_source(source_lib, digest):
            moved += 1
        else:
            errors.append(f"{orig_name}: sent to server, but could not remove from {source_lib.name}")

    return {"moved": moved, "errors": errors}


def pull_pending_moves(link, quiet: bool = False) -> int:
    """Pull queued Server -> Laptop moves whose target library is plugged in here."""
    if link.server is None:
        return 0
    reply = link.ask("GET", "/remote/moves", timeout=30)
    if not reply or not reply.get("moves"):
        return 0
    libs = {lib.id: lib for lib in photo_library.online()}
    completed = 0
    for move in reply["moves"]:
        lib = libs.get(move["target_lib_id"])
        if not lib or photo_library.hold(lib.id) or not os.access(lib.path, os.W_OK):
            continue
        move_id = move["id"]
        digest = move["sha256"]
        tmp_path = None
        tok = photo_library.use(lib)
        try:
            with db.get_db() as conn:
                if db.deleted_hashes(conn, [digest]):
                    raise MoveError(f"already deleted from '{lib.name}'")
                existing = db.existing_file(conn, digest)
            if not existing:
                fd, tmp_str = tempfile.mkstemp(dir=lib.path, prefix=".part_")
                tmp_path = Path(tmp_str)
                hasher = hashlib.sha256()
                size_bytes = 0
                req = urllib.request.Request(
                    f"{link.server.base}/remote/moves/{move_id}/file",
                    headers=link.server.auth,
                )
                with urllib.request.urlopen(req, context=link.server.ctx, timeout=600) as resp, os.fdopen(fd, "wb") as out:
                    while chunk := resp.read(1024 * 1024):
                        hasher.update(chunk)
                        out.write(chunk)
                        size_bytes += len(chunk)
                    out.flush()
                    os.fsync(out.fileno())
                if hasher.hexdigest() != digest:
                    raise MoveError("downloaded file checksum mismatch")
                res = photo_store.store_file(
                    lib, tmp_path, digest, size_bytes,
                    move.get("saved_filename") or f"{digest}.jpg",
                    move.get("original_filename"),
                    json.loads(move.get("metadata") or "{}"),
                    allow_duplicates=True,
                    sidecar_xml=move.get("sidecar_xml"),
                )
                tmp_path = None
                if res["status"] not in ("saved", "repaired", "duplicate_skipped"):
                    raise MoveError(f"refused by '{lib.name}' ({res['status']})")
            with db.get_db() as conn:
                apply_moved_metadata(conn, digest, move)
                photo_catalogue.refresh(lib.id, conn, [digest])
            link.call("POST", f"/remote/moves/{move_id}/complete", {}, timeout=30)
            completed += 1
            if not quiet:
                print(f"  moved '{move.get('original_filename') or digest[:12]}' into '{lib.name}'")
        except Exception as exc:
            if tmp_path is not None:
                tmp_path.unlink(missing_ok=True)
            link.ask("POST", f"/remote/moves/{move_id}/fail", {"error": str(exc)}, timeout=15)
            if not quiet:
                print(f"  move {move_id[:8]} failed: {exc}")
        finally:
            photo_library._current.reset(tok)
    return completed

