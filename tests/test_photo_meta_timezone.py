"""A library's capture dates must survive re-indexing on another machine."""
import json
import os
import time
import unittest
from unittest.mock import patch

import photo_db
import photo_library
import photo_meta
from isolation import isolate


class CaptureTimezoneTests(unittest.TestCase):
    def setUp(self):
        base = isolate(self)
        self.lib = photo_library.create(base / 'library', 'Portable')
        token = photo_library.use(self.lib)
        self.addCleanup(photo_library._current.reset, token)
        self.addCleanup(time.tzset)
        self.env = patch.dict(os.environ, {'TZ': 'Australia/Melbourne'})
        self.env.start(); self.addCleanup(self.env.stop)
        time.tzset()
        self.path = self.lib.path / 'clip.mp4'
        self.path.write_bytes(b'synthetic clip')
        with photo_db.get_db() as conn:
            photo_db.insert_photo(conn, 'a' * 64, self.path.name, self.path.name, 14, {})

    def test_quicktime_only_capture_date_survives_a_timezone_change(self):
        meta = {'QuickTime:CreateDate': '2026:11:13 23:10:00'}
        with patch.object(photo_meta, 'run_exiftool', return_value=meta):
            with photo_db.get_db() as conn:
                photo_meta.index_photo(conn, 'a' * 64)
                before = conn.execute('SELECT taken_at FROM photos').fetchone()[0]
            os.environ['TZ'] = 'America/Los_Angeles'; time.tzset()
            with photo_db.get_db() as conn:
                photo_meta.index_photo(conn, 'a' * 64)
                after = conn.execute('SELECT taken_at FROM photos').fetchone()[0]
        self.assertEqual(before, '2026-11-14 10:10:00')
        self.assertEqual(after, before)

    def test_first_index_records_timezone_in_portable_row_metadata(self):
        with patch.object(photo_meta, 'run_exiftool', return_value={}):
            with photo_db.get_db() as conn:
                photo_meta.index_photo(conn, 'a' * 64)
                metadata = json.loads(conn.execute('SELECT metadata FROM photos').fetchone()[0])
        self.assertEqual(metadata.get('capture_timezone'), 'Australia/Melbourne')

    def test_old_library_recovers_original_capture_offset_on_another_host(self):
        meta = {'QuickTime:CreateDate': '2026:11:13 23:10:00'}
        with photo_db.get_db() as conn:
            photo_db.update_columns(conn, 'a' * 64, {'taken_at': '2026-11-14 10:10:00', 'exif': json.dumps(meta)})
        os.environ['TZ'] = 'America/Los_Angeles'; time.tzset()
        with patch.object(photo_meta, 'run_exiftool', return_value=meta):
            with photo_db.get_db() as conn:
                photo_meta.index_photo(conn, 'a' * 64)
                row = conn.execute('SELECT taken_at,metadata FROM photos').fetchone()
        self.assertEqual(row['taken_at'], '2026-11-14 10:10:00')
        self.assertEqual(json.loads(row['metadata'])['capture_timezone'], '+11:00')

    def test_camera_local_dates_keep_their_priority(self):
        meta = {'ExifIFD:DateTimeOriginal': '2026:11:14 09:00:00', 'QuickTime:CreateDate': '2026:11:13 23:10:00'}
        self.assertEqual(photo_meta.summarize(meta, self.path, 'America/Los_Angeles')['taken_at'], '2026-11-14 09:00:00')

    def test_invalid_date_does_not_block_valid_fallback(self):
        meta = {'ExifIFD:DateTimeOriginal': '2026:99:99 10:00:00', 'QuickTime:CreateDate': '2026:11:13 23:10:00'}
        self.assertEqual(photo_meta.summarize(meta, self.path, 'Australia/Melbourne')['taken_at'], '2026-11-14 10:10:00')

    def test_legacy_trash_restore_keeps_its_original_wall_time_abroad(self):
        import photo_trash
        meta = {'QuickTime:CreateDate': '2026:11:13 23:10:00'}
        with photo_db.get_db() as conn:
            photo_db.update_columns(conn, 'a' * 64, {'taken_at': '2026-11-14 10:10:00', 'exif': json.dumps(meta)})
            photo_db.delete_photos(conn, ['a' * 64])
        self.lib.trash_dir.mkdir()
        self.path.rename(self.lib.trash_dir / self.path.name)
        os.environ['TZ'] = 'America/Los_Angeles'; time.tzset()
        with patch.object(photo_meta, 'run_exiftool', return_value=meta):
            with photo_db.get_db() as conn:
                photo_trash.restore_one(self.lib, conn, 'a' * 64)
                row = conn.execute('SELECT taken_at,metadata FROM photos').fetchone()
        self.assertEqual(row['taken_at'], '2026-11-14 10:10:00')
        self.assertEqual(json.loads(row['metadata'])['capture_timezone'], '+11:00')
