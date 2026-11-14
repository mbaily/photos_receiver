"""Exercise the same library routes used on the laptop and behind the server proxy."""
import hashlib
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

import browse_photos
import photo_db
import photo_library
from photo_store import store_file
from isolation import isolate


class BrowserWorkflowTests(unittest.TestCase):
    def setUp(self):
        base = isolate(self)
        self.first = photo_library.create(base / 'first', 'First')
        self.second = photo_library.create(base / 'second', 'Second')
        token = photo_library.use(self.first)
        self.addCleanup(photo_library._current.reset, token)
        data = b'original camera photo'
        self.sha = hashlib.sha256(data).hexdigest()
        tmp = self.first.path / '.part_test'
        tmp.write_bytes(data)
        store_file(self.first, tmp, self.sha, len(data), 'camera.jpg')
        self.client = TestClient(browse_photos.LibraryRouter(browse_photos.app))
        self.root = '/lib/' + self.first.id

    def post(self, path, data):
        reply = self.client.post(self.root + path, data=data)
        self.assertEqual(reply.status_code, 200, reply.text)
        return reply

    def test_notes_bulk_tags_album_and_search_remain_scoped_to_library(self):
        self.post('/photo/' + self.sha + '/notes', {'note1': 'Trip #holiday', 'note2': 'Family'})
        self.post('/photos/tags', {'sha': [self.sha], 'hashtags': '#keeper', 'mode': 'add'})
        self.post('/collections', {'kind': 'folder', 'name': 'Trips'})
        with photo_db.get_db() as conn:
            folder = conn.execute('SELECT id FROM collections').fetchone()[0]
        self.post('/picker/new-album', {'name': 'Coast', 'parent': str(folder), 'sha': [self.sha]})
        with photo_db.get_db() as conn:
            album = conn.execute("SELECT id FROM collections WHERE kind='album'").fetchone()[0]
        for query in ('#holiday', '#keeper', 'Family', 'camera.jpg'):
            r = self.client.get(self.root + '/grid', params={'q': query})
            self.assertEqual(r.status_code, 200)
            self.assertIn(self.sha, r.text)
        detail = self.client.get(self.root + '/photo/' + self.sha, params={'album': album})
        self.assertEqual(detail.status_code, 200)
        self.assertIn('Trips', detail.text)
        self.assertIn('Coast', detail.text)
        other = self.client.get('/lib/' + self.second.id + '/grid', params={'q': '#keeper'})
        self.assertNotIn(self.sha, other.text)
        self.assertEqual(self.client.get('/lib/' + self.second.id + '/photo/' + self.sha).status_code, 404)
        self.post('/photos/tags', {'sha': [self.sha], 'hashtags': '#holiday', 'mode': 'remove'})
        self.assertNotIn(self.sha, self.client.get(self.root + '/grid', params={'q': '#holiday'}).text)
        self.assertIn(self.sha, self.client.get(self.root + '/grid', params={'q': '#keeper'}).text)
        self.post('/albums/' + str(album) + '/remove', {'sha': [self.sha]})
        self.assertEqual(self.client.get(self.root + '/file/' + self.sha).content, b'original camera photo')

    def test_proxy_prefix_is_preserved_in_grid_and_viewer_urls(self):
        for path in ('/grid', '/photo/' + self.sha):
            reply = self.client.get(self.root + path, headers={'X-Forwarded-Prefix': '/photos'})
            self.assertEqual(reply.status_code, 200)
            self.assertIn('/photos' + self.root + '/', reply.text)
            self.assertNotIn('"/lib/', reply.text)

    def test_saved_note_response_refreshes_grid_snippet_with_secondary_fallback(self):
        reply = self.post('/photo/' + self.sha + '/notes', {'note1': '', 'note2': 'Secondary #keep'})
        self.assertIn('id="card-note-' + self.sha + '" hx-swap-oob="true"', reply.text)
        self.assertIn('Secondary #keep</p>', reply.text)
        grid = self.client.get(self.root + '/grid')
        self.assertIn('id="card-note-' + self.sha, grid.text)
        self.assertIn('Secondary #keep</p>', grid.text)
        cleared = self.post('/photo/' + self.sha + '/notes', {'note1': '', 'note2': ''})
        self.assertIn('line-clamp-2 hidden', cleared.text)
