"""Deleting in the browser moves files to the trash and never touches anything else."""
import tempfile
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

import browse_photos
import photo_db as db
import photo_library
from isolation import isolate


class DeleteTests(unittest.TestCase):
    def setUp(self):
        isolate(self)
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.lib = photo_library.Library('abcdef02', 'Test', Path(self.temp.name), True)
        token = photo_library.use(self.lib)
        self.addCleanup(photo_library._current.reset, token)
        with db.get_db() as conn:
            for digest in ('aa11', 'bb22'):
                (self.lib.path / f'{digest}_2026-01-01_00-00-00.jpg').write_bytes(b'original')
                db.insert_photo(conn, digest, f'{digest}_2026-01-01_00-00-00.jpg', 'x.jpg', 8, {})
        self.lib.cache_dir.mkdir()
        for digest in ('aa11', 'bb22'):
            (self.lib.cache_dir / f'{digest}_480.jpg').write_bytes(b'thumb')
        self.client = TestClient(browse_photos.app)

    def names(self, folder):
        return sorted(p.name for p in folder.iterdir() if p.is_file() and p.suffix == '.jpg')

    def test_delete_moves_to_trash_and_clears_only_that_thumbnail(self):
        self.assertEqual(self.client.post('/photos/delete', data={'sha': ['aa11']}).status_code, 200)
        self.assertEqual(self.names(self.lib.path), ['bb22_2026-01-01_00-00-00.jpg'])
        self.assertEqual(self.names(self.lib.trash_dir), ['aa11_2026-01-01_00-00-00.jpg'])
        self.assertEqual(self.names(self.lib.cache_dir), ['bb22_480.jpg'])

    def test_file_patterns_in_the_request_cannot_remove_originals(self):
        self.client.post('/photos/delete', data={'sha': ['../*', '*', '../.trash/*']})
        self.assertEqual(len(self.names(self.lib.path)), 2)
        self.assertEqual(len(self.names(self.lib.cache_dir)), 2)


if __name__ == '__main__':
    unittest.main()
