import hashlib
import os
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

import browse_photos
import photo_collections as coll
import photo_db as db
import photo_library
import photo_store
import receiver
from tests.isolation import isolate


def _store_bytes(lib, data: bytes, name: str = "IMG_0001.JPG"):
    digest = hashlib.sha256(data).hexdigest()
    tmp = lib.path / f".tmp_{name}"
    tmp.write_bytes(data)
    tok = photo_library.use(lib)
    try:
        return photo_store.store_file(lib, tmp, digest, len(data), name, name), digest
    finally:
        photo_library._current.reset(tok)


class ConsistencyFixesTests(unittest.TestCase):
    def setUp(self):
        self.base = isolate(self)
        self.roots = self.base / "roots"
        self.lib1 = photo_library.create(self.roots / "One", "One")
        self.lib2 = photo_library.create(self.roots / "Two", "Two")
        self.browser = TestClient(browse_photos.LibraryRouter(browse_photos.app))
        self.receiver = TestClient(receiver.app)

    def test_uppercase_library_id_in_browser_url(self):
        res = self.browser.get(f"/lib/{self.lib2.id.upper()}/", follow_redirects=False)
        self.assertEqual(res.status_code, 200)
        self.assertIn(self.lib2.name, res.text)

    def test_new_album_with_photos_rolls_back_on_error(self):
        with patch("browse_albums.coll.add_photos", side_effect=coll.CollectionError("boom")):
            res = self.browser.post(
                f"/lib/{self.lib1.id}/picker/new-album",
                data={"name": "ShouldNotExist", "parent": ""},
            )
        self.assertEqual(res.status_code, 200)
        self.assertIn("boom", res.headers.get("HX-Trigger", ""))
        tok = photo_library.use(self.lib1)
        try:
            with db.get_db() as conn:
                rows = conn.execute("SELECT name FROM collections WHERE name = 'ShouldNotExist'").fetchall()
                self.assertEqual(rows, [])
        finally:
            photo_library._current.reset(tok)

    def test_wanted_handles_over_1000_hashes(self):
        _, digest = _store_bytes(self.lib1, b"photo-data", "a.jpg")
        hashes = [f"{i:064x}" for i in range(1200)] + [digest]
        res = self.receiver.post("/wanted", json={"hashes": hashes}, headers={"X-Photo-Library": self.lib1.id})
        self.assertEqual(res.status_code, 200)
        wanted = res.json()["wanted"]
        self.assertEqual(len(wanted), 1200)
        self.assertNotIn(digest, wanted)

    def test_upload_to_readonly_library_returns_503(self):
        with patch("receiver.os.access", return_value=False):
            res = self.receiver.put(
                "/test.jpg",
                content=b"abc",
                headers={"X-Photo-Library": self.lib1.id},
            )
        self.assertEqual(res.status_code, 503)
        self.assertIn("read-only", res.json()["detail"])

    def test_rename_rejects_empty_and_duplicate_names(self):
        with patch.object(sys, "argv", ["photo_library.py", "rename", self.lib1.id, "   "]):
            rc = photo_library.main()
        self.assertEqual(rc, 1)

        with patch.object(sys, "argv", ["photo_library.py", "rename", self.lib1.id, "two"]):
            rc = photo_library.main()
        self.assertEqual(rc, 1)
        self.assertEqual(photo_library.find(self.lib1.id, photo_library.all_libraries(fresh=True)).name, "One")

    def test_attach_sidecar_refuses_held_library(self):
        _, digest = _store_bytes(self.lib1, b"video-data", "C0001.MP4")
        photo_library.set_hold(self.lib1.id, "suspected older copy", 5)
        tok = photo_library.use(self.lib1)
        try:
            with self.assertRaises(photo_library.LibraryError):
                photo_store.attach_sidecar(self.lib1, digest, "<NonRealTimeMeta/>")
        finally:
            photo_library._current.reset(tok)

    def test_store_file_repair_clears_stale_preview_cache(self):
        res, digest = _store_bytes(self.lib1, b"original-bytes", "IMG_0002.JPG")
        self.assertEqual(res["status"], "saved")
        # Simulate missing original + stale cached thumbnail
        (self.lib1.path / res["file"]).unlink()
        self.lib1.cache_dir.mkdir(exist_ok=True)
        stale = self.lib1.cache_dir / f"{digest}_480.jpg"
        stale.write_bytes(b"stale-thumb")

        res2, _ = _store_bytes(self.lib1, b"original-bytes", "IMG_0002.JPG")
        self.assertEqual(res2["status"], "repaired")
        self.assertFalse(stale.exists())
