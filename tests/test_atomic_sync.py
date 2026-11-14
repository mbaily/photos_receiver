"""An interrupted or competing transfer must never change the published catalogue."""
import copy
import urllib.error

import photo_catalogue
from test_ingest_camera import TwoMachines


class AtomicSyncTests(TwoMachines):
    def setUp(self):
        super().setUp()
        self.ingest()
        self.old_rows = self.catalogue()
        self.old_info = copy.deepcopy(self.server_library().remote)

    def begin(self, rows, deleted=()):
        stamps = {sha: photo_catalogue.row_stamp(r) for sha, r in rows.items()}
        fp = photo_catalogue.fingerprint(stamps, deleted)
        reply = self.call('PUT', f'/remote/libraries/{self.trip.id}', {
            'name': 'Renamed Trip', 'host': 'laptop', 'path': '/new/path',
            'items': len(rows), 'deleted': len(deleted), 'fingerprint': fp})
        return stamps, fp, reply['sync_id']

    def changed(self):
        rows = copy.deepcopy(self.old_rows)
        for row in rows.values(): row['camera'] = 'New camera'
        return rows

    def test_partial_same_count_metadata_update_keeps_old_rows_and_timestamp(self):
        rows = self.changed()
        stamps, fp, token = self.begin(rows)
        reply = self.call('POST', f'/remote/libraries/{self.trip.id}/hashes', {'photos': stamps, 'sync_id': token})
        self.assertEqual(len(reply['need']), 3)
        one = [next(iter(rows.values()))]
        self.call('POST', f'/remote/libraries/{self.trip.id}/rows', {'rows': one, 'sync_id': token})
        self.assertEqual(self.catalogue(), self.old_rows)
        self.assertEqual(self.server_library().remote, self.old_info)
        with self.assertRaises(urllib.error.HTTPError) as error:
            self.call('POST', f'/remote/libraries/{self.trip.id}/commit', {'sync_id': token, 'fingerprint': fp})
        self.assertEqual(error.exception.code, 409)
        self.assertEqual(self.catalogue(), self.old_rows)
        reply = self.call('POST', f'/remote/libraries/{self.trip.id}/rows', {'rows': list(rows.values()), 'sync_id': token})
        self.assertTrue(reply['committed'])
        self.call('POST', f'/remote/libraries/{self.trip.id}/commit', {'sync_id': token, 'fingerprint': fp})
        self.assertTrue(all(r['camera'] == 'New camera' for r in self.catalogue().values()))
        self.assertEqual(self.server_library().name, 'Renamed Trip')

    def test_deleted_hashes_are_published_together_with_complete_rows(self):
        rows = self.changed(); removed = next(iter(rows)); rows.pop(removed)
        stamps, fp, token = self.begin(rows, [removed])
        self.call('POST', f'/remote/libraries/{self.trip.id}/hashes', {'photos': stamps, 'deleted': [removed], 'sync_id': token})
        self.assertEqual(self.catalogue(), self.old_rows)
        self.assertNotIn(removed, self.catalogue('remote_deleted'))
        self.call('POST', f'/remote/libraries/{self.trip.id}/rows', {'rows': list(rows.values()), 'sync_id': token})
        self.assertEqual(set(self.catalogue()), set(rows))
        self.assertIn(removed, self.catalogue('remote_deleted'))

    def test_deletion_only_transfer_commits_without_requesting_rows(self):
        rows = copy.deepcopy(self.old_rows); removed = next(iter(rows)); rows.pop(removed)
        stamps, fp, token = self.begin(rows, [removed])
        reply = self.call('POST', f'/remote/libraries/{self.trip.id}/hashes', {'photos': stamps, 'deleted': [removed], 'sync_id': token})
        self.assertEqual(reply['need'], [])
        self.assertTrue(reply['committed'])
        self.assertEqual(set(self.catalogue()), set(rows))

    def test_superseded_session_and_wrong_row_cannot_publish(self):
        rows = self.changed(); stamps, fp, old_token = self.begin(rows)
        _, _, token = self.begin(rows)
        with self.assertRaises(urllib.error.HTTPError):
            self.call('POST', f'/remote/libraries/{self.trip.id}/hashes', {'photos': stamps, 'sync_id': old_token})
        self.call('POST', f'/remote/libraries/{self.trip.id}/hashes', {'photos': stamps, 'sync_id': token})
        wrong = copy.deepcopy(next(iter(rows.values()))); wrong['camera'] = 'Wrong'
        with self.assertRaises(urllib.error.HTTPError):
            self.call('POST', f'/remote/libraries/{self.trip.id}/rows', {'rows': [wrong], 'sync_id': token})
        self.assertEqual(self.catalogue(), self.old_rows)

    def test_old_client_sequence_remains_atomic_without_session_tokens(self):
        rows = self.changed(); stamps, _, _ = self.begin(rows)
        self.call('POST', f'/remote/libraries/{self.trip.id}/hashes', {'photos': stamps})
        self.call('POST', f'/remote/libraries/{self.trip.id}/rows', {'rows': [next(iter(rows.values()))]})
        self.assertEqual(self.catalogue(), self.old_rows)
        self.call('POST', f'/remote/libraries/{self.trip.id}/rows', {'rows': list(rows.values())})
        self.assertTrue(all(r['camera'] == 'New camera' for r in self.catalogue().values()))
