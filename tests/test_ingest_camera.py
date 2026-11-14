"""Laptop ingest and server sync, with two pretend machines in one process.

No camera, network or real library is touched: each "machine" is a registry file in a
temporary folder, library discovery is limited to remembered paths, and the laptop's
HTTP calls are handed to the receiver app directly.
"""
import contextlib
import hashlib
import io
import subprocess
import sys
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

import ingest_camera
import photo_catalogue
import photo_db
import photo_library
import receiver
import send_camera

REAL_RUN = subprocess.run
SIDECAR = '<?xml version="1.0"?><NonRealTimeMeta><CreationDate value="2026-05-04T10:11:12+10:00"/></NonRealTimeMeta>'


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


class TwoMachines(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name)
        self.patches = contextlib.ExitStack()
        self.addCleanup(self.temp.cleanup)
        self.addCleanup(self.patches.close)
        for target, name, value in (
            (photo_library, "_candidate_dirs", lambda known: [Path(p) for p in known]),
            (photo_library, "PHOTO_DIR", self.base / "no-default-library"),
            (send_camera, "CACHE_FILE", self.base / "hash-cache.json"),
            (send_camera, "TOKEN_FILE", self.base / "no-token"),
            (ingest_camera.Link, "call", self.call),
        ):
            self.patches.enter_context(patch.object(target, name, value))
        self.client = TestClient(receiver.app)
        self.network = True
        self.requests = []
        self.machine = None
        self.on("laptop")
        self.trip = photo_library.create(self.base / "laptop-disk" / "trip", "Trip")
        self.card = self.base / "card"
        self.files = {
            "DCIM/100MSDCF/DSC0001.JPG": b"photo one",
            "DCIM/100MSDCF/DSC0002.JPG": b"photo two",
            "PRIVATE/M4ROOT/CLIP/C0001.MP4": b"a video clip",
        }
        for rel, data in self.files.items():
            (self.card / rel).parent.mkdir(parents=True, exist_ok=True)
            (self.card / rel).write_bytes(data)
        (self.card / "PRIVATE/M4ROOT/CLIP/C0001M01.XML").write_text(SIDECAR)

    def on(self, machine: str) -> str:
        """Switch which machine's registry (and catalogue) the code sees."""
        previous, self.machine = self.machine, machine
        photo_library.REGISTRY = self.base / machine / "libraries.json"
        photo_library._scan.cache = None
        return previous

    def tearDown(self):
        photo_library.REGISTRY = self.base / "unused" / "libraries.json"
        photo_library._scan.cache = None

    def call(self, method, path, body=None, timeout=60):
        """Stands in for Link.call: the laptop's request, answered by the server's app."""
        if not self.network:
            raise urllib.error.URLError("network is unreachable")
        self.requests.append((method, path))
        back = self.on("server")
        current = photo_library._current.set(None)
        try:
            response = self.client.request(method, path, json=body)
        finally:
            photo_library._current.reset(current)
            self.on(back)
        if response.status_code >= 400:
            raise urllib.error.HTTPError(path, response.status_code, response.text, {}, io.BytesIO(response.content))
        return response.json()

    def ingest(self, *options, source=True):
        argv = ["ingest_camera.py", *([str(self.card)] if source else []), *options]
        out, err = io.StringIO(), io.StringIO()
        current = photo_library._current.set(None)
        try:
            with patch.object(sys, "argv", argv), contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                status = ingest_camera.main()
        finally:
            photo_library._current.reset(current)
        self.output = out.getvalue() + err.getvalue()
        return status

    def rows(self, lib=None, table="photos"):
        token = photo_library.use(lib or self.trip)
        try:
            with photo_db.get_db() as conn:
                return {r["sha256"]: dict(r) for r in conn.execute(f"SELECT * FROM {table}")}
        finally:
            photo_library._current.reset(token)

    def server_library(self, lib_id=None):
        back = self.on("server")
        try:
            return next((l for l in photo_library.all_libraries(fresh=True) if l.id == (lib_id or self.trip.id)), None)
        finally:
            self.on(back)

    def catalogue(self, table="remote_photos"):
        back = self.on("server")
        try:
            with photo_catalogue.connect() as conn:
                return {r["sha256"]: dict(r) for r in conn.execute(f"SELECT * FROM {table}")}
        finally:
            self.on(back)


class IngestTests(TwoMachines):
    def test_card_is_copied_named_and_recorded_like_an_upload(self):
        self.assertEqual(self.ingest(), 0, self.output)
        rows = self.rows()
        self.assertEqual(set(rows), {sha(d) for d in self.files.values()})
        clip = rows[sha(b"a video clip")]
        self.assertEqual(clip["saved_filename"], f"{sha(b'a video clip')}_2026-05-04_10-11-12.mp4")
        self.assertEqual(clip["original_filename"], "C0001.MP4")
        self.assertEqual(clip["sidecar_xml"], SIDECAR)
        self.assertEqual(clip["media_type"], "video")
        self.assertIn('"source_path": "PRIVATE/M4ROOT/CLIP/C0001.MP4"', clip["metadata"])
        for row in rows.values():
            self.assertEqual(hashlib.sha256((self.trip.path / row["saved_filename"]).read_bytes()).hexdigest(),
                             row["sha256"])
        self.assertEqual(list(self.trip.path.glob(".part_*")), [])
        # The card is never modified.
        self.assertEqual({p.relative_to(self.card).as_posix() for p in self.card.rglob("*") if p.is_file()},
                         {*self.files, "PRIVATE/M4ROOT/CLIP/C0001M01.XML"})

    def test_second_run_ingests_nothing(self):
        self.ingest()
        before = sorted(p.name for p in self.trip.path.iterdir())
        self.assertEqual(self.ingest(), 0)
        self.assertIn("0 ingested, 3 already in the library", self.output)
        self.assertEqual(sorted(p.name for p in self.trip.path.iterdir()), before)

    def test_deleted_photos_are_not_ingested_again(self):
        self.ingest()
        token = photo_library.use(self.trip)
        with photo_db.get_db() as conn:
            photo_db.delete_photos(conn, [sha(b"photo one")])
        photo_library._current.reset(token)
        self.assertEqual(self.ingest(), 0)
        self.assertNotIn(sha(b"photo one"), self.rows())
        self.assertIn("1 skipped (deleted from this library)", self.output)

    def test_dry_run_copies_nothing_and_does_not_sync(self):
        self.assertEqual(self.ingest("--dry-run"), 0)
        self.assertEqual(self.rows(), {})
        self.assertIn("would ingest DCIM/100MSDCF/DSC0001.JPG", self.output)
        self.assertFalse(any(path.startswith("/remote") for _, path in self.requests))

    def test_no_library_on_this_machine_is_explained(self):
        self.on("empty-laptop")
        self.assertEqual(self.ingest(), 1)
        self.assertIn("libraries.sh create", self.output)

    def test_not_enough_room_copies_nothing(self):
        usage = ingest_camera.shutil.disk_usage(self.base)
        with patch.object(ingest_camera.shutil, "disk_usage", return_value=usage._replace(free=10)):
            self.assertEqual(self.ingest(), 1)
        self.assertIn("not enough room", self.output)
        self.assertEqual(self.rows(), {})

    def test_file_in_another_local_library_is_skipped_unless_allowed(self):
        self.ingest("--no-sync")
        other = photo_library.create(self.base / "laptop-disk" / "other", "Other")
        self.assertEqual(self.ingest("--library", "Other", "--no-sync"), 0)
        self.assertEqual(self.rows(other), {})
        self.assertIn("already exist in another library (3 in 'Trip')", self.output)
        self.assertEqual(self.ingest("--library", "Other", "--no-sync", "--allow-duplicates"), 0)
        self.assertEqual(len(self.rows(other)), 3)

    def test_receiver_sweeps_stale_temporary_files_too(self):
        import os
        old = self.trip.path / ".part_dead_upload"
        old.write_bytes(b"x")
        os.utime(old, (1_000_000, 1_000_000))
        ingest_camera.sweep_parts.__globals__["_swept"].clear()
        token = photo_library._current.set(None)
        try:   # "Trip" is this machine's upload library
            self.assertEqual(self.client.put("/a.jpg", content=b"fresh").json()["status"], "saved")
        finally:
            photo_library._current.reset(token)
        self.assertFalse(old.exists())

    def test_uploads_cannot_take_the_names_of_the_librarys_own_files(self):
        token = photo_library._current.set(None)
        try:
            names = {}
            for n, name in enumerate(("inbox.db-wal", "INBOX.DB-shm", ".trash", ".part_x", "library.json",
                                      ".photos-receiver-library", "..")):
                reply = self.client.put("/" + name.replace(".", "%2E"), content=b"payload %d" % n)
                names[name] = reply.json().get("file") if reply.status_code == 200 else reply.status_code
        finally:
            photo_library._current.reset(token)
        for name, stored in names.items():
            self.assertTrue(stored == 404 or str(stored).startswith("upload"), (name, stored))
        self.assertEqual(photo_library.read_marker(self.trip.path)["id"], self.trip.id)
        self.assertEqual(len(self.rows()), sum(1 for s in names.values() if s != 404))

    def test_stale_temporary_files_are_swept(self):
        import os
        old, new = self.trip.path / ".part_old", self.trip.path / ".part_new"
        old.write_bytes(b"x"); new.write_bytes(b"x")
        os.utime(old, (1_000_000, 1_000_000))
        self.ingest("--no-sync")
        self.assertFalse(old.exists())
        self.assertTrue(new.exists())


class GPhotoIngestTests(TwoMachines):
    def command(self, command, *, stdout=None, **kwargs):
        if command[0] != "gphoto2":          # e.g. exiftool, reading the ingested file
            return REAL_RUN(command, stdout=stdout, **kwargs)
        self.assertFalse(any(flag.startswith("--delete") for flag in command))
        if "--auto-detect" in command:
            output = "Model  Port\n-----\nCanon PowerShot SX540 HS        usb:001,041\n"
        elif "--list-files" in command:
            output = "\n".join(f"FILENAME='/store/{p}' FILESIZE={len(d)} FILEMTIME=1700000000"
                               for p, d in self.files.items())
        else:
            remote = command[command.index("--folder") + 1] + "/" + command[command.index("--get-file") + 1]
            self.reads.append(remote)
            self.spooled.append(Path(stdout.name))
            stdout.write(self.files[remote.removeprefix("/store/")])
            return subprocess.CompletedProcess(command, 0, None, b"")
        return subprocess.CompletedProcess(command, 0, output.encode(), b"")

    def gphoto(self, *options):
        self.reads, self.spooled = [], []
        with patch.object(send_camera.shutil, "which", return_value="/usr/bin/gphoto2"), \
             patch.object(send_camera.subprocess, "run", side_effect=self.command):
            return self.ingest("--gphoto", "--no-sync", *options, source=False)

    def test_each_file_is_downloaded_once_onto_the_library_disk(self):
        self.files = {"DCIM/100CANON/IMG_0001.JPG": b"canon one", "DCIM/100CANON/IMG_0002.JPG": b"canon two"}
        self.assertEqual(self.gphoto(), 0, self.output)
        self.assertEqual(len(self.reads), 2)
        self.assertTrue(all(self.trip.path in p.parents for p in self.spooled))
        self.assertEqual(set(self.rows()), {sha(b"canon one"), sha(b"canon two")})
        self.assertEqual(list(self.trip.path.glob(".part_*")), [])
        self.assertEqual(self.gphoto(), 0)
        self.assertEqual(len(self.reads), 2)          # has to read again to recognise them...
        self.assertEqual(len(self.rows()), 2)         # ...but keeps nothing new
        self.assertEqual(list(self.trip.path.glob(".part_*")), [])

    def test_dry_run_keeps_nothing(self):
        self.files = {"DCIM/100CANON/IMG_0001.JPG": b"canon one"}
        self.assertEqual(self.gphoto("--dry-run"), 0)
        self.assertEqual(self.rows(), {})
        self.assertEqual([p.name for p in self.trip.path.iterdir() if p.suffix == ".jpg"], [])


class SyncTests(TwoMachines):
    def test_ingest_tells_the_server_and_a_second_sync_sends_nothing(self):
        self.assertEqual(self.ingest(), 0, self.output)
        lib = self.server_library()
        self.assertEqual((lib.name, lib.online, lib.path, lib.host), ("Trip", False, None, ingest_camera.HOST))
        self.assertEqual(lib.remote["items"], 3)
        self.assertEqual(lib.remote["path"], str(self.trip.path))
        self.assertEqual(lib.where, f"{ingest_camera.HOST}:{self.trip.path}")
        catalogue = self.catalogue()
        self.assertEqual(set(catalogue), set(self.rows()))
        self.assertEqual(catalogue[sha(b"a video clip")]["original_filename"], "C0001.MP4")

        self.requests.clear()
        self.assertEqual(self.ingest("sync", source=False), 0)
        self.assertEqual(self.requests, [("GET", "/remote/moves"), ("PUT", f"/remote/libraries/{self.trip.id}")])
        self.assertIn("server is up to date (3 items)", self.output)

    def test_offline_ingest_succeeds_and_syncs_later(self):
        self.network = False
        self.assertEqual(self.ingest(), 0, self.output)
        self.assertEqual(len(self.rows()), 3)
        self.assertIn("Offline", self.output)
        self.assertIsNone(self.server_library())
        self.network = True
        self.assertEqual(self.ingest("sync", source=False), 0)
        self.assertEqual(len(self.catalogue()), 3)

    def test_deletions_and_changes_reach_the_catalogue(self):
        self.ingest()
        token = photo_library.use(self.trip)
        with photo_db.get_db() as conn:
            photo_db.delete_photos(conn, [sha(b"photo one")])
            conn.execute("UPDATE photos SET camera = 'ZV-E10' WHERE sha256 = ?", (sha(b"photo two"),))
        photo_library._current.reset(token)
        self.assertEqual(self.ingest("sync", source=False), 0)
        catalogue = self.catalogue()
        self.assertNotIn(sha(b"photo one"), catalogue)
        self.assertEqual(catalogue[sha(b"photo two")]["camera"], "ZV-E10")
        self.assertEqual(set(self.catalogue("remote_deleted")), {sha(b"photo one")})
        self.assertEqual(self.server_library().remote["items"], 2)
        self.assertIn("(1 new or changed)", self.output)

    def test_lost_catalogue_is_rebuilt(self):
        self.ingest()
        self.on("server"); photo_catalogue.forget(self.trip.id); self.on("laptop")
        self.assertEqual(self.catalogue(), {})
        self.ingest("sync", source=False)
        self.assertEqual(len(self.catalogue()), 3)

    def test_rename_on_the_laptop_reaches_the_server(self):
        self.ingest()
        with patch.object(sys, "argv", ["libraries", "rename", "Trip", "Trip 2026"]), \
             contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(photo_library.main(), 0)
        self.ingest("sync", source=False)
        self.assertEqual(self.server_library().name, "Trip 2026")

    def test_remote_info_survives_the_disk_being_plugged_in_to_the_server(self):
        self.ingest()
        self.on("server")
        # Plugging the disk in = the server finds the marker at some path of its own.
        with patch.object(photo_library, "_candidate_dirs", lambda known: [self.trip.path]):
            lib = photo_library.find(self.trip.id, photo_library.all_libraries(fresh=True))
            self.assertTrue(lib.online)
            self.assertIsNone(lib.host)                       # it is here now
            self.assertEqual(lib.remote["items"], 3)          # ...and the laptop's report is kept
            self.assertEqual(lib.where, str(self.trip.path))
            # While it is here, a sync for the same id from elsewhere is a copied library.
            response = self.client.put(f"/remote/libraries/{self.trip.id}", json={"name": "Trip"})
            self.assertEqual(response.status_code, 409)
        # Unplugged from the server: it is the server's own offline library now, not "on the
        # laptop", and what it held when it was plugged in still counts for duplicates.
        with patch.object(photo_library, "_candidate_dirs", lambda known: []):
            lib = photo_library.find(self.trip.id, photo_library.all_libraries(fresh=True))
            self.assertFalse(lib.online)
            self.assertIsNone(lib.host)
            self.assertEqual(lib.where, str(self.trip.path))
            self.assertEqual(self.client.post("/known", json={"hashes": [sha(b"photo one")]}).json()["known"],
                             {sha(b"photo one"): "Trip (not plugged in)"})
            # Back on the laptop, which syncs later than the server last saw the disk.
            photo_library.register_remote(self.trip.id, "Trip", {**lib.remote, "synced_at": "2099-01-01T00:00:00+00:00"})
            lib = photo_library.find(self.trip.id, photo_library.all_libraries(fresh=True))
            self.assertEqual(lib.host, ingest_camera.HOST)
        self.on("laptop")

    def test_name_clash_with_a_server_library_is_reported(self):
        self.on("server")
        photo_library.create(self.base / "server-disk" / "trip", "trip")
        self.on("laptop")
        self.assertEqual(self.ingest(), 0)
        self.assertIn("already has another library called 'trip'", self.output)

    def test_laptop_library_cannot_become_the_upload_library_and_forget_drops_its_catalogue(self):
        self.ingest()
        self.on("server")
        err = io.StringIO()
        with patch.object(sys, "argv", ["libraries", "inbox", "Trip"]), \
             contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(err):
            self.assertEqual(photo_library.main(), 1)
        self.assertIn("uploads cannot go to it", err.getvalue())
        with patch.object(sys, "argv", ["libraries", "forget", "Trip"]), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(photo_library.main(), 0)
        self.on("laptop")
        self.assertEqual(self.catalogue(), {})

    def test_sync_cannot_touch_local_fields_or_take_bad_ids(self):
        self.on("server")
        mine = photo_library.create(self.base / "server-disk" / "main", "Main")
        self.assertEqual(self.client.put(f"/remote/libraries/{mine.id}", json={"name": "Hijack"}).status_code, 409)
        self.assertEqual(self.client.put("/remote/libraries/NOT-AN-ID", json={"name": "x"}).status_code, 400)
        self.assertEqual(self.client.post("/remote/libraries/0123abcd/hashes", json={"photos": {}}).status_code, 404)
        lib = photo_library.find(mine.id, photo_library.all_libraries(fresh=True))
        self.assertEqual((lib.name, lib.inbox, lib.path), ("Main", True, mine.path))
        self.on("laptop")


class DuplicateTests(TwoMachines):
    def setUp(self):
        super().setUp()
        self.on("server")
        self.main = photo_library.create(self.base / "server-disk" / "main", "Main")
        self.on("laptop")

    def upload(self, data: bytes, name="x.jpg", headers=None):
        back = self.on("server")
        current = photo_library._current.set(None)
        try:
            return self.client.put(f"/{name}", content=data, headers=headers or {}).json()
        finally:
            photo_library._current.reset(current)
            self.on(back)

    def wanted(self, *hashes):
        back = self.on("server")
        try:
            return self.client.post("/wanted", json={"hashes": list(hashes)}).json()
        finally:
            self.on(back)

    def test_receiver_upload_still_works_and_shortcut_reply_is_unchanged(self):
        result = self.upload(b"phone photo", name=f"{sha(b'phone photo')}_2026-01-01_00-00-00")
        self.assertEqual(result["status"], "saved")
        self.assertEqual(result["file"], f"{sha(b'phone photo')}_2026-01-01_00-00-00")   # no known type, no extension
        self.assertEqual(self.upload(b"\xff\xd8\xff jpeg", name="noext")["file"], "noext.jpg")
        self.assertEqual(self.upload(b"phone photo")["status"], "duplicate_skipped")
        self.assertEqual(self.wanted(sha(b"phone photo"), sha(b"never seen")), {"wanted": [sha(b"never seen")]})

    def test_online_ingest_skips_what_a_server_library_has(self):
        self.upload(b"photo one")
        self.assertEqual(self.ingest(), 0, self.output)
        self.assertEqual(set(self.rows()), {sha(b"photo two"), sha(b"a video clip")})
        self.assertIn("1 in 'Main (server)'", self.output)
        self.assertEqual(self.ingest("--allow-duplicates"), 0)
        self.assertEqual(len(self.rows()), 3)

    def test_offline_duplicates_are_reported_at_sync(self):
        self.upload(b"photo one")
        self.network = False
        self.ingest()
        self.assertEqual(len(self.rows()), 3)
        self.network = True
        self.ingest("sync", source=False)
        self.assertIn("1 in 'Main' of them also exist in another library", self.output)

    def test_server_refuses_what_a_synced_laptop_library_has(self):
        self.ingest()
        digest = sha(b"photo two")
        self.assertEqual(self.wanted(digest), {"wanted": []})
        result = self.upload(b"photo two")
        self.assertEqual(result["status"], "elsewhere_skipped")
        self.assertEqual(result["library"], f"Trip (on {ingest_camera.HOST})")
        self.assertEqual(self.upload(b"photo two", headers={"X-Allow-Duplicates": "1"})["status"], "saved")

    def test_unplugged_server_library_still_counts_for_duplicates(self):
        self.on("server")
        archive = photo_library.create(self.base / "server-disk" / "archive", "Archive")
        self.on("laptop")
        headers = {"X-Photo-Library": archive.id}
        for data in (b"kept on the archive disk", b"deleted from the archive disk"):
            self.assertEqual(self.upload(data, headers=headers)["status"], "saved")
        self.on("server")
        token = photo_library.use(archive)
        with photo_db.get_db() as conn:      # as the browser's delete does
            photo_db.delete_photos(conn, [sha(b"deleted from the archive disk")])
            conn.commit()
            photo_catalogue.refresh(archive.id, conn, [sha(b"deleted from the archive disk")])
        photo_library._current.reset(token)
        self.on("laptop")

        unplugged = lambda known: [Path(p) for p in known if Path(p) != archive.path]
        with patch.object(photo_library, "_candidate_dirs", unplugged):
            kept = self.upload(b"kept on the archive disk")
            self.assertEqual((kept["status"], kept["library"]), ("elsewhere_skipped", "Archive (not plugged in)"))
            self.assertEqual(self.wanted(sha(b"kept on the archive disk")), {"wanted": []})
            self.assertEqual(self.upload(b"deleted from the archive disk")["status"], "saved")
            self.assertEqual(self.upload(b"kept on the archive disk", headers={"X-Allow-Duplicates": "1"})["status"],
                             "saved")
            # Forgetting the library forgets what it held.
            self.on("server")
            with patch.object(sys, "argv", ["libraries", "forget", "Archive"]), contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(photo_library.main(), 0)
            known = self.client.post("/known", json={"hashes": [sha(b"kept on the archive disk")],
                                                     "library": self.main.id}).json()["known"]
            self.on("laptop")
            self.assertEqual(known, {})

    def test_library_is_recorded_when_found_even_without_uploads(self):
        self.on("server")
        token = photo_library.use(self.main)
        with photo_db.get_db() as conn:      # a photo that got in without the receiver knowing
            photo_db.insert_photo(conn, sha(b"old photo"), "old.jpg", "old.jpg", 9, {})
        photo_library._current.reset(token)
        elsewhere = self.base / "server-disk" / "moved"
        self.main.path.rename(elsewhere)     # the disk turns up at another mount point
        with patch.object(photo_library, "_candidate_dirs", lambda known: [elsewhere]):
            photo_library.all_libraries(fresh=True)
        self.on("laptop")
        self.assertIn(sha(b"old photo"), self.catalogue())

    def test_photo_deleted_on_the_server_is_ingested_with_a_note(self):
        self.upload(b"photo one")
        self.on("server")
        token = photo_library.use(self.main)
        with photo_db.get_db() as conn:
            photo_db.delete_photos(conn, [sha(b"photo one")])
        photo_library._current.reset(token)
        self.on("laptop")
        self.assertEqual(self.ingest(), 0)
        self.assertIn(sha(b"photo one"), self.rows())
        self.assertIn("1 of these were deleted from a library on the server (1 in 'Main')", self.output)


if __name__ == "__main__":
    unittest.main()

class ParityRegressionTests(TwoMachines):
    def test_late_sidecar_is_repaired_without_copying_video_again(self):
        sidecar = self.card / 'PRIVATE/M4ROOT/CLIP/C0001M01.XML'
        sidecar.unlink()
        self.assertEqual(self.ingest('--no-sync'), 0)
        digest = sha(b'a video clip')
        original = self.rows()[digest]['saved_filename']
        self.assertIsNone(self.rows()[digest]['sidecar_xml'])
        sidecar.write_text(SIDECAR)
        self.assertEqual(self.ingest('--no-sync', '--dry-run'), 0)
        self.assertIsNone(self.rows()[digest]['sidecar_xml'])
        self.assertEqual(self.ingest('--no-sync'), 0)
        row = self.rows()[digest]
        self.assertEqual(row['sidecar_xml'], SIDECAR)
        self.assertEqual(row['saved_filename'], original)
        self.assertEqual(row['taken_at'], '2026-05-04 10:11:12')
        self.assertEqual(len(self.rows()), 3)

    def test_repeated_filename_collisions_preserve_every_original(self):
        from photo_store import store_file
        photo_library.use(self.trip)
        name = 'same.jpg'
        (self.trip.path / name).write_bytes(b'first original')
        digest = sha(b'new photo')
        collision = self.trip.path / f'same_{digest[:8]}.jpg'
        collision.write_bytes(b'second original')
        tmp = self.trip.path / '.part_test'
        tmp.write_bytes(b'new photo')
        result = store_file(self.trip, tmp, digest, 9, name)
        self.assertEqual((self.trip.path / name).read_bytes(), b'first original')
        self.assertEqual(collision.read_bytes(), b'second original')
        self.assertEqual((self.trip.path / result['file']).read_bytes(), b'new photo')

    def test_server_sidecar_updates_catalogue_as_well_as_library(self):
        photo_library._current.set(None)
        reply = self.client.put('/clip.mp4', content=b'clip').json()
        digest = reply['sha256']
        response = self.client.put('/sidecar/' + digest, content=SIDECAR)
        self.assertEqual(response.status_code, 200)
        with photo_catalogue.connect() as conn:
            row = conn.execute('SELECT taken_at FROM remote_photos WHERE library_id = ? AND sha256 = ?',
                               (self.trip.id, digest)).fetchone()
        self.assertEqual(row['taken_at'], '2026-05-04 10:11:12')

    def test_database_context_closes_connection(self):
        import sqlite3
        photo_library.use(self.trip)
        for connect in (photo_db.get_db, photo_catalogue.connect):
            with connect() as conn:
                conn.execute('SELECT 1')
            with self.assertRaises(sqlite3.ProgrammingError):
                conn.execute('SELECT 1')

    def test_identical_files_on_card_are_ingested_once_instead_of_all_skipped(self):
        duplicate = self.card / 'DCIM/100MSDCF/COPY.JPG'
        duplicate.write_bytes(b'photo one')
        self.assertEqual(self.ingest('--no-sync'), 0)
        self.assertEqual(len(self.rows()), 3)
        self.assertIn(sha(b'photo one'), self.rows())
        self.assertIn('1 repeated on the camera', self.output)

    def test_concurrent_uploads_with_same_name_keep_both_files(self):
        from concurrent.futures import ThreadPoolExecutor
        from photo_store import store_file
        def upload(data):
            token = photo_library.use(self.trip)
            try:
                fd, name = tempfile.mkstemp(dir=self.trip.path, prefix='.part_')
                import os
                with os.fdopen(fd, 'wb') as f:
                    f.write(data)
                return store_file(self.trip, Path(name), sha(data), len(data), 'shared.jpg')
            finally:
                photo_library._current.reset(token)
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(upload, [b'first upload', b'second upload']))
        self.assertTrue(all(r['status'] == 'saved' for r in results))
        self.assertEqual(len(self.rows()), 2)
        for row in self.rows().values():
            self.assertEqual(sha((self.trip.path / row['saved_filename']).read_bytes()), row['sha256'])

    def test_concurrent_uploads_to_different_libraries_obey_duplicate_refusal(self):
        from concurrent.futures import ThreadPoolExecutor
        import threading
        import time
        import photo_store
        other = photo_library.create(self.base / 'laptop-disk' / 'other', 'Other')
        photo_library.all_libraries(fresh=True)
        start = threading.Barrier(2)
        real_elsewhere = photo_store.elsewhere
        def slow_check(*args):
            found = real_elsewhere(*args)
            time.sleep(0.1)  # both requests can finish checking before either commits
            return found
        def upload(lib):
            token = photo_library.use(lib)
            try:
                path = lib.path / '.part_simultaneous'
                path.write_bytes(b'same original')
                start.wait(timeout=5)
                return photo_store.store_file(lib, path, sha(b'same original'), 13, 'same.jpg')
            finally:
                photo_library._current.reset(token)
        with patch.object(photo_store, 'elsewhere', side_effect=slow_check), ThreadPoolExecutor(2) as pool:
            results = list(pool.map(upload, [self.trip, other]))
        self.assertEqual(sorted(r['status'] for r in results), ['elsewhere_skipped', 'saved'])
        self.assertEqual(len(self.rows(self.trip)) + len(self.rows(other)), 1)

    def test_ingest_rechecks_duplicates_that_arrive_after_initial_scan(self):
        import photo_store
        other = photo_library.create(self.base / 'laptop-disk' / 'other', 'Other')
        token = photo_library.use(self.trip)
        try:
            job = ingest_camera.Ingest(self.trip, ingest_camera.Link(None), False, False)
            source = self.card / 'DCIM/100MSDCF/DSC0001.JPG'
            digest = sha(source.read_bytes())
            self.assertEqual(job.classify([digest]), {})
            with_token = photo_library.use(other)
            try:
                tmp, copied, size = ingest_camera.copy_in(source, other.path)
                photo_store.store_file(other, tmp, copied, size, 'arrived.jpg')
            finally:
                photo_library._current.reset(with_token)
            tmp, copied, size = ingest_camera.copy_in(source, self.trip.path)
            result = job.store(tmp, copied, size, source, source.name)
            self.assertEqual(result['status'], 'elsewhere_skipped')
            self.assertEqual(job.saved, 0)
            self.assertEqual(job.have, 0)
            self.assertEqual(job.elsewhere, {digest: 'Other'})
            self.assertEqual(self.rows(), {})
        finally:
            photo_library._current.reset(token)
