"""Native-task invariants using fake APIs only; no cluster or credential access."""
import base64
import copy
import hashlib
import json
import shlex
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

SCRIPTS = Path(__file__).resolve().parents[1] / 'scripts'
sys.path.insert(0, str(SCRIPTS))
from native import (NativeTasks, build_task_payload, prepare_task_spec, parse_native_receipt,
                    spec_fingerprint, _successful_receipt, RECEIPT_PREFIX)
from api import ApiError


PROFILE = {'portal': 'https://cluster.example.edu', 'account': 'sample-user',
           'training_group_id': 'training-group', 'development_group_id': 'development-group',
           'image': 'registry.example.edu/pytorch:tag', 'framework': 'pytorch',
           'card_type': 'Example GPU', 'persistent_root': '/sample-user/runs'}
USER = {'account': 'sample-user', 'userId': 'sample-user-id', 'groupId': 'sample-project'}


class FakeAPI:
    def __init__(self, *, label='usual', busy=False, mismatch=False, terminal=False):
        self.label, self.busy, self.mismatch, self.terminal = label, busy, mismatch, terminal
        self.calls = []
        self.tasks = {}
        self.raise_after_create = False
        self.no_visibility = False
        self.receipt = False
        self.log_data = None
        self.pod_data = {'total': 0, 'data': []}
    def call(self, method, path, params=None, body=None):
        self.calls.append((method, path, copy.deepcopy(params), copy.deepcopy(body)))
        if method == 'GET' and path.endswith('/node-group'):
            return {'data': [{'groupId': 'training-group', 'groupName': 'Example training', 'groupLabel': self.label}]}
        if method == 'GET' and path.endswith('/node'):
            return {'data': [{'groupId': 'training-group', 'nodeName': 'example-node', 'cardType': 'Example GPU',
                              'cpu': 48, 'cpuUsage': 0, 'acceleratorCard': 4,
                              'acceleratorCardUsage': 4 if self.busy else 0}]}
        if method == 'GET' and path.endswith('/job-pod-instance'):
            return copy.deepcopy(self.pod_data)
        if method == 'GET' and path.endswith('/train'):
            if self.no_visibility:
                return {'data': []}
            values = list(self.tasks.values())
            if params.get('id'):
                values = [row for row in values if row['id'] == params['id']]
            if params.get('name'):
                values = [row for row in values if row['name'] == params['name']]
            if params.get('statusFlag') == 0:
                values = [row for row in values if row['status'] not in ('Succeeded', 'Failed', 'Stopped')]
            elif params.get('statusFlag') == 3:
                values = [row for row in values if row['status'] in ('Succeeded', 'Failed', 'Stopped')]
            return {'data': copy.deepcopy(values)}
        if method == 'POST' and path.endswith('/train'):
            row = copy.deepcopy(body)
            row.update(id='native-task-' + str(len(self.tasks) + 1), userId=USER['userId'], userName=USER['account'],
                       status='Succeeded' if self.terminal else 'Pending')
            if self.mismatch:
                config = json.loads(row['config']); config['worker']['acceleratorCardNum'] = 0
                row.update(config=json.dumps(config), acceleratorCardKind='CPU', acceleratorCardType='')
            self.tasks[row['id']] = row
            if self.raise_after_create:
                raise ConnectionError('synthetic-hidden-transport-detail')
            return {'id': row['id']}
        if method == 'DELETE' and path == '/api/iresource/v1/train':
            assert body['endTaskListFlag'] is False
            for task_id in body['jobIdList']:
                self.tasks[task_id]['status'] = 'Stopped'
            return None
        if method == 'POST' and path.endswith('/stop'):
            task_id = path.split('/')[-2]
            self.tasks[task_id]['status'] = 'Stopped'
            return None
        if method == 'GET' and path.endswith('/read-log'):
            if self.log_data is not None:
                return self.log_data
            if self.receipt:
                task = self.tasks[path.split('/')[-2]]
                manifest = json.loads(base64.urlsafe_b64decode(shlex.split(task['command'])[-1]))
                receipt = dict(manifest, schema=1, attempt_id='attempt-one', sequence=4, state='SUCCEEDED',
                               expected_gpu_count=manifest['gpus'], expected_gpu_model=manifest['card_type'],
                               actual_gpu_names=[manifest['card_type']] * manifest['gpus'],
                               hardware_verified=True, storage_verified=True, receipt_persisted=True,
                               program_exit_code=0, exit_code=0)
                return RECEIPT_PREFIX + json.dumps(receipt) + '\n'
            return 'synthetic platform log\n'
        raise AssertionError('Unexpected fake API request: ' + method + ' ' + path)
    def close(self): pass
    def count_creates(self):
        return sum(method == 'POST' and path.endswith('/train') for method, path, *_ in self.calls)
    def count_stops(self):
        return sum(method == 'POST' and path.endswith('/stop') or method == 'DELETE' and path == '/api/iresource/v1/train' for method, path, *_ in self.calls)


class NativeTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.api = FakeAPI()
        self.client = NativeTasks(PROFILE, state_dir=self.directory.name, client=self.api, user=USER)
        self.spec = prepare_task_spec({'run_id': 'example', 'argv': ['/opt/conda/bin/python', '-u', '/sample-user/train.py'],
                                       'max_seconds': 86400, 'command_max_bytes': 20000}, PROFILE)

    def test_offline_plan_never_authenticates_or_accesses_network(self):
        client = NativeTasks(PROFILE, state_dir=self.directory.name)
        with patch('native.get_password', side_effect=AssertionError('credential access')):
            result = client.plan(self.spec)
        self.assertFalse(result['availability_checked'])
        self.assertIsNone(result['eligible_nodes_now'])
        self.assertNotIn('projectId', result['request'])
        self.assertFalse(result['project_id_resolved'])
        self.assertIn('tasks', str(client.registry.root))
        self.assertEqual(result['native_compatibility'], 'validated_single_node')
        self.assertFalse(result['experimental_required'])

    def test_normal_native_submission_does_not_require_experimental_flag(self):
        result = self.client.submit(self.spec, execute=True)
        self.assertEqual(result['state'], 'QUEUED')
        self.assertEqual(self.api.count_creates(), 1)

    def test_default_command_budget_rejects_long_embedded_command_before_network(self):
        source = dict(self.spec)
        source.pop('command_max_bytes')
        prepared = prepare_task_spec(source, PROFILE)
        self.assertEqual(prepared['command_max_bytes'], 1000)
        with self.assertRaisesRegex(ValueError, 'stage guard_script and guard_manifest'):
            self.client.plan(prepared, online=True)
        self.assertEqual(self.api.calls, [])

    def test_native_duration_does_not_use_development_limit(self):
        self.assertGreater(self.spec['max_seconds'], 13800)
        with self.assertRaises(ValueError):
            prepare_task_spec(dict(self.spec, group_max_seconds=3600), PROFILE)

    def test_guide_and_multi_node_specs_cannot_execute(self):
        for changes in ({'backend': 'native-guide'}, {'executable': False}, {'nodes': 2}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                self.client.submit(dict(self.spec, **changes), execute=True, experimental=True)
        self.assertEqual(self.api.calls, [])

    def test_payload_has_once_encoded_config_and_explicit_mounts_only(self):
        payload = build_task_payload(self.spec, USER['groupId'])
        self.assertIsInstance(payload['config'], str)
        worker = json.loads(payload['config'])['worker']
        self.assertEqual(worker['nodeNum'], 1)
        self.assertEqual(worker['acceleratorCardNum'], 1)
        self.assertEqual(payload['jobVolume'], [])
        self.assertNotIn('taskType', payload)
        self.assertEqual(payload['projectId'], USER['groupId'])
        self.assertIn('--kill-after=30', payload['command'])
        self.assertTrue(payload['description'].startswith('wlai:'))

    def test_compressed_loader_restores_exact_runner_source_and_keeps_manifest_separate(self):
        command = shlex.split(build_task_payload(self.spec)['command'])
        code_index = command.index('-c')
        loader = base64.b64decode(command[code_index + 2]).decode('utf-8')
        manifest = command[-1]
        captured = []
        def capture_compile(source, filename, mode):
            captured.append((source, filename, mode))
            return compile('pass', filename, mode)
        exec(loader, {'compile': capture_compile})
        self.assertEqual(captured, [(SCRIPTS.joinpath('native_runner.py').read_bytes(), '<wlai-native-runner>', 'exec')])
        self.assertEqual(json.loads(base64.urlsafe_b64decode(manifest))['argv'], self.spec['argv'])
        self.assertNotIn('def verify_storage', loader)

    def test_guard_digest_is_pinned_to_current_bundled_source(self):
        expected = hashlib.sha256(SCRIPTS.joinpath('native_runner.py').read_bytes()).hexdigest()
        self.assertEqual(self.spec['guard_script_sha256'], expected)
        staged = prepare_task_spec(dict(self.spec, guard_script='/sample-user/tools/native_runner.py'), PROFILE)
        self.assertEqual(staged['guard_script_sha256'], expected)
        for invalid in (None, False, '', '00' * 32, expected.upper()):
            with self.subTest(invalid=invalid), self.assertRaisesRegex(ValueError, 'current bundled'):
                prepare_task_spec(dict(staged, guard_script_sha256=invalid), PROFILE)

    def test_staged_guard_must_be_an_absolute_path_below_personal_mount(self):
        for path in ('native_runner.py', '/sample-user/../other/runner.py', '/sample-user',
                     '/other/runner.py', '/sample-user-other/runner.py', '', None):
            with self.subTest(path=path), self.assertRaises(ValueError):
                prepare_task_spec(dict(self.spec, guard_script=path), PROFILE)

    def test_all_guard_commands_are_single_line_including_literal_multiline_argv(self):
        for mode in ('embedded', 'staged', 'staged-files'):
            with self.subTest(mode=mode):
                source = dict(self.spec, argv=self.spec['argv'] + ['literal\nargument\rvalue'])
                if mode != 'embedded':
                    source['guard_script'] = '/sample-user/tools/native_runner.py'
                if mode == 'staged-files':
                    source['guard_manifest'] = '/sample-user/manifests/example.json'
                command = build_task_payload(prepare_task_spec(source, PROFILE))['command']
                self.assertNotIn('\n', command)
                self.assertNotIn('\r', command)
                self.assertNotIn(chr(34), command)
                self.assertEqual(command.replace(chr(34), chr(39)).replace('\n', ' '), command)

    def local_staged_guard(self):
        root = Path(self.directory.name)
        script = root / "native runner '; 源.py"
        script.write_bytes(SCRIPTS.joinpath('native_runner.py').read_bytes())
        profile = dict(PROFILE, persistent_root=str(root / 'runs'), storage_mount=str(root))
        spec = prepare_task_spec(dict(self.spec, run_id='local_staged', storage_mount=str(root),
                                      guard_script=str(script)), profile)
        command = shlex.split(build_task_payload(spec)['command'])
        return spec, script, base64.b64decode(command[command.index('-c') + 2]).decode('utf-8'), command[-1]

    def test_staged_loader_executes_verified_complete_source_with_separate_manifest(self):
        spec, script, loader, manifest = self.local_staged_guard()
        captured = []
        def capture_compile(source, filename, mode):
            captured.append((source, filename, mode))
            # Changing the path after verification cannot substitute different
            # bytes between the checksum and execution.
            script.write_bytes(b"raise RuntimeError('must not reopen guard script')\n")
            return compile(source, filename, mode)
        namespace = {'__name__': 'guard_loader_test', 'compile': capture_compile}
        exec(loader, namespace)
        self.assertEqual(captured, [(SCRIPTS.joinpath('native_runner.py').read_bytes(), str(script), 'exec')])
        self.assertEqual(namespace['normalized_model']('Example_GPU'), 'examplegpu')
        for name in ('verify_storage', 'run', 'main'):
            self.assertTrue(callable(namespace[name]))
        self.assertEqual(json.loads(base64.urlsafe_b64decode(manifest))['argv'], spec['argv'])
        self.assertNotIn(manifest, loader)
        self.assertNotIn('def verify_storage', loader)

    def test_staged_guard_checksum_mismatch_never_compiles_or_executes_program(self):
        spec, script, loader, manifest = self.local_staged_guard()
        marker = Path(self.directory.name) / 'scientific_program_executed'
        script.write_bytes(script.read_bytes() + ("\nopen(" + repr(str(marker))
                           + ", 'w').write('executed')\n").encode('utf-8'))
        namespace = {'__name__': 'guard_loader_test'}
        loader_code = compile(loader, '<test-loader>', 'exec')
        with patch('builtins.compile', side_effect=AssertionError('unverified source compiled')) as compiler:
            with self.assertRaisesRegex(SystemExit, 'WLAI_NATIVE_GUARD_SHA256_MISMATCH'):
                exec(loader_code, namespace)
        compiler.assert_not_called()
        self.assertNotIn('run', namespace)
        self.assertFalse(marker.exists())

    def test_missing_staged_guard_never_compiles_or_executes_program(self):
        spec, script, loader, manifest = self.local_staged_guard()
        script.unlink()
        loader_code = compile(loader, '<test-loader>', 'exec')
        with patch('builtins.compile', side_effect=AssertionError('missing source compiled')) as compiler:
            with self.assertRaises(FileNotFoundError):
                exec(loader_code, {'__name__': 'guard_loader_test'})
        compiler.assert_not_called()

    def test_staged_manifest_requires_guard_and_same_personal_mount(self):
        with self.assertRaisesRegex(ValueError, 'requires a staged guard_script'):
            prepare_task_spec(dict(self.spec, guard_manifest='/sample-user/manifest.json'), PROFILE)
        for path in ('manifest.json', '/sample-user/../other/manifest.json', '/sample-user',
                     '/other/manifest.json', '/sample-user-other/manifest.json', '', None):
            with self.subTest(path=path), self.assertRaises(ValueError):
                prepare_task_spec(dict(self.spec, guard_script='/sample-user/tools/native_runner.py',
                                       guard_manifest=path), PROFILE)
        with self.assertRaisesRegex(ValueError, 'is derived'):
            prepare_task_spec(dict(self.spec, guard_manifest_sha256='00' * 32), PROFILE)

    def local_staged_files(self):
        spec, script, _, _ = self.local_staged_guard()
        root = Path(self.directory.name)
        manifest = root / "manifest '; 参数.json"
        profile = dict(PROFILE, persistent_root=spec['persistent_root'], storage_mount=spec['storage_mount'])
        spec = prepare_task_spec(dict(spec, guard_manifest=str(manifest)), profile)
        client = NativeTasks(profile, state_dir=root / 'staged_files_state', client=self.api, user=USER)
        plan = client.plan(spec)
        manifest.write_bytes(json.dumps(plan['guard_manifest_content'], sort_keys=True,
                                        separators=(',', ':')).encode('utf-8'))
        command = shlex.split(plan['request']['command'])
        return spec, script, manifest, base64.b64decode(command[command.index('-c') + 2]).decode('utf-8'), plan

    def test_staged_files_loader_verifies_both_complete_buffers_before_execution(self):
        spec, script, manifest, loader, plan = self.local_staged_files()
        expected_manifest = manifest.read_bytes()
        captured = []
        def capture_compile(source, filename, mode):
            captured.append((source, filename, mode))
            script.write_bytes(b"raise RuntimeError('must not reopen guard')\n")
            manifest.write_bytes(b'{}')
            return compile(source, filename, mode)
        namespace = {'__name__': 'guard_loader_test', 'compile': capture_compile}
        with patch.object(sys, 'argv', ['-c', 'old_manifest', 'old_extra']):
            exec(loader, namespace)
            self.assertEqual(len(sys.argv), 2)
            self.assertEqual(base64.urlsafe_b64decode(sys.argv[1]), expected_manifest)
        self.assertEqual(captured, [(SCRIPTS.joinpath('native_runner.py').read_bytes(), '<wlai_native_guard>', 'exec')])
        self.assertEqual(namespace['normalized_model']('Example_GPU'), 'examplegpu')
        self.assertTrue(callable(namespace['run']))
        self.assertEqual(hashlib.sha256(expected_manifest).hexdigest(), plan['guard_manifest_sha256'])
        self.assertEqual(plan['guard_manifest_content']['spec_sha256'], spec_fingerprint(spec))
        self.assertNotIn('guard_manifest_sha256', spec)

    def test_wire_envelope_survives_observed_platform_quote_and_newline_rewrite(self):
        spec, script, manifest, loader, plan = self.local_staged_files()
        command = plan['request']['command']
        rewritten = command.replace(chr(34), chr(39)).replace('\n', ' ')
        self.assertEqual(rewritten, command)
        words = shlex.split(rewritten)
        offset = words.index('-c')
        captured = []
        def capture_compile(source, filename, mode):
            captured.append(source)
            return compile(source, filename, mode)
        namespace = {'__name__': 'guard_wire_test', 'compile': capture_compile}
        with patch.object(sys, 'argv', ['-c', *words[offset + 2:]]):
            exec(words[offset + 1], namespace)
            self.assertEqual(json.loads(base64.urlsafe_b64decode(sys.argv[1])), plan['guard_manifest_content'])
        self.assertEqual(captured, [SCRIPTS.joinpath('native_runner.py').read_bytes()])
        self.assertTrue(callable(namespace['run']))

    def test_new_submission_stops_if_platform_changes_command(self):
        original = self.api.call
        def changed(method, path, params=None, body=None):
            result = original(method, path, params, body)
            if method == 'POST' and path.endswith('/train'):
                self.api.tasks[result['id']]['command'] += ' ; true'
            return result
        self.api.call = changed
        result = self.client.submit(self.spec, execute=True, experimental=True)
        self.assertEqual(result['state'], 'COMMAND_MISMATCH')
        self.assertTrue(result['command_mismatch'])
        self.assertTrue(result['resource_released'])
        self.assertEqual(self.api.count_creates(), 1)
        self.assertEqual(self.api.count_stops(), 1)

    def test_observer_reports_changed_command_without_mutating_platform(self):
        result = self.client.submit(self.spec, execute=True, experimental=True)
        self.api.tasks[result['task_id']]['command'] += ' ; true'
        updated = self.client.status(self.spec['run_id'])
        self.assertEqual(updated['state'], 'COMMAND_MISMATCH')
        self.assertTrue(updated['requires_stop'])
        self.assertEqual(self.api.count_stops(), 0)

    def test_staged_files_checksum_mismatch_in_either_file_never_compiles(self):
        for changed in ('guard', 'manifest', 'both'):
            with self.subTest(changed=changed):
                spec, script, manifest, loader, plan = self.local_staged_files()
                if changed in ('guard', 'both'):
                    script.write_bytes(script.read_bytes() + b'\nraise RuntimeError("unverified program executed")\n')
                if changed in ('manifest', 'both'):
                    manifest.write_bytes(manifest.read_bytes() + b'\n')
                loader_code = compile(loader, '<test-loader>', 'exec')
                namespace = {'__name__': 'guard_loader_test'}
                with patch.object(sys, 'argv', ['-c', 'unchanged']), \
                     patch('builtins.compile', side_effect=AssertionError('unverified file compiled')) as compiler:
                    with self.assertRaisesRegex(SystemExit, 'WLAI_NATIVE_FILE_SHA256_MISMATCH'):
                        exec(loader_code, namespace)
                    self.assertEqual(sys.argv, ['-c', 'unchanged'])
                compiler.assert_not_called()
                self.assertNotIn('run', namespace)

    def test_missing_staged_manifest_never_compiles(self):
        spec, script, manifest, loader, plan = self.local_staged_files()
        manifest.unlink()
        loader_code = compile(loader, '<test-loader>', 'exec')
        with patch('builtins.compile', side_effect=AssertionError('missing manifest compiled')) as compiler:
            with self.assertRaises(FileNotFoundError):
                exec(loader_code, {'__name__': 'guard_loader_test'})
        compiler.assert_not_called()

    def test_configured_command_budget_rejects_before_any_api_access(self):
        smaller = prepare_task_spec(dict(self.spec, command_max_bytes=100), PROFILE)
        for action in (lambda: self.client.plan(smaller, online=True),
                       lambda: self.client.submit(smaller, execute=True, experimental=True)):
            with self.assertRaisesRegex(ValueError, 'exceeding configured command_max_bytes'):
                action()
        self.assertEqual(self.api.calls, [])
        for invalid in (False, 0, -1, '8000'):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                prepare_task_spec(dict(self.spec, command_max_bytes=invalid), PROFILE)

    def test_plan_reports_utf8_command_bytes_without_guessing_server_limit(self):
        plan = self.client.plan(self.spec)
        self.assertEqual(plan['command_bytes'], len(plan['request']['command'].encode('utf-8')))
        self.assertEqual(plan['guard_mode'], 'embedded')
        self.assertEqual(plan['guard_script_sha256'], self.spec['guard_script_sha256'])
        self.assertIsNone(plan['guard_manifest_content'])
        self.assertIsNone(plan['guard_manifest_sha256'])
        self.assertEqual(plan['configured_command_max_bytes'], 20000)
        limited = prepare_task_spec(dict(self.spec, run_id='limited', command_max_bytes=20000), PROFILE)
        self.assertEqual(self.client.plan(limited)['configured_command_max_bytes'], 20000)

    def test_staged_plan_reports_short_loader_without_reading_remote_script(self):
        embedded_bytes = len(build_task_payload(self.spec)['command'].encode('utf-8'))
        staged = prepare_task_spec(dict(self.spec, run_id='staged',
            guard_script='/sample-user/tools/native_runner.py', command_max_bytes=embedded_bytes - 1), PROFILE)
        with patch('native.get_password', side_effect=AssertionError('credential access')):
            plan = self.client.plan(staged)
        self.assertEqual(plan['guard_mode'], 'staged')
        self.assertEqual(plan['guard_script_sha256'], staged['guard_script_sha256'])
        self.assertEqual(plan['command_bytes'], len(plan['request']['command'].encode('utf-8')))
        self.assertLess(plan['command_bytes'], embedded_bytes)
        self.assertEqual(self.api.calls, [])

    def test_staged_files_plan_exports_canonical_manifest_without_embedding_long_argv(self):
        staged = prepare_task_spec(dict(self.spec, run_id='staged_files',
            guard_script='/sample-user/tools/native_runner.py', guard_manifest='/sample-user/manifests/example.json',
            argv=self.spec['argv'] + ['long-literal-argument;' * 500]), PROFILE)
        with patch('native.get_password', side_effect=AssertionError('credential access')):
            plan = self.client.plan(staged)
        self.assertEqual(plan['guard_mode'], 'staged-files')
        content = plan['guard_manifest_content']
        serialized = json.dumps(content, sort_keys=True, separators=(',', ':')).encode('utf-8')
        self.assertEqual(plan['guard_manifest_sha256'], hashlib.sha256(serialized).hexdigest())
        self.assertEqual(content['argv'], staged['argv'])
        self.assertEqual(content['spec_sha256'], spec_fingerprint(staged))
        self.assertNotIn('guard_manifest_sha256', plan['spec'])
        self.assertNotIn(staged['argv'][-1], plan['request']['command'])
        self.assertEqual(plan['command_bytes'], len(plan['request']['command'].encode('utf-8')))
        short = prepare_task_spec(dict(staged, argv=self.spec['argv']), PROFILE)
        self.assertEqual(plan['command_bytes'], len(build_task_payload(short)['command'].encode('utf-8')))
        self.assertEqual(self.api.calls, [])

    def test_environment_values_remain_literal_arguments(self):
        spec = prepare_task_spec(dict(self.spec, env={'EXPERIMENT': 'with spaces; $(not-executed)'}), PROFILE)
        import shlex
        command = shlex.split(build_task_payload(spec)['command'])
        manifest = json.loads(base64.urlsafe_b64decode(command[-1]))
        self.assertEqual(manifest['env'], spec['env'])
        self.assertEqual(manifest['argv'], spec['argv'])
        with self.assertRaises(ValueError):
            prepare_task_spec(dict(self.spec, env={'BAD-KEY': 'value'}), PROFILE)

    def test_training_group_query_uses_observed_labels(self):
        plan = self.client.plan(self.spec, online=True)
        self.assertTrue(plan['group_label_verified'])
        query = next(params for method, path, params, body in self.api.calls if path.endswith('/node-group'))
        self.assertEqual(query['groupLabel'], 'usual,train')

    def test_development_only_group_is_rejected(self):
        self.api.label = 'develop'
        with self.assertRaises(ValueError):
            self.client.submit(self.spec, execute=True, experimental=True)
        self.assertEqual(self.api.count_creates(), 0)

    def test_busy_compatible_node_requires_explicit_queue(self):
        self.api.busy = True
        with self.assertRaises(RuntimeError):
            self.client.submit(self.spec, execute=True, experimental=True)
        self.assertEqual(self.api.count_creates(), 0)
        result = self.client.submit(self.spec, execute=True, allow_queue=True, experimental=True)
        self.assertEqual(result['state'], 'QUEUED')
        self.assertEqual(self.api.count_creates(), 1)

    def test_queue_does_not_turn_impossible_capacity_into_multi_node(self):
        larger = prepare_task_spec(dict(self.spec, gpus=8), PROFILE)
        with self.assertRaises(ValueError):
            self.client.submit(larger, execute=True, allow_queue=True, experimental=True)
        self.assertEqual(self.api.count_creates(), 0)

    def test_repeat_submit_never_creates_a_duplicate(self):
        first = self.client.submit(self.spec, execute=True, experimental=True)
        second = self.client.submit(self.spec, execute=True)
        self.assertEqual(first['task_id'], second['task_id'])
        self.assertEqual(self.api.count_creates(), 1)
        self.assertFalse(second['scientific_outputs_verified'])

    def test_unknown_creation_reconciles_without_repeating_post(self):
        self.api.raise_after_create = True
        result = self.client.submit(self.spec, execute=True, experimental=True)
        self.assertEqual(result['state'], 'UNKNOWN')
        self.assertNotIn('synthetic-hidden-transport-detail', json.dumps(result))
        with self.assertRaises(RuntimeError):
            self.client.submit(self.spec, execute=True, experimental=True)
        resolved = self.client.reconcile('example')
        self.assertEqual(resolved['state'], 'QUEUED')
        self.assertEqual(self.api.count_creates(), 1)

    def test_reconciliation_uses_frozen_command_across_client_upgrade(self):
        self.api.raise_after_create = True
        self.client.submit(self.spec, execute=True, experimental=True)
        snapshot = self.client.registry.get('example')['submitted_request']['command']
        with patch('native.build_task_payload', side_effect=AssertionError('must not rebuild old command')):
            result = self.client.reconcile('example')
        self.assertEqual(result['state'], 'QUEUED')
        self.assertEqual(next(iter(self.api.tasks.values()))['command'], snapshot)
        self.assertEqual(self.api.count_creates(), 1)

    def test_staged_reconciliation_uses_frozen_request_without_current_runner_validation(self):
        staged = prepare_task_spec(dict(self.spec, guard_script='/sample-user/tools/native_runner.py'), PROFILE)
        self.api.raise_after_create = True
        self.client.submit(staged, execute=True, experimental=True)
        snapshot = self.client.registry.get('example')['submitted_request']
        with patch('native.prepare_task_spec', side_effect=AssertionError('old digest must not be renormalized')), \
             patch('native.build_task_payload', side_effect=AssertionError('old command must not be rebuilt')):
            result = self.client.reconcile('example')
        self.assertEqual(result['state'], 'QUEUED')
        self.assertEqual(next(iter(self.api.tasks.values()))['command'], snapshot['command'])
        self.assertEqual(self.api.count_creates(), 1)

    def test_staged_files_reconciliation_keeps_frozen_command_and_manifest_digest(self):
        staged = prepare_task_spec(dict(self.spec, guard_script='/sample-user/tools/native_runner.py',
                                       guard_manifest='/sample-user/manifests/example.json'), PROFILE)
        self.api.raise_after_create = True
        self.client.submit(staged, execute=True, experimental=True)
        snapshot = self.client.registry.get('example')['submitted_request']
        with patch('native.prepare_task_spec', side_effect=AssertionError('must not renormalize old files')), \
             patch('native.build_task_payload', side_effect=AssertionError('must not rebuild old loader')), \
             patch('native._runner_manifest_bytes', side_effect=AssertionError('must not regenerate old manifest')):
            result = self.client.reconcile('example')
        self.assertEqual(result['state'], 'QUEUED')
        self.assertEqual(next(iter(self.api.tasks.values()))['command'], snapshot['command'])
        self.assertEqual(self.api.count_creates(), 1)

    def test_rejected_creation_keeps_code_without_echoing_message(self):
        class Rejected(RuntimeError):
            code = 'IRESOURCE_MAX_WOKR_PLATFORM_COUNT_EXCEED'
        original = self.api.call
        def rejected(method, path, params=None, body=None):
            if method == 'POST' and path.endswith('/train'):
                raise Rejected('synthetic-hidden-credential-material')
            return original(method, path, params, body)
        self.api.call = rejected
        result = self.client.submit(self.spec, execute=True, experimental=True)
        self.assertEqual(result['state'], 'UNKNOWN')
        self.assertEqual(result['creation_error']['platform_code'], Rejected.code)
        self.assertNotIn('synthetic-hidden-credential-material', json.dumps(result))

    def test_known_flag_false_rejection_is_not_an_unknown_or_failed_gpu_run(self):
        original = self.api.call
        attempts = []
        def rejected(method, path, params=None, body=None):
            if method == 'POST' and path.endswith('/train'):
                attempts.append(body)
                raise ApiError('response suppressed', code='IRESOURCE_STRING_LENGTH_ILLEGAL',
                               operation=path, method=method)
            return original(method, path, params, body)
        self.api.call = rejected
        result = self.client.submit(self.spec, execute=True, experimental=True)
        self.assertEqual(result['state'], 'REJECTED')
        self.assertIsNone(result['uncertain_action'])
        self.assertTrue(result['resource_released'])
        self.assertFalse(result['requires_reconcile'])
        self.assertEqual(result['release_basis'], 'platform_rejected_no_task_created')
        self.assertEqual(result['creation_error']['platform_code'], 'IRESOURCE_STRING_LENGTH_ILLEGAL')
        self.assertNotIn('task_id', result)
        self.assertEqual(self.client.submit(self.spec, execute=True, experimental=True), result)
        self.assertEqual(len(attempts), 1)

    def test_rejected_task_operations_use_local_evidence_without_network_or_unknown(self):
        self.client.plan(self.spec)
        rejected = self.client.registry.update('example', state='REJECTED', resource_released=True,
            uncertain_action=None, requires_reconcile=False,
            release_basis='platform_rejected_no_task_created',
            creation_error={'type': 'ApiError', 'platform_code': 'COMMON_PARAMETER_NOT_MATCH'})
        self.api.call = lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError('Rejected run used network'))
        for operation in (self.client.status, self.client.logs, self.client.stop, self.client.reconcile,
                          lambda run_id: self.client.watch(run_id, interval=0.01, timeout=0.01)):
            with self.subTest(operation=operation):
                self.assertEqual(operation('example'), rejected)

    def test_http_500_and_transport_timeout_stay_unknown_even_with_validation_like_code(self):
        for index, error in enumerate((ApiError('http failure', status=500, code='COMMON_PARAMETER_NOT_MATCH'),
                                       TimeoutError('synthetic-hidden-transport-detail'))):
            with self.subTest(error=type(error).__name__):
                original = FakeAPI().call
                def ambiguous(method, path, params=None, body=None):
                    if method == 'POST' and path.endswith('/train'):
                        raise error
                    return original(method, path, params, body)
                self.api.call = ambiguous
                spec = prepare_task_spec(dict(self.spec, run_id='ambiguous_' + str(index)), PROFILE)
                result = self.client.submit(spec, execute=True, experimental=True)
                self.assertEqual(result['state'], 'UNKNOWN')
                self.assertFalse(result['resource_released'])
                self.assertEqual(result['uncertain_action'], 'task-creation')

    def test_only_known_api_validation_codes_can_claim_no_task_was_created(self):
        codes = ('COMMON_PARAMETER_NOT_MATCH', 'IRESOURCE_INPUT_GPU_MORE_THAN_WORK_PLATFORM_CONFIG_LIMIT',
                 'IRESOURCE_CREATE_WORK_PLATFORM_RES_GROUP_NOT_MATCH_LABEL', 'IRESOURCE_UNCLASSIFIED_FAILURE')
        for index, code in enumerate(codes):
            with self.subTest(code=code):
                original = FakeAPI().call
                def rejected(method, path, params=None, body=None):
                    if method == 'POST' and path.endswith('/train'):
                        raise ApiError('response suppressed', code=code)
                    return original(method, path, params, body)
                self.api.call = rejected
                spec = prepare_task_spec(dict(self.spec, run_id='rejected_' + str(index)), PROFILE)
                result = self.client.submit(spec, execute=True, experimental=True)
                known = code != 'IRESOURCE_UNCLASSIFIED_FAILURE'
                self.assertEqual(result['state'], 'REJECTED' if known else 'UNKNOWN')
                self.assertEqual(result['resource_released'], known)

    def test_name_collision_requires_matching_fingerprint(self):
        self.api.raise_after_create = True
        self.client.submit(self.spec, execute=True, experimental=True)
        row = next(iter(self.api.tasks.values())); row['description'] = 'other submission'
        with self.assertRaises(RuntimeError):
            self.client.reconcile('example')
        self.assertEqual(self.api.count_stops(), 0)

    def test_cpu_fallback_is_stopped_without_using_workplatform(self):
        self.api.mismatch = True
        result = self.client.submit(self.spec, execute=True, experimental=True)
        self.assertEqual(result['state'], 'RESOURCE_MISMATCH')
        self.assertTrue(result['resource_released'])
        self.assertFalse(result['gpu_config_verified'])
        self.assertEqual(self.api.count_stops(), 1)
        self.assertFalse(any('work-platform' in path for method, path, *_ in self.api.calls))

    def _running_task(self):
        result = self.client.submit(self.spec, execute=True)
        self.api.tasks[result['task_id']]['status'] = 'Running'
        return result['task_id']

    def test_invisible_stop_does_not_mutate_and_later_visible_stop_recovers_legacy_intent(self):
        self._running_task()
        self.client.registry.update('example', stop_requested=True, stop_uncertain=True)
        self.api.no_visibility = True
        first = self.client.stop('example')
        self.assertFalse(first['resource_released'])
        self.assertFalse(first['stop_confirmed'])
        self.assertTrue(first['stop_uncertain'])
        self.assertEqual(self.api.count_stops(), 0)
        self.api.no_visibility = False
        second = self.client.stop('example')
        self.assertTrue(second['resource_released'])
        self.assertTrue(second['stop_confirmed'])
        self.assertFalse(second['stop_uncertain'])
        self.assertEqual(self.api.count_stops(), 1)

    def test_running_stop_uses_post_without_body(self):
        task_id = self._running_task()
        result = self.client.stop('example')
        self.assertTrue(result['resource_released'])
        self.assertTrue(result['stop_confirmed'])
        requests = [(method, path, body) for method, path, _, body in self.api.calls if method != 'GET']
        self.assertEqual(requests[-1], ('POST', '/api/iresource/v1/train/' + task_id + '/stop', None))
        self.assertEqual(result['stop_action'], 'stop_running')
        self.assertTrue(result['stop_attempts'][-1]['accepted'])

    def test_each_queued_state_uses_only_unfinished_delete(self):
        for index, platform in enumerate(('Pending', 'Queuing', 'ImagePulling', 'DatasetPulling')):
            with self.subTest(platform=platform):
                spec = prepare_task_spec(dict(self.spec, run_id='queued_' + str(index), task_name='queued_' + str(index)), PROFILE)
                task_id = self.client.submit(spec, execute=True)['task_id']
                self.api.tasks[task_id]['status'] = platform
                other = copy.deepcopy(self.api.tasks[task_id]); other.update(id='unrelated', userId='someone-else')
                self.api.tasks['unrelated'] = other
                result = self.client.stop(spec['run_id'])
                self.assertTrue(result['resource_released'])
                mutations = [(method, path, body) for method, path, _, body in self.api.calls if method == 'DELETE']
                self.assertEqual(mutations[-1], ('DELETE', '/api/iresource/v1/train',
                    {'jobIdList': [task_id], 'endTaskListFlag': False}))
                self.assertEqual(self.api.tasks['unrelated']['status'], platform)
                self.assertEqual(result['stop_platform_state'], platform)
                self.assertEqual(result['stop_action'], 'cancel_unfinished')

    def test_explicit_rejection_can_be_retried_by_next_stop_call(self):
        self._running_task()
        original = self.api.call
        attempts = []
        def first_rejected(method, path, params=None, body=None):
            if method == 'POST' and path.endswith('/stop'):
                attempts.append(path)
                if len(attempts) == 1:
                    raise ApiError('synthetic rejection', code='IRESOURCE_STOP_TRAIN_JOB_FAILED')
            return original(method, path, params=params, body=body)
        self.api.call = first_rejected
        first = self.client.stop('example')
        self.assertFalse(first['stop_accepted'])
        self.assertFalse(first['stop_requested'])
        self.assertTrue(first['stop_rejected'])
        self.assertFalse(first['resource_released'])
        second = self.client.stop('example')
        self.assertEqual(len(attempts), 2)
        self.assertTrue(second['resource_released'])
        self.assertTrue(second['stop_confirmed'])
        self.assertEqual(second['stop_attempts'][0]['error']['platform_code'], 'IRESOURCE_STOP_TRAIN_JOB_FAILED')

    def test_legacy_stop_requested_does_not_block_a_new_explicit_stop(self):
        self._running_task()
        self.client.registry.update('example', stop_requested=True, stop_uncertain=True)
        result = self.client.stop('example')
        self.assertTrue(result['resource_released'])
        self.assertEqual(self.api.count_stops(), 1)

    def test_stop_rejection_racing_with_terminal_state_confirms_release(self):
        task_id = self._running_task()
        original = self.api.call
        def race(method, path, params=None, body=None):
            if method == 'POST' and path.endswith('/stop'):
                self.api.tasks[task_id]['status'] = 'Stopped'
                raise ApiError('synthetic rejection', code='IRESOURCE_STOP_TRAIN_JOB_FAILED')
            return original(method, path, params=params, body=body)
        self.api.call = race
        result = self.client.stop('example')
        self.assertTrue(result['resource_released'])
        self.assertTrue(result['stop_confirmed'])
        self.assertFalse(result['stop_uncertain'])
        self.assertEqual(result['state'], 'CANCELLED')
        self.assertEqual(result['stop_error']['platform_code'], 'IRESOURCE_STOP_TRAIN_JOB_FAILED')

    def test_transport_error_after_server_stop_is_resolved_by_readback(self):
        task_id = self._running_task()
        original = self.api.call
        def accepted_then_disconnected(method, path, params=None, body=None):
            if method == 'POST' and path.endswith('/stop'):
                self.api.tasks[task_id]['status'] = 'Stopped'
                raise TimeoutError('synthetic hidden transport detail')
            return original(method, path, params=params, body=body)
        self.api.call = accepted_then_disconnected
        result = self.client.stop('example')
        self.assertTrue(result['resource_released'])
        self.assertTrue(result['stop_confirmed'])
        self.assertFalse(result['stop_uncertain'])
        self.assertIsNone(result['stop_accepted'])

    def test_ambiguous_stop_is_not_replayed_in_same_call_and_can_be_retried_explicitly(self):
        self._running_task()
        original = self.api.call
        attempts = []
        def disconnected(method, path, params=None, body=None):
            if method == 'POST' and path.endswith('/stop'):
                attempts.append(path)
                raise TimeoutError('synthetic hidden transport detail')
            return original(method, path, params=params, body=body)
        self.api.call = disconnected
        clock = [0.0]
        with patch('native.time.monotonic', side_effect=lambda: clock[0]), \
             patch('native.time.sleep', side_effect=lambda delay: clock.__setitem__(0, clock[0] + delay)):
            result = self.client.stop('example', timeout=2, interval=1)
        self.assertEqual(len(attempts), 1)
        self.assertTrue(result['stop_uncertain'])
        self.assertFalse(result['resource_released'])
        self.api.call = original
        result = self.client.stop('example')
        self.assertTrue(result['resource_released'])
        self.assertTrue(result['stop_confirmed'])

    def test_disappearance_after_queued_cancel_is_not_release_evidence(self):
        task_id = self.client.submit(self.spec, execute=True)['task_id']
        original = self.api.call
        def disappear(method, path, params=None, body=None):
            if method == 'DELETE':
                self.api.tasks.pop(task_id)
                return None
            return original(method, path, params=params, body=body)
        self.api.call = disappear
        clock = [0.0]
        with patch('native.time.monotonic', side_effect=lambda: clock[0]), \
             patch('native.time.sleep', side_effect=lambda delay: clock.__setitem__(0, clock[0] + delay)):
            result = self.client.stop('example', timeout=2, interval=1)
        self.assertTrue(result['stop_accepted'])
        self.assertTrue(result['stop_uncertain'])
        self.assertFalse(result['stop_confirmed'])
        self.assertFalse(result['resource_released'])

    def test_terminating_task_is_observed_without_another_mutation(self):
        task_id = self._running_task()
        self.api.tasks[task_id]['status'] = 'Terminating'
        original = self.api.call
        reads = []
        def finish(method, path, params=None, body=None):
            if method == 'GET' and path.endswith('/train'):
                reads.append(path)
                if len(reads) > 1:
                    self.api.tasks[task_id]['status'] = 'Stopped'
            return original(method, path, params=params, body=body)
        self.api.call = finish
        result = self.client.stop('example')
        self.assertTrue(result['resource_released'])
        self.assertEqual(self.api.count_stops(), 0)
        self.assertEqual(result['stop_action'], 'await_termination')

    def test_cancelled_pending_row_requires_delete_flag_and_empty_pods(self):
        task_id = self.client.submit(self.spec, execute=True)['task_id']
        original = self.api.call
        mutations = []
        def mark_deleted(method, path, params=None, body=None):
            if method == 'DELETE':
                mutations.append(copy.deepcopy(body))
                self.api.tasks[task_id].update(status='Pending', deleteFlag=True, deleteTime=12345, endTaskWay=1)
                return {'trainNames': self.spec['task_name']}
            return original(method, path, params=params, body=body)
        self.api.call = mark_deleted
        result = self.client.stop('example')
        self.assertEqual(result['state'], 'CANCELLED')
        self.assertEqual(result['platform_state'], 'Pending')
        self.assertTrue(result['platform_deleted'])
        self.assertTrue(result['stop_confirmed'])
        self.assertTrue(result['resource_released'])
        self.assertEqual(result['release_basis'], 'platform_delete_flag_and_no_active_pods')
        self.assertEqual(result['cancel_release_evidence']['pod_total'], 0)
        for operation in (self.client.status, self.client.reconcile,
                          lambda run: self.client.watch(run, timeout=.01, interval=.001)):
            observed = operation('example')
            self.assertEqual(observed['state'], 'CANCELLED')
            self.assertTrue(observed['resource_released'])
        self.assertEqual(len(mutations), 1)

    def test_invisible_cancelled_task_needs_exact_owned_ack_and_empty_pods(self):
        task_id = self.client.submit(self.spec, execute=True)['task_id']
        original = self.api.call
        def remove_and_ack(method, path, params=None, body=None):
            if method == 'DELETE':
                self.api.tasks.pop(task_id)
                return {'trainNames': self.spec['task_name']}
            return original(method, path, params=params, body=body)
        self.api.call = remove_and_ack
        result = self.client.stop('example')
        self.assertTrue(result['stop_owner_verified'])
        self.assertEqual(result['cancel_ack_task_name'], self.spec['task_name'])
        self.assertTrue(result['resource_released'])
        self.assertEqual(result['release_basis'], 'platform_cancel_ack_and_no_active_pods')
        self.assertTrue(self.client.status('example')['resource_released'])
        self.assertTrue(self.client.stop('example')['resource_released'])

    def test_wrong_cancel_ack_cannot_prove_invisible_task_release(self):
        task_id = self.client.submit(self.spec, execute=True)['task_id']
        original = self.api.call
        def wrong_ack(method, path, params=None, body=None):
            if method == 'DELETE':
                self.api.tasks.pop(task_id)
                return {'trainNames': 'someone_else_task'}
            return original(method, path, params=params, body=body)
        self.api.call = wrong_ack
        clock = [0.0]
        with patch('native.time.monotonic', side_effect=lambda: clock[0]), \
             patch('native.time.sleep', side_effect=lambda delay: clock.__setitem__(0, clock[0] + delay)):
            result = self.client.stop('example', timeout=2, interval=1)
        self.assertIsNone(result['cancel_ack_task_name'])
        self.assertFalse(result['resource_released'])
        self.assertFalse(result['stop_confirmed'])

    def test_delete_flag_without_complete_no_active_pod_evidence_is_not_release(self):
        task_id = self.client.submit(self.spec, execute=True)['task_id']
        self.api.tasks[task_id].update(deleteFlag=True, status='Pending')
        for data in ({'total': 1, 'data': [{'podStatus': 'Running'}]},
                     {'total': 1, 'data': [{'podStatus': 'Terminating'}]},
                     {'total': 2, 'data': [{'podStatus': 'Stopped'}]},
                     {'data': []}, {'total': True, 'data': []}, {'total': 1, 'data': [{}]}):
            with self.subTest(data=data):
                self.api.pod_data = data
                result = self.client.status('example')
                self.assertEqual(result['state'], 'STOP_REQUESTED')
                self.assertFalse(result['resource_released'])
                self.assertFalse(result['requires_stop'])
        self.assertEqual(self.api.count_stops(), 0)

    def test_no_delete_flag_or_untrusted_flag_cannot_use_empty_pods_as_terminal_evidence(self):
        task_id = self.client.submit(self.spec, execute=True)['task_id']
        for flag in (None, False, 'true', '1', 1.0):
            with self.subTest(flag=flag):
                self.api.tasks[task_id]['deleteFlag'] = flag
                result = self.client.status('example')
                self.assertFalse(result['resource_released'])
        self.assertFalse(any(path.endswith('/job-pod-instance') for _, path, _, _ in self.api.calls))
        self.api.tasks[task_id]['deleteFlag'] = 1
        self.assertTrue(self.client.status('example')['resource_released'])

    def test_pod_observation_error_does_not_confirm_deleted_task_release(self):
        task_id = self.client.submit(self.spec, execute=True)['task_id']
        self.api.tasks[task_id]['deleteFlag'] = True
        original = self.api.call
        def pod_unavailable(method, path, params=None, body=None):
            if path.endswith('/job-pod-instance'):
                raise TimeoutError('synthetic hidden pod error')
            return original(method, path, params=params, body=body)
        self.api.call = pod_unavailable
        result = self.client.status('example')
        self.assertFalse(result['resource_released'])
        self.assertEqual(result['cancel_release_error']['type'], 'TimeoutError')
        self.assertNotIn('synthetic hidden', json.dumps(result))

    def test_accepted_cancel_is_only_observed_by_next_stop_call(self):
        task_id = self.client.submit(self.spec, execute=True)['task_id']
        original = self.api.call
        mutations = []
        def accepted_pending(method, path, params=None, body=None):
            if method == 'DELETE':
                mutations.append(path)
                return {'trainNames': self.spec['task_name']}
            return original(method, path, params=params, body=body)
        self.api.call = accepted_pending
        clock = [0.0]
        with patch('native.time.monotonic', side_effect=lambda: clock[0]), \
             patch('native.time.sleep', side_effect=lambda delay: clock.__setitem__(0, clock[0] + delay)):
            first = self.client.stop('example', timeout=2, interval=1)
            second = self.client.stop('example', timeout=2, interval=1)
        self.assertTrue(first['stop_accepted'])
        self.assertFalse(second['resource_released'])
        self.assertEqual(len(mutations), 1)
        self.api.tasks[task_id]['deleteFlag'] = True
        self.assertTrue(self.client.stop('example')['resource_released'])
        self.assertEqual(len(mutations), 1)

    def test_deleted_marker_is_verified_against_same_owner_before_pod_query(self):
        task_id = self.client.submit(self.spec, execute=True)['task_id']
        self.api.tasks[task_id].update(deleteFlag=True, userId='someone-else')
        with self.assertRaises(RuntimeError):
            self.client.status('example')
        self.assertFalse(any(path.endswith('/job-pod-instance') for _, path, _, _ in self.api.calls))

    def test_terminal_task_requires_no_pause_or_extra_stop(self):
        self.api.terminal = self.api.receipt = True
        result = self.client.submit(self.spec, execute=True, experimental=True)
        self.assertEqual(result['state'], 'SUCCEEDED')
        self.assertTrue(result['resource_released'])
        self.assertFalse(result['scientific_outputs_verified'])
        before = len(self.api.calls)
        self.assertEqual(self.client.stop('example')['state'], 'SUCCEEDED')
        self.assertEqual(before, len(self.api.calls))
        self.assertEqual(self.api.count_stops(), 0)
        self.assertEqual(result['release_basis'], 'platform_terminal')

    def test_platform_success_requires_persisted_guard_receipt_and_can_refresh(self):
        self.api.terminal = True
        result = self.client.submit(self.spec, execute=True, experimental=True)
        self.assertEqual(result['state'], 'COMPLETED_UNVERIFIED')
        self.assertFalse(result['hardware_verified'])
        self.api.receipt = True
        result = self.client.status('example')
        self.assertEqual(result['state'], 'SUCCEEDED')
        self.assertTrue(result['hardware_verified'])
        self.assertFalse(result['scientific_outputs_verified'])
        self.api.receipt = False
        self.client.logs('example')
        self.assertEqual(self.client.status('example', refresh=False)['state'], 'COMPLETED_UNVERIFIED')

    def test_gpu_count_alias_needs_actual_model_receipt(self):
        self.client.submit(self.spec, execute=True, experimental=True)
        row = next(iter(self.api.tasks.values()))
        row.update(status='Succeeded', acceleratorCardType='GPU:1')
        self.assertEqual(self.client.status('example')['state'], 'COMPLETED_UNVERIFIED')
        self.api.receipt = True
        self.assertEqual(self.client.status('example')['state'], 'SUCCEEDED')
        self.api.receipt = False
        receipt = self.final_receipt(actual_gpu_names=['Other GPU'])
        self.api.log_data = RECEIPT_PREFIX + json.dumps(receipt)
        self.assertEqual(self.client.status('example')['state'], 'COMPLETED_UNVERIFIED')

    def test_failed_terminal_log_refresh_cannot_reuse_old_success_receipt(self):
        self.api.terminal = self.api.receipt = True
        self.assertEqual(self.client.submit(self.spec, execute=True, experimental=True)['state'], 'SUCCEEDED')
        original = self.api.call
        def unavailable(method, path, params=None, body=None):
            if path.endswith('/read-log'):
                raise ConnectionError('synthetic log failure')
            return original(method, path, params=params, body=body)
        self.api.call = unavailable
        result = self.client.status('example')
        self.assertEqual(result['state'], 'COMPLETED_UNVERIFIED')
        self.assertFalse(result['hardware_verified'])
        self.assertIsNone(result['native_receipt'])

    def test_status_and_watch_do_not_post_stop_on_newly_observed_mismatch(self):
        self.client.submit(self.spec, execute=True, experimental=True)
        row = next(iter(self.api.tasks.values()))
        row['acceleratorCardKind'] = 'CPU'
        result = self.client.status('example')
        self.assertTrue(result['requires_stop'])
        self.assertEqual(self.api.count_stops(), 0)
        now = [0.0]
        with patch('native.time.monotonic', side_effect=lambda: now[0]), \
             patch('native.time.sleep', side_effect=lambda delay: now.__setitem__(0, now[0] + delay)):
            with self.assertRaises(TimeoutError):
                self.client.watch('example', interval=1, timeout=2)
        self.assertEqual(self.api.count_stops(), 0)

    def test_cancel_during_preflight_prevents_creation(self):
        original = self.api.call
        cancelled = []
        def cancel_on_node(method, path, params=None, body=None):
            if path.endswith('/node') and not cancelled:
                cancelled.append(self.client.stop('example'))
            return original(method, path, params=params, body=body)
        self.api.call = cancel_on_node
        with self.assertRaises(InterruptedError):
            self.client.submit(self.spec, execute=True, experimental=True)
        self.assertEqual(cancelled[0]['state'], 'CANCELLED')
        self.assertEqual(self.api.count_creates(), 0)

    def test_cancel_during_post_is_preserved_and_stops_accepted_task(self):
        original = self.api.call
        cancelled = []
        def cancel_on_post(method, path, params=None, body=None):
            if method == 'POST' and path.endswith('/train'):
                cancelled.append(self.client.stop('example'))
            return original(method, path, params=params, body=body)
        self.api.call = cancel_on_post
        result = self.client.submit(self.spec, execute=True, experimental=True)
        self.assertTrue(cancelled[0]['requires_reconcile'])
        self.assertTrue(result['resource_released'])
        self.assertEqual(result['state'], 'CANCELLED')
        self.assertEqual(self.api.count_creates(), 1)
        self.assertEqual(self.api.count_stops(), 1)

    def test_terminal_visibility_loss_does_not_revoke_confirmed_release(self):
        self.api.terminal = True
        self.client.submit(self.spec, execute=True, experimental=True)
        self.api.no_visibility = True
        self.assertTrue(self.client.status('example')['resource_released'])

    def test_pending_platform_state_does_not_hide_requested_stop(self):
        self.client.submit(self.spec, execute=True, experimental=True)
        self.client.registry.update('example', stop_requested=True, state='STOP_REQUESTED')
        self.assertEqual(self.client.status('example')['state'], 'STOP_REQUESTED')

    def test_stop_and_logs_refuse_wrong_owner(self):
        self.client.submit(self.spec, execute=True, experimental=True)
        row = next(iter(self.api.tasks.values())); row['userId'] = 'someone-else'
        with self.assertRaises(RuntimeError):
            self.client.stop('example')
        with self.assertRaises(RuntimeError):
            self.client.logs('example')
        self.assertEqual(self.api.count_stops(), 0)

    def test_watch_timeout_is_only_an_observer_timeout(self):
        self.client.submit(self.spec, execute=True, experimental=True)
        now = [0.0]
        with patch('native.time.monotonic', side_effect=lambda: now[0]), \
             patch('native.time.sleep', side_effect=lambda delay: now.__setitem__(0, now[0] + delay)):
            with self.assertRaises(TimeoutError):
                self.client.watch('example', interval=1, timeout=2)
        self.assertEqual(self.api.count_stops(), 0)
        self.assertEqual(self.client.registry.get('example')['state'], 'QUEUED')

    def test_logs_use_platform_read_log_endpoint(self):
        self.client.submit(self.spec, execute=True, experimental=True)
        self.assertEqual(self.client.logs('example')['platform_log'], 'synthetic platform log\n')
        self.assertEqual(self.api.count_creates(), 1)
        cached = self.client.registry.root / 'logs' / 'example.json'
        self.assertEqual(cached.stat().st_mode & 0o777, 0o600)
        calls = len(self.api.calls)
        self.assertEqual(self.client.logs('example', refresh=False)['platform_log'], 'synthetic platform log\n')
        self.assertEqual(len(self.api.calls), calls)

    def final_receipt(self, **changes):
        value = {'schema': 1, 'run_id': self.spec['run_id'], 'spec_sha256': spec_fingerprint(self.spec),
                 'attempt_id': 'attempt-one', 'sequence': 4, 'state': 'SUCCEEDED',
                 'expected_gpu_count': 1, 'expected_gpu_model': self.spec['card_type'],
                 'actual_gpu_names': [self.spec['card_type']], 'hardware_verified': True,
                 'storage_verified': True, 'receipt_persisted': True,
                 'exit_code': 0, 'program_exit_code': 0}
        return dict(value, **changes)

    def test_receipt_parser_uses_sequence_instead_of_log_order(self):
        success, old = self.final_receipt(), self.final_receipt(sequence=2, state='RUNNING')
        for values in ([success, old], [old, success]):
            result = parse_native_receipt({'logs': '\n'.join(RECEIPT_PREFIX + json.dumps(row) for row in values)}, self.spec)
            self.assertTrue(_successful_receipt(result, self.spec))

    def test_multiple_attempts_cannot_claim_success_in_any_log_order(self):
        success = self.final_receipt()
        newer = self.final_receipt(attempt_id='new-attempt', state='GUARD_FAILED', exit_code=125)
        for values in ([success, newer], [newer, success]):
            result = parse_native_receipt('\n'.join(RECEIPT_PREFIX + json.dumps(row) for row in values), self.spec)
            self.assertTrue(result['attempts_ambiguous'])
            self.assertEqual(result['attempt_count'], 2)
            self.assertFalse(_successful_receipt(result, self.spec))

    def test_receipt_same_sequence_conflict_and_noninteger_zero_fail(self):
        left = self.final_receipt()
        right = self.final_receipt(state='FAILED')
        result = parse_native_receipt('\n'.join(RECEIPT_PREFIX + json.dumps(row) for row in (left, right)), self.spec)
        self.assertTrue(result['protocol_conflict'])
        self.assertFalse(_successful_receipt(result, self.spec))
        for field in ('exit_code', 'program_exit_code'):
            for value in (False, 0.0):
                self.assertFalse(_successful_receipt(self.final_receipt(**{field: value}), self.spec))


if __name__ == '__main__':
    unittest.main()
