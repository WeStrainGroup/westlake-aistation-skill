#!/usr/bin/env python3
"""Self-contained task startup guard and per-attempt receipt writer.

This source is embedded in native task commands. It requires only Python's
standard library and the image's nvidia-smi for GPU tasks. It never imports the
client package, credentials or a machine-learning framework.
"""
import base64
import datetime
import json
import os
import re
import signal
import subprocess
import sys
import tempfile
import threading
import time
import uuid


RECEIPT_PREFIX = 'WLAI_NATIVE_RECEIPT '
TERMINAL_STATES = {'SUCCEEDED', 'FAILED', 'TIMED_OUT', 'CANCELLED', 'GUARD_FAILED'}
_VIRTUAL_FILESYSTEMS = {'overlay', 'tmpfs', 'ramfs', 'proc', 'sysfs', 'devtmpfs', 'devpts'}


class GuardError(RuntimeError):
    pass


class Cancelled(BaseException):
    pass


def utcnow():
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def normalized_model(value):
    return re.sub(r'[\s_-]+', '', value).casefold()


def _inside(path, parent):
    return path == parent or path.startswith(parent.rstrip('/') + '/')


def _unescape(value):
    return re.sub(r'\\([0-7]{3})', lambda match: chr(int(match.group(1), 8)), value)


def read_mountinfo():
    with open('/proc/self/mountinfo', encoding='utf-8') as source:
        return source.read()


def verify_storage(spec):
    """Verify the personal mount before creating only this run's directories."""
    personal, root = spec['storage_mount'], spec['run_dir']
    if personal in ('/', '/tmp', '/root') or not _inside(root, personal) or root == personal:
        raise GuardError('STORAGE_PATH_INVALID')
    mounts = []
    for line in read_mountinfo().splitlines():
        before, separator, after = line.partition(' - ')
        fields, filesystem = before.split(), after.split()
        if separator and len(fields) >= 6 and len(filesystem) >= 2:
            target = _unescape(fields[4])
            if target != '/':
                mounts.append((target, filesystem[0]))
    personal_mounts = [entry for entry in mounts if _inside(personal, entry[0])]
    actual_mounts = [entry for entry in mounts if _inside(root, entry[0])]
    if not personal_mounts or not actual_mounts:
        raise GuardError('STORAGE_MOUNT_UNVERIFIED')
    selected = max(personal_mounts, key=lambda entry: len(entry[0]))
    actual = max(actual_mounts, key=lambda entry: len(entry[0]))
    if (selected[1].lower() in _VIRTUAL_FILESYSTEMS or actual[1].lower() in _VIRTUAL_FILESYSTEMS
            or actual[0] != selected[0]):
        raise GuardError('STORAGE_SHADOWED_OR_EPHEMERAL')
    if (sum(entry[0] == selected[0] for entry in personal_mounts) != 1
            or sum(entry[0] == actual[0] for entry in actual_mounts) != 1):
        raise GuardError('STORAGE_STACKED_MOUNT_AMBIGUOUS')
    if not os.path.isdir(personal) or os.path.realpath(personal) != personal:
        raise GuardError('PERSONAL_STORAGE_MISSING_OR_SYMLINK')
    if os.path.realpath(root) != root:
        raise GuardError('RUN_DIRECTORY_SYMLINK')
    os.makedirs(root, mode=0o700, exist_ok=True)
    attempts = os.path.join(root, 'attempts')
    if os.path.realpath(attempts) != attempts:
        raise GuardError('ATTEMPT_DIRECTORY_SYMLINK')
    os.makedirs(attempts, mode=0o700, exist_ok=True)
    return attempts


def gpu_models(timeout=15):
    paths = ('/usr/bin/nvidia-smi', '/bin/nvidia-smi', '/usr/local/nvidia/bin/nvidia-smi')
    executable = next((path for path in paths if os.path.isfile(path) and os.access(path, os.X_OK)), None)
    if executable is None:
        raise GuardError('NVIDIA_SMI_UNAVAILABLE')
    try:
        result = subprocess.run([executable, '--query-gpu=name', '--format=csv,noheader'],
                                capture_output=True, text=True, timeout=timeout, check=False)
    except (OSError, subprocess.TimeoutExpired):
        raise GuardError('GPU_QUERY_FAILED') from None
    if result.returncode:
        raise GuardError('GPU_QUERY_FAILED')
    names = [line.strip() for line in result.stdout.splitlines() if line.strip()]
    if len(names) > 64 or any(len(name) > 160 or any(ord(char) < 32 for char in name) for name in names):
        raise GuardError('GPU_QUERY_INVALID')
    return names


def write_receipt(path, state):
    """Publish a complete JSON snapshot; never replace a different attempt."""
    parent = os.path.dirname(path)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile('w', encoding='utf-8', dir=parent,
                                         prefix='.native-receipt-', delete=False) as target:
            temporary = target.name
            os.chmod(temporary, 0o600)
            json.dump(dict(state, receipt_persisted=True), target, ensure_ascii=False)
            target.write('\n'); target.flush()
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            try: os.unlink(temporary)
            except FileNotFoundError: pass


def run(spec):
    os.umask(0o077)
    attempt = datetime.datetime.now(datetime.timezone.utc).strftime('%Y%m%dT%H%M%S') + '-' + uuid.uuid4().hex
    state = {'schema': 1, 'run_id': spec['run_id'], 'attempt_id': attempt,
             'spec_sha256': spec['spec_sha256'], 'state': 'STARTING', 'started_at': utcnow(),
             'expected_gpu_count': spec['gpus'], 'expected_gpu_model': spec.get('card_type', ''),
             'actual_gpu_names': [], 'hardware_verified': False, 'storage_verified': False,
             'receipt_persisted': False, 'sequence': 0}
    receipt_path, child, timer = None, None, None
    timed_out = threading.Event()
    cleanup_lock = threading.Lock()
    previous_handlers = {}

    def report():
        state['sequence'] += 1
        state['receipt_persisted'] = False
        print(RECEIPT_PREFIX + json.dumps(state, ensure_ascii=False, separators=(',', ':')), flush=True)
        if receipt_path is not None:
            persisted = dict(state, sequence=state['sequence'] + 1, receipt_persisted=True)
            write_receipt(receipt_path, persisted)
            state.update(persisted)
            print(RECEIPT_PREFIX + json.dumps(state, ensure_ascii=False, separators=(',', ':')), flush=True)

    def terminate_group():
        if child is None:
            return
        with cleanup_lock:
            try: os.killpg(child.pid, signal.SIGTERM)
            except ProcessLookupError: pass
            try: child.wait(timeout=3)
            except subprocess.TimeoutExpired: pass
            try: os.killpg(child.pid, signal.SIGKILL)
            except ProcessLookupError: pass
            try: child.wait(timeout=3)
            except subprocess.TimeoutExpired: pass

    def expire():
        timed_out.set()
        terminate_group()

    def cancel(signum, _frame):
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
        signal.signal(signal.SIGINT, signal.SIG_IGN)
        raise Cancelled(signum)

    for signum in (signal.SIGTERM, signal.SIGINT):
        previous_handlers[signum] = signal.signal(signum, cancel)
    report()
    try:
        state['phase'] = 'storage'
        attempts = verify_storage(spec)
        state['storage_verified'] = True
        receipt_path = os.path.join(attempts, attempt + '.json')
        # An attempt identifier is claimed exclusively before any snapshots.
        with open(receipt_path, 'x', encoding='utf-8'):
            pass
        if spec['gpus']:
            state['phase'] = 'gpu'
            names = gpu_models(min(15, spec.get('guard_seconds', 60)))
            state['actual_gpu_names'] = names
            if len(names) != spec['gpus'] or any(normalized_model(name) != normalized_model(spec['card_type']) for name in names):
                raise GuardError('GPU_COUNT_OR_MODEL_MISMATCH')
        state['hardware_verified'] = True
        state['phase'] = 'program'
        environment = os.environ.copy()
        environment.update(spec.get('env', {}))
        child = subprocess.Popen(spec['argv'], cwd=spec['run_dir'], env=environment, start_new_session=True)
        state.update(state='RUNNING', runner_pid=os.getpid(), program_pid=child.pid)
        # Start the deadline before persisting RUNNING; storage latency cannot
        # extend the scientific program's allocated runtime.
        timer = threading.Timer(spec['max_seconds'], expire)
        timer.daemon = True; timer.start()
        report()
        code = child.wait()
        timer.cancel()
        state['program_exit_code'] = code
        state.update(state='TIMED_OUT' if timed_out.is_set() else 'SUCCEEDED' if code == 0 else 'FAILED',
                     exit_code=124 if timed_out.is_set() else code)
    except Cancelled as error:
        terminate_group()
        state.update(state='CANCELLED', exit_code=128 + int(error.args[0]), error_code='TASK_SIGNALLED')
    except GuardError as error:
        state.update(state='GUARD_FAILED', exit_code=125, error_code=str(error))
    except BaseException as error:
        terminate_group()
        state.update(state='FAILED', exit_code=125, error_code='RUNNER_' + type(error).__name__.upper())
    finally:
        if timer is not None:
            timer.cancel()
        terminate_group()
        state['phase'] = 'complete'
        state['ended_at'] = utcnow()
        try:
            report()
        except BaseException:
            # A success without its persisted terminal receipt is not a verified
            # successful task. The earlier unpersisted marker remains evidence.
            state.update(state='FAILED', exit_code=125, error_code='RECEIPT_WRITE_FAILED', receipt_persisted=False)
            state['sequence'] += 1
            print(RECEIPT_PREFIX + json.dumps(state, ensure_ascii=False, separators=(',', ':')), flush=True)
        for signum, handler in previous_handlers.items():
            signal.signal(signum, handler)
    if state['state'] == 'SUCCEEDED' and state['receipt_persisted']:
        return 0
    code = int(state.get('exit_code') or 125)
    return code if code > 0 else 128 + abs(code)


def main(payload):
    try:
        spec = json.loads(base64.urlsafe_b64decode(payload.encode('ascii')).decode('utf-8'))
        if not isinstance(spec, dict):
            raise ValueError('Invalid task manifest')
    except (ValueError, UnicodeError, TypeError):
        print(RECEIPT_PREFIX + json.dumps({'schema': 1, 'state': 'GUARD_FAILED', 'error_code': 'INVALID_MANIFEST'}), flush=True)
        return 125
    return run(spec)


if __name__ == '__main__':
    raise SystemExit(main(sys.argv[1]))
