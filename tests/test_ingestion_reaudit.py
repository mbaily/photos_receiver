"""Focused parity regressions for both command-line ingestion destinations."""
import os
from pathlib import Path
from unittest.mock import patch

import photo_db
import photo_library
import send_camera
from test_ingest_camera import TwoMachines, sha


class IngestionReauditTests(TwoMachines):
    def test_changed_same_size_mtime_source_is_not_silently_skipped(self):
        self.assertEqual(self.ingest('--no-sync'), 0)
        source = self.card / 'DCIM/100MSDCF/DSC0001.JPG'
        stat = source.stat()
        source.write_bytes(b'PHOTO ONE')
        os.utime(source, ns=(stat.st_atime_ns, stat.st_mtime_ns))
        self.assertEqual(self.ingest('--no-sync'), 0)
        self.assertIn(sha(b'PHOTO ONE'), self.rows())
        self.assertIn(sha(b'photo one'), self.rows())

    def test_malformed_sidecar_date_does_not_block_valid_original(self):
        (self.card / 'PRIVATE/M4ROOT/CLIP/C0001M01.XML').write_text(
            '<NonRealTimeMeta><CreationDate value="2026-99-99T10:11:12"/></NonRealTimeMeta>')
        self.assertEqual(self.ingest('--no-sync'), 0, self.output)
        self.assertEqual(len(self.rows()), 3)

    def test_missing_local_original_is_recovered_without_losing_notes_or_tags(self):
        self.ingest('--no-sync')
        digest = sha(b'photo one');row = self.rows()[digest]
        token = photo_library.use(self.trip)
        try:
            with photo_db.get_db() as conn:
                photo_db.set_notes(conn, digest, 'Keep #note', 'Second')
                photo_db.edit_hashtags(conn, [digest], ['keeper'], 'add')
        finally:
            photo_library._current.reset(token)
        (self.trip.path / row['saved_filename']).unlink()
        self.assertEqual(self.ingest('--no-sync'), 0, self.output)
        after = self.rows()[digest]
        self.assertEqual((self.trip.path / after['saved_filename']).read_bytes(), b'photo one')
        self.assertEqual((after['note1'], after['note2'], after['hashtags']), ('Keep #note', 'Second', '#keeper'))

    def test_dry_run_does_not_claim_duplicates_were_ingested(self):
        self.ingest('--no-sync')
        other = photo_library.create(self.base / 'laptop-disk/other', 'Other')
        self.assertEqual(self.ingest('--library', 'Other', '--allow-duplicates', '--dry-run', '--no-sync'), 0)
        self.assertEqual(self.rows(other), {})
        self.assertNotIn('ingested into', self.output)

    def test_dry_run_reports_only_a_missing_sidecar_repair(self):
        self.ingest('--no-sync')
        token = photo_library.use(self.trip)
        try:
            with photo_db.get_db() as conn:
                conn.execute('UPDATE photos SET sidecar_xml=NULL WHERE sha256=?', (sha(b'a video clip'),))
        finally:
            photo_library._current.reset(token)
        self.assertEqual(self.ingest('--dry-run', '--no-sync'), 0)
        self.assertIn('would repair sidecar', self.output)
        self.assertIsNone(self.rows()[sha(b'a video clip')]['sidecar_xml'])


import contextlib
import hashlib
import io
import sys
import tempfile
import unittest
import test_send_camera_gphoto as sender_fixtures


class StandaloneReauditTests(unittest.TestCase):
    setUp = sender_fixtures.CameraSyncTests.setUp
    sync = sender_fixtures.CameraSyncTests.sync
    command = sender_fixtures.CameraSyncTests.command

    def test_mounted_upload_not_skipped_after_same_size_mtime_source_change(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            for remote, data in self.files.items():
                local = root / remote.removeprefix('/store_00010001/')
                local.parent.mkdir(parents=True, exist_ok=True);local.write_bytes(data)
            with patch.object(send_camera, 'CACHE_FILE', root / 'cache.json'):
                self.assertEqual(self.sync(str(root)), 0)
                self.known = {hashlib.sha256(data).hexdigest() for data in self.files.values()}
                self.uploads.clear()
                remote = '/store_00010001/DCIM/100___09/IMG_0002.JPG'
                local = root / remote.removeprefix('/store_00010001/');before = local.stat()
                self.files[remote] = b'NEW PHOTO ONE';local.write_bytes(self.files[remote])
                os.utime(local, ns=(before.st_atime_ns, before.st_mtime_ns))
                self.assertEqual(self.sync(str(root)), 0)
                self.assertEqual([data for _, data in self.uploads], [b'NEW PHOTO ONE'])

    def test_sidecar_invalid_date_falls_back_to_file_time(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / 'C0001.MP4';path.write_bytes(b'video')
            path.with_name('C0001M01.XML').write_text('<CreationDate value="2026-99-99T10:11:12"/>')
            self.assertEqual(send_camera.capture_time(path), send_camera.datetime.fromtimestamp(path.stat().st_mtime))

    def test_sender_reports_actual_receiver_duplicate_decision(self):
        class Receiver:
            def __init__(self, *a, **kw):pass
            def libraries(self):return None
            def wanted(self, hashes, sidecars):return set(hashes), set(), {h:'Other library' for h in hashes}
        with tempfile.TemporaryDirectory() as d:
            root = Path(d);(root / 'DCIM').mkdir();(root / 'DCIM/photo.jpg').write_bytes(b'photo')
            out = io.StringIO()
            with patch.object(send_camera, 'Server', Receiver), patch.object(send_camera, 'CACHE_FILE', root / 'cache.json'), \
                 patch.object(sys, 'argv', ['send_camera.py', str(root), '--dry-run']), contextlib.redirect_stdout(out):
                self.assertEqual(send_camera.main(), 0)
            self.assertIn('would send', out.getvalue())
            self.assertNotIn('and were skipped', out.getvalue())


    def test_standalone_sender_supplies_expected_content_hash(self):
        import json
        with tempfile.TemporaryDirectory() as d:
            root=Path(d);path=root/'photo.jpg';path.write_bytes(b'photo')
            digest=sha(b'photo');captured=[]
            def respond(req,**kwargs):
                captured.append(req)
                return io.BytesIO(json.dumps({'status':'saved','sha256':digest}).encode())
            server=send_camera.Server('http://example',None)
            with patch.object(send_camera.urllib.request,'urlopen',side_effect=respond):
                server.upload(path,digest,root)
            self.assertEqual(captured[0].get_header('X-photo-sha256'),digest)


class ServerRepairReauditTests(TwoMachines):
    def setUp(self):
        super().setUp()
        self.on('server');self.main=photo_library.create(self.base/'server-disk/main','Main');self.on('laptop')

    def upload(self, data):
        back=self.on('server');token=photo_library._current.set(None)
        try:return self.client.put('/photo.jpg',content=data).json()
        finally:photo_library._current.reset(token);self.on(back)

    def wanted(self,digest):return self.call('POST','/wanted',{'hashes':[digest]})['wanted']

    def test_server_recovers_missing_original_and_keeps_organisation(self):
        data=b'photo one';digest=sha(data);first=self.upload(data)
        back=self.on('server');token=photo_library.use(self.main)
        try:
            with photo_db.get_db() as conn:
                photo_db.set_notes(conn,digest,'Keep #note','Second')
                import photo_collections
                album=photo_collections.create(conn,'album','Kept')
                photo_collections.add_photos(conn,album,[digest])
            (self.main.path/first['file']).unlink()
        finally:photo_library._current.reset(token);self.on(back)
        self.assertEqual(self.wanted(digest),[digest])
        repaired=self.upload(data)
        self.assertEqual(repaired['status'],'repaired')
        self.assertEqual((self.main.path/repaired['file']).read_bytes(),data)
        row=self.rows(self.main)[digest]
        self.assertEqual((row['note1'],row['note2']),('Keep #note','Second'))
        token=photo_library.use(self.main)
        try:
            with photo_db.get_db() as conn:
                self.assertEqual(conn.execute('SELECT count(*) FROM album_photos WHERE sha256=?',(digest,)).fetchone()[0],1)
        finally:photo_library._current.reset(token)

    def test_wrong_sized_original_is_preserved_while_correct_original_is_recovered(self):
        data=b'photo one';digest=sha(data);first=self.upload(data)
        damaged=self.main.path/first['file'];damaged.write_bytes(b'bad')
        self.assertEqual(self.wanted(digest),[digest])
        repaired=self.upload(data)
        self.assertEqual(repaired['status'],'repaired')
        self.assertNotEqual(first['file'],repaired['file'])
        self.assertEqual(damaged.read_bytes(),b'bad')
        self.assertEqual((self.main.path/repaired['file']).read_bytes(),data)
        self.assertEqual(self.wanted(digest),[])

    def test_receiver_rejects_changed_bytes_before_storing_any_row(self):
        back=self.on('server');token=photo_library._current.set(None)
        try:
            reply=self.client.put('/wrong.jpg',content=b'changed',headers={'X-Photo-SHA256':sha(b'original')})
            self.assertEqual(reply.status_code,409)
            self.assertIn('changed after hashing',reply.text)
            photo_library.use(self.main)
            with photo_db.get_db() as conn:
                self.assertEqual(conn.execute('SELECT count(*) FROM photos').fetchone()[0],0)
            self.assertFalse(list(self.main.path.glob('.part_*')))
        finally:photo_library._current.reset(token);self.on(back)
