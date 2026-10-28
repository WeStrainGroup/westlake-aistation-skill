"""CPU staging with fake cluster calls only; no network or credentials."""
import contextlib
import hashlib
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import staging
from paramiko.ssh_exception import NoValidConnectionsError

PROFILE = {'portal': 'https://cluster.example.edu', 'account': 'sample',
           'training_group_id': 'train', 'development_group_id': 'dev',
           'image': 'registry.example.edu/image:tag', 'card_type': 'Example GPU',
           'persistent_root': '/sample/runs', 'storage_mount': '/sample',
           'ssh_host_fingerprint': 'SHA256:' + 'A' * 43}
SPEC = {'run_id': 'science', 'argv': ['/bin/echo', '{run_dir}'], 'max_seconds': 60}


class FakeCluster:
    def __init__(self, *args, **kwargs):
        self.profile = kwargs['profile']; self.record = None; self.registry = self
        self.stops = []; self.uploads = []; self.checked = False; self.ssh_attempts = 0
        self.connect_error = None; self.upload_error = None; self.stop_error = None
    @contextlib.contextmanager
    def operation(self, run_id, name):
        assert name == 'submission'
        yield
    def plan(self, spec): self.spec = spec; self.record = {'state': 'PLANNED'}
    def update(self, run_id, **values):
        self.record.update(values); return self.record
    def create_environment(self, run_id): self.record.update(resource_id='owned-stage')
    def wait_environment(self, run_id): return self.record
    @contextlib.contextmanager
    def ssh(self, resource_id):
        self.ssh_attempts += 1
        if self.connect_error is not None:
            error, self.connect_error = self.connect_error, None
            raise error
        yield object()
    def _storage_check(self, run_id, client, write): self.checked = True
    def put(self, client, local, remote, expected_hash=None):
        if self.upload_error: raise self.upload_error
        assert self.checked
        data = Path(local).read_bytes()
        assert hashlib.sha256(data).hexdigest() == expected_hash
        self.uploads.append((remote, data))
        return {'sha256': expected_hash}
    def get(self, run_id):
        if self.record is None: raise FileNotFoundError(run_id)
        return self.record
    def stop_environment(self, run_id):
        self.stops.append(run_id)
        if self.stop_error: raise self.stop_error
        self.record.update(resource_released=True, final_resource_state='Pause')
        return self.record
    def close(self): pass


class StagingTests(unittest.TestCase):
    def test_dry_run_never_constructs_cluster(self):
        with patch('staging.Cluster', side_effect=AssertionError('allocation')):
            result = staging.stage_task(PROFILE, SPEC, '/unused')
        self.assertFalse(result['executed'])
        with patch('staging.Cluster', side_effect=AssertionError('allocation')):
            repeated = staging.stage_task(PROFILE, SPEC, '/unused')
        self.assertEqual(result['staged_spec'], repeated['staged_spec'])
        self.assertEqual(result['stage_run_id'], repeated['stage_run_id'])
        self.assertEqual(result['staging_resources'], {'cpus': 1, 'gpus': 0})
        self.assertLessEqual(result['command_bytes'], 1000)
        self.assertTrue(result['staged_spec']['guard_script'].startswith('/sample/.wlai/'))
    def test_uploads_canonical_manifest_and_releases_only_owned_environment(self):
        fake = FakeCluster(profile=PROFILE)
        with patch('staging.Cluster', return_value=fake):
            result = staging.stage_task(PROFILE, SPEC, '/unused', execute=True)
        self.assertTrue(result['executed']); self.assertTrue(result['resource_released'])
        self.assertEqual(fake.spec['gpus'], 0); self.assertEqual(fake.spec['cpus'], 1)
        self.assertEqual(fake.stops, [result['stage_run_id']])
        content = fake.uploads[1][1]
        self.assertEqual(content, json.dumps(json.loads(content), sort_keys=True, separators=(',', ':')).encode())
        self.assertEqual(hashlib.sha256(content).hexdigest(), result['manifest_sha256'])
    def test_only_endpoint_not_ready_is_retried(self):
        fake = FakeCluster(profile=PROFILE)
        fake.connect_error = NoValidConnectionsError({('example.invalid', 2222): ConnectionRefusedError()})
        with patch('staging.Cluster', return_value=fake), patch('staging.time.sleep'):
            result = staging.stage_task(PROFILE, SPEC, '/unused', execute=True)
        self.assertEqual(fake.ssh_attempts, 2); self.assertTrue(result['resource_released'])
    def test_identity_failure_is_not_retried_and_cleanup_preserves_primary_error(self):
        fake = FakeCluster(profile=PROFILE)
        fake.connect_error = RuntimeError('identity changed')
        fake.stop_error = RuntimeError('cleanup failed')
        with patch('staging.Cluster', return_value=fake), self.assertRaisesRegex(RuntimeError, 'identity changed') as caught:
            staging.stage_task(PROFILE, SPEC, '/unused', execute=True)
        self.assertEqual(fake.ssh_attempts, 1); self.assertEqual(len(fake.stops), 1)
        self.assertEqual(caught.exception.cleanup_error['type'], 'RuntimeError')
    def test_repeated_success_reuses_staging_evidence_without_reallocating(self):
        fake = FakeCluster(profile=PROFILE)
        with patch('staging.Cluster', return_value=fake):
            first = staging.stage_task(PROFILE, SPEC, '/unused', execute=True)
            second = staging.stage_task(PROFILE, SPEC, '/unused', execute=True)
        self.assertTrue(second['reused']); self.assertTrue(second['resource_released'])
        self.assertEqual(first['staged_spec'], second['staged_spec'])
        self.assertEqual(len(fake.stops), 1); self.assertEqual(len(fake.uploads), 2)
    def test_previous_failed_staging_is_not_reused_as_success(self):
        fake = FakeCluster(profile=PROFILE); fake.record = {'resource_released': True, 'final_resource_state': 'Pause'}
        with patch('staging.Cluster', return_value=fake), self.assertRaisesRegex(ValueError, 'new science run_id'):
            staging.stage_task(PROFILE, SPEC, '/unused', execute=True)
        self.assertEqual(fake.stops, []); self.assertEqual(fake.uploads, [])
    def test_missing_staging_configuration_is_clear_before_constructing_cluster(self):
        for key in ('development_group_id', 'ssh_host_fingerprint'):
            profile = dict(PROFILE); profile.pop(key)
            with patch('staging.Cluster', side_effect=AssertionError('allocation')), self.assertRaisesRegex(ValueError, key):
                staging.stage_task(profile, SPEC, '/unused', execute=True)
    def test_upload_failure_still_releases_environment(self):
        fake = FakeCluster(profile=PROFILE); fake.upload_error = ValueError('upload hash mismatch')
        with patch('staging.Cluster', return_value=fake), self.assertRaises(ValueError):
            staging.stage_task(PROFILE, SPEC, '/unused', execute=True)
        self.assertEqual(len(fake.stops), 1)


if __name__ == '__main__': unittest.main()
