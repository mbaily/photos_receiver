import hashlib
import io
import os
import tempfile
import unittest
from pathlib import Path

from fastapi.testclient import TestClient
from PIL import Image

import browse_photos
import photo_db as db
import photo_library


def _jpeg_bytes() -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (64, 48), (120, 180, 90)).save(buf, format="JPEG")
    return buf.getvalue()


class BrowserUploadTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.old_env = {k: os.environ.get(k) for k in ("PHOTO_REGISTRY", "PHOTO_CATALOGUE_DB")}
        os.environ["PHOTO_REGISTRY"] = str(self.root / "libraries.json")
        os.environ["PHOTO_CATALOGUE_DB"] = str(self.root / "catalogue.sqlite3")
        photo_library.REGISTRY = Path(os.environ["PHOTO_REGISTRY"])
        photo_library._scan.cache = None

        self.lib = photo_library.create(self.root / "Lib", "UploadTest")
        self.client = TestClient(browse_photos.LibraryRouter(browse_photos.app))

    def tearDown(self):
        for k, v in self.old_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        photo_library.REGISTRY = Path(os.environ.get("PHOTO_REGISTRY") or Path.home() / ".config" / "photos" / "libraries.json")
        photo_library._scan.cache = None

    def test_upload_form_and_multipart_photo_and_video(self):
        form = self.client.get(f"/lib/{self.lib.id}/upload")
        self.assertEqual(form.status_code, 200)
        self.assertIn("Upload photos or videos", form.text)

        jpg = _jpeg_bytes()
        mp4 = b"\x00\x00\x00\x18ftypmp42\x00\x00\x00\x00mp42isom" + b"\x00" * 32
        res = self.client.post(
            f"/lib/{self.lib.id}/photos/upload",
            files=[
                ("files", ("IMG_001.JPG", jpg, "image/jpeg")),
                ("files", ("CLIP_001.MP4", mp4, "video/mp4")),
            ],
        )
        self.assertEqual(res.status_code, 200)
        self.assertIn("Uploaded 2 items", res.headers.get("HX-Trigger", ""))

        tok = photo_library.use(self.lib)
        try:
            with db.get_db() as conn:
                rows = conn.execute("SELECT original_filename, media_type FROM photos ORDER BY original_filename").fetchall()
                self.assertEqual([(r[0], r[1]) for r in rows], [("CLIP_001.MP4", "video"), ("IMG_001.JPG", "image")])
        finally:
            photo_library._current.reset(tok)

        # Re-uploading the same JPEG reports already in library
        dup = self.client.post(
            f"/lib/{self.lib.id}/photos/upload",
            files=[("files", ("IMG_001.JPG", jpg, "image/jpeg"))],
        )
        self.assertEqual(dup.status_code, 200)
        self.assertIn("1 already in library", dup.headers.get("HX-Trigger", ""))

    def test_reupload_deleted_photo_reports_restore_from_trash(self):
        jpg = _jpeg_bytes()
        digest = hashlib.sha256(jpg).hexdigest()
        self.client.post(f"/lib/{self.lib.id}/photos/upload", files=[("files", ("DEL.JPG", jpg, "image/jpeg"))])
        self.client.post(f"/lib/{self.lib.id}/photos/delete", data={"sha": [digest]})

        res = self.client.post(f"/lib/{self.lib.id}/photos/upload", files=[("files", ("DEL.JPG", jpg, "image/jpeg"))])
        self.assertEqual(res.status_code, 200)
        self.assertIn("previously deleted (restore from Trash)", res.headers.get("HX-Trigger", ""))

    def test_upload_button_only_on_library_not_album_view(self):
        import photo_collections as coll
        tok = photo_library.use(self.lib)
        try:
            with db.get_db() as conn:
                album_id = coll.create(conn, "album", "Album One")
        finally:
            photo_library._current.reset(tok)

        lib_page = self.client.get(f"/lib/{self.lib.id}/")
        self.assertIn("Upload…", lib_page.text)
        album_page = self.client.get(f"/lib/{self.lib.id}/albums/{album_id}")
        self.assertEqual(album_page.status_code, 200)
        self.assertNotIn("Upload…", album_page.text)

