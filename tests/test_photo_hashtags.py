import sqlite3
import tempfile
import unittest
from pathlib import Path

import photo_db as db
import photo_library
from isolation import isolate


class PhotoHashtagTests(unittest.TestCase):
    def setUp(self):
        isolate(self)
        self.temp = tempfile.TemporaryDirectory()
        self.lib = photo_library.Library('abcdef01', 'Test', Path(self.temp.name), True)
        self.context = photo_library.use(self.lib)
        self.conn = db.get_db()
        for digest in ('a', 'b', 'c'):
            db.insert_photo(self.conn, digest, digest + '.jpg', digest + '.jpg', 100, {})
        self.conn.commit()

    def tearDown(self):
        self.conn.close()
        photo_library._current.reset(self.context)
        self.temp.cleanup()

    def test_add_is_separate_deduplicated_and_only_applies_to_selection(self):
        db.set_notes(self.conn, 'a', 'An unchanged note #old', 'Second note')
        self.assertEqual(db.edit_hashtags(self.conn, ['a', 'a', 'b', 'missing'], ['family', 'holiday'], 'add'), 2)
        self.assertEqual(db.edit_hashtags(self.conn, ['a', 'b'], ['family'], 'add'), 0)
        row = self.conn.execute("SELECT * FROM photos WHERE sha256='a'").fetchone()
        self.assertEqual(row['hashtags'], '#family #holiday')
        self.assertEqual((row['note1'], row['note2']), ('An unchanged note #old', 'Second note'))
        self.assertEqual(db.tags_for(self.conn, ['c']), {'c': []})
        self.assertEqual(set(db.search_ids(self.conn, '#family')), {'a', 'b'})
        counts = {row['tag']: row['photos'] for row in db.all_tags(self.conn)}
        self.assertEqual(counts['family'], 2)

    def test_note_save_preserves_applied_tags(self):
        db.edit_hashtags(self.conn, ['a'], ['family'], 'add')
        self.assertEqual(db.set_notes(self.conn, 'a', '#new', 'changed'), ['family', 'new'])
        self.assertEqual(db.set_notes(self.conn, 'a', '', ''), ['family'])

    def test_remove_clears_manual_and_all_exact_note_tokens_only(self):
        db.set_notes(self.conn, 'a', 'Holiday #Holiday, #holiday! holiday #holidayish #holiday-trip',
                     'Other #HOLIDAY. Keep #other /#holiday &#holiday x#holiday')
        db.edit_hashtags(self.conn, ['a', 'b'], ['holiday', 'other'], 'add')
        db.edit_hashtags(self.conn, ['a'], ['holiday'], 'remove')
        row = self.conn.execute("SELECT * FROM photos WHERE sha256='a'").fetchone()
        self.assertEqual(row['hashtags'], '#other')
        self.assertEqual(row['note1'], 'Holiday , ! holiday #holidayish #holiday-trip')
        self.assertEqual(row['note2'], 'Other . Keep #other /#holiday &#holiday x#holiday')
        self.assertEqual(db.tags_for(self.conn, ['a'])['a'], ['holiday-trip', 'holidayish', 'other'])
        self.assertEqual(db.tags_for(self.conn, ['b'])['b'], ['holiday', 'other'])
        db.set_notes(self.conn, 'a', row['note1'], row['note2'])
        self.assertNotIn('holiday', db.tags_for(self.conn, ['a'])['a'])

    def test_unicode_and_hyphenated_tags_use_same_normalization(self):
        db.set_notes(self.conn, 'a', '#CAFÉ #trip-2026 #trip', '')
        db.edit_hashtags(self.conn, ['a'], ['café', 'trip-2026'], 'remove')
        self.assertEqual(db.tags_for(self.conn, ['a'])['a'], ['trip'])

    def test_transaction_can_roll_back_bulk_changes(self):
        db.edit_hashtags(self.conn, ['a', 'b'], ['family'], 'add')
        self.conn.rollback()
        self.assertEqual(db.search_ids(self.conn, '#family'), [])

    def test_legacy_database_gets_empty_hashtags_column(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)
            conn = sqlite3.connect(path / 'inbox.db')
            conn.execute('CREATE TABLE photos (sha256 TEXT PRIMARY KEY, saved_filename TEXT NOT NULL, original_filename TEXT NOT NULL, size_bytes INTEGER NOT NULL, uploaded_at TEXT NOT NULL, metadata TEXT NOT NULL DEFAULT "{}")')
            conn.execute('INSERT INTO photos VALUES ("legacy", "x.jpg", "x.jpg", 1, "2026-01-01", "{}")')
            conn.commit(); conn.close()
            context = photo_library.use(photo_library.Library('abcdef02', 'Legacy', path, True))
            try:
                with db.get_db() as upgraded:
                    self.assertEqual(upgraded.execute('SELECT hashtags FROM photos').fetchone()[0], '')
                    db.edit_hashtags(upgraded, ['legacy'], ['family'], 'add')
                    self.assertEqual(db.search_ids(upgraded, '#family'), ['legacy'])
            finally:
                photo_library._current.reset(context)


if __name__ == '__main__':
    unittest.main()
