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
    def profile_source(self, root):
        (root / 'server-ca.pem').write_text('synthetic public certificate data')
        return {'portal': 'https://aistation.example.edu', 'account': 'example-user',
                'ca_file': str(root / 'server-ca.pem'), 'certificate_sha256': 'ab' * 32,
                'development_group_id': 'example-development', 'image': 'registry.example.edu/image:tag',
                'ssh_host_fingerprint': 'SHA256:' + 'A' * 43, 'card_type': 'Example GPU',
                'persistent_root': '/example-user/wlai_runs'}

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
        fake = {'account': 'sample_user', 'portal': 'https://aistation.example.edu'}
        events = []
        class FakeClient:
            def __init__(self, *args, **kwargs): pass
            def login(self, account, password): events.append(('login', account, password))
            def close(self): events.append(('close',))
        secret = 'test-only-input-that-must-not-be-printed'
        with patch('config.load_profile', return_value=fake), patch('api.AIStation', FakeClient), \
             patch('credentials.store_password') as store, \
             patch('credentials._read_stored', side_effect=AssertionError('Default login opened OS store')), \
             patch('cli.sys.stdin.isatty', return_value=True), \
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

    def test_system_store_read_requires_explicit_global_choice(self):
        fake = {'account': 'sample_user', 'portal': 'https://aistation.example.edu'}
        class FakeClient:
            def __init__(self, profile): self.profile = profile
            def login(self, account, password):
                self.profile['authenticated_using_store'] = password == 'synthetic-store-value'
            def close(self): pass
        with patch('config.load_profile', return_value=fake), patch('api.AIStation', FakeClient), \
             patch('credentials._read_stored', return_value='synthetic-store-value') as store, \
             patch('credentials.getpass.getpass') as prompt, contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(cli.main(['--use-keychain', '--non-interactive', 'login']), 0)
            store.assert_called_once()
            prompt.assert_not_called()
        self.assertTrue(fake['authenticated_using_store'])

    def test_connection_setup_and_discovery_do_not_require_compute_fields(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = self.profile_source(root)
            source = {key: source[key] for key in ('portal', 'account', 'ca_file', 'certificate_sha256')}
            incoming, profile = root / 'incoming.json', root / 'profile.json'
            incoming.write_text(json.dumps(source))
            with contextlib.redirect_stdout(io.StringIO()) as output:
                self.assertEqual(cli.main(['--profile', str(profile), 'init', '--connection-only', '--from-file', str(incoming)]), 0)
            self.assertFalse(json.loads(output.getvalue())['execution_ready'])
            self.assertEqual(profile.stat().st_mode & 0o777, 0o600)
            with patch('cluster.Cluster') as cluster, contextlib.redirect_stdout(io.StringIO()) as output:
                cluster.return_value.resources.return_value = [dict(group='Example group', group_id='example-development', node='node-a', available_cpus=8, available_gpus=1, card_type='Example GPU')]
                self.assertEqual(cli.main(['--profile', str(profile), 'resources', '--format', 'table']), 0)
                cluster.return_value.resources.assert_called_once_with(None)
                self.assertIn('example-development', output.getvalue())

    def test_configure_storage_is_explicit_and_does_not_move_data(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); profile = root / 'profile.json'
            profile.write_text(json.dumps(self.profile_source(root)))
            with patch('cluster.Cluster') as cluster, contextlib.redirect_stdout(io.StringIO()) as output:
                self.assertEqual(cli.main(['--profile', str(profile), 'configure', '--storage', 'ssdwork']), 0)
                cluster.assert_not_called()
            saved = json.loads(profile.read_text())
            self.assertEqual(saved['storage_mount'], '/ssdwork/example-user')
            self.assertEqual(saved['persistent_root'], '/ssdwork/example-user/wlai_runs')
            self.assertFalse(json.loads(output.getvalue())['data_moved'])

    def test_job_to_offline_plan_needs_no_credentials(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); profile = root / 'profile.json'
            profile.write_text(json.dumps(self.profile_source(root)))
            script = root / 'main.py'; script.write_text('print(42)\n')
            job = root / 'job.json'
            with patch.dict(os.environ, {'XDG_STATE_HOME': str(root / 'state')}), \
                 patch('cluster.get_password', side_effect=AssertionError('Offline plan authenticated')), \
                 contextlib.redirect_stdout(io.StringIO()) as output:
                self.assertEqual(cli.main(['--profile', str(profile), 'job', '--script', str(script), '--run-id', 'offline', '--minutes', '10', '--out', str(job), '--', '--batch-size', '16']), 0)
                output.truncate(0); output.seek(0)
                self.assertEqual(cli.main(['--profile', str(profile), 'plan', '--spec', str(job)]), 0)
                plan = json.loads(output.getvalue())
            self.assertFalse(plan['availability_checked'])
            self.assertIsNone(plan['eligible_nodes_now'])
            self.assertEqual(plan['upload_total_bytes'], script.stat().st_size)
            self.assertEqual(plan['spec']['argv'][-2:], ['--batch-size', '16'])

    def test_training_plan_and_guide_are_local_and_never_fall_back_to_development(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); profile = root / 'profile.json'
            source = self.profile_source(root)
            source['training_group_id'] = 'example-training'
            profile.write_text(json.dumps(source))
            job, guide = root / 'training.json', root / 'task-guide.md'
            with contextlib.redirect_stdout(io.StringIO()), patch('cluster.get_password', side_effect=AssertionError('Guide authenticated')):
                self.assertEqual(cli.main(['--profile', str(profile), 'job', '--remote-script', '/example-user/train.py', '--purpose', 'training', '--minutes', '2880', '--out', str(job)]), 0)
                self.assertEqual(cli.main(['--profile', str(profile), 'task-guide', '--spec', str(job), '--out', str(guide)]), 0)
                with self.assertRaises(ValueError):
                    cli.main(['--profile', str(profile), 'run', '--spec', str(job), '--execute'])
            self.assertIn('https://hpc.westlake.edu.cn/document/ai/aistation/6.task.html', guide.read_text())

    def test_training_configuration_does_not_require_development_ssh_trust(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); profile = root / 'profile.json'
            source = self.profile_source(root)
            source.pop('ssh_host_fingerprint'); source.pop('development_group_id')
            profile.write_text(json.dumps(source))
            with contextlib.redirect_stdout(io.StringIO()), patch('cluster.Cluster') as cluster:
                self.assertEqual(cli.main(['--profile', str(profile), 'configure', '--purpose', 'training',
                                           '--group-id', 'example-training']), 0)
                cluster.assert_not_called()
            saved = json.loads(profile.read_text())
            self.assertEqual(saved['training_group_id'], 'example-training')
            self.assertNotIn('development_group_id', saved)
            self.assertNotIn('ssh_host_fingerprint', saved)

    def test_native_plan_is_offline_with_connection_and_training_settings_only(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); profile = root / 'profile.json'
            source = self.profile_source(root)
            source.pop('ssh_host_fingerprint'); source.pop('development_group_id')
            source['training_group_id'] = 'example-training'
            profile.write_text(json.dumps(source))
            spec = root / 'native.json'
            spec.write_text(json.dumps({'run_id': 'native_offline', 'max_seconds': 86400, 'command_max_bytes': 20000,
                                        'argv': ['/opt/conda/bin/python', '/example-user/train.py']}))
            with contextlib.redirect_stdout(io.StringIO()) as output, \
                 patch('native.get_password', side_effect=AssertionError('offline authentication')):
                self.assertEqual(cli.main(['--profile', str(profile), '--state-dir', str(root / 'state'),
                                           'task-plan', '--spec', str(spec)]), 0)
            plan = json.loads(output.getvalue())
            self.assertEqual(plan['backend'], 'native')
            self.assertFalse(plan['availability_checked'])
            self.assertFalse(plan['submitted'])


if __name__ == '__main__':
    unittest.main()
