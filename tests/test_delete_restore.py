"""Ordering, deleting, restoring and emptying the trash."""
import contextlib
import io
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

import browse_photos
import photo_collections as coll
import photo_db as db
import photo_library
import photo_catalogue
import refresh_metadata
from isolation import isolate
import restore_deleted
import photo_trash

SIDECAR = '<NonRealTimeMeta><CreationDate value="2026-05-04T10:11:12+10:00"/></NonRealTimeMeta>'


class Base(unittest.TestCase):
    def setUp(self):
        isolate(self)
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.lib = photo_library.Library('abcdef03', 'Test', Path(self.temp.name), True)
        token = photo_library.use(self.lib)
        self.addCleanup(photo_library._current.reset, token)
        self.conn = db.get_db()
        self.addCleanup(self.conn.close)
        self.client = TestClient(browse_photos.app)

    def add(self, digest, taken_at=None, uploaded_at=None):
        name = f'{digest}_x.mp4'
        (self.lib.path / name).write_bytes(b'data ' + digest.encode())
        db.insert_photo(self.conn, digest, name, digest + '.MP4', 9, {'source': 'usb_camera'})
        if taken_at:
            self.conn.execute('UPDATE photos SET taken_at = ? WHERE sha256 = ?', (taken_at, digest))
        if uploaded_at:
            self.conn.execute('UPDATE photos SET uploaded_at = ? WHERE sha256 = ?', (uploaded_at, digest))
        self.conn.commit()
        return name

    def restore(self, *argv, index=None):
        out = io.StringIO()
        with patch.object(sys, 'argv', ['restore_deleted.py', *argv]), \
             patch.object(photo_library, 'cli_library', return_value=self.lib), \
             patch.object(photo_trash, 'elsewhere', return_value={}), \
             patch.object(photo_trash, 'index_photo', side_effect=index), \
             contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
            status = restore_deleted.main()
        return status, out.getvalue()


class OrderTests(Base):
    def test_items_without_a_capture_time_sort_and_search_by_arrival_time(self):
        self.add('a1', taken_at='2026-03-10 09:00:00')
        self.add('b2', uploaded_at='2026-03-10T12:00:00+00:00')      # no capture time
        self.add('c3', taken_at='2026-03-10 18:00:00')
        self.add('d4', uploaded_at='2026-02-01T00:00:00+00:00')
        self.assertEqual(db.search_ids(self.conn), ['c3', 'b2', 'a1', 'd4'])
        self.assertEqual(db.search_ids(self.conn, '2026-03-10'), ['c3', 'b2', 'a1'])
        self.assertEqual(db.search_ids(self.conn, '2026-02'), ['d4'])
        plan = ' '.join(r[3] for r in self.conn.execute(f'EXPLAIN QUERY PLAN SELECT sha256 FROM photos {db.ORDER_BY}'))
        self.assertIn('photos_when', plan)


class DeleteRestoreTests(Base):
    def test_restore_brings_back_notes_tags_sidecar_and_albums(self):
        name = self.add('a1')
        self.conn.execute('UPDATE photos SET sidecar_xml = ? WHERE sha256 = ?', (SIDECAR, 'a1'))
        db.set_notes(self.conn, 'a1', 'At the #beach', 'second')
        db.edit_hashtags(self.conn, ['a1'], ['family'], 'add')
        kept, dropped = coll.create(self.conn, 'album', 'Kept'), coll.create(self.conn, 'album', 'Dropped')
        coll.add_photos(self.conn, kept, ['a1']); coll.add_photos(self.conn, dropped, ['a1'])
        coll.set_cover(self.conn, kept, 'a1')
        before = dict(self.conn.execute("SELECT * FROM photos WHERE sha256 = 'a1'").fetchone())
        self.conn.commit()

        self.assertEqual(self.client.post('/photos/delete', data={'sha': ['a1']}).status_code, 200)
        self.assertTrue((self.lib.trash_dir / name).is_file())
        self.assertIsNone(self.conn.execute("SELECT 1 FROM photos WHERE sha256 = 'a1'").fetchone())
        coll.delete(self.conn, dropped); self.conn.commit()

        status, out = self.restore('a1')
        self.assertEqual(status, 0, out)
        self.assertIn('back in 1 album(s)', out)
        after = dict(self.conn.execute("SELECT * FROM photos WHERE sha256 = 'a1'").fetchone())
        for column in ('note1', 'note2', 'hashtags', 'sidecar_xml', 'uploaded_at', 'metadata', 'saved_filename'):
            self.assertEqual(after[column], before[column], column)
        self.assertEqual(db.tags_for(self.conn, ['a1'])['a1'], ['beach', 'family'])
        self.assertEqual([a['name'] for a in coll.albums_for_photo(self.conn, 'a1')], ['Kept'])
        self.assertEqual(coll.get(self.conn, kept)['cover_sha'], 'a1')
        self.assertTrue((self.lib.path / name).is_file())
        self.assertEqual(self.conn.execute('SELECT COUNT(*) FROM deleted_photos').fetchone()[0], 0)

    def test_restore_keeps_a_replacement_album_cover(self):
        self.add('a1'); self.add('b2')
        album = coll.create(self.conn, 'album', 'Album')
        coll.add_photos(self.conn, album, ['a1', 'b2'])
        coll.set_cover(self.conn, album, 'a1'); self.conn.commit()
        self.client.post('/photos/delete', data={'sha': ['a1']})
        coll.set_cover(self.conn, album, 'b2'); self.conn.commit()
        status, out = self.restore('a1')
        self.assertEqual(status, 0, out)
        self.assertEqual(coll.get(self.conn, album)['cover_sha'], 'b2')

    def test_restore_catalogue_contains_reextracted_metadata(self):
        self.add('a1')
        self.client.post('/photos/delete', data={'sha': ['a1']})
        def reindex(conn, digest):
            db.update_columns(conn, digest, {'taken_at': '2026-05-04 10:11:12', 'camera': 'Sony ZV-E10'})
        status, out = self.restore('a1', index=reindex)
        self.assertEqual(status, 0, out)
        with photo_catalogue.connect() as conn:
            row = conn.execute('SELECT taken_at, camera FROM remote_photos WHERE library_id=? AND sha256=?',
                               (self.lib.id, 'a1')).fetchone()
        self.assertEqual(tuple(row), ('2026-05-04 10:11:12', 'Sony ZV-E10'))

    def test_metadata_refresh_also_updates_catalogue(self):
        self.add('a1')
        def reindex(conn, digest):
            db.update_columns(conn, digest, {'taken_at': '2026-05-04 10:11:12', 'camera': 'Sony ZV-E10'})
        with patch.object(sys, 'argv', ['refresh_metadata.py', '--all']), \
             patch.object(photo_library, 'cli_library', return_value=self.lib), \
             patch.object(refresh_metadata, 'index_photo', side_effect=reindex), \
             contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(refresh_metadata.main(), 0)
        with photo_catalogue.connect() as conn:
            row = conn.execute('SELECT taken_at, camera FROM remote_photos WHERE library_id=? AND sha256=?',
                               (self.lib.id, 'a1')).fetchone()
        self.assertIsNotNone(row)
        self.assertEqual(tuple(row), ('2026-05-04 10:11:12', 'Sony ZV-E10'))

    def test_photo_deleted_by_an_older_version_still_restores(self):
        name = self.add('a1')
        self.client.post('/photos/delete', data={'sha': ['a1']})
        self.conn.execute('UPDATE deleted_photos SET details = NULL'); self.conn.commit()
        status, out = self.restore('a1')
        self.assertEqual(status, 0, out)
        self.assertIn('deleted by an older version', out)
        self.assertTrue((self.lib.path / name).is_file())

    def test_restore_never_overwrites_a_file(self):
        name = self.add('a1')
        self.client.post('/photos/delete', data={'sha': ['a1']})
        (self.lib.path / name).write_bytes(b'something else')
        status, out = self.restore('a1')
        self.assertEqual(status, 1)
        self.assertEqual((self.lib.path / name).read_bytes(), b'something else')
        self.assertTrue((self.lib.trash_dir / name).is_file())

    def test_photo_whose_file_cannot_be_moved_is_kept(self):
        self.add('a1'); self.add('b2')
        real = os.replace

        def replace(src, dest):
            if Path(src).name.startswith('a1'):
                raise PermissionError('read-only trash')
            return real(src, dest)

        with patch.object(browse_photos.os, 'replace', replace):
            response = self.client.post('/photos/delete', data={'sha': ['a1', 'b2']})
        self.assertIn('1 could not be moved', response.headers['HX-Trigger'])
        self.assertEqual([r[0] for r in self.conn.execute('SELECT sha256 FROM photos')], ['a1'])
        self.assertEqual([r[0] for r in self.conn.execute('SELECT sha256 FROM deleted_photos')], ['b2'])

    def test_purge_removes_old_trash_but_keeps_the_hashes(self):
        old, new = self.add('a1'), self.add('b2')
        self.client.post('/photos/delete', data={'sha': ['a1', 'b2']})
        self.conn.execute("UPDATE deleted_photos SET deleted_at = '2020-01-01T00:00:00+00:00' WHERE sha256 = 'a1'")
        self.conn.commit()
        status, out = self.restore('--purge', '30')
        self.assertEqual(status, 0, out)
        self.assertIn('removed 1 file(s)', out)
        self.assertFalse((self.lib.trash_dir / old).exists())
        self.assertTrue((self.lib.trash_dir / new).exists())
        self.assertEqual(db.deleted_hashes(self.conn, ['a1', 'b2']), {'a1', 'b2'})
        self.assertEqual(self.restore('a1')[0], 1)        # gone for good
        self.assertEqual(self.restore('--purge', '0')[0], 0)
        self.assertFalse((self.lib.trash_dir / new).exists())


if __name__ == '__main__':
    unittest.main()
