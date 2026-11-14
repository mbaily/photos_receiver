import hashlib
import os
import tempfile
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

import browse_photos
import photo_db as db
import photo_library
import photo_meta
import photo_store


class SourcesTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.old_env = {k: os.environ.get(k) for k in ("PHOTO_REGISTRY", "PHOTO_CATALOGUE_DB")}
        os.environ["PHOTO_REGISTRY"] = str(self.root / "libraries.json")
        os.environ["PHOTO_CATALOGUE_DB"] = str(self.root / "catalogue.sqlite3")
        photo_library.REGISTRY = Path(os.environ["PHOTO_REGISTRY"])
        photo_library._scan.cache = None

        self.lib = photo_library.create(self.root / "Lib", "SourcesTest")
        self.client = TestClient(browse_photos.LibraryRouter(browse_photos.app))

    def tearDown(self):
        for k, v in self.old_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        photo_library.REGISTRY = Path(os.environ.get("PHOTO_REGISTRY") or Path.home() / ".config" / "photos" / "libraries.json")
        photo_library._scan.cache = None

    def _add_item(self, content: bytes, filename: str, camera: str | None):
        digest = hashlib.sha256(content).hexdigest()
        p = self.lib.path / f".tmp_{filename}"
        p.write_bytes(content)
        tok = photo_library.use(self.lib)
        try:
            photo_store.store_file(self.lib, p, digest, len(content), filename, filename, {"source": "test", "ingested_on": "laptop-pc"})
            with db.get_db() as conn:
                conn.execute("UPDATE photos SET camera = ? WHERE sha256 = ?", (camera, digest))
        finally:
            photo_library._current.reset(tok)
        return digest

    def test_summarize_identifies_camera_and_smartphone_names(self):
        s1 = photo_meta.summarize({"Make": "Apple", "Model": "iPhone 15 Pro"}, Path("a.heic"))
        self.assertEqual(s1["camera"], "Apple iPhone 15 Pro")

        s2 = photo_meta.summarize({"Make": "SONY", "Model": "SONY ILCE-7M4"}, Path("b.arw"))
        self.assertEqual(s2["camera"], "SONY ILCE-7M4")

        s3 = photo_meta.summarize({"AndroidManufacturer": "Google", "AndroidModel": "Pixel 8"}, Path("c.mp4"))
        self.assertEqual(s3["camera"], "Google Pixel 8")

        s4 = photo_meta.summarize({}, Path("d.jpg"))
        self.assertIsNone(s4["camera"])

    def test_sources_page_and_camera_filtering(self):
        d_iphone = self._add_item(b"iphone-photo-1", "IMG_0001.JPG", "Apple iPhone 15 Pro")
        d_sony = self._add_item(b"sony-photo-1", "DSC00001.JPG", "SONY ILCE-7M4")
        d_unknown = self._add_item(b"unknown-photo-1", "SCREENSHOT.JPG", None)

        # /sources lists both devices + Unknown device
        res = self.client.get(f"/lib/{self.lib.id}/sources")
        self.assertEqual(res.status_code, 200)
        self.assertIn("Apple iPhone 15 Pro", res.text)
        self.assertIn("SONY ILCE-7M4", res.text)
        self.assertIn("Unknown device", res.text)

        # Filtering by Apple iPhone 15 Pro shows only its photo
        grid = self.client.get(f"/lib/{self.lib.id}/grid", params={"camera": "Apple iPhone 15 Pro"})
        self.assertEqual(grid.status_code, 200)
        self.assertIn(d_iphone, grid.text)
        self.assertNotIn(d_sony, grid.text)
        self.assertNotIn(d_unknown, grid.text)

        # Filtering by __unknown__ shows only the item without camera metadata
        grid_u = self.client.get(f"/lib/{self.lib.id}/grid", params={"camera": "__unknown__"})
        self.assertEqual(grid_u.status_code, 200)
        self.assertIn(d_unknown, grid_u.text)
        self.assertNotIn(d_iphone, grid_u.text)

        # Detail view includes clickable link to filter by camera
        detail = self.client.get(f"/lib/{self.lib.id}/photo/{d_iphone}", params={"camera": "Apple iPhone 15 Pro"})
        self.assertEqual(detail.status_code, 200)
        self.assertIn("?camera=Apple%20iPhone%2015%20Pro", detail.text)

