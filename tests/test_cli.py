"""Cold-start and local-only CLI checks with no cluster or credential access."""
import contextlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

SCRIPTS = Path(__file__).resolve().parents[1] / 'scripts'
sys.path.insert(0, str(SCRIPTS))
import cli


class CLITests(unittest.TestCase):
    def test_help_needs_no_profile_or_dependencies(self):
        with tempfile.TemporaryDirectory() as directory:
            env = dict(os.environ, XDG_CONFIG_HOME=directory)
            env.pop('WLAI_PROFILE', None)
            result = subprocess.run([sys.executable, str(SCRIPTS / 'cli.py'), '--help'],
                                    env=env, capture_output=True, text=True, timeout=10)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn('submit', result.stdout)

    def test_init_rejects_existing_profile_before_reading_source(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / 'profile.json'
            target.write_text('{}')
            args = cli.parser().parse_args(['--profile', str(target), 'init', '--from-file', '/missing'])
            with self.assertRaises(FileExistsError):
                cli.initialize(args)
            self.assertEqual(target.read_text(), '{}')

    def test_login_hidden_input_does_not_save_password_without_opt_in(self):
        fake = {'account': 'sample_user'}
        events = []
        class FakeClient:
            def __init__(self, *args, **kwargs): pass
            def login(self, account, password): events.append(('login', account, password))
            def close(self): events.append(('close',))
        secret = 'test-only-input-that-must-not-be-printed'
        with patch('config.load_profile', return_value=fake), patch('api.AIStation', FakeClient), \
             patch('credentials.store_password') as store, patch('cli.sys.stdin.isatty', return_value=True), \
             patch('cli.getpass.getpass', return_value=secret), contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(cli.main(['login']), 0)
            store.assert_not_called()
            self.assertNotIn(secret, output.getvalue())
        self.assertEqual(events[0], ('login', 'sample_user', secret))

    def test_runs_reads_local_state_without_constructing_cluster(self):
        from registry import Registry, profile_identity
        with tempfile.TemporaryDirectory() as directory:
            profile = {'portal': 'https://aistation.example.edu', 'account': 'sample_user'}
            registry = Registry(directory, profile_identity(profile))
            registry.reserve('local_run', {'argv': ['/bin/true']}, 'run')
            with patch('config.load_profile', return_value=profile), patch('cluster.Cluster') as cluster, \
                 contextlib.redirect_stdout(io.StringIO()) as output:
                self.assertEqual(cli.main(['--state-dir', directory, 'runs']), 0)
                cluster.assert_not_called()
                self.assertEqual(json.loads(output.getvalue())[0]['run_id'], 'local_run')


if __name__ == '__main__':
    unittest.main()
