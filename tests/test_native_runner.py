"""Offline startup guard, persistent receipts and process cleanup checks."""
import contextlib
import io
import hashlib
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

SCRIPTS = Path(__file__).resolve().parents[1] / 'scripts'
sys.path.insert(0, str(SCRIPTS))
import native_runner as runner


class NativeRunnerTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.personal = Path(self.directory.name).resolve()
        self.run_dir = self.personal / 'runs' / 'example'
        self.attempts = self.run_dir / 'attempts'
        self.spec = {'run_id': 'example', 'spec_sha256': 'a' * 64,
                     'storage_mount': str(self.personal), 'run_dir': str(self.run_dir),
                     'gpus': 0, 'card_type': '', 'guard_seconds': 10,
                     'max_seconds': 5, 'receipt_seconds': 2,
                     'argv': [sys.executable, '-c', 'pass'], 'env': {}}
        self.umask = os.umask(0o022)
        self.addCleanup(os.umask, self.umask)

    def mounts(self, extra=''):
        return ('1 0 0:1 / / rw - overlay overlay rw\n'
                f'2 1 0:2 / {self.personal} rw - nfs server:/personal rw\n' + extra)

    def run_local(self, changes=None):
        spec = dict(self.spec, **(changes or {}))
        output = io.StringIO()
        with patch.object(runner, 'read_mountinfo', return_value=self.mounts()), contextlib.redirect_stdout(output):
            code = runner.run(spec)
        markers = [json.loads(line[len(runner.RECEIPT_PREFIX):]) for line in output.getvalue().splitlines()
                   if line.startswith(runner.RECEIPT_PREFIX)]
        return code, markers

    def receipt(self):
        paths = list(self.attempts.glob('*.json'))
        self.assertEqual(len(paths), 1)
        return json.loads(paths[0].read_text()), paths[0]

    def test_storage_requires_actual_persistent_mount_before_creating_run(self):
        with patch.object(runner, 'read_mountinfo', return_value='1 0 0:1 / / rw - overlay overlay rw\n'):
            with self.assertRaisesRegex(runner.GuardError, 'STORAGE_MOUNT_UNVERIFIED'):
                runner.verify_storage(self.spec)
        self.assertFalse(self.run_dir.exists())

    def test_manifest_file_entrypoint_checks_exact_bytes_before_running(self):
        path = self.personal / 'manifest.json'
        content = json.dumps(self.spec, sort_keys=True, separators=(',', ':')).encode()
        path.write_bytes(content)
        digest = hashlib.sha256(content).hexdigest()
        with patch.object(runner, 'run', return_value=17) as run:
            self.assertEqual(runner.main_args(['--manifest-file', str(path), '--manifest-sha256', digest]), 17)
        run.assert_called_once_with(self.spec)

    def test_manifest_file_mismatch_or_missing_never_runs_program(self):
        path = self.personal / 'manifest.json'
        path.write_bytes(b'{}')
        for arguments in (['--manifest-file', str(path), '--manifest-sha256', '0' * 64],
                          ['--manifest-file', str(path.with_name('missing.json')), '--manifest-sha256', '0' * 64],
                          ['--manifest-file', 'relative.json', '--manifest-sha256', '0' * 64],
                          ['--manifest-file', str(path), '--manifest-sha256', 'invalid'], []):
            with self.subTest(arguments=arguments), patch.object(runner, 'run') as run, \
                 contextlib.redirect_stdout(io.StringIO()) as output:
                self.assertEqual(runner.main_args(arguments), 125)
                run.assert_not_called()
                self.assertNotIn(str(path), output.getvalue())

    def test_storage_rejects_nested_shadow_and_stacked_mounts(self):
        for extra, reason in [(f'3 2 0:3 / {self.run_dir} rw - tmpfs tmpfs rw\n', 'STORAGE_SHADOWED_OR_EPHEMERAL'),
                              (f'3 2 0:3 / {self.personal} rw - nfs server:/other rw\n', 'STORAGE_STACKED_MOUNT_AMBIGUOUS')]:
            with self.subTest(reason=reason), patch.object(runner, 'read_mountinfo', return_value=self.mounts(extra)):
                with self.assertRaisesRegex(runner.GuardError, reason):
                    runner.verify_storage(self.spec)
        self.assertFalse(self.run_dir.exists())

    def test_storage_rejects_symlink_without_replacing_it(self):
        self.run_dir.parent.mkdir()
        target = self.personal / 'other'; target.mkdir()
        self.run_dir.symlink_to(target, target_is_directory=True)
        with patch.object(runner, 'read_mountinfo', return_value=self.mounts()):
            with self.assertRaisesRegex(runner.GuardError, 'RUN_DIRECTORY_SYMLINK'):
                runner.verify_storage(self.spec)
        self.assertTrue(self.run_dir.is_symlink())
        self.assertEqual(list(target.iterdir()), [])

    def test_gpu_mismatch_never_starts_user_program(self):
        with patch.object(runner, 'gpu_models', return_value=['Other GPU']), \
             patch.object(runner.subprocess, 'Popen', side_effect=AssertionError('science must not start')) as popen:
            code, markers = self.run_local({'gpus': 1, 'card_type': 'Expected GPU'})
        self.assertEqual(code, 125)
        self.assertFalse(popen.called)
        receipt, _ = self.receipt()
        self.assertEqual(receipt['state'], 'GUARD_FAILED')
        self.assertEqual(receipt['error_code'], 'GPU_COUNT_OR_MODEL_MISMATCH')
        self.assertFalse(receipt['hardware_verified'])
        self.assertEqual(receipt, markers[-1])

    def test_cpu_program_receipt_is_persisted_without_argv_or_environment(self):
        code = 'import os,pathlib,sys;pathlib.Path("result.txt").write_text(os.environ["EXPLICIT"] + sys.argv[1])'
        with patch.object(runner, 'gpu_models', side_effect=AssertionError('CPU must not query GPU')):
            result, markers = self.run_local({'argv': [sys.executable, '-c', code, 'private-argument'],
                                              'env': {'EXPLICIT': 'private-environment'}})
        self.assertEqual(result, 0)
        self.assertEqual((self.run_dir / 'result.txt').read_text(), 'private-environmentprivate-argument')
        receipt, path = self.receipt()
        self.assertEqual(receipt, markers[-1])
        self.assertEqual(receipt['state'], 'SUCCEEDED')
        self.assertTrue(receipt['receipt_persisted'])
        self.assertTrue(receipt['storage_verified'])
        self.assertTrue(receipt['hardware_verified'])
        self.assertEqual(receipt['program_exit_code'], 0)
        self.assertEqual(receipt['exit_code'], 0)
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        serialized = json.dumps(markers) + path.read_text()
        self.assertNotIn('private-argument', serialized)
        self.assertNotIn('private-environment', serialized)

    def test_repeated_starts_keep_separate_attempts_and_previous_receipt(self):
        self.assertEqual(self.run_local()[0], 0)
        first_path = next(self.attempts.glob('*.json'))
        first = first_path.read_bytes()
        self.assertEqual(self.run_local()[0], 0)
        paths = list(self.attempts.glob('*.json'))
        self.assertEqual(len(paths), 2)
        self.assertEqual(first_path.read_bytes(), first)
        self.assertEqual(len({json.loads(path.read_text())['attempt_id'] for path in paths}), 2)

    def test_final_receipt_failure_cannot_report_success(self):
        original = runner.write_receipt
        def fail_terminal(path, state):
            if state['state'] == 'SUCCEEDED':
                raise OSError('synthetic storage failure')
            return original(path, state)
        with patch.object(runner, 'write_receipt', side_effect=fail_terminal):
            code, markers = self.run_local()
        self.assertEqual(code, 125)
        self.assertEqual(markers[-1]['state'], 'FAILED')
        self.assertEqual(markers[-1]['error_code'], 'RECEIPT_WRITE_FAILED')
        self.assertFalse(markers[-1]['receipt_persisted'])

    def program_with_descendant(self):
        # The descendant records TERM receipt, proving cleanup reached the group.
        grandchild = ('import pathlib,signal,sys,time; '
                      'p=pathlib.Path("descendant.txt"); '
                      'signal.signal(signal.SIGTERM,lambda *_:(p.write_text("terminated"),sys.exit(0))); '
                      'p.write_text("running"); time.sleep(60)')
        # Reap the descendant before the parent exits, avoiding a Darwin orphan
        # zombie-only process group (killpg reports EPERM for that group).
        child = ('import pathlib,signal,subprocess,sys,time\n'
                 f'p=subprocess.Popen([sys.executable,"-c",{grandchild!r}])\n'
                 'def term(*_):\n p.wait(timeout=5)\n sys.exit(0)\n'
                 'signal.signal(signal.SIGTERM,term)\n'
                 'pathlib.Path("program.pid").write_text(str(p.pid))\n'
                 'time.sleep(60)')
        return [sys.executable, '-c', child]

    def test_program_timeout_terminates_entire_process_group(self):
        started = time.monotonic()
        code, markers = self.run_local({'argv': self.program_with_descendant(), 'max_seconds': 1})
        self.assertEqual(code, 124)
        self.assertLess(time.monotonic() - started, 10)
        self.assertEqual(markers[-1]['state'], 'TIMED_OUT')
        self.assertEqual((self.run_dir / 'descendant.txt').read_text(), 'terminated')

    def test_sigterm_terminates_process_group_and_persists_cancel_receipt(self):
        spec = dict(self.spec, argv=self.program_with_descendant(), max_seconds=30)
        harness = ('import json,sys; sys.path.insert(0,sys.argv[1]); import native_runner as r; '
                   'r.read_mountinfo=lambda:sys.argv[3]; raise SystemExit(r.run(json.loads(sys.argv[2])))')
        process = subprocess.Popen([sys.executable, '-u', '-c', harness, str(SCRIPTS), json.dumps(spec), self.mounts()],
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        self.addCleanup(lambda: process.kill() if process.poll() is None else None)
        deadline = time.monotonic() + 5
        marker = self.run_dir / 'descendant.txt'
        while time.monotonic() < deadline and not marker.exists() and process.poll() is None:
            time.sleep(0.02)
        self.assertTrue(marker.exists(), 'descendant did not become ready')
        process.send_signal(signal.SIGTERM)
        stdout, stderr = process.communicate(timeout=10)
        self.assertEqual(process.returncode, 128 + signal.SIGTERM, stderr)
        self.assertEqual(marker.read_text(), 'terminated')
        receipt, _ = self.receipt()
        self.assertEqual(receipt['state'], 'CANCELLED')
        self.assertTrue(receipt['receipt_persisted'])
        self.assertIn('TASK_SIGNALLED', stdout)


if __name__ == '__main__':
    unittest.main()
