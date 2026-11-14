"""Recovery is available in the UI; offline catalogues never require the original disk."""
import hashlib
import io
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient
from PIL import Image

import browse_photos
import photo_catalogue
import photo_collections
import photo_db
import photo_library
from photo_store import store_file
from isolation import isolate


class RecoveryTests(unittest.TestCase):
    def setUp(self):
        base = isolate(self)
        self.lib = photo_library.create(base / 'first', 'First')
        self.other = photo_library.create(base / 'second', 'Second')
        token = photo_library.use(self.lib)
        self.addCleanup(photo_library._current.reset, token)
        buf = io.BytesIO(); Image.new('RGB', (20, 20), 'blue').save(buf, 'JPEG')
        self.original = buf.getvalue()
        self.sha = hashlib.sha256(self.original).hexdigest()
        tmp = self.lib.path / '.part_test'; tmp.write_bytes(self.original)
        self.name = store_file(self.lib, tmp, self.sha, len(self.original), 'image.jpg')['file']
        with photo_db.get_db() as conn:
            photo_db.set_notes(conn, self.sha, 'A #note', 'Second note')
            photo_db.edit_hashtags(conn, [self.sha], ['keeper'], 'add')
            self.album = photo_collections.create(conn, 'album', 'Album')
            photo_collections.add_photos(conn, self.album, [self.sha])
            photo_collections.set_cover(conn, self.album, self.sha)
        self.client = TestClient(browse_photos.LibraryRouter(browse_photos.app))
        self.root = '/lib/' + self.lib.id
        self.client.post(self.root + '/photos/delete', data={'sha': [self.sha]})

    def test_browser_restore_returns_original_and_organisation(self):
        reply = self.client.get(self.root + '/trash')
        self.assertEqual(reply.status_code, 200)
        self.assertIn('image.jpg', reply.text)
        reply = self.client.post(self.root + '/trash/restore', data={'sha': [self.sha]})
        self.assertEqual(reply.status_code, 200)
        self.assertIn('Restored 1 item', reply.text)
        self.assertEqual((self.lib.path / self.name).read_bytes(), self.original)
        with photo_db.get_db() as conn:
            row = conn.execute('SELECT * FROM photos WHERE sha256=?', (self.sha,)).fetchone()
            self.assertEqual((row['note1'], row['note2']), ('A #note', 'Second note'))
            self.assertEqual(photo_db.tags_for(conn, [self.sha])[self.sha], ['keeper', 'note'])
            self.assertEqual(photo_collections.get(conn, self.album)['cover_sha'], self.sha)
            self.assertEqual(len(photo_collections.albums_for_photo(conn, self.sha)), 1)
        self.assertNotIn('image.jpg', self.client.get(self.root + '/trash').text)

    def test_purge_hides_original_but_keeps_deleted_history_and_hash(self):
        reply = self.client.post(self.root + '/trash/purge', data={'sha': [self.sha]})
        self.assertIn('Permanently deleted 1 item', reply.text)
        self.assertFalse((self.lib.trash_dir / self.name).exists())
        self.assertNotIn('image.jpg', self.client.get(self.root + '/trash').text)
        history = self.client.get(self.root + '/trash?show=all')
        self.assertIn('image.jpg', history.text)
        self.assertIn('Original unavailable', history.text)
        with photo_db.get_db() as conn:
            self.assertEqual(photo_db.deleted_hashes(conn, [self.sha]), {self.sha})
        reply = self.client.post(self.root + '/trash/restore', data={'sha': [self.sha]})
        self.assertIn('cannot be restored', reply.text)
        self.assertFalse((self.lib.path / self.name).exists())

    def test_cross_library_recovery_and_empty_selection_cannot_touch_original(self):
        other_root = '/lib/' + self.other.id
        for action in ('restore', 'purge'):
            reply = self.client.post(other_root + '/trash/' + action, data={'sha': [self.sha]})
            self.assertIn('no longer in the trash', reply.text)
        self.assertTrue((self.lib.trash_dir / self.name).is_file())
        reply = self.client.post(self.root + '/trash/purge', data={})
        self.assertIn('Select between 1 and 100', reply.text)
        self.assertTrue((self.lib.trash_dir / self.name).is_file())

    def test_restore_never_overwrites_an_existing_original(self):
        (self.lib.path / self.name).write_bytes(b'keep me')
        reply = self.client.post(self.root + '/trash/restore', data={'sha': [self.sha]})
        self.assertIn('not overwritten', reply.text)
        self.assertEqual((self.lib.path / self.name).read_bytes(), b'keep me')
        self.assertTrue((self.lib.trash_dir / self.name).is_file())

    def test_restore_failure_rolls_file_back_into_trash(self):
        with patch('photo_trash.restore_photo', side_effect=RuntimeError('database failure')):
            with self.assertRaisesRegex(RuntimeError, 'database failure'):
                self.client.post(self.root + '/trash/restore', data={'sha': [self.sha]})
        self.assertTrue((self.lib.trash_dir / self.name).is_file())
        self.assertFalse((self.lib.path / self.name).exists())

    def test_trash_is_prefixed_and_html_escaped(self):
        with photo_db.get_db() as conn:
            conn.execute('UPDATE deleted_photos SET original_filename=?', ('<script>bad</script>.jpg',))
        reply = self.client.get(self.root + '/trash', headers={'X-Forwarded-Prefix': '/photos'})
        self.assertIn('/photos' + self.root + '/trash/restore', reply.text)
        self.assertIn('&lt;script&gt;', reply.text)
        self.assertNotIn('<script>bad', reply.text)


class CatalogueTests(unittest.TestCase):
    def setUp(self):
        self.base = isolate(self)
        self.lib = photo_library.create(self.base / 'disk' / 'photos', 'Travelling library')
        token = photo_library.use(self.lib)
        self.addCleanup(photo_library._current.reset, token)
        with photo_db.get_db() as conn:
            for n, (name, kind) in enumerate([('coast_100%.JPG', 'image'), ('C0001.MP4', 'video')]):
                digest = str(n) * 64
                photo_db.insert_photo(conn, digest, name, name, 100 + n, {})
                photo_db.update_columns(conn, digest, {'taken_at': '2026-11-14 10:00:00', 'camera': 'Sony ZV-E10', 'media_type': kind})
                photo_db.set_notes(conn, digest, 'Private note #private', '')
            conn.commit()
        photo_catalogue.snapshot(self.lib)
        self.lib.path.rename(self.base / 'detached-library')
        photo_library._scan.cache = None
        self.client = TestClient(browse_photos.LibraryRouter(browse_photos.app))
        self.url = '/libraries/' + self.lib.id + '/catalogue'

    def test_offline_inventory_search_uses_cache_and_no_original_database(self):
        with patch.object(photo_db, 'get_db', side_effect=AssertionError('must not open offline library')):
            reply = self.client.get(self.url, params={'q': 'Sony 2026-11', 'type': 'video'})
        self.assertEqual(reply.status_code, 200)
        self.assertIn('C0001.MP4', reply.text)
        self.assertNotIn('coast_100%.JPG', reply.text)
        self.assertNotIn('Private note', reply.text)
        self.assertNotIn('#private', reply.text)
        self.assertIn('read-only', reply.text)
        self.assertEqual(self.client.post(self.url).status_code, 405)
        self.assertFalse(self.lib.path.exists())

    def test_literal_wildcard_search_and_library_scoping(self):
        reply = self.client.get(self.url, params={'q': '100%'})
        self.assertIn('coast_100%.JPG', reply.text)
        self.assertNotIn('C0001.MP4', reply.text)
        other = photo_library.create(self.base / 'other', 'Other')
        self.assertEqual(self.client.get('/libraries/' + other.id + '/catalogue', follow_redirects=False).status_code, 303)
        self.assertEqual(self.client.get('/libraries/ffffffff/catalogue').status_code, 404)

    def test_returned_disk_redirects_to_live_library_with_filters(self):
        (self.base / 'detached-library').rename(self.lib.path)
        photo_library._scan.cache = None
        reply = self.client.get(self.url, params={'q': 'Sony', 'type': 'video'},
                                headers={'X-Forwarded-Prefix': '/photos'}, follow_redirects=False)
        self.assertEqual(reply.status_code, 303)
        self.assertEqual(reply.headers['location'], '/photos/lib/' + self.lib.id + '/?q=Sony&type=video')

    def test_empty_catalogue_does_not_create_a_cache_database(self):
        photo_catalogue.db_file().unlink()
        reply = self.client.get(self.url)
        self.assertEqual(reply.status_code, 200)
        self.assertIn('No cached items', reply.text)
        self.assertFalse(photo_catalogue.db_file().exists())

    def test_offline_counts_agree_in_browser_cli_and_receiver(self):
        import receiver
        lib = photo_library.find(self.lib.id)
        self.assertFalse(lib.online)
        self.assertEqual(photo_library._photo_count(lib), '2')
        libraries = TestClient(receiver.app).get('/libraries').json()['libraries']
        self.assertEqual(next(l['items'] for l in libraries if l['id'] == lib.id), 2)
        self.assertIn('2 cached items', self.client.get('/libraries').text)
