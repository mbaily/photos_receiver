"""Integrity across disks and machines: verifying and repairing originals, noticing an
older or copied library, and keeping the catalogue honest. Everything is pretend: temporary
folders stand in for disks, and two registry files for two machines."""
import hashlib
import io
import os
import shutil
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

import browse_photos
import photo_catalogue
import photo_collections
import photo_db
import photo_library
import photo_store
import photo_trash
import photo_verify
from isolation import isolate
from test_ingest_camera import TwoMachines


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


class Disks(unittest.TestCase):
    def setUp(self):
        self.base = isolate(self)
        self.lib = photo_library.create(self.base / 'disk-a' / 'photos', 'First')
        self.other = photo_library.create(self.base / 'disk-b' / 'photos', 'Second')

    def store(self, data: bytes, name='image.jpg', lib=None, allow_duplicates=False):
        lib = lib or self.lib
        token = photo_library.use(lib)
        try:
            tmp = lib.path / '.part_test'
            tmp.write_bytes(data)
            return photo_store.store_file(lib, tmp, sha(data), len(data), name, allow_duplicates=allow_duplicates)
        finally:
            photo_library._current.reset(token)

    def db(self, lib=None):
        """Run a function with a library's database: self.db()(lambda conn: ...)"""
        lib = lib or self.lib

        def run(func):
            token = photo_library.use(lib)
            try:
                with photo_db.get_db() as conn:
                    return func(conn)
            finally:
                photo_library._current.reset(token)
        return run

    def verify(self, lib=None, repair=False):
        out = io.StringIO()
        status = photo_verify.run(lib or self.lib, repair, out)
        self.output = out.getvalue()
        return status

    def warnings(self, lib=None):
        return [w['kind'] for w in photo_catalogue.warnings((lib or self.lib).id)]

    def scan(self):
        return {l.id: l for l in photo_library.all_libraries(fresh=True)}

    def backup(self, disk='backup-disk'):
        self.db()(lambda c: c.execute('PRAGMA wal_checkpoint(TRUNCATE)'))
        copy = self.base / disk / 'photos'
        shutil.copytree(self.lib.path, copy)
        return copy

    def swap_in(self, copy):
        shutil.rmtree(self.lib.path)
        shutil.copytree(copy, self.lib.path)
        photo_db._initialised.discard(str(self.lib.db_path))


class VerifyTests(Disks):
    def test_intact_library_passes_and_records_when_it_was_checked(self):
        self.store(b'one'); self.store(b'two', 'b.jpg')
        self.assertEqual(self.verify(), 0, self.output)
        self.assertIn('2 originals verified', self.output)
        self.assertTrue(self.db()(lambda c: photo_db.get_meta(c, 'verified_at')))
        self.assertEqual(self.warnings(), [])

    def test_same_size_damage_is_found_and_reported_without_touching_anything(self):
        name = self.store(b'precious')['file']
        (self.lib.path / name).write_bytes(b'preciouX')
        self.assertEqual(self.verify(), 1)
        self.assertIn('DAMAGED', self.output)
        self.assertEqual((self.lib.path / name).read_bytes(), b'preciouX')
        self.assertFalse((self.lib.path / '.quarantine').exists())
        self.assertEqual(self.warnings(), ['verify'])
        ids = self.scan()
        page = TestClient(browse_photos.app).get('/libraries').text
        self.assertIn('data-library-warning="verify"', page)
        self.assertIn(self.lib.id, ids)

    def test_repair_quarantines_then_next_import_restores_with_organisation(self):
        data = b'precious'
        name = self.store(data)['file']

        def organise(conn):
            photo_db.set_notes(conn, sha(data), 'A #note', '')
            album = photo_collections.create(conn, 'album', 'Album')
            photo_collections.add_photos(conn, album, [sha(data)])
        self.db()(organise)
        (self.lib.path / name).write_bytes(b'preciouX')
        self.assertEqual(self.verify(repair=True), 1)
        self.assertIn('unrecovered', self.output)
        self.assertFalse((self.lib.path / name).exists())
        self.assertEqual((self.lib.path / '.quarantine' / name).read_bytes(), b'preciouX')
        self.assertEqual(self.db()(lambda c: c.execute('SELECT note1 FROM photos').fetchone()[0]), 'A #note')
        # The camera is read again: the same photo is put back, nothing else changes.
        self.assertEqual(self.store(data)['status'], 'repaired')
        self.assertEqual(self.verify(), 0, self.output)
        self.assertEqual(self.warnings(), [])
        self.assertEqual(self.db()(lambda c: c.execute('SELECT note1 FROM photos').fetchone()[0]), 'A #note')
        self.assertEqual(self.db()(lambda c: c.execute('SELECT COUNT(*) FROM album_photos').fetchone()[0]), 1)
        self.assertEqual((self.lib.path / '.quarantine' / name).read_bytes(), b'preciouX')

    def test_repair_recovers_from_a_stray_good_copy_in_the_same_library(self):
        data = b'precious'
        name = self.store(data)['file']
        (self.lib.path / name).write_bytes(b'preciouX')
        (self.lib.path / 'rescued copy.jpg').write_bytes(data)
        (self.lib.path / 'unrelated.txt').write_bytes(b'something else')
        self.assertEqual(self.verify(repair=True), 0, self.output)
        self.assertIn('recovered from rescued copy.jpg', self.output)
        self.assertEqual((self.lib.path / name).read_bytes(), data)
        self.assertFalse((self.lib.path / 'rescued copy.jpg').exists())
        self.assertEqual((self.lib.path / 'unrelated.txt').read_bytes(), b'something else')
        self.assertIn('STRAY FILE: unrelated.txt', self.output)
        self.assertEqual(self.verify(), 0, self.output)

    def test_missing_original_is_recovered_from_another_plugged_in_library(self):
        data = b'in two places'
        name = self.store(data)['file']
        other_name = self.store(data, lib=self.other, allow_duplicates=True)['file']
        (self.lib.path / name).unlink()
        self.assertEqual(self.verify(), 1)
        self.assertIn('MISSING', self.output)
        self.assertEqual(self.verify(repair=True), 0, self.output)
        self.assertIn("recovered from library 'Second'", self.output)
        self.assertEqual((self.lib.path / name).read_bytes(), data)
        self.assertEqual((self.other.path / other_name).read_bytes(), data)
        self.assertFalse(list(self.lib.path.glob('.part_*')))

    def test_a_damaged_copy_elsewhere_is_never_used_as_the_repair(self):
        data = b'in two places'
        name = self.store(data)['file']
        other_name = self.store(data, lib=self.other, allow_duplicates=True)['file']
        (self.lib.path / name).unlink()
        (self.other.path / other_name).write_bytes(b'in two placeX')
        self.assertEqual(self.verify(repair=True), 1)
        self.assertIn('unrecovered', self.output)
        self.assertFalse((self.lib.path / name).exists())
        self.assertFalse(list(self.lib.path.glob('.part_*')))
        self.assertEqual(self.verify(self.other), 1)

    def test_wrong_recorded_size_with_an_intact_file_corrects_the_record(self):
        data = b'right bytes'
        name = self.store(data)['file']
        self.db()(lambda c: c.execute('UPDATE photos SET size_bytes = 3'))
        self.assertEqual(self.verify(), 1)
        self.assertIn('WRONG SIZE', self.output)
        self.assertEqual(self.verify(repair=True), 0, self.output)
        self.assertEqual(self.db()(lambda c: c.execute('SELECT size_bytes FROM photos').fetchone()[0]), len(data))
        self.assertEqual((self.lib.path / name).read_bytes(), data)
        self.assertFalse((self.lib.path / '.quarantine').exists())

    def test_truncated_original_is_quarantined_not_overwritten(self):
        data = b'a longer original'
        name = self.store(data)['file']
        (self.lib.path / name).write_bytes(b'a long')
        self.assertEqual(self.verify(repair=True), 1)
        self.assertEqual((self.lib.path / '.quarantine' / name).read_bytes(), b'a long')
        # A second damaged file of the same name does not replace the first in quarantine.
        (self.lib.path / name).write_bytes(b'again')
        self.assertEqual(self.verify(repair=True), 1)
        self.assertEqual(len(list((self.lib.path / '.quarantine').iterdir())), 2)

    def test_damage_in_the_trash_is_reported(self):
        data = b'thrown away'
        name = self.store(data)['file']

        def delete(conn):
            photo_db.delete_photos(conn, [sha(data)])
        self.db()(delete)
        self.lib.trash_dir.mkdir(exist_ok=True)
        os.replace(self.lib.path / name, self.lib.trash_dir / name)
        self.assertEqual(self.verify(), 0, self.output)
        (self.lib.trash_dir / name).write_bytes(b'thrown awaX')
        self.assertEqual(self.verify(), 1)
        self.assertIn('DAMAGED IN TRASH', self.output)

    def test_database_and_marker_are_not_mistaken_for_strays(self):
        self.store(b'one')
        self.assertEqual(self.verify(), 0, self.output)
        self.assertNotIn('STRAY', self.output)

    @unittest.skipIf(os.geteuid() == 0, 'root ignores read-only folders')
    def test_read_only_library_can_be_verified_but_not_repaired(self):
        name = self.store(b'one')['file']
        (self.lib.path / name).write_bytes(b'onX')
        self.db()(lambda c: c.execute('PRAGMA wal_checkpoint(TRUNCATE)'))
        os.chmod(self.lib.path, 0o555)
        self.addCleanup(os.chmod, self.lib.path, 0o755)
        self.assertEqual(self.verify(), 1)
        self.assertIn('DAMAGED', self.output)
        self.assertEqual(self.verify(repair=True), 1)
        self.assertEqual((self.lib.path / name).read_bytes(), b'onX')

    def test_command_line_verify(self):
        self.store(b'one')
        out = io.StringIO()
        with patch('sys.argv', ['libraries', 'verify', 'First']), patch('sys.stdout', out):
            self.assertEqual(photo_library.main(), 0)
        self.assertIn('OK: 1 originals verified', out.getvalue())


class CopyTests(Disks):
    """A library folder that was copied, or put back from a backup."""

    def test_older_copy_with_fewer_photos_is_reported_once(self):
        self.store(b'one'); self.store(b'two', 'b.jpg')
        copy = self.backup()
        self.store(b'three', 'c.jpg')
        self.scan()
        self.assertEqual(self.warnings(), [])
        self.swap_in(copy)
        self.scan()
        self.assertEqual(sorted(self.warnings()), ['missing', 'older-copy'])
        detail = ' '.join(w['detail'] for w in photo_catalogue.warnings(self.lib.id))
        self.assertIn('1 item recorded earlier is no longer in this library', detail)
        self.assertIn('c.jpg', detail)
        self.scan(); self.scan()
        self.assertEqual(len(self.warnings()), 2)
        # The catalogue now describes what the disk really holds.
        self.assertEqual(photo_catalogue.item_counts()[self.lib.id], 2)
        self.assertEqual(photo_catalogue.holders([sha(b'three')], set()), {})
        out = io.StringIO()
        with patch('sys.argv', ['libraries', 'list']), patch('sys.stdout', out):
            photo_library.main()
        self.assertIn("WARNING 'First'", out.getvalue())
        with patch('sys.argv', ['libraries', 'acknowledge', 'First']), patch('sys.stdout', io.StringIO()):
            photo_library.main()
        self.scan()
        self.assertEqual(self.warnings(), [])

    def test_older_copy_differing_only_in_notes_and_albums_is_reported(self):
        self.store(b'one')
        self.scan()
        copy = self.backup()

        def organise(conn):
            photo_db.set_notes(conn, sha(b'one'), 'written later', '')
            photo_collections.create(conn, 'album', 'Later album')
        self.db()(organise)
        self.scan()
        self.assertEqual(self.warnings(), [])
        self.swap_in(copy)
        self.scan()
        self.assertEqual(self.warnings(), ['older-copy'])

    def test_deleting_restoring_and_editing_never_raise_a_warning(self):
        data = b'one'
        name = self.store(data)['file']
        self.store(b'two', 'b.jpg')
        self.scan()
        token = photo_library.use(self.lib)
        try:
            with photo_db.get_db() as conn:
                photo_db.delete_photos(conn, [sha(data)])
                self.lib.trash_dir.mkdir(exist_ok=True)
                os.replace(self.lib.path / name, self.lib.trash_dir / name)
                conn.commit()
                photo_catalogue.refresh(self.lib.id, conn, [sha(data)])
            self.scan()
            conn = photo_db.get_db()
            with patch.object(photo_trash, 'index_photo'):
                photo_trash.restore_one(self.lib, conn, sha(data))
            conn.close()
        finally:
            photo_library._current.reset(token)
        self.scan()
        self.db()(lambda c: photo_db.set_notes(c, sha(data), 'note', ''))
        self.scan()
        self.assertEqual(self.warnings(), [])
        self.assertEqual(self.verify(), 0, self.output)

    def test_two_copies_plugged_in_at_once_are_reported_and_only_one_is_used(self):
        self.store(b'one')
        copy = self.backup()
        with patch.object(photo_library, '_candidate_dirs', lambda known: [Path(p) for p in known] + [copy]):
            libs = self.scan()
            self.assertEqual(libs[self.lib.id].path, self.lib.path)
            self.assertEqual(libs[self.lib.id].copies, (str(copy),))
            self.assertEqual(libs[self.other.id].copies, ())
            out = io.StringIO()
            with patch('sys.argv', ['libraries', 'list']), patch('sys.stdout', out):
                photo_library.main()
            self.assertIn('another copy of this library is plugged in at ' + str(copy), out.getvalue())
            page = TestClient(browse_photos.app).get('/libraries').text
            self.assertIn('data-library-copy="' + self.lib.id, page)
            self.store(b'new', 'n.jpg')
        self.assertFalse(list(copy.glob('*n.jpg')))

    def test_newer_original_wins_over_an_older_copy_that_was_seen_alone(self):
        self.store(b'one')
        copy = self.backup()
        self.store(b'two', 'b.jpg')
        self.scan()
        original, away = self.lib.path, self.base / 'original-unplugged'
        both = lambda known: [Path(p) for p in known] + [copy, original]
        with patch.object(photo_library, '_candidate_dirs', both):
            original.rename(away)                      # only the older copy is plugged in
            self.assertEqual(self.scan()[self.lib.id].path, copy)
            self.assertEqual(sorted(self.warnings()), ['missing', 'older-copy'])
            away.rename(original)                      # the original returns beside it
            lib = self.scan()[self.lib.id]
            self.assertEqual(lib.path, original)
            self.assertEqual(lib.copies, (str(copy),))
            self.assertEqual(self.scan()[self.lib.id].path, original)
            self.store(b'three', 'c.jpg', lib=lib)
        self.assertTrue(list(original.glob('*c.jpg')))
        self.assertFalse(list(copy.glob('*c.jpg')))
        self.assertEqual(photo_catalogue.item_counts()[self.lib.id], 3)

    def test_the_same_folder_reached_by_two_paths_is_not_a_copy(self):
        link = self.base / 'link'
        link.symlink_to(self.lib.path)
        with patch.object(photo_library, '_candidate_dirs', lambda known: [Path(p) for p in known] + [link]):
            self.assertEqual(self.scan()[self.lib.id].copies, ())


class HoldTests(Disks):
    """A suspected older copy is read-only until acknowledged."""

    def older_copy_in_place(self):
        self.data = b'one'
        self.name = self.store(self.data)['file']
        copy = self.backup()
        self.store(b'two', 'b.jpg')
        self.scan()
        self.assertIsNone(photo_library.hold(self.lib.id))
        self.swap_in(copy)
        self.lib = self.scan()[self.lib.id]
        self.assertTrue(self.lib.hold)
        return copy

    def test_nothing_is_written_to_a_held_library_by_any_route(self):
        self.older_copy_in_place()
        before = {p.name: p.read_bytes() for p in self.lib.path.iterdir() if p.is_file() and p.suffix == '.jpg'}
        revision = self.db()(photo_db.revision)
        # storing (uploads and camera ingests end here)
        with self.assertRaises(photo_library.LibraryError) as error:
            self.store(b'new photo', 'n.jpg')
        self.assertIn('acknowledge', str(error.exception))
        (self.lib.path / '.part_test').unlink()        # this test's own temporary file
        # the receiver refuses uploads and says why
        import receiver
        client = TestClient(receiver.app)
        reply = client.put('/n.jpg', content=b'new photo', headers={'x-photo-library': self.lib.id})
        self.assertEqual(reply.status_code, 409, reply.text)
        self.assertIn('read-only for now', reply.text)
        self.assertEqual(client.post('/wanted', json={'hashes': ['a' * 64]},
                                     headers={'x-photo-library': self.lib.id}).status_code, 409)
        # the browser shows it but changes nothing
        web = TestClient(browse_photos.LibraryRouter(browse_photos.app))
        root = '/lib/' + self.lib.id
        self.assertEqual(web.get(root + '/').status_code, 200)
        self.assertEqual(web.post(root + '/photos/delete', data={'sha': [sha(self.data)]}).status_code, 409)
        page = web.get('/libraries').text
        self.assertIn('data-library-hold="' + self.lib.id, page)
        self.assertIn('Acknowledge and allow changes', page)
        # direct database writes fail too, whatever forgot to ask first
        import sqlite3
        with self.assertRaises(sqlite3.OperationalError):
            self.db()(lambda c: photo_db.set_notes(c, sha(self.data), 'sneaky', ''))
        # verify reads; repair is refused
        self.assertEqual(self.verify(), 0, self.output)
        (self.lib.path / self.name).write_bytes(b'onX')
        self.assertEqual(self.verify(repair=True), 1)
        self.assertEqual((self.lib.path / self.name).read_bytes(), b'onX')
        (self.lib.path / self.name).write_bytes(self.data)
        after = {p.name: p.read_bytes() for p in self.lib.path.iterdir() if p.is_file() and p.suffix == '.jpg'}
        self.assertEqual(after, before)
        self.assertEqual(self.db()(photo_db.revision), revision)
        self.assertFalse(list(self.lib.path.glob('.part_*')))
        self.assertFalse((self.lib.path / '.quarantine').exists())
        self.assertIsNone(self.db()(lambda c: photo_db.get_meta(c, 'verified_at')))

    def test_camera_ingest_into_a_held_library_copies_nothing(self):
        import contextlib, sys
        import ingest_camera
        self.older_copy_in_place()
        card = self.base / 'card' / 'DCIM'
        card.mkdir(parents=True)
        (card / 'DSC1.JPG').write_bytes(b'from the camera')
        out = io.StringIO()
        with patch.object(sys, 'argv', ['ingest_camera.py', str(card.parent), '--library', self.lib.id, '--no-sync']), \
             contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
            status = ingest_camera.main()
        self.assertEqual(status, 1, out.getvalue())
        self.assertIn('read-only for now', out.getvalue())
        self.assertIn('Nothing was copied', out.getvalue())
        self.assertEqual(self.db()(lambda c: c.execute('SELECT COUNT(*) FROM photos').fetchone()[0]), 1)

    def test_acknowledging_in_the_browser_or_command_line_allows_changes_again(self):
        self.older_copy_in_place()
        web = TestClient(browse_photos.LibraryRouter(browse_photos.app))
        reply = web.post(f'/libraries/{self.lib.id}/acknowledge', follow_redirects=False)
        self.assertEqual(reply.status_code, 303)
        self.assertIsNone(photo_library.hold(self.lib.id))
        self.assertEqual(self.warnings(), [])
        self.assertEqual(self.store(b'new photo', 'n.jpg')['status'], 'saved')
        self.scan(); self.scan()
        self.assertIsNone(photo_library.hold(self.lib.id))        # not raised again
        self.assertEqual(self.warnings(), [])
        self.db()(lambda c: photo_db.set_notes(c, sha(self.data), 'allowed', ''))
        # and from the command line
        copy = self.backup('second-backup')
        self.store(b'three', 'c.jpg'); self.scan()
        self.swap_in(copy); self.scan()
        self.assertTrue(photo_library.hold(self.lib.id))
        out = io.StringIO()
        with patch('sys.argv', ['libraries', 'list']), patch('sys.stdout', out):
            photo_library.main()
        self.assertIn('READ-ONLY until acknowledged', out.getvalue())
        out = io.StringIO()
        with patch('sys.argv', ['libraries', 'acknowledge', 'First']), patch('sys.stdout', out):
            photo_library.main()
        self.assertIn('can be written to again', out.getvalue())
        self.assertEqual(self.store(b'four', 'd.jpg')['status'], 'saved')

    def test_hold_lifts_by_itself_when_the_up_to_date_copy_returns(self):
        copy = self.older_copy_in_place()
        newer = self.base / 'newer'
        shutil.copytree(copy, newer)                 # stands in for the disk with the later changes
        token = photo_library.use(photo_library.Library(self.lib.id, 'First', newer, True))
        try:
            photo_library.clear_hold(self.lib.id)
            with photo_db.get_db() as conn:
                for i in range(40):
                    photo_db.set_notes(conn, sha(self.data), f'later change {i}', '')
        finally:
            photo_library._current.reset(token)
        photo_library.set_hold(self.lib.id, 'it is an older copy or a restored backup of the library.',
                               self.db()(photo_db.revision) + 5)
        self.scan()
        self.assertTrue(photo_library.hold(self.lib.id))           # still the older copy
        shutil.rmtree(self.lib.path)
        shutil.copytree(newer, self.lib.path)
        photo_db._initialised.discard(str(self.lib.db_path))
        self.scan()
        self.assertIsNone(photo_library.hold(self.lib.id))
        self.assertEqual(self.store(b'new photo', 'n.jpg')['status'], 'saved')

    def test_photos_vanishing_without_a_counter_drop_also_holds_until_acknowledged(self):
        self.store(b'one'); self.store(b'two', 'b.jpg')
        self.scan()
        # Rows removed behind the application's back (a damaged or tampered database).
        import sqlite3
        raw = sqlite3.connect(self.lib.db_path)
        raw.execute('DELETE FROM photos WHERE original_filename = ?', ('b.jpg',))
        raw.commit(); raw.close()
        self.scan()
        self.assertEqual(self.warnings(), ['missing'])
        held = photo_library.hold(self.lib.id)
        self.assertTrue(held)
        self.assertIsNone(held['expected'])
        self.scan(); self.scan()
        self.assertTrue(photo_library.hold(self.lib.id))           # only an acknowledgement lifts it
        photo_library.acknowledge(self.lib.id)
        self.assertEqual(self.store(b'three', 'c.jpg')['status'], 'saved')

    def test_command_line_tools_say_why_instead_of_failing_obscurely(self):
        import contextlib, sys
        import refresh_metadata, restore_deleted
        self.data = b'one'
        name = self.store(self.data)['file']
        self.store(b'two', 'b.jpg')

        def delete(conn):
            photo_db.delete_photos(conn, [sha(self.data)])
        self.db()(delete)
        self.lib.trash_dir.mkdir(exist_ok=True)
        os.replace(self.lib.path / name, self.lib.trash_dir / name)
        copy = self.backup()
        self.store(b'three', 'c.jpg'); self.scan()
        self.swap_in(copy); self.scan()
        self.assertTrue(photo_library.hold(self.lib.id))

        def run(main, *argv):
            out = io.StringIO()
            with patch.object(sys, 'argv', ['tool', *argv]), contextlib.redirect_stdout(out), \
                 contextlib.redirect_stderr(out):
                status = main()
            return status, out.getvalue()
        for main, argv in ((restore_deleted.main, ['--library', self.lib.id, sha(self.data)[:12]]),
                           (restore_deleted.main, ['--library', self.lib.id, '--purge', '0']),
                           (refresh_metadata.main, ['--library', self.lib.id, '--all']),
                           (photo_library.main, ['rename', self.lib.id, 'Another name'])):
            status, text = run(main, *argv)
            self.assertEqual(status, 1, text)
            self.assertIn('read-only for now', text)
        self.assertTrue((self.lib.trash_dir / name).is_file())
        self.assertEqual(photo_library.read_marker(self.lib.path)['name'], 'First')
        status, text = run(restore_deleted.main, '--library', self.lib.id)      # listing still works
        self.assertEqual(status, 0, text)
        self.assertIn('1 deleted item(s)', text)

    def test_receiver_lists_a_held_library_as_read_only(self):
        import receiver
        self.older_copy_in_place()
        libs = {l['id']: l for l in TestClient(receiver.app).get('/libraries').json()['libraries']}
        self.assertIn('older copy', libs[self.lib.id]['read_only'])
        self.assertIsNone(libs[self.other.id]['read_only'])

    def test_library_pages_carry_a_banner_while_held(self):
        self.older_copy_in_place()
        web = TestClient(browse_photos.LibraryRouter(browse_photos.app))
        for path in ('/', '/albums', '/trash', '/tags'):
            self.assertIn('data-library-hold="' + self.lib.id, web.get(f'/lib/{self.lib.id}{path}').text, path)
        self.assertNotIn('data-library-hold', web.get(f'/lib/{self.other.id}/').text)

    def test_other_libraries_and_normal_use_are_never_held(self):
        self.older_copy_in_place()
        self.assertIsNone(photo_library.hold(self.other.id))
        self.assertEqual(self.store(b'elsewhere', 'e.jpg', lib=self.other)['status'], 'saved')
        self.scan()
        self.assertIsNone(photo_library.hold(self.other.id))


class PulledDiskTests(Disks):
    """The disk goes away while pages and uploads are using the library."""

    def setUp(self):
        super().setUp()
        self.digest = sha(b'one')
        self.store(b'one')
        self.scan()
        self.lib.path.rename(self.base / 'pulled')       # the last scan still lists it for a few seconds

    def test_browser_refuses_changes_visibly_and_sends_page_loads_to_the_library_list(self):
        web = TestClient(browse_photos.LibraryRouter(browse_photos.app))
        root = '/lib/' + self.lib.id
        reply = web.post(f'{root}/photo/{self.digest}/notes', data={'note1': 'x', 'note2': ''}, follow_redirects=False)
        self.assertEqual(reply.status_code, 409)
        self.assertIn('disconnected', reply.text)
        reply = web.get(root + '/', follow_redirects=False)
        self.assertEqual(reply.status_code, 302)
        self.assertIn('/libraries?missing=', reply.headers['location'])
        reply = web.get(root + '/grid', headers={'HX-Request': 'true'}, follow_redirects=False)
        self.assertEqual(reply.status_code, 200)
        self.assertIn('/libraries?missing=', reply.headers['hx-redirect'])
        self.assertEqual(reply.text, '')
        self.assertFalse(self.lib.path.exists())

    def test_receiver_answers_with_the_reason_not_a_server_error(self):
        import receiver
        client = TestClient(receiver.app, raise_server_exceptions=False)
        headers = {'x-photo-library': self.lib.id}
        for reply in (client.put('/n.jpg', content=b'new', headers=headers),
                      client.post('/wanted', json={'hashes': [self.digest]}, headers=headers)):
            self.assertEqual(reply.status_code, 503, reply.text)
            self.assertIn('disconnected', reply.json()['detail'])
        self.assertFalse(self.lib.path.exists())


class CrossFeatureTests(Disks):
    def test_restore_never_lands_in_a_new_album_that_reused_the_old_number(self):
        data = b'one'
        name = self.store(data)['file']
        self.store(b'two', 'b.jpg')

        def organise(conn):
            keep = photo_collections.create(conn, 'album', 'Keep')
            gone = photo_collections.create(conn, 'album', 'Holiday')
            for album in (keep, gone):
                photo_collections.add_photos(conn, album, [sha(data)])
                photo_collections.set_cover(conn, album, sha(data))
            photo_db.delete_photos(conn, [sha(data)])
            photo_collections.delete(conn, gone)
            return keep, gone, photo_collections.create(conn, 'album', 'Work')
        keep, gone, work = self.db()(organise)
        self.assertEqual(work, gone)                    # the number was given out again
        self.lib.trash_dir.mkdir(exist_ok=True)
        os.replace(self.lib.path / name, self.lib.trash_dir / name)
        token = photo_library.use(self.lib)
        try:
            conn = photo_db.get_db()
            with patch.object(photo_trash, 'index_photo'):
                result = photo_trash.restore_one(self.lib, conn, sha(data))
            conn.close()
        finally:
            photo_library._current.reset(token)
        self.assertEqual(result['albums'], 1)
        members = self.db()(lambda c: {r[0] for r in c.execute('SELECT album_id FROM album_photos')})
        self.assertEqual(members, {keep})
        covers = self.db()(lambda c: dict(c.execute('SELECT id, cover_sha FROM collections').fetchall()))
        self.assertEqual(covers, {keep: sha(data), work: None})

    def test_restore_of_a_record_made_by_an_older_version_still_returns_to_its_albums(self):
        import json
        data = b'one'
        name = self.store(data)['file']

        def organise(conn):
            album = photo_collections.create(conn, 'album', 'Album')
            photo_collections.add_photos(conn, album, [sha(data)])
            photo_db.delete_photos(conn, [sha(data)])
            details = json.loads(conn.execute('SELECT details FROM deleted_photos').fetchone()[0])
            details.pop('album_created')
            conn.execute('UPDATE deleted_photos SET details = ?', (json.dumps(details),))
            return photo_db.restore_photo(conn, sha(data), len(data))
        self.assertEqual(self.db()(organise)['albums'], 1)

    def test_tag_search_agrees_with_how_tags_are_stored_and_words_find_grid_tags(self):
        self.store(b'one'); self.store(b'two', 'b.jpg'); self.store(b'three', 'c.jpg')

        def organise(conn):
            photo_db.set_notes(conn, sha(b'one'), 'at the #Beach-', '')
            photo_db.edit_hashtags(conn, [sha(b'two')], ['beach'], 'add')
            album = photo_collections.create(conn, 'album', 'Trip', note='#Beach_ days')
            return ([len(photo_db.search(conn, q)) for q in ('#beach', '#Beach-', '#BEACH_', 'beach', '#bea')],
                    [len(photo_collections.search(conn, q)) for q in ('#beach', '#Beach-', 'beach')])
        photos, albums = self.db()(organise)
        self.assertEqual(photos, [2, 2, 2, 2, 0])
        self.assertEqual(albums, [1, 1, 1])

    def test_absurd_numbers_are_not_found_rather_than_a_server_error(self):
        web = TestClient(browse_photos.LibraryRouter(browse_photos.app), raise_server_exceptions=False)
        root, big = '/lib/' + self.lib.id, '9' * 30
        for method, path in (('GET', f'/albums/{big}'), ('GET', f'/grid?album={big}&page={big}'),
                             ('GET', f'/collections/{big}/edit'), ('POST', f'/albums/{big}/add'),
                             ('DELETE', f'/collections/{big}')):
            self.assertEqual(web.request(method, root + path).status_code, 404, path)

    def test_very_long_upload_names_are_shortened_not_refused(self):
        for name in ('a' * 300 + '.jpg', 'é' * 200 + '.JPG', 'x' * 400):
            data = name.encode()
            result = self.store(data, name)
            self.assertEqual(result['status'], 'saved', result)
            self.assertLessEqual(len(result['file'].encode()), 255)
            self.assertEqual((self.lib.path / result['file']).read_bytes(), data)
        self.assertEqual(photo_store.stored_name('normal name.jpg'), 'normal name.jpg')
        self.assertEqual(Path(photo_store.stored_name('a' * 300 + '.jpg')).suffix, '.jpg')
        self.assertEqual(self.verify(), 0, self.output)

    def test_repair_discards_previews_made_from_the_damaged_file(self):
        data = b'precious'
        name = self.store(data)['file']
        self.lib.cache_dir.mkdir(exist_ok=True)
        stale = self.lib.cache_dir / f'{sha(data)}_400.jpg'
        stale.write_bytes(b'preview of damage')
        keep = self.lib.cache_dir / f'{sha(b"other")}_400.jpg'
        keep.write_bytes(b'unrelated')
        (self.lib.path / name).write_bytes(b'preciouX')
        self.verify(repair=True)
        self.assertFalse(stale.exists())
        self.assertTrue(keep.exists())

    def test_an_unrecoverable_original_on_an_unplugged_disk_does_not_block_the_photo_elsewhere(self):
        lost, kept = b'lost on the other disk', b'still fine there'
        name = self.store(lost, lib=self.other)['file']
        self.store(kept, 'k.jpg', lib=self.other)
        (self.other.path / name).write_bytes(b'lost on the other disX')
        self.assertEqual(self.verify(self.other, repair=True), 1)
        self.other.path.rename(self.base / 'unplugged')
        self.scan()
        self.assertFalse(photo_library.find(self.other.id).online)
        self.assertEqual(self.store(kept, 'k.jpg')['status'], 'elsewhere_skipped')
        self.assertEqual(self.store(lost, 'l.jpg')['status'], 'saved')        # the only good copy is kept
        # Back on its disk, importing it again repairs it and it counts once more.
        (self.base / 'unplugged').rename(self.other.path)
        self.scan()
        self.assertEqual(self.store(lost, lib=self.other, allow_duplicates=True)['status'], 'repaired')
        self.assertEqual(self.verify(self.other), 0, self.output)
        self.other.path.rename(self.base / 'unplugged')
        self.scan()
        self.assertEqual(photo_catalogue.holders([sha(lost)], {self.lib.id}), {sha(lost): self.other.id})


class SmallThingsTests(unittest.TestCase):
    def test_command_line_dates_are_local_like_the_browser(self):
        import time
        from web_templates import nice_date
        stamp = '2026-11-13T22:53:44+00:00'
        with patch.dict(os.environ, {'TZ': 'Australia/Melbourne'}):
            time.tzset()
            try:
                self.assertEqual(photo_library.local_time(stamp), '2026-11-14')
                self.assertEqual(photo_library.local_time(stamp, seconds=True), '2026-11-14 09:53:44')
                self.assertIn('14 Nov 2026', nice_date(stamp))
            finally:
                os.environ.pop('TZ', None)
        time.tzset()
        self.assertEqual(photo_library.local_time(None), '')
        self.assertEqual(photo_library.local_time('not a date'), 'not a date')

    def test_library_id_is_recognised_whatever_its_case(self):
        base = isolate(self)
        lib = photo_library.create(base / 'x', 'Some Name')
        self.assertEqual(photo_library.find(lib.id.upper()).id, lib.id)
        self.assertEqual(photo_library.find('some name').id, lib.id)

    def test_send_camera_reports_what_the_server_said(self):
        import urllib.error
        import send_camera
        error = urllib.error.HTTPError('u', 409, 'Conflict', {}, io.BytesIO(b'{"detail": "library is read-only for now"}'))
        self.assertEqual(send_camera.refusal(error), 'library is read-only for now')
        error = urllib.error.HTTPError('u', 502, 'Bad Gateway', {}, io.BytesIO(b'<html>'))
        self.assertEqual(send_camera.refusal(error), 'HTTP 502 Bad Gateway')


class CatalogueTests(Disks):
    def test_a_change_whose_catalogue_update_was_lost_is_caught_up(self):
        self.scan()
        with patch.object(photo_catalogue, 'refresh'):        # the process died before it ran
            self.store(b'one')
        self.assertEqual(photo_catalogue.holders([sha(b'one')], set()), {})
        self.scan()
        self.assertEqual(photo_catalogue.holders([sha(b'one')], set()), {sha(b'one'): self.lib.id})
        self.assertEqual(self.warnings(), [])

    def test_duplicate_is_still_refused_just_after_the_other_disk_is_pulled(self):
        data = b'only once'
        self.store(data, lib=self.other)
        self.scan()
        # The disk goes away, but for a few seconds the last scan still lists it as plugged in.
        self.other.path.rename(self.base / 'pulled')
        self.assertTrue(photo_library.find(self.other.id).online)
        result = self.store(data)
        self.assertEqual(result['status'], 'elsewhere_skipped', result)
        self.assertIn('Second', result['library'])
        self.assertEqual(self.db()(lambda c: c.execute('SELECT COUNT(*) FROM photos').fetchone()[0]), 0)
        self.assertFalse(list(self.lib.path.glob('.part_*')))
        self.assertFalse((self.other.path).exists())     # nothing was created in its place

    def test_an_empty_library_counts_zero_and_an_unrecorded_one_is_unknown(self):
        counts = photo_catalogue.item_counts()
        self.assertEqual(counts.get(self.lib.id), 0)
        self.assertIsNone(counts.get('0badc0de'))

    def test_forgetting_a_library_drops_its_state_and_warnings(self):
        with photo_catalogue.connect() as cat:
            photo_catalogue.warn(cat, self.lib.id, 'verify', 'something')
        photo_catalogue.forget(self.lib.id)
        self.assertEqual(self.warnings(), [])
        self.assertNotIn(self.lib.id, photo_catalogue.item_counts())

    def test_change_counter_rises_with_every_kind_of_change(self):
        seen = [self.db()(photo_db.revision)]

        def step(change):
            self.db()(change)
            seen.append(self.db()(photo_db.revision))
        self.store(b'one'); seen.append(self.db()(photo_db.revision))
        step(lambda c: photo_db.set_notes(c, sha(b'one'), 'a #tag', ''))
        step(lambda c: photo_collections.create(c, 'album', 'Album'))
        step(lambda c: photo_db.delete_photos(c, [sha(b'one')]))
        self.assertEqual(seen, sorted(set(seen)), seen)
        before = seen[-1]
        self.db()(lambda c: photo_db.set_meta(c, 'verified_at', 'now'))
        self.assertEqual(self.db()(photo_db.revision), before)

    def test_marker_is_replaced_whole(self):
        marker = photo_library.read_marker(self.lib.path)
        marker['name'] = 'Renamed'
        photo_library.save_marker(self.lib.path, marker)
        self.assertEqual(photo_library.read_marker(self.lib.path)['name'], 'Renamed')
        self.assertFalse((self.lib.path / 'library.json.tmp').exists())
        with patch.object(os, 'replace', side_effect=OSError('disk pulled')):
            with self.assertRaises(OSError):
                photo_library.save_marker(self.lib.path, {**marker, 'name': 'Half'})
        self.assertEqual(photo_library.read_marker(self.lib.path)['name'], 'Renamed')
        self.store(b'one')
        self.assertEqual(self.verify(), 0, self.output)
        self.assertNotIn('STRAY', self.output)

    def test_every_stored_original_is_flushed_before_it_is_recorded(self):
        flushed = []
        real = photo_store.fsync_path
        with patch.object(photo_store, 'fsync_path', side_effect=lambda p: (flushed.append(Path(p)), real(p))):
            self.store(b'one')
        self.assertEqual(flushed[0].name, '.part_test')
        self.assertIn(self.lib.path, flushed)


class RemoteTests(TwoMachines):
    """The same checks when the library is on another machine and reports over the network."""

    def server_warnings(self):
        back = self.on('server')
        try:
            return photo_catalogue.warnings(self.trip.id)
        finally:
            self.on(back)

    def test_older_copy_synced_from_the_laptop_is_reported_on_both_machines(self):
        token = photo_library.use(self.trip)
        with photo_db.get_db() as conn:
            conn.execute('PRAGMA wal_checkpoint(TRUNCATE)')
        photo_library._current.reset(token)
        copy = self.base / 'backup' / 'trip'
        shutil.copytree(self.trip.path, copy)
        self.assertEqual(self.ingest(), 0, self.output)
        self.assertEqual(self.server_warnings(), [])
        self.assertEqual(self.ingest('sync', source=False), 0, self.output)
        self.assertNotIn('WARNING', self.output)
        shutil.rmtree(self.trip.path)
        shutil.copytree(copy, self.trip.path)
        photo_db._initialised.discard(str(self.trip.db_path))
        self.assertEqual(self.ingest('sync', source=False), 0, self.output)
        self.assertIn("WARNING 'Trip'", self.output)
        self.assertIn('3 items recorded earlier are no longer in this library', self.output)
        kinds = sorted(w['kind'] for w in self.server_warnings())
        self.assertEqual(kinds, ['missing', 'older-copy'])
        # On the laptop the copy is read-only; the server, which holds no files, is not affected.
        self.assertTrue(photo_library.hold(self.trip.id))
        self.assertIsNone(self.server_library().hold)
        self.assertEqual(self.ingest(), 1)
        self.assertIn('Nothing was copied', self.output)
        self.assertEqual(self.rows(), {})
        self.assertEqual(self.catalogue(), {})
        # Reported once, not on every later sync.
        self.assertEqual(self.ingest('sync', source=False), 0, self.output)
        self.assertEqual(len(self.server_warnings()), 2)

    def test_normal_ingest_delete_and_resync_raise_no_warning(self):
        self.assertEqual(self.ingest(), 0, self.output)
        digest = next(iter(self.rows()))
        token = photo_library.use(self.trip)
        try:
            with photo_db.get_db() as conn:
                photo_db.delete_photos(conn, [digest])
                conn.commit()
                photo_catalogue.refresh(self.trip.id, conn, [digest])
        finally:
            photo_library._current.reset(token)
        self.assertEqual(self.ingest('sync', source=False), 0, self.output)
        self.assertNotIn('WARNING', self.output)
        self.assertEqual(self.server_warnings(), [])
        self.assertEqual(len(self.catalogue()), 2)

    def test_an_abandoned_transfer_is_shown_as_stalled_not_in_progress(self):
        self.assertEqual(self.ingest(), 0, self.output)
        back = self.on('server')
        try:
            photo_catalogue.begin_sync(self.trip.id, {'name': 'Trip', 'remote': {'items': 9, 'deleted': 0}}, 'f' * 64)
            self.assertEqual(photo_catalogue.pending_syncs()[self.trip.id]['state'], 'syncing')
            with patch.object(photo_catalogue, 'STALLED_AFTER', 0):
                self.assertEqual(photo_catalogue.pending_syncs()[self.trip.id]['state'], 'stalled')
                page = TestClient(browse_photos.app).get('/libraries').text
            self.assertIn('never finished', page)
        finally:
            self.on(back)
        self.assertEqual(len(self.catalogue()), 3)


if __name__ == '__main__':
    unittest.main()
