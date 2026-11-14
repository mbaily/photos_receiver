"""Exercise USB camera sync without camera hardware or the live photo library."""
import contextlib
import hashlib
import io
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

import send_camera as sender


class CameraSyncTests(unittest.TestCase):
    def setUp(self):
        # Two folders deliberately contain the same filename.
        self.files = {
            '/store_00010001/DCIM/100___09/IMG_0001.JPG': b'already present',
            '/store_00010001/DCIM/100___09/IMG_0002.JPG': b'new photo one',
            '/store_00010001/DCIM/101___09/IMG_0002.JPG': b'new photo two',
        }
        self.reads = []
        self.temp_paths = []
        self.uploads = []
        self.known = {hashlib.sha256(next(iter(self.files.values()))).hexdigest()}
        self.fail_read = False
        self.multiple = False
        self.change_on_upload = False

    def command(self, command, *, stdout, **kwargs):
        self.assertEqual(kwargs['env']['LC_ALL'], 'C')
        self.assertFalse(any(flag.startswith('--delete') for flag in command))
        if '--auto-detect' in command:
            output = 'Model                          Port\n----------------------------------------------------------\nCanon PowerShot SX540 HS        usb:001,041\n'
            if self.multiple:
                output += 'Other Camera                   usb:001,042\n'
        elif '--list-files' in command:
            output = '\n'.join(f"FILENAME='{p}' FILESIZE={len(data)} FILEMTIME=1700000000" for p, data in self.files.items())
        else:
            folder = command[command.index('--folder') + 1]
            name = command[command.index('--get-file') + 1]
            remote = folder + '/' + name
            self.reads.append(remote)
            self.temp_paths.append(Path(stdout.name))
            data = self.files[remote]
            if self.change_on_upload and self.reads.count(remote) > 1:
                data = b'x' * len(data)
            stdout.write(data[:-1] if self.fail_read else data)
            return subprocess.CompletedProcess(command, 0, None, b'')
        return subprocess.CompletedProcess(command, 0, output.encode(), b'')

    def sync(self, *options):
        owner = self

        class Receiver:
            def __init__(self, *args, **kwargs):
                pass

            def libraries(self):
                return None

            def wanted(self, hashes, sidecars):
                owner.assertEqual(set(hashes), {hashlib.sha256(d).hexdigest() for d in owner.files.values()})
                return set(hashes) - owner.known, set(), {}

            def upload(self, path, digest, root):
                data = path.read_bytes()
                owner.assertEqual(hashlib.sha256(data).hexdigest(), digest)
                owner.uploads.append((path.relative_to(root).as_posix(), data))
                return {'sha256': digest, 'status': 'saved'}

        with patch.object(sys, 'argv', ['send_camera.py', *options]), \
             patch.object(sender.shutil, 'which', return_value='/usr/bin/gphoto2'), \
             patch.object(sender.subprocess, 'run', side_effect=self.command), \
             patch.object(sender, 'Server', Receiver), \
             patch.object(sender, 'find_camera_roots', return_value=[]), \
             contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            result = sender.main()
        self.assertTrue(all(not p.exists() for p in self.temp_paths), 'temporary camera files leaked')
        return result

    def test_auto_detection_uploads_only_new_and_preserves_folders(self):
        self.assertEqual(self.sync(), 0)
        self.assertEqual(len(self.reads), 5)  # hash all three, re-read only two wanted
        self.assertEqual(len(self.uploads), 2)
        self.assertNotEqual(self.uploads[0][0], self.uploads[1][0])

    def test_repeat_sync_uploads_nothing(self):
        self.known = {hashlib.sha256(d).hexdigest() for d in self.files.values()}
        self.assertEqual(self.sync('--gphoto'), 0)
        self.assertEqual(len(self.reads), 3)
        self.assertEqual(self.uploads, [])

    def test_dry_run_reads_for_hashing_but_does_not_upload(self):
        self.assertEqual(self.sync('--gphoto', '--dry-run'), 0)
        self.assertEqual(len(self.reads), 3)
        self.assertEqual(self.uploads, [])

    def test_truncated_camera_read_stops_sync(self):
        self.fail_read = True
        self.assertEqual(self.sync('--gphoto'), 1)
        self.assertEqual(self.uploads, [])

    def test_file_changed_between_hash_and_upload_is_rejected(self):
        self.change_on_upload = True
        self.assertEqual(self.sync('--gphoto'), 1)
        self.assertEqual(self.uploads, [])

    def test_multiple_cameras_require_explicit_port(self):
        self.multiple = True
        self.assertEqual(self.sync('--gphoto'), 1)
        self.assertEqual(self.sync('--camera-port', 'usb:001,041'), 0)

    def test_unsafe_camera_paths_rejected(self):
        self.files = {'/store/../escape.JPG': b'bad'}
        self.assertEqual(self.sync('--gphoto'), 1)
        self.assertEqual(self.reads, [])

    def test_mounted_card_flow_still_works(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            for remote, data in self.files.items():
                local = root / remote.removeprefix('/store_00010001/')
                local.parent.mkdir(parents=True, exist_ok=True)
                local.write_bytes(data)
            with patch.object(sender, 'CACHE_FILE', root / 'cache.json'):
                self.assertEqual(self.sync(str(root)), 0)
            self.assertEqual(len(self.uploads), 2)
            self.assertEqual(self.reads, [])


if __name__ == '__main__':
    unittest.main()
