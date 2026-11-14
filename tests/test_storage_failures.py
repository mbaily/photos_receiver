"""Capacity forecasts, detach guards and resumable partial imports."""
import shutil
from pathlib import Path
from unittest.mock import patch

import ingest_camera
import photo_db
import photo_library
import photo_store
import send_camera
from test_ingest_camera import TwoMachines, sha
import test_ingest_camera as fixtures


class StorageTests(TwoMachines):
    command = fixtures.GPhotoIngestTests.command
    gphoto = fixtures.GPhotoIngestTests.gphoto

    def test_mounted_fat32_rejects_the_whole_plan_including_dry_run(self):
        big = self.card / 'PRIVATE/M4ROOT/CLIP/BIG.MP4'
        with big.open('wb') as f: f.truncate(2**32)
        real_hash = send_camera.sha256_of
        def hash_file(path, cache):
            return 'f' * 64 if path == big else real_hash(path, cache)
        with patch.object(ingest_camera, 'filesystem_type', return_value='vfat'), \
             patch.object(send_camera, 'sha256_of', side_effect=hash_file):
            for args in (('--no-sync',), ('--dry-run', '--no-sync')):
                self.assertEqual(self.ingest(*args), 1)
                self.assertIn('FAT32', self.output)
                self.assertIn('Nothing was copied', self.output)
                self.assertEqual(self.rows(), {})
                self.assertFalse(list(self.trip.path.glob('.part_*')))

    def test_dry_run_reports_insufficient_batch_space(self):
        usage = shutil.disk_usage(self.base)
        with patch.object(ingest_camera.shutil, 'disk_usage', return_value=usage._replace(free=10)):
            self.assertEqual(self.ingest('--dry-run', '--no-sync'), 1)
        self.assertIn('not enough room', self.output)
        self.assertEqual(self.rows(), {})

    def test_disconnected_library_does_not_create_files_in_empty_mountpoint(self):
        offline = self.trip.path.with_name('detached')
        self.trip.path.rename(offline)
        self.trip.path.mkdir()
        token = photo_library.use(self.trip)
        try:
            with self.assertRaises(photo_library.LibraryError): photo_db.get_db()
            temp = self.base / 'original.part'; temp.write_bytes(b'original')
            with self.assertRaises(photo_library.LibraryError):
                photo_store.store_file(self.trip, temp, sha(b'original'), 8, 'image.jpg')
            self.assertEqual(list(self.trip.path.iterdir()), [])
            self.assertEqual(temp.read_bytes(), b'original')
            self.assertTrue((offline / 'library.json').is_file())
        finally:
            photo_library._current.reset(token)

    def test_store_failure_removes_finished_copy_temporary_files(self):
        with patch.object(ingest_camera, 'store_file', side_effect=OSError('write failed')):
            self.assertEqual(self.ingest('--no-sync'), 1)
        self.assertFalse(list(self.trip.path.glob('.part_*')))
        self.assertEqual(self.rows(), {})

    def test_partial_import_is_reported_and_retry_resumes_without_duplicates(self):
        copy = ingest_camera.copy_in
        count = 0
        def interrupted(*args):
            nonlocal count
            count += 1
            if count > 1: raise OSError('disk disconnected')
            return copy(*args)
        with patch.object(ingest_camera, 'copy_in', side_effect=interrupted):
            self.assertEqual(self.ingest('--no-sync'), 1)
        self.assertIn('PARTIAL IMPORT', self.output)
        self.assertEqual(len(self.rows()), 1)
        self.assertFalse(list(self.trip.path.glob('.part_*')))
        self.assertEqual(self.ingest('--no-sync'), 0)
        self.assertEqual(len(self.rows()), 3)

    def test_ptp_dry_run_accounts_for_cumulative_predicted_imports(self):
        self.files = {'DCIM/A.JPG': b'12345678', 'DCIM/B.JPG': b'abcdefgh'}
        usage = shutil.disk_usage(self.base)._replace(free=ingest_camera.SPARE_BYTES + 12)
        with patch.object(ingest_camera.shutil, 'disk_usage', return_value=usage):
            self.assertEqual(self.gphoto('--dry-run'), 1)
        self.assertIn('predicted imports would exhaust', self.output)
        self.assertEqual(self.rows(), {})
        self.assertFalse(list(self.trip.path.glob('.part_*')))

    def test_ptp_stops_before_next_download_when_storage_fills(self):
        self.files = {'DCIM/A.JPG': b'12345678', 'DCIM/B.JPG': b'abcdefgh'}
        usage = shutil.disk_usage(self.base)
        calls = 0
        def disk_usage(path):
            nonlocal calls
            calls += 1
            return usage if calls <= 2 else usage._replace(free=1)
        with patch.object(ingest_camera.shutil, 'disk_usage', side_effect=disk_usage):
            self.assertEqual(self.gphoto(), 1)
        self.assertEqual(len(self.rows()), 1)
        self.assertEqual(len(self.reads), 1)
        self.assertIn('PARTIAL IMPORT', self.output)
        self.assertFalse(list(self.trip.path.glob('.part_*')))

    def test_read_only_library_can_be_viewed_but_ingest_is_refused(self):
        self.assertEqual(self.ingest('--no-sync'), 0)
        token = photo_library.use(self.trip)
        try:
            with photo_db.get_db() as conn: conn.execute('PRAGMA wal_checkpoint(TRUNCATE)')
            with patch('photo_db.os.access', return_value=False):
                with photo_db.get_db() as conn:
                    self.assertEqual(conn.execute('SELECT count(*) FROM photos').fetchone()[0], 3)
                self.assertEqual(self.ingest('--no-sync'), 1)
                self.assertIn('read-only', self.output)
        finally:
            photo_library._current.reset(token)

    def test_hash_mismatch_invalidates_cache_so_retry_can_rehash_changed_original(self):
        import os
        source = self.card / 'DCIM/100MSDCF/DSC0001.JPG'
        real_copy = ingest_camera.copy_in
        changed = False
        def change_then_copy(path, folder):
            nonlocal changed
            if path == source and not changed:
                before = path.stat()
                path.write_bytes(b'PHOTO ONE')  # same length and recorded mtime as before
                os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns))
                changed = True
            return real_copy(path, folder)
        with patch.object(ingest_camera, 'copy_in', side_effect=change_then_copy):
            self.assertEqual(self.ingest('--no-sync'), 1)
        self.assertIn('copy does not match', self.output)
        self.assertNotIn(sha(b'photo one'), self.rows())
        self.assertEqual(self.ingest('--no-sync'), 0, self.output)
        self.assertIn(sha(b'PHOTO ONE'), self.rows())
