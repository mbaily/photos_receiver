import hashlib
import io
import json
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

import browse_photos
import ingest_camera
import photo_catalogue
import photo_db as db
import photo_library
import photo_move
import photo_store
import receiver
import send_camera as sc
from tests.isolation import isolate


def _store(lib, data: bytes, name: str = "IMG_0001.JPG", note1: str = "", sidecar: str | None = None):
    digest = hashlib.sha256(data).hexdigest()
    tmp = lib.path / f".tmp_{name}"
    tmp.write_bytes(data)
    tok = photo_library.use(lib)
    try:
        res = photo_store.store_file(lib, tmp, digest, len(data), name, name, {"source": "test"}, sidecar_xml=sidecar)
        if note1:
            with db.get_db() as conn:
                db.set_notes(conn, digest, note1, "second note")
                photo_catalogue.refresh(lib.id, conn, [digest])
        return res, digest
    finally:
        photo_library._current.reset(tok)


class _TestClientResponse:
    def __init__(self, resp):
        self._resp = resp
        self._buf = io.BytesIO(resp.content)

    def read(self, n=-1):
        return self._buf.read(n)

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


class _ClientLink:
    """Adapter exposing TestClient(receiver.app) as an ingest_camera.Link for move tests."""

    def __init__(self, client: TestClient):
        self.client = client
        self.server = type("Srv", (), {"base": "http://testserver", "auth": {}, "ctx": None})()

    def ask(self, method: str, path: str, body=None, timeout: int = 60):
        resp = self.client.request(method, path, json=body)
        return resp.json() if resp.status_code < 400 else None

    def call(self, method: str, path: str, body=None, timeout: int = 60):
        resp = self.client.request(method, path, json=body)
        resp.raise_for_status()
        return resp.json()


class LibraryMoveTests(unittest.TestCase):
    def setUp(self):
        self.base = isolate(self)
        self.roots = self.base / "roots"
        self.lib1 = photo_library.create(self.roots / "One", "One")
        self.lib2 = photo_library.create(self.roots / "Two", "Two")
        self.browser = TestClient(browse_photos.LibraryRouter(browse_photos.app))
        self.receiver = TestClient(receiver.app)

    def test_local_move_preserves_notes_tags_sidecar_and_prevents_reimport(self):
        _, digest = _store(self.lib1, b"fake-video-bytes", "C0001.MP4",
                           note1="Great clip #holiday", sidecar="<NonRealTimeMeta/>")

        res = self.browser.get(f"/lib/{self.lib1.id}/move", params=[("sha", digest)])
        self.assertEqual(res.status_code, 200)
        self.assertIn(self.lib2.name, res.text)

        res = self.browser.post(
            f"/lib/{self.lib1.id}/photos/move",
            data={"target": self.lib2.id, "sha": [digest]},
        )
        self.assertEqual(res.status_code, 200)
        self.assertIn("Moved 1 item", res.headers.get("HX-Trigger", ""))

        # Gone from lib1, recorded in deleted_photos without leaving a .trash duplicate
        tok1 = photo_library.use(self.lib1)
        try:
            with db.get_db() as conn:
                self.assertIsNone(conn.execute("SELECT 1 FROM photos WHERE sha256=?", (digest,)).fetchone())
                self.assertTrue(db.deleted_hashes(conn, [digest]))
            self.assertFalse((self.lib1.trash_dir / "C0001.MP4").exists())
        finally:
            photo_library._current.reset(tok1)

        # Present in lib2 with notes, hashtags, and sidecar
        tok2 = photo_library.use(self.lib2)
        try:
            with db.get_db() as conn:
                row = conn.execute("SELECT * FROM photos WHERE sha256=?", (digest,)).fetchone()
                self.assertIsNotNone(row)
                self.assertEqual(row["note1"], "Great clip #holiday")
                self.assertEqual(row["note2"], "second note")
                self.assertEqual(row["sidecar_xml"], "<NonRealTimeMeta/>")
                self.assertEqual(db.tags_for(conn, [digest])[digest], ["holiday"])
                self.assertTrue(db.original_present(row))
        finally:
            photo_library._current.reset(tok2)

    def test_local_move_refuses_held_target_and_deleted_in_target(self):
        _, digest = _store(self.lib1, b"photo-bytes", "IMG_1.JPG")
        photo_library.set_hold(self.lib2.id, "suspected older copy", 3)
        res = self.browser.post(
            f"/lib/{self.lib1.id}/photos/move",
            data={"target": self.lib2.id, "sha": [digest]},
        )
        self.assertEqual(res.status_code, 409)
        photo_library.clear_hold(self.lib2.id)

        # Mark digest as deleted in lib2: move must refuse and keep it in lib1
        tok2 = photo_library.use(self.lib2)
        try:
            with db.get_db() as conn:
                conn.execute(
                    "INSERT INTO deleted_photos (sha256, saved_filename, original_filename, deleted_at) "
                    "VALUES (?, 'x.jpg', 'x.jpg', '2026-11-14T00:00:00Z')",
                    (digest,),
                )
        finally:
            photo_library._current.reset(tok2)
        out = photo_move.move_local(self.lib1, self.lib2, [digest])
        self.assertEqual(out["moved"], 0)
        self.assertTrue(out["errors"])

    def test_server_to_laptop_queued_move_and_pull(self):
        _, digest = _store(self.lib1, b"server-photo-bytes", "SRV_1.JPG", note1="From server #trip")
        # Register a remote laptop library
        photo_library.register_remote("aabbccdd", "LaptopLib", {
            "host": "laptop-host", "path": "/tmp/lap", "items": 0, "deleted": 0, "synced_at": "2026-11-14T00:00:00Z",
        })
        photo_library._scan.cache = None

        res = self.browser.post(
            f"/lib/{self.lib1.id}/photos/move",
            data={"target": "aabbccdd", "sha": [digest]},
        )
        self.assertEqual(res.status_code, 200)
        self.assertIn("Queued 1 item", res.headers.get("HX-Trigger", ""))

        # Libraries page shows queued move
        libs_page = self.browser.get("/libraries")
        self.assertIn("1 item queued to move", libs_page.text)

        # Server still holds the photo until laptop pulls it
        moves = self.receiver.get("/remote/moves").json()["moves"]
        self.assertEqual(len(moves), 1)
        self.assertEqual(moves[0]["sha256"], digest)
        self.assertEqual(moves[0]["note1"], "From server #trip")

        # Simulate laptop pulling the move into a local library whose id is aabbccdd
        lap_dir = self.roots / "LaptopPull"
        lap_lib = photo_library.create(lap_dir, "LaptopPull")
        # Re-key lap_lib to aabbccdd in marker & registry for the pull test
        marker = photo_library.read_marker(lap_dir)
        marker["id"] = "aabbccdd"
        photo_library.save_marker(lap_dir, marker)
        photo_library._update(lambda d: d["libraries"].__setitem__("aabbccdd", d["libraries"].pop(lap_lib.id)))

        link = _ClientLink(self.receiver)
        with patch("photo_move.urllib.request.urlopen",
                   side_effect=lambda req, **kw: _TestClientResponse(self.receiver.get(req.full_url.replace("http://testserver", "")))):
            completed = photo_move.pull_pending_moves(link, quiet=True)
        self.assertEqual(completed, 1)

        # Pending move is cleared and source lib1 no longer holds it
        self.assertEqual(self.receiver.get("/remote/moves").json()["moves"], [])
        tok1 = photo_library.use(self.lib1)
        try:
            with db.get_db() as conn:
                self.assertIsNone(conn.execute("SELECT 1 FROM photos WHERE sha256=?", (digest,)).fetchone())
        finally:
            photo_library._current.reset(tok1)

        # Laptop library now has the photo + note + hashtag
        pulled = photo_library.find("aabbccdd", photo_library.all_libraries(fresh=True))
        tok_lap = photo_library.use(pulled)
        try:
            with db.get_db() as conn:
                row = conn.execute("SELECT * FROM photos WHERE sha256=?", (digest,)).fetchone()
                self.assertIsNotNone(row)
                self.assertEqual(row["note1"], "From server #trip")
                self.assertEqual(db.tags_for(conn, [digest])[digest], ["trip"])
        finally:
            photo_library._current.reset(tok_lap)

    def test_orphaned_pending_move_is_cleaned_up(self):
        _, digest = _store(self.lib1, b"orphan-move-bytes", "ORPHAN.JPG")
        photo_library.register_remote("11223344", "LaptopOrphan", {
            "host": "laptop-host", "path": "/tmp/lap2", "items": 0, "deleted": 0, "synced_at": "2026-11-14T00:00:00Z",
        })
        photo_library._scan.cache = None
        photo_catalogue.queue_moves(self.lib1.id, "11223344", [(digest, "ORPHAN.JPG")])
        # Delete the photo from lib1 before the laptop syncs
        photo_move.remove_from_source(self.lib1, digest)
        self.assertEqual(self.receiver.get("/remote/moves").json()["moves"], [])
        self.assertEqual(photo_catalogue.pending_moves(), [])

    def test_failed_move_error_shown_on_libraries_page(self):
        _, digest = _store(self.lib1, b"fail-move-bytes", "FAIL.JPG")
        photo_library.register_remote("55667788", "LaptopFail", {
            "host": "laptop-host", "path": "/tmp/lap3", "items": 0, "deleted": 0, "synced_at": "2026-11-14T00:00:00Z",
        })
        photo_library._scan.cache = None
        photo_catalogue.queue_moves(self.lib1.id, "55667788", [(digest, "FAIL.JPG")])
        move_id = photo_catalogue.pending_moves()[0]["id"]
        self.receiver.post(f"/remote/moves/{move_id}/fail", json={"error": "already deleted from target"})
        libs_page = self.browser.get("/libraries")
        self.assertIn("already deleted from target", libs_page.text)

    def test_push_move_upload_refuses_previously_deleted_hash_on_server(self):
        raw = b"server-deleted-bytes"
        _, digest = _store(self.lib2, raw, "DELSRV.JPG")
        photo_move.remove_from_source(self.lib2, digest)
        resp = self.receiver.put(
            "/DELSRV.JPG",
            content=raw,
            headers={
                "X-Photo-Library": self.lib2.id,
                "X-Photo-Source": "library_move",
                "X-Photo-SHA256": digest,
                "X-Original-Filename": "DELSRV.JPG",
                "X-Allow-Duplicates": "1",
            },
        )
        self.assertEqual(resp.status_code, 409)
        self.assertIn("previously deleted", resp.text)

