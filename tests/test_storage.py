"""Offline mount evidence and isolated filesystem probe tests."""
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

SCRIPTS = Path(__file__).resolve().parents[1] / 'scripts'
sys.path.insert(0, str(SCRIPTS))
from storage import StorageError, verify_mount, PROBE_PROGRAM, parse_probe_output, last_probe_phase


ROOT = '1 0 0:1 / / rw - overlay overlay rw\n'
MASTER = '2 1 0:2 /users/example /example rw - fuse.storage storage rw\n'
SSD = '3 1 8:1 / /ssdwork rw - xfs /dev/example rw\n'


class StorageTests(unittest.TestCase):
    def test_personal_master_and_parent_ssd_mount_are_verified(self):
        master = verify_mount(ROOT + MASTER, '/example', '/example/runs/id')
        self.assertTrue(master['mount_verified'])
        self.assertFalse(master['group_quota_verified'])
        self.assertEqual(master['mount_point'], '/example')
        ssd = verify_mount(ROOT + SSD, '/ssdwork/example', '/ssdwork/example/runs/id')
        self.assertEqual(ssd['mount_point'], '/ssdwork')

    def test_existing_directory_on_overlay_is_not_persistent_mount_evidence(self):
        with self.assertRaises(StorageError):
            verify_mount(ROOT, '/ssdwork/example', '/ssdwork/example/runs/id')
        temporary = '2 1 0:2 / /ssdwork rw - tmpfs tmpfs rw\n'
        with self.assertRaises(StorageError):
            verify_mount(ROOT + temporary, '/ssdwork/example', '/ssdwork/example/runs/id')

    def test_outside_path_and_ephemeral_roots_are_rejected(self):
        for mount, destination in [('/example', '/elsewhere/runs/id'), ('/tmp', '/tmp/runs/id'),
                                   ('/root', '/root/runs/id'), ('/', '/runs/id')]:
            with self.subTest(mount=mount):
                with self.assertRaises(StorageError):
                    verify_mount(ROOT + MASTER, mount, destination)

    def test_mountinfo_escapes_and_longest_mount_are_supported(self):
        nested = '4 2 0:4 / /example/nested rw - ext4 /dev/nested rw\n'
        evidence = verify_mount(ROOT + MASTER + nested, '/example/nested', '/example/nested/runs/id')
        self.assertEqual(evidence['mount_point'], '/example/nested')
        escaped = '5 1 0:5 / /data\\040space rw - ext4 /dev/example rw\n'
        self.assertEqual(verify_mount(ROOT + escaped, '/data space', '/data space/run')['mount_point'], '/data space')

    def test_nested_mount_cannot_hide_behind_verified_personal_storage(self):
        for kind, source in [('tmpfs', 'tmpfs'), ('ext4', '/dev/other')]:
            nested = f'4 2 0:4 / /example/runs rw - {kind} {source} rw\n'
            with self.subTest(kind=kind), self.assertRaises(StorageError):
                verify_mount(ROOT + MASTER + nested, '/example', '/example/runs/id')

    def test_stacked_mounts_are_rejected_when_visible_filesystem_is_ambiguous(self):
        shadow = '4 2 0:4 / /example rw - tmpfs tmpfs rw\n'
        with self.assertRaises(StorageError):
            verify_mount(ROOT + MASTER + shadow, '/example', '/example/runs/id')

    def test_probe_write_read_remove_and_space_do_not_claim_group_quota(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve() / 'runs' / 'unique'
            result = subprocess.run([sys.executable, '-S', '-u', '-c', PROBE_PROGRAM, str(root), '1', str(Path(directory).resolve())],
                                    capture_output=True, text=True, timeout=5)
            self.assertEqual(result.returncode, 0, result.stderr)
            receipt = parse_probe_output(result.stdout)
            self.assertTrue(receipt['writable_checked'])
            self.assertFalse(receipt['group_quota_verified'])
            self.assertGreater(receipt['available_bytes'], 0)
            self.assertEqual(list(root.iterdir()), [])

    def test_read_only_probe_does_not_create_the_run_directory(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve() / 'not-created'
            result = subprocess.run([sys.executable, '-S', '-u', '-c', PROBE_PROGRAM, str(root), '0', str(Path(directory).resolve())],
                                    capture_output=True, text=True, timeout=5)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertFalse(parse_probe_output(result.stdout)['writable_checked'])
            self.assertFalse(root.exists())

    def test_missing_personal_directory_is_never_created(self):
        with tempfile.TemporaryDirectory() as directory:
            personal = Path(directory).resolve() / 'missing-personal'
            root = personal / 'runs' / 'unique'
            for write in ('0', '1'):
                result = subprocess.run([sys.executable, '-S', '-u', '-c', PROBE_PROGRAM,
                                         str(root), write, str(personal)],
                                        capture_output=True, text=True, timeout=5)
                self.assertNotEqual(result.returncode, 0)
                self.assertFalse(personal.exists())
                self.assertEqual(last_probe_phase(result.stdout), 'inspect')

    def test_partial_probe_identifies_last_safe_stage(self):
        partial = '{"phase":"inspect"}\n{"phase":"mkdir","elapsed_seconds":0.01}\n'
        with self.assertRaisesRegex(StorageError, 'mkdir'):
            parse_probe_output(partial)
        self.assertEqual(last_probe_phase(partial), 'mkdir')
        self.assertEqual(last_probe_phase('unstructured arbitrary output'), 'start')


if __name__ == '__main__':
    unittest.main()
