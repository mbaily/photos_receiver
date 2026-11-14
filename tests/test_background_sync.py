"""Durable progress, retries and browser scheduling without launching real services."""
import json
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

import browse_photos
import ingest_camera
import photo_catalogue
import photo_db
import photo_library
import photo_sync
from test_ingest_camera import TwoMachines, sha


class BackgroundSyncTests(TwoMachines):
    def setUp(self):
        super().setUp()
        photo_sync.configure('https://photo-server', None)
        self.scheduler_patch = patch.object(photo_sync, 'schedule')
        self.scheduler = self.patches.enter_context(self.scheduler_patch)

    def test_catalogue_mutation_marks_pending_and_requests_nonblocking_sync(self):
        photo_library.use(self.trip)
        with photo_db.get_db() as conn:
            photo_db.insert_photo(conn, sha(b'one'), 'one.jpg', 'one.jpg', 3, {})
            conn.commit()
            photo_catalogue.refresh(self.trip.id, conn, [sha(b'one')])
        self.assertEqual(photo_sync.statuses()[self.trip.id]['state'], 'pending')
        self.scheduler.assert_called_once()

    def test_success_failure_and_offline_retry_preserve_last_success(self):
        self.assertEqual(self.ingest(), 0, self.output)
        first = photo_sync.statuses()[self.trip.id]
        self.assertEqual(first['state'], 'synced')
        self.network = False
        self.assertEqual(self.ingest('sync', source=False), 0)
        state = photo_sync.statuses()[self.trip.id]
        self.assertEqual(state['state'], 'failed')
        self.assertTrue(state['error'])
        self.assertEqual(state['last_success'], first['last_success'])
        self.network = True
        self.assertEqual(self.ingest('sync', source=False), 0)
        self.assertEqual(photo_sync.statuses()[self.trip.id]['state'], 'synced')

    def test_change_during_transfer_stays_pending_after_older_snapshot_succeeds(self):
        generation = photo_sync.begin(self.trip.id)
        photo_sync.changed(self.trip.id, launch=False)
        photo_sync.finish(self.trip.id, generation, success=True)
        state = photo_sync.statuses()[self.trip.id]
        self.assertEqual(state['state'], 'pending')
        self.assertTrue(state['last_success'])

    def test_no_sync_does_not_launch_a_background_request(self):
        # schedule() itself checks the defer context; mock only the external process.
        self.scheduler_patch.stop()
        with patch('photo_sync.subprocess.run') as process:
            with photo_sync.defer():
                photo_sync.changed(self.trip.id)
        process.assert_not_called()
        self.assertEqual(photo_sync.statuses()[self.trip.id]['state'], 'pending')
        self.assertEqual(self.ingest('--no-sync'), 0)
        self.assertFalse(self.requests)

    def test_killed_worker_is_shown_as_pending_and_manual_sync_is_serialized(self):
        photo_sync.begin(self.trip.id)
        self.assertEqual(photo_sync.view(self.trip)['state'], 'pending')
        with photo_sync.run_lock() as acquired:
            self.assertTrue(acquired)
            self.assertEqual(photo_sync.view(self.trip)['state'], 'syncing')
            self.assertEqual(self.ingest('sync', source=False), 0)
            self.assertIn('already running', self.output)
            self.assertFalse(self.requests)

    def test_browser_shows_progress_and_sync_button_uses_public_prefix(self):
        photo_sync.changed(self.trip.id, launch=False)
        client = TestClient(browse_photos.LibraryRouter(browse_photos.app))
        reply = client.get('/libraries', headers={'X-Forwarded-Prefix': '/photos'})
        self.assertIn('Pending sync', reply.text)
        self.assertIn('action="/photos/sync"', reply.text)
        reply = client.post('/sync', headers={'X-Forwarded-Prefix': '/photos'}, follow_redirects=False)
        self.assertEqual(reply.status_code, 303)
        self.assertEqual(reply.headers['location'], '/photos/libraries?sync_requested=1')
        self.scheduler.assert_called_once()
        self.on('unconfigured')
        self.assertEqual(client.post('/sync').status_code, 409)

    def test_progress_file_contains_no_token_and_has_private_permissions(self):
        photo_sync.begin(self.trip.id)
        path = photo_library.REGISTRY.parent / 'sync-state.json'
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        self.assertNotIn('token', path.read_text())

    def test_sync_command_is_not_shadowed_by_a_directory_named_sync(self):
        import contextlib
        (self.base / 'sync').mkdir()
        with contextlib.chdir(self.base):
            self.assertEqual(self.ingest('sync', source=False), 0, self.output)
        self.assertEqual(photo_sync.statuses()[self.trip.id]['state'], 'synced')
