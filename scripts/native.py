"""Platform-owned, single-node AIStation training tasks.

The payload follows observed task-form fields. This client does not provide a
development-environment fallback, a multi-node launcher, or a scientific result
validator. Credentials and API session tokens remain in process memory.
"""
import json
import hashlib
import base64
import os
from pathlib import Path, PurePosixPath
import re
import shlex
import tempfile
import time
import zlib

from api import AIStation, ApiError, rows
from config import load_profile, state_directory
from credentials import get_password
from registry import Registry, profile_identity, utcnow, validate_run_id


TERMINAL = {'SUCCEEDED', 'FAILED', 'CANCELLED', 'DELETED', 'REJECTED', 'RESOURCE_MISMATCH', 'COMMAND_MISMATCH', 'COMPLETED_UNVERIFIED'}
RECEIPT_PREFIX = 'WLAI_NATIVE_RECEIPT '
PLATFORM_TERMINAL = {'Succeeded', 'Failed', 'Stopped', 'Deleted'}
_VALIDATION_REJECTIONS = {
    'IRESOURCE_STRING_LENGTH_ILLEGAL', 'COMMON_PARAMETER_NOT_MATCH',
    'IRESOURCE_INPUT_GPU_MORE_THAN_WORK_PLATFORM_CONFIG_LIMIT',
    'IRESOURCE_CREATE_WORK_PLATFORM_RES_GROUP_NOT_MATCH_LABEL',
    'IRESOURCE_COMMON_RES_GROUP_CAN_NOT_BE_USED',
    'IRESOURCE_DUPLICATED_ACCOUNT_MOUNT_PATH',
    'IRESOURCE_MAX_WOKR_PLATFORM_COUNT_EXCEED',
    'IRESOURCE_MAX_WORK_PLATFORM_COUNT_EXCEED',
}
_STATES = {'Pending': 'QUEUED', 'Queuing': 'QUEUED', 'ImagePulling': 'STARTING',
           'DatasetPulling': 'STARTING', 'Running': 'RUNNING', 'Terminating': 'STOP_REQUESTED',
           'Succeeded': 'SUCCEEDED', 'Failed': 'FAILED', 'Stopped': 'CANCELLED', 'Deleted': 'DELETED'}


def _text(value, field):
    if not isinstance(value, str) or not value.strip() or any(ord(char) < 32 for char in value):
        raise ValueError(field + ' must be a nonempty string without control characters')
    return value


def _absolute(value, field):
    value = _text(value, field)
    path = PurePosixPath(value)
    if not path.is_absolute() or '..' in path.parts or str(path) == '/':
        raise ValueError(field + ' must be an absolute personal path without parent traversal')
    return str(path)


def prepare_task_spec(source, profile):
    """Validate a production task locally without imposing the development 4h cap."""
    if not isinstance(source, dict):
        raise ValueError('Task specification must be a JSON object')
    spec = json.loads(json.dumps(source))
    validate_run_id(spec.get('run_id'))
    if spec.get('backend', 'native') != 'native' or spec.get('executable') is False:
        raise ValueError('This is not an executable native task specification')
    if spec.get('purpose', 'training') not in ('training', 'production'):
        raise ValueError('Native tasks require training/production purpose')
    spec.update(backend='native', purpose='training')
    spec.setdefault('nodes', 1)
    if type(spec['nodes']) is not int or spec['nodes'] != 1:
        raise ValueError('This backend supports one node per task; independent tasks are not DDP')
    spec.setdefault('cpus', 12)
    spec.setdefault('gpus', 1)
    spec.setdefault('memory_gb', 0)
    for key, low, high in [('cpus', 1, 500), ('gpus', 0, 25), ('memory_gb', 0, 1048576)]:
        if type(spec[key]) is not int or not low <= spec[key] <= high:
            raise ValueError(f'{key} must be an integer from {low} to {high}')
    if type(spec.get('max_seconds')) is not int or not 1 <= spec['max_seconds'] <= 2147483647:
        raise ValueError('An explicit positive max_seconds is required for the training program')
    spec.setdefault('image', profile.get('image'))
    spec['image'] = _text(spec['image'], 'image')
    spec.setdefault('framework', profile.get('framework', 'pytorch'))
    spec['framework'] = _text(spec['framework'], 'framework')
    spec.setdefault('resource_group_id', profile.get('training_group_id'))
    spec['resource_group_id'] = _text(spec['resource_group_id'], 'training resource_group_id')
    if spec['gpus']:
        spec.setdefault('card_type', profile.get('card_type'))
        spec['card_type'] = _text(spec['card_type'], 'card_type')
    else:
        spec['card_type'] = ''
    spec['run_dir'] = _absolute(profile['persistent_root'], 'persistent_root') + '/' + spec['run_id']
    argv = spec.get('argv')
    if not isinstance(argv, list) or not argv or not all(isinstance(arg, str) and '\x00' not in arg for arg in argv):
        raise ValueError('argv must be a nonempty list of strings without NUL characters')
    _absolute(argv[0], 'remote executable')
    spec['argv'] = [arg.replace('{run_dir}', spec['run_dir']) for arg in argv]
    environment = spec.setdefault('env', {})
    if not isinstance(environment, dict) or not all(
            isinstance(key, str) and re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]*', key)
            and isinstance(value, str) and '\x00' not in value for key, value in environment.items()):
        raise ValueError('env must map valid environment variable names to string values')
    if spec.get('uploads'):
        raise ValueError('Stage production inputs/scripts once before task submission; native tasks do not use container SSH uploads')
    volumes = spec.setdefault('volumes', [])
    if not isinstance(volumes, list) or not all(isinstance(value, dict) for value in volumes):
        raise ValueError('volumes must be an explicit list copied from the verified task form')
    spec.setdefault('group_max_seconds', None)
    maximum = spec['group_max_seconds']
    if maximum is not None and (type(maximum) is not int or maximum < 1):
        raise ValueError('group_max_seconds must be a verified positive duration or null')
    if maximum is not None and spec['max_seconds'] > maximum:
        raise ValueError('Requested runtime exceeds the explicitly configured training-group time limit')
    spec.setdefault('guard_seconds', 60)
    spec.setdefault('receipt_seconds', 30)
    for field in ('guard_seconds', 'receipt_seconds'):
        if type(spec[field]) is not int or not 1 <= spec[field] <= 1800:
            raise ValueError(field + ' must be an integer from 1 through 1800')
    if maximum is not None and sum(spec[field] for field in ('max_seconds', 'guard_seconds', 'receipt_seconds')) > maximum:
        raise ValueError('Program, startup guard and receipt allowance exceed the configured group limit')
    spec.setdefault('guard_python', profile.get('remote_python', '/opt/conda/bin/python'))
    spec['guard_python'] = _absolute(spec['guard_python'], 'guard_python')
    personal = '/' + profile['account']
    root = _absolute(profile['persistent_root'], 'persistent_root')
    default_mount = personal if root.startswith(personal + '/') else '/ssdwork' + personal if root.startswith('/ssdwork' + personal + '/') else None
    spec.setdefault('storage_mount', profile.get('storage_mount') or default_mount)
    spec['storage_mount'] = _absolute(spec['storage_mount'], 'storage_mount')
    if spec['storage_mount'] in ('/tmp', '/root') or not root.startswith(spec['storage_mount'] + '/'):
        raise ValueError('Persistent task root must remain below its verified personal storage mount')
    spec['persistent_root'] = root
    if 'guard_script' in spec:
        spec['guard_script'] = _absolute(spec['guard_script'], 'guard_script')
        if not spec['guard_script'].startswith(spec['storage_mount'] + '/'):
            raise ValueError('guard_script must remain below the verified personal storage_mount')
    if 'guard_manifest' in spec:
        if not spec.get('guard_script'):
            raise ValueError('guard_manifest requires a staged guard_script')
        spec['guard_manifest'] = _absolute(spec['guard_manifest'], 'guard_manifest')
        if not spec['guard_manifest'].startswith(spec['storage_mount'] + '/'):
            raise ValueError('guard_manifest must remain below the verified personal storage_mount')
    if 'guard_manifest_sha256' in spec:
        raise ValueError('guard_manifest_sha256 is derived from the normalized manifest; do not provide it in the spec')
    runner_sha256 = hashlib.sha256(Path(__file__).with_name('native_runner.py').read_bytes()).hexdigest()
    if 'guard_script_sha256' in spec and spec['guard_script_sha256'] != runner_sha256:
        raise ValueError('guard_script_sha256 must match the current bundled native_runner.py source')
    spec['guard_script_sha256'] = runner_sha256
    spec.setdefault('task_name', 'wl_' + spec['run_id'])
    spec['task_name'] = _text(spec['task_name'], 'task_name')
    if len(spec['task_name']) > 63 or not re.fullmatch(r'[A-Za-z0-9_][A-Za-z0-9_.-]*', spec['task_name']):
        raise ValueError('task_name must use at most 63 letters, numbers, underscores, dots or hyphens')
    # Conservative client budget, not a promise about every server deployment.
    # Large launchers/argument lists should be staged as verified files.
    spec.setdefault('command_max_bytes', 1000)
    command_maximum = spec.get('command_max_bytes')
    if command_maximum is not None:
        if type(command_maximum) is not int or command_maximum < 1:
            raise ValueError('command_max_bytes must be a configured positive byte budget or null')
        spec['command_max_bytes'] = command_maximum
    return spec


def spec_fingerprint(spec):
    return hashlib.sha256(json.dumps(spec, sort_keys=True).encode()).hexdigest()


def _runner_manifest(spec):
    fields = ('run_id', 'run_dir', 'storage_mount', 'persistent_root', 'gpus', 'card_type',
              'max_seconds', 'guard_seconds', 'receipt_seconds', 'argv', 'env')
    return dict({key: spec[key] for key in fields}, spec_sha256=spec_fingerprint(spec))


def _runner_manifest_bytes(spec):
    return json.dumps(_runner_manifest(spec), sort_keys=True, separators=(',', ':')).encode('utf-8')


def build_task_payload(spec, project_id=None):
    """Observed single-node form body. config is serialized exactly once."""
    manifest_bytes = _runner_manifest_bytes(spec)
    if spec.get('guard_manifest'):
        # Both files are immutable inputs to this command. Hash both complete
        # buffers before exposing the manifest or compiling the guard source.
        loader = ("import base64,hashlib,sys;from pathlib import Path;s=Path(" + repr(spec['guard_script'])
                  + ").read_bytes();m=Path(" + repr(spec['guard_manifest']) + ").read_bytes();"
                  + "(hashlib.sha256(s).hexdigest()==" + repr(spec['guard_script_sha256'])
                  + " and hashlib.sha256(m).hexdigest()==" + repr(hashlib.sha256(manifest_bytes).hexdigest())
                  + ") or sys.exit('WLAI_NATIVE_FILE_SHA256_MISMATCH');"
                  + "sys.argv[1:]=[base64.urlsafe_b64encode(m).decode('ascii')];"
                  + "exec(compile(s,'<wlai_native_guard>','exec'))")
    elif spec.get('guard_script'):
        # Compile the same bytes that were hashed; never reopen the staged file
        # after verification or use assert (which Python optimization can skip).
        loader = ("import hashlib,sys;from pathlib import Path;s=Path("
                  + repr(spec['guard_script']) + ").read_bytes();"
                  + "(hashlib.sha256(s).hexdigest()==" + repr(spec['guard_script_sha256'])
                  + ") or sys.exit('WLAI_NATIVE_GUARD_SHA256_MISMATCH');"
                  + "exec(compile(s," + repr(spec['guard_script']) + ",'exec'))")
    else:
        runner = Path(__file__).with_name('native_runner.py').read_bytes()
        compressed = base64.b64encode(zlib.compress(runner, 9)).decode('ascii')
        loader = ("import base64,zlib;exec(compile(zlib.decompress(base64.b64decode("
                  + repr(compressed) + ")), '<wlai-native-runner>', 'exec'))")
    outer_seconds = spec['max_seconds'] + spec['guard_seconds'] + spec['receipt_seconds']
    # This deployment rewrites double quotes to single quotes and literal
    # newlines to spaces before storing commands. Shell quote splicing is not
    # stable across that boundary. Keep all loader text in a base64 argument;
    # the outer Python expression has no embedded string literals or newlines.
    envelope = 'import base64,sys;exec(base64.b64decode(sys.argv.pop(1)))'
    command_argv = ['timeout', '--signal=TERM', '--kill-after=30', str(outer_seconds),
                    spec['guard_python'], '-u', '-c', envelope,
                    base64.b64encode(loader.encode('utf-8')).decode('ascii')]
    if not spec.get('guard_manifest'):
        command_argv.append(base64.urlsafe_b64encode(manifest_bytes).decode('ascii'))
    command = shlex.join(command_argv)
    if any(character in command for character in ('"', '\r', '\n')):
        raise ValueError('The native launcher executable path requires unsupported platform quoting; use an equivalent path without quotes or control characters')
    command_bytes = len(command.encode('utf-8'))
    maximum = spec.get('command_max_bytes')
    if maximum is not None and command_bytes > maximum:
        raise ValueError(f'Native command uses {command_bytes} bytes, exceeding configured command_max_bytes={maximum}; stage guard_script and guard_manifest on persistent storage, then plan again; no task request was sent')
    worker = {'nodeNum': 1, 'cpuNum': spec['cpus'], 'acceleratorCardNum': spec['gpus'],
              'memory': spec['memory_gb'], 'minNodeNum': -1}
    fingerprint = spec_fingerprint(spec)
    # Do not copy the frontend route's taskType into this creation body. On
    # the validated deployment its presence selected data processing and
    # discarded GPU resources; omission created the intended training task.
    result = {'name': spec['task_name'], 'description': 'wlai:' + fingerprint, 'imageType': spec['framework'],
              'resGroupId': spec['resource_group_id'], 'acceleratorCardType': spec['card_type'],
              'image': spec['image'], 'mountDir': '', 'startScript': '', 'logOut': '',
              'distFlag': False, 'enUpdateDataSet': 0, 'ports': '', 'param': None,
              'execDir': '', 'nodeName': '', 'mpiFlag': False, 'type': spec['framework'],
              'shmSize': 4, 'datasetId': None, 'emergencyFlag': False, 'imageFlag': 0,
              'switchType': 'ib',
              'acceleratorCardKind': 'GPU' if spec['gpus'] else 'CPU', 'models': [],
              'config': json.dumps({'worker': worker}, separators=(',', ':')),
              'command': command, 'commandScriptList': [], 'jobVolume': spec['volumes'],
              'env': [{'name': key, 'value': value} for key, value in sorted(spec['env'].items())]}
    if project_id is not None:
        result['projectId'] = project_id
    return result


def parse_native_receipt(data, spec, fingerprint=None):
    """Keep each attempt's highest sequence; never resolve restarts by log order."""
    fingerprint = fingerprint or spec_fingerprint(spec)
    attempts = {}
    allowed_states = {'STARTING', 'RUNNING', 'SUCCEEDED', 'FAILED', 'TIMED_OUT', 'CANCELLED', 'GUARD_FAILED'}
    def strings(value):
        if isinstance(value, str):
            yield value
        elif isinstance(value, dict):
            for item in value.values():
                yield from strings(item)
        elif isinstance(value, list):
            for item in value:
                yield from strings(item)
    for text in strings(data):
        for line in text.splitlines():
            position = line.find(RECEIPT_PREFIX)
            if position < 0:
                continue
            try:
                receipt, _ = json.JSONDecoder().raw_decode(line[position + len(RECEIPT_PREFIX):])
            except (ValueError, TypeError):
                continue
            if (not isinstance(receipt, dict) or type(receipt.get('schema')) is not int or receipt['schema'] != 1
                    or receipt.get('run_id') != spec['run_id'] or receipt.get('spec_sha256') != fingerprint
                    or receipt.get('state') not in allowed_states):
                continue
            if not isinstance(receipt.get('attempt_id'), str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,128}', receipt['attempt_id']):
                continue
            if type(receipt.get('sequence')) is not int or receipt['sequence'] < 0:
                continue
            # Only generated protocol fields are retained as structured evidence.
            allowed = {'schema', 'run_id', 'attempt_id', 'spec_sha256', 'state', 'started_at', 'ended_at',
                       'expected_gpu_count', 'expected_gpu_model', 'actual_gpu_names', 'hardware_verified',
                       'storage_verified', 'receipt_persisted', 'sequence', 'phase', 'runner_pid', 'program_pid',
                       'program_exit_code', 'exit_code', 'error_code'}
            candidate = {key: value for key, value in receipt.items() if key in allowed}
            old = attempts.get(candidate['attempt_id'])
            if old is None or candidate['sequence'] > old['sequence']:
                attempts[candidate['attempt_id']] = candidate
            elif candidate['sequence'] == old['sequence'] and candidate != old:
                attempts[candidate['attempt_id']] = dict(old, protocol_conflict=True)
    if len(attempts) > 1:
        return {'schema': 1, 'run_id': spec['run_id'], 'spec_sha256': fingerprint,
                'state': 'AMBIGUOUS_ATTEMPTS', 'attempts_ambiguous': True,
                'attempt_count': len(attempts), 'attempts': list(attempts.values()),
                'hardware_verified': False, 'receipt_persisted': False}
    return next(iter(attempts.values()), None)


def _hardware_receipt_valid(receipt, spec):
    if (not receipt or receipt.get('protocol_conflict') or receipt.get('hardware_verified') is not True
            or type(receipt.get('expected_gpu_count')) is not int or receipt['expected_gpu_count'] != spec['gpus']):
        return False
    if spec['gpus'] == 0:
        return receipt.get('actual_gpu_names') == []
    names = receipt.get('actual_gpu_names')
    normalized = lambda value: re.sub(r'[\s_-]+', '', value).casefold()
    return (isinstance(names, list) and len(names) == spec['gpus']
            and receipt.get('expected_gpu_model') == spec['card_type']
            and all(isinstance(name, str) and normalized(name) == normalized(spec['card_type']) for name in names))


def _successful_receipt(receipt, spec):
    return bool(_hardware_receipt_valid(receipt, spec) and receipt.get('state') == 'SUCCEEDED'
                and type(receipt.get('exit_code')) is int and receipt['exit_code'] == 0
                and type(receipt.get('program_exit_code')) is int and receipt['program_exit_code'] == 0
                and receipt.get('storage_verified') is True
                and receipt.get('receipt_persisted') is True)


def _safe_error(error):
    code = getattr(error, 'code', None) or getattr(error, 'err_code', None)
    if not isinstance(code, str) or not re.fullmatch(r'[A-Z][A-Z0-9_]{0,127}', code):
        code = None
    return {'type': type(error).__name__, 'platform_code': code}


class NativeTasks:
    def __init__(self, profile=None, state_dir=None, interactive=True, client=None, user=None):
        self.profile = profile or load_profile(require_execution=False)
        root = Path(state_dir) if state_dir is not None else state_directory(self.profile)
        self.registry = Registry(root / 'tasks', profile_identity(self.profile))
        self.interactive = interactive
        self._client = client
        self._user = user

    @property
    def client(self):
        if self._client is None:
            provider = lambda: get_password(self.profile, interactive=self.interactive)
            client = AIStation(self.profile, password_provider=provider)
            password = provider()
            try:
                self._user = client.login(self.profile['account'], password)
            except BaseException:
                client.close()
                raise
            finally:
                del password
            self._client = client
        return self._client

    @property
    def user(self):
        self.client
        if self._user is None:
            self._user = self.client.call('GET', '/api/ibase/v1/login')
        if not isinstance(self._user, dict) or not self._user.get('userId'):
            raise RuntimeError('Authenticated account identity is unavailable')
        return self._user

    def close(self):
        if self._client is not None:
            self._client.close()

    def _pages(self, endpoint, params=None):
        result = []
        for page in range(1, 51):
            data = self.client.call('GET', endpoint, params=dict(params or {}, page=page, pageSize=100))
            batch = rows(data); result.extend(batch)
            total = data.get('total') if isinstance(data, dict) else None
            if not batch or len(batch) < 100 or isinstance(total, int) and len(result) >= total:
                return result
        raise RuntimeError('Pagination limit exceeded; refine the task query')

    def training_groups(self):
        # This query, including both labels, is from the current front-end form.
        data = self.client.call('GET', '/api/iresource/v1/node-group',
                                params={'groupLabel': 'usual,train', 'page': -1, 'groupStatus': 1})
        result = []
        for row in rows(data):
            label = row.get('groupLabel')
            labels = set(label if isinstance(label, list) else str(label or '').split(','))
            if label is not None and not labels.intersection({'usual', 'train'}):
                continue
            result.append(row)
        return result

    def plan(self, spec, online=False):
        spec = prepare_task_spec(spec, self.profile)
        request = build_task_payload(spec, self._user.get('groupId') if self._user else None)
        record = self.registry.reserve(spec['run_id'], spec, 'task')
        compatible = eligible = None
        group = None
        if online:
            group = next((row for row in self.training_groups() if str(row.get('groupId')) == spec['resource_group_id']), None)
            if group is None:
                raise ValueError('Resource group is not available to task management; a development-only group cannot be substituted')
            nodes = self._pages('/api/iresource/v1/node', {'groupId': spec['resource_group_id'], 'getUsage': 1})
            compatible, eligible = [], []
            for node in nodes:
                if str(node.get('groupId')) != spec['resource_group_id']:
                    continue
                if spec['gpus'] and node.get('cardType') != spec['card_type']:
                    continue
                cpu, cards = int(node.get('cpu') or 0), int(node.get('acceleratorCard') or 0)
                if cpu >= spec['cpus'] and cards >= spec['gpus']:
                    compatible.append(node.get('nodeName'))
                    if cpu - int(node.get('cpuUsage') or 0) >= spec['cpus'] and cards - int(node.get('acceleratorCardUsage') or 0) >= spec['gpus']:
                        eligible.append(node.get('nodeName'))
        warnings = ['Inputs and scripts must already be staged in verified persistent storage.',
                    'Node availability does not prove account quota or storage quota approval.',
                    'Platform task execution continues independently of this client; watch is only an observer.',
                    'Native outputs remain in persistent storage; this command does not copy result files through container SSH.',
                    'Program exit and platform success do not validate scientific outputs.',
                    'Compatibility evidence is deployment-specific; verify returned command/resources, actual hardware and a persisted receipt for every run.',
                    'The default 1000-byte command budget is a client safeguard, not an official universal platform limit.']
        if spec.get('group_max_seconds') is None:
            warnings.append('Training-group lifetime is not verified; confirm the selected group policy instead of assuming five days.')
        if spec.get('command_max_bytes') is None:
            warnings.append('Platform command length limit is unknown; command_max_bytes can set a verified deployment-specific local budget.')
        if self._user and self._user.get('groupId'):
            request['projectId'] = self._user['groupId']
        manifest_bytes = _runner_manifest_bytes(spec) if spec.get('guard_manifest') else None
        return {'run_id': spec['run_id'], 'state': record['state'], 'backend': 'native',
                'spec': spec, 'spec_sha256': record['spec_sha256'], 'availability_checked': bool(online),
                'compatible_nodes': compatible, 'eligible_nodes_now': eligible,
                'request': request, 'command_bytes': len(request['command'].encode('utf-8')),
                'guard_mode': 'staged-files' if spec.get('guard_manifest') else 'staged' if spec.get('guard_script') else 'embedded',
                'guard_script_sha256': spec['guard_script_sha256'],
                'guard_manifest_content': json.loads(manifest_bytes) if manifest_bytes is not None else None,
                'guard_manifest_sha256': hashlib.sha256(manifest_bytes).hexdigest() if manifest_bytes is not None else None,
                'configured_command_max_bytes': spec.get('command_max_bytes'),
                'project_id_resolved': bool(self._user and self._user.get('groupId')),
                'group_label_verified': bool(group), 'warnings': warnings,
                'native_compatibility': 'validated_single_node', 'experimental_required': False,
                'submitted': bool(record.get('task_id'))}

    def _record(self, run_id):
        record = self.registry.get(run_id)
        if record.get('kind') != 'task':
            raise ValueError('This record is not a native training task')
        return record

    def _owned(self, row, record, task_id=None):
        if task_id is not None and str(row.get('id')) != str(task_id):
            raise RuntimeError('Task identity differs from the recorded task ID')
        if row.get('name') != record['spec']['task_name']:
            raise RuntimeError('Task name differs from the immutable submission')
        uid, account = row.get('userId'), row.get('userName') or row.get('account')
        if uid is None and account is None:
            raise RuntimeError('Task ownership is unavailable; no mutation attempted')
        if uid is not None and str(uid) != str(self.user['userId']):
            raise RuntimeError('Refusing to operate on another account task')
        if account is not None and account != self.profile['account']:
            raise RuntimeError('Refusing to operate on another account task')

    def _get_task(self, task_id):
        if not isinstance(task_id, (str, int)) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]{0,127}', str(task_id)):
            raise ValueError('Task ID is not a valid platform identifier')
        for flag in (0, 3):
            data = self.client.call('GET', '/api/iresource/v1/train',
                                    params={'id': task_id, 'statusFlag': flag, 'page': 1, 'pageSize': 1})
            found = [row for row in rows(data) if str(row.get('id')) == str(task_id)]
            if found:
                return found[0]
        return None

    def _by_name(self, record):
        found = []
        for flag in (0, 3):
            found.extend(row for row in self._pages('/api/iresource/v1/train',
                {'name': record['spec']['task_name'], 'statusFlag': flag}) if row.get('name') == record['spec']['task_name'])
        unique = {str(row.get('id')): row for row in found if row.get('id')}
        for row in unique.values():
            self._owned(row, record)
        return list(unique.values())

    def _matches_submission(self, row, record):
        spec = record['spec']
        payload = record.get('submitted_request')
        if payload is None:
            if record['state'] != 'PLANNED':
                return False
            payload = build_task_payload(spec)
        return (row.get('image') == spec['image']
                and (row.get('resGroupId') or row.get('resourceGroupId')) == spec['resource_group_id']
                and row.get('command') == payload['command']
                and row.get('description') == payload['description'])

    def submit(self, spec, execute=False, allow_queue=False, experimental=False):
        spec = prepare_task_spec(spec, self.profile)
        record = self.registry.reserve(spec['run_id'], spec, 'task')
        if not execute:
            return self.plan(spec)
        run_id = spec['run_id']
        with self.registry.operation(run_id, 'submission'):
            record = self._record(run_id)
            if record.get('task_id'):
                return self.status(run_id)
            if record['state'] == 'REJECTED':
                # A known validation rejection is final for this immutable ID.
                return record
            if record['state'] != 'PLANNED':
                raise RuntimeError('Creation outcome requires task-reconcile; no task creation was repeated')
            # experimental remains an accepted compatibility argument. Native
            # submission is now the normal single-node task-management path.
            # Validate an explicit command budget before authentication or live
            # resource discovery. Existing tasks keep their frozen old request.
            build_task_payload(spec)
            live = self.plan(spec, online=True)
            if not live['compatible_nodes']:
                raise ValueError('No compatible single node can satisfy this task; independent tasks are not multi-node training')
            if not live['eligible_nodes_now'] and not allow_queue:
                raise RuntimeError('No compatible node is free now; explicitly allow queueing to submit')
            project_id = self.user.get('groupId')
            if not project_id:
                raise RuntimeError('Authenticated project/group ID is unavailable')
            payload = build_task_payload(spec, project_id)
            # Freeze the exact wrapper command before the one creation request.
            # Future client upgrades must not regenerate it while reconciling.
            record = self.registry.update(run_id, submitted_request=payload,
                submitted_command_sha256=hashlib.sha256(payload['command'].encode()).hexdigest())
            existing = self._by_name(record)
            if existing:
                if len(existing) != 1 or not self._matches_submission(existing[0], record):
                    raise RuntimeError('Task name already exists with unverified parameters; choose a new run ID')
                with self.registry.lock(run_id):
                    current = self._record(run_id)
                    if current['state'] != 'PLANNED' or current.get('cancel_requested'):
                        raise InterruptedError('Task cancellation/state change prevented task adoption')
                    current.update(task_id=existing[0]['id'], state='SUBMITTED', reused=True)
                    self.registry.write(run_id, current)
                return self._observe(run_id, existing[0])
            with self.registry.lock(run_id):
                current = self._record(run_id)
                if current['state'] != 'PLANNED' or current.get('cancel_requested'):
                    raise InterruptedError('Task cancellation/state change prevented the creation request')
                current.update(state='SUBMITTING', submitted_at=utcnow(), allow_queue=bool(allow_queue))
                self.registry.write(run_id, current)
            try:
                created = self.client.call('POST', '/api/iresource/v1/train', body=payload)
            except BaseException as error:
                if (isinstance(error, ApiError) and error.status is None
                        and error.code in _VALIDATION_REJECTIONS):
                    return self.registry.update(run_id, state='REJECTED', uncertain_action=None,
                        creation_error=_safe_error(error), resource_released=True, requires_reconcile=False,
                        release_basis='platform_rejected_no_task_created')
                unknown = self.registry.update(run_id, state='UNKNOWN', uncertain_action='task-creation',
                                               creation_error=_safe_error(error), resource_released=False)
                if not isinstance(error, Exception):
                    raise
                return unknown
            task_id = created if isinstance(created, str) else None
            if isinstance(created, dict):
                task_id = next((created.get(key) for key in ('id', 'taskId', 'jobId', 'trainId') if created.get(key)), None)
            if task_id is not None and not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]{0,127}', str(task_id)):
                task_id = None
            if not task_id:
                self.registry.update(run_id, state='UNKNOWN', uncertain_action='task-creation', resource_released=False)
                return self.reconcile(run_id, enforce_resources=True)
            with self.registry.lock(run_id):
                current = self._record(run_id)
                current.update(state='STOP_REQUESTED' if current.get('cancel_requested') else 'SUBMITTED',
                               task_id=str(task_id), resource_released=False, requires_reconcile=False)
                self.registry.write(run_id, current)
            if current.get('cancel_requested'):
                return self.stop(run_id)
            try:
                return self.status(run_id, enforce_resources=True)
            except Exception as error:
                # An observer failure never undoes or repeats an accepted submission.
                return self.registry.update(run_id, observation_error=_safe_error(error), visibility_pending=True)

    def _resource_check(self, row, spec):
        try:
            config = row.get('config')
            config = json.loads(config) if isinstance(config, str) else config
            worker = config.get('worker') if isinstance(config, dict) else None
            if not isinstance(worker, dict):
                return None, 'Resource configuration has not been returned'
            count = int(worker['acceleratorCardNum'])
            cpus = int(worker['cpuNum'])
            nodes = int(worker['nodeNum'])
        except (ValueError, TypeError, KeyError):
            return False, 'Returned resource configuration is invalid'
        if nodes != 1 or cpus != spec['cpus'] or count != spec['gpus']:
            return False, 'Returned CPU/GPU/node counts differ from the immutable plan'
        if spec['gpus']:
            if row.get('acceleratorCardKind') != 'GPU':
                return False, 'GPU request was returned as a non-GPU task'
            reported = row.get('acceleratorCardType')
            if reported != spec['card_type']:
                alias = re.fullmatch(r'GPU:([0-9]+)', reported or '')
                if not alias or int(alias.group(1)) != spec['gpus']:
                    return False, 'Returned GPU model is not verified against the immutable plan'
        return True, None

    @staticmethod
    def _is_deleted(row):
        value = row.get('deleteFlag') if isinstance(row, dict) else None
        return type(value) in (bool, int) and value == 1

    def _cancel_release_evidence(self, run_id, row=None):
        """Require owned deletion/ack evidence AND a complete no-active-pod view."""
        record = self._record(run_id)
        if row is not None:
            self._owned(row, record, record['task_id'])
            if not self._is_deleted(row):
                return None
            basis = 'platform_delete_flag_and_no_active_pods'
        else:
            if not (record.get('stop_action') == 'cancel_unfinished'
                    and record.get('stop_accepted') is True
                    and record.get('stop_owner_verified') is True
                    and record.get('cancel_ack_task_name') == record['spec']['task_name']):
                return None
            basis = 'platform_cancel_ack_and_no_active_pods'
        try:
            pods = self.client.call('GET', '/api/iresource/v1/train/job-pod-instance',
                                    params={'id': str(record['task_id'])})
        except Exception as error:
            self.registry.update(run_id, cancel_release_error=_safe_error(error))
            return None
        total = pods.get('total') if isinstance(pods, dict) else None
        data = pods.get('data') if isinstance(pods, dict) else None
        # A partial page or unknown status cannot establish absence of active pods.
        if type(total) is not int or total < 0 or not isinstance(data, list) or len(data) != total:
            return None
        statuses = [pod.get('podStatus') if isinstance(pod, dict) else None for pod in data]
        terminal_pods = {'Stopped', 'Deleted', 'Succeeded', 'Failed'}
        if not all(status in terminal_pods for status in statuses):
            return None
        return {'basis': basis, 'task_id': str(record['task_id']), 'pod_total': total,
                'pod_statuses': statuses, 'active_pods': 0, 'observed_at': utcnow()}

    def _confirm_cancel(self, run_id, evidence, platform=None):
        record = self._record(run_id)
        state = ('COMMAND_MISMATCH' if record.get('command_mismatch') else
                 'RESOURCE_MISMATCH' if record.get('resource_mismatch') else 'CANCELLED')
        return self.registry.update(run_id, state=state, platform_state=platform or 'Deleted',
            platform_deleted=True, resource_released=True, release_basis=evidence['basis'],
            cancel_release_evidence=evidence, cancel_release_error=None,
            stop_confirmed=True, stop_uncertain=False, requires_stop=False,
            scientific_outputs_verified=False, last_observed_at=utcnow())

    def _observe(self, run_id, row, enforce_resources=False):
        record = self._record(run_id)
        self._owned(row, record, record.get('task_id'))
        platform = row.get('status')
        terminal = platform in PLATFORM_TERMINAL
        deleted = self._is_deleted(row)
        if deleted and not terminal:
            evidence = self._cancel_release_evidence(run_id, row)
            if evidence is not None:
                return self._confirm_cancel(run_id, evidence, platform)
        expected_command = (record.get('submitted_request') or {}).get('command')
        returned_command = row.get('command')
        command_mismatch = bool(record.get('command_mismatch') or
            isinstance(expected_command, str) and isinstance(returned_command, str)
            and returned_command != expected_command)
        verified, mismatch = self._resource_check(row, record['spec'])
        state = _STATES.get(platform, 'UNRECOGNIZED_PLATFORM_STATE')
        if record.get('resource_mismatch') or verified is False:
            state = 'RESOURCE_MISMATCH'
        elif verified is None and platform == 'Running':
            state, verified, mismatch = 'RESOURCE_MISMATCH', False, 'Running task resources are unverified'
        elif (record.get('stop_requested') or record.get('cancel_requested')) and not terminal:
            state = 'STOP_REQUESTED'
        if command_mismatch:
            state = 'COMMAND_MISMATCH'
        values = {'state': state, 'platform_state': platform, 'resource_released': terminal,
                  'gpu_config_verified': verified is True, 'scientific_outputs_verified': False,
                  'release_basis': 'platform_terminal' if terminal else None,
                  'last_observed_at': utcnow()}
        if command_mismatch:
            values.update(command_mismatch=True, command_validation_message='The platform changed the immutable submitted command; execution is not verified',
                          returned_command_sha256=hashlib.sha256(returned_command.encode()).hexdigest()
                          if isinstance(returned_command, str) else None)
        if mismatch:
            values['resource_validation_message'] = mismatch
        if verified is False:
            values['resource_mismatch'] = True
        if terminal and verified is None:
            values.update(state='RESOURCE_MISMATCH', resource_mismatch=True)
        values['requires_stop'] = bool((record.get('cancel_requested') or verified is False
                                       or record.get('resource_mismatch') or command_mismatch) and not terminal)
        if terminal:
            try:
                receipt = self._fetch_logs(run_id, row).get('native_receipt')
            except Exception as error:
                values['log_observation_error'] = _safe_error(error)
                # A cached successful attempt cannot prove that the current
                # platform terminal state belongs to that same attempt.
                receipt = None
                values['native_receipt'] = None
            values['hardware_verified'] = _hardware_receipt_valid(receipt, record['spec'])
            if platform == 'Succeeded' and state not in ('RESOURCE_MISMATCH', 'COMMAND_MISMATCH'):
                values['state'] = 'SUCCEEDED' if _successful_receipt(receipt, record['spec']) else 'COMPLETED_UNVERIFIED'
        if deleted and not terminal:
            values.update(state='STOP_REQUESTED', platform_deleted=True, requires_stop=False)
        updated = self.registry.update(run_id, **values)
        if enforce_resources and updated.get('requires_stop') and not terminal and not updated.get('stop_requested'):
            return self.stop(run_id)
        return updated

    def status(self, run_id, refresh=True, enforce_resources=False):
        record = self._record(run_id)
        if not refresh:
            return record
        if record['state'] == 'REJECTED' and not record.get('task_id'):
            return record
        if not record.get('task_id'):
            raise RuntimeError('Task ID is unknown; use task-reconcile before further action')
        row = self._get_task(record['task_id'])
        if row is None:
            evidence = self._cancel_release_evidence(run_id)
            if evidence is not None:
                return self._confirm_cancel(run_id, evidence)
            return self.registry.update(run_id, visibility_pending=True,
                                        resource_released=bool(record.get('resource_released')))
        return self._observe(run_id, row, enforce_resources=enforce_resources)

    def _fetch_logs(self, run_id, row):
        record = self._record(run_id)
        self._owned(row, record, record['task_id'])
        data = self.client.call('GET', '/api/iresource/v1/train/' + str(record['task_id']) + '/read-log')
        receipt = parse_native_receipt(data, record['spec'], record['spec_sha256'])
        cached = {'run_id': run_id, 'task_id': record['task_id'], 'platform_log': data,
                  'native_receipt': receipt, 'hardware_verified': _hardware_receipt_valid(receipt, record['spec'])}
        directory = self.registry.root / 'logs'; directory.mkdir(exist_ok=True, mode=0o700)
        target = directory / (run_id + '.json')
        temporary = None
        try:
            with tempfile.NamedTemporaryFile('w', encoding='utf-8', dir=directory, delete=False) as output:
                temporary = Path(output.name); os.chmod(temporary, 0o600)
                json.dump(cached, output, ensure_ascii=False); output.write('\n'); output.flush(); os.fsync(output.fileno())
            os.replace(temporary, target)
        finally:
            if temporary is not None: temporary.unlink(missing_ok=True)
        values = {'logs_cache': str(target), 'native_receipt': receipt,
                  'hardware_verified': cached['hardware_verified']}
        if record.get('platform_state') == 'Succeeded' and not record.get('resource_mismatch'):
            values['state'] = 'SUCCEEDED' if _successful_receipt(receipt, record['spec']) else 'COMPLETED_UNVERIFIED'
        self.registry.update(run_id, **values)
        return cached

    def logs(self, run_id, refresh=True):
        record = self._record(run_id)
        if record['state'] == 'REJECTED' and not record.get('task_id'):
            return record
        if not refresh and record.get('logs_cache'):
            return json.loads(Path(record['logs_cache']).read_text())
        if not record.get('task_id'):
            raise RuntimeError('Task ID is unknown; reconcile before reading logs')
        row = self._get_task(record['task_id'])
        if row is None:
            raise RuntimeError('Task is not currently visible; no unrelated logs were read')
        return self._fetch_logs(run_id, row)

    def stop(self, run_id, timeout=60, interval=2):
        if timeout <= 0 or interval <= 0:
            raise ValueError('Stop timeout and polling interval must be positive')
        with self.registry.operation(run_id, 'stop'):
            deadline = time.monotonic() + timeout
            with self.registry.lock(run_id):
                record = self._record(run_id)
                if record.get('resource_released') and record['state'] in TERMINAL:
                    return record
                if not record.get('task_id'):
                    if record['state'] == 'PLANNED':
                        record.update(state='CANCELLED', cancel_requested=True, resource_released=True,
                                      platform_state='NotSubmitted', release_basis='not_submitted')
                    else:
                        record.update(cancel_requested=True, requires_reconcile=True, resource_released=False)
                    self.registry.write(run_id, record)
                    return record
                # Intent is independent from whether a previous stop request was
                # accepted. Legacy stop_requested=True must never suppress a new
                # explicit stop after a rejected or ambiguous request.
                record['cancel_requested'] = True
                self.registry.write(run_id, record)
            row = self._get_task(record['task_id'])
            if row is None:
                evidence = self._cancel_release_evidence(run_id)
                if evidence is not None:
                    return self._confirm_cancel(run_id, evidence)
                return self.registry.update(run_id, state='STOP_REQUESTED', visibility_pending=True,
                    stop_confirmed=False, stop_uncertain=True, resource_released=False)
            current = self._observe(run_id, row, enforce_resources=False)
            if current['resource_released']:
                return self.registry.update(run_id, stop_confirmed=True, stop_uncertain=False,
                                            requires_stop=False)
            platform = row.get('status')
            if self._is_deleted(row):
                method, path, action, body = None, None, 'cancel_unfinished', None
            elif platform in ('Pending', 'Queuing', 'ImagePulling', 'DatasetPulling'):
                method, path, action = 'DELETE', '/api/iresource/v1/train', 'cancel_unfinished'
                body = {'jobIdList': [str(record['task_id'])], 'endTaskListFlag': False}
            elif platform == 'Running':
                method, action = 'POST', 'stop_running'
                path = '/api/iresource/v1/train/' + str(record['task_id']) + '/stop'
                body = None
            elif platform == 'Terminating':
                method, path, action, body = None, None, 'await_termination', None
            else:
                return self.registry.update(run_id, stop_confirmed=False, stop_accepted=False,
                    stop_uncertain=True, stop_error={'type': 'UnsupportedPlatformState'},
                    resource_released=False)
            if method is not None and current.get('stop_accepted') is True and current.get('stop_action') == action:
                # An acknowledged operation is observed, not sent again. A prior
                # rejection/transport ambiguity/legacy intent flag can be retried
                # by a new explicit call after this fresh state query.
                method = None
            rejected = False
            if method is not None:
                if time.monotonic() >= deadline:
                    return self.registry.update(run_id, stop_confirmed=False, stop_uncertain=True,
                                                resource_released=False)
                attempt = {'action': action, 'method': method, 'path': path,
                           'platform_state': platform, 'requested_at': utcnow(), 'accepted': None}
                attempts = list(current.get('stop_attempts') or [])[-19:] + [attempt]
                self.registry.update(run_id, stop_requested=True, stop_accepted=None,
                    stop_confirmed=False, stop_rejected=False, stop_action=action, stop_method=method,
                    stop_platform_state=platform, stop_attempts=attempts, stop_error=None,
                    stop_owner_verified=True, cancel_ack_task_name=None,
                    state='COMMAND_MISMATCH' if current.get('command_mismatch') else
                    'RESOURCE_MISMATCH' if current.get('resource_mismatch') else 'STOP_REQUESTED')
                try:
                    response = self.client.call(method, path, **({'body': body} if body is not None else {}))
                except Exception as error:
                    rejected = (isinstance(error, ApiError) and error.status is None
                                and error.code not in (None, 'PLATFORM_REJECTED'))
                    attempt.update(accepted=False if rejected else None, error=_safe_error(error))
                    self.registry.update(run_id, stop_accepted=False if rejected else None,
                        stop_requested=False if rejected else True, stop_rejected=rejected,
                        stop_error=_safe_error(error), stop_attempts=attempts,
                        stop_uncertain=not rejected, resource_released=False)
                else:
                    attempt['accepted'] = True
                    names = response.get('trainNames') if isinstance(response, dict) else None
                    expected = record['spec']['task_name']
                    acknowledged = names == expected or names == [expected]
                    self.registry.update(run_id, stop_accepted=True, stop_attempts=attempts,
                        cancel_ack_task_name=expected if action == 'cancel_unfinished' and acknowledged else None,
                        stop_uncertain=True)
            else:
                self.registry.update(run_id, stop_requested=True, stop_action=action,
                                     stop_confirmed=False, stop_uncertain=True)
            # Always re-read after a response, even an explicit rejection: the
            # task may have reached a terminal state while the mutation raced.
            first_observation = True
            while first_observation or time.monotonic() < deadline:
                first_observation = False
                try:
                    row = self._get_task(record['task_id'])
                except Exception as error:
                    return self.registry.update(run_id, stop_observation_error=_safe_error(error),
                        stop_confirmed=False, stop_uncertain=True, resource_released=False)
                if row is None:
                    evidence = self._cancel_release_evidence(run_id)
                    if evidence is not None:
                        return self._confirm_cancel(run_id, evidence)
                else:
                    current = self._observe(run_id, row, enforce_resources=False)
                    if current['resource_released']:
                        return self.registry.update(run_id, stop_confirmed=True, stop_uncertain=False,
                                                    requires_stop=False)
                if rejected:
                    return self.registry.update(run_id, stop_confirmed=False, stop_uncertain=False,
                                                resource_released=False)
                # Disappearance after DELETE alone does not prove resource release.
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                time.sleep(min(interval, remaining))
            return self.registry.update(run_id, stop_confirmed=False, stop_uncertain=True,
                                        resource_released=False)

    cancel = stop

    def watch(self, run_id, interval=5, timeout=600):
        if interval <= 0 or timeout <= 0:
            raise ValueError('Observation timeout and interval must be positive')
        deadline = time.monotonic() + timeout
        while True:
            record = self.status(run_id)
            if record.get('resource_released') and record['state'] in TERMINAL:
                return record
            if time.monotonic() >= deadline:
                raise TimeoutError('Local observation timed out; the platform task continues until its own terminal state')
            time.sleep(min(interval, max(0, deadline - time.monotonic())))

    def reconcile(self, run_id, enforce_resources=False):
        record = self._record(run_id)
        if record['state'] == 'REJECTED' and not record.get('task_id'):
            return record
        if record.get('task_id'):
            return self.status(run_id)
        found = self._by_name(record)
        if len(found) != 1:
            return self.registry.update(run_id, state='UNKNOWN', reconcile_matches=len(found),
                                        uncertain_action='task-creation', resource_released=False)
        if not self._matches_submission(found[0], record):
            raise RuntimeError('Matching task name has different command/image/group; it was not adopted')
        self.registry.update(run_id, task_id=str(found[0]['id']), state='SUBMITTED', uncertain_action=None)
        return self._observe(run_id, found[0], enforce_resources=enforce_resources)
