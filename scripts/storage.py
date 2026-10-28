"""Mount evidence and a bounded personal-directory probe for AIStation storage."""
import json
import posixpath
import re


class StorageError(RuntimeError):
    """The configured persistent storage could not be verified."""


def _unescape(value):
    return re.sub(r"\\([0-7]{3})", lambda match: chr(int(match.group(1), 8)), value)


def _inside(path, parent):
    return path == parent or path.startswith(parent.rstrip('/') + '/')


def verify_mount(mountinfo, storage_mount, run_dir):
    """Require a distinct mounted filesystem, never a similarly named empty folder."""
    storage_mount = posixpath.normpath(storage_mount)
    run_dir = posixpath.normpath(run_dir)
    if not storage_mount.startswith('/') or storage_mount in ('/', '/tmp', '/root') or not _inside(run_dir, storage_mount):
        raise StorageError('Run directory must stay within the configured personal storage mount')
    candidates = []
    for line in mountinfo.splitlines():
        before, separator, after = line.partition(' - ')
        fields, filesystem = before.split(), after.split()
        if not separator or len(fields) < 6 or len(filesystem) < 2:
            continue
        target = _unescape(fields[4])
        if target != '/' and _inside(storage_mount, target):
            candidates.append({'mount_point': target, 'filesystem': filesystem[0],
                               'source': _unescape(filesystem[1])})
    if not candidates:
        raise StorageError('No independent mount covers the configured personal storage; no directory was created')
    selected = max(candidates, key=lambda row: len(row['mount_point']))
    if selected['filesystem'].lower() in {'overlay', 'tmpfs', 'ramfs', 'proc', 'sysfs', 'devtmpfs', 'devpts'}:
        raise StorageError('Configured storage resolves to an ephemeral/virtual filesystem')
    return dict(selected, storage_mount=storage_mount, run_dir=run_dir,
                mount_verified=True, group_quota_verified=False)


# Its only writes are in the verified personal run directory. It never creates
# a mount point, changes access control, or sends credentials to a subprocess.
PROBE_PROGRAM = r'''
import json, os, sys, time, uuid
root = sys.argv[1]
write_probe = sys.argv[2] == '1'
personal = sys.argv[3]
started = time.monotonic()
def report(phase, **values):
    print(json.dumps(dict(phase=phase, elapsed_seconds=round(time.monotonic()-started, 4), **values)), flush=True)
report('inspect')
if not os.path.isdir(personal):
    raise RuntimeError('Configured personal storage directory is missing; it will not be created')
if os.path.realpath(personal) != personal:
    raise RuntimeError('Configured personal storage directory is a symlink')
if os.path.realpath(root) != root:
    raise RuntimeError('Run path contains a symlink; explicit storage verification is required')
if root == personal or not root.startswith(personal.rstrip('/') + '/'):
    raise RuntimeError('Run path must be below the existing personal storage directory')
if write_probe:
    report('mkdir')
    os.makedirs(root, mode=0o700, exist_ok=True)
    path = os.path.join(root, '.wlai-storage-probe-' + uuid.uuid4().hex)
    try:
        report('write')
        with open(path, 'xb') as handle:
            handle.write(b'wlai-storage-check\n'); handle.flush()
        report('read')
        with open(path, 'rb') as handle:
            if handle.read() != b'wlai-storage-check\n':
                raise RuntimeError('Storage probe round trip mismatch')
    finally:
        report('remove')
        try: os.unlink(path)
        except FileNotFoundError: pass
report('space')
existing = root
while not os.path.exists(existing):
    existing = os.path.dirname(existing)
space = os.statvfs(existing)
report('complete', writable_checked=write_probe,
       available_bytes=space.f_bavail * space.f_frsize,
       group_quota_verified=False)
'''


def parse_probe_output(output):
    phases = []
    for line in output.splitlines():
        try:
            record = json.loads(line)
        except (ValueError, TypeError):
            continue
        if isinstance(record, dict) and record.get('phase') in {
                'inspect', 'mkdir', 'write', 'read', 'remove', 'space', 'complete'}:
            phases.append(record)
    if not phases or phases[-1]['phase'] != 'complete':
        stage = phases[-1]['phase'] if phases else 'start'
        raise StorageError('Storage check did not complete (last stage: ' + stage + ')')
    result = dict(phases[-1], phases=phases)
    result['latency_warning'] = result.get('elapsed_seconds', 0) > 5
    return result


def last_probe_phase(output):
    try:
        return parse_probe_output(output)['phase']
    except StorageError:
        for line in reversed(output.splitlines()):
            try: value = json.loads(line)
            except (ValueError, TypeError): continue
            if isinstance(value, dict) and value.get('phase') in {'inspect','mkdir','write','read','remove','space'}:
                return value['phase']
    return 'start'
