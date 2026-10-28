"""Account-scoped, atomic run records and per-run process locks (POSIX only)."""
from contextlib import contextmanager
import datetime
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import tempfile


def utcnow():
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def validate_run_id(run_id):
    if not isinstance(run_id, str) or not re.fullmatch(r"[A-Za-z0-9_][A-Za-z0-9_-]{0,47}", run_id):
        raise ValueError("run_id must contain 1-48 letters, numbers, underscores or hyphens")
    return run_id


def profile_identity(profile):
    return hashlib.sha256((profile['portal'].rstrip('/') + '\0' + profile['account']).encode()).hexdigest()


class Registry:
    def __init__(self, root, identity=None):
        self.root = Path(root).expanduser().resolve()
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        if identity is not None:
            with self.lock():
                marker = self.root / '.identity'
                if marker.exists():
                    if marker.read_text().strip() != identity:
                        raise ValueError("State directory belongs to a different portal/account")
                else:
                    # Adopting nonempty unscoped state could expose another account's jobs.
                    if list(self.root.glob('*.json')):
                        raise ValueError("Refusing to adopt an existing unscoped state directory")
                    with marker.open('x') as handle:
                        os.chmod(marker, 0o600)
                        handle.write(identity + '\n'); handle.flush(); os.fsync(handle.fileno())

    def path(self, run_id):
        return self.root / (validate_run_id(run_id) + ".json")

    @contextmanager
    def lock(self, run_id=None):
        filename = '.lock' if run_id is None else validate_run_id(run_id) + '.record.lock'
        with (self.root / filename).open("a+") as handle:
            os.chmod(handle.name, 0o600)
            fcntl.flock(handle, fcntl.LOCK_EX)
            try: yield
            finally: fcntl.flock(handle, fcntl.LOCK_UN)

    @contextmanager
    def operation(self, run_id, name):
        if name not in ('allocation','submission','collection','stop'):
            raise ValueError('Unknown operation lock')
        with (self.root / (validate_run_id(run_id) + '.' + name + '.lock')).open('a+') as handle:
            os.chmod(handle.name, 0o600)
            fcntl.flock(handle, fcntl.LOCK_EX)
            try: yield
            finally: fcntl.flock(handle, fcntl.LOCK_UN)

    @contextmanager
    def controller(self, run_id):
        with (self.root / (validate_run_id(run_id) + ".controller.lock")).open("a+") as handle:
            os.chmod(handle.name, 0o600)
            try:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                yield False
                return
            try: yield True
            finally: fcntl.flock(handle, fcntl.LOCK_UN)

    def controller_active(self, run_id):
        with self.controller(run_id) as owner:
            return not owner

    def get(self, run_id):
        return json.loads(self.path(run_id).read_text())

    def write(self, run_id, record):
        record['updated_at'] = utcnow()
        temporary = None
        try:
            with tempfile.NamedTemporaryFile('w', dir=self.root, delete=False) as output:
                temporary = Path(output.name)
                os.chmod(temporary, 0o600)
                json.dump(record, output, ensure_ascii=False, indent=2)
                output.write('\n'); output.flush(); os.fsync(output.fileno())
            os.replace(temporary, self.path(run_id))
        finally:
            if temporary is not None: temporary.unlink(missing_ok=True)

    def update(self, run_id, **values):
        with self.lock(run_id):
            record = self.get(run_id); record.update(values); self.write(run_id, record)
        return record

    def reserve(self, run_id, spec, kind):
        fingerprint = hashlib.sha256(json.dumps(spec, sort_keys=True).encode()).hexdigest()
        with self.lock(run_id):
            if self.path(run_id).exists():
                record = self.get(run_id)
                if record['spec_sha256'] != fingerprint or record['kind'] != kind:
                    raise ValueError('run_id belongs to different parameters; use a new run_id')
                return record
            record = {'run_id':run_id, 'kind':kind, 'state':'PLANNED', 'spec':spec,
                      'spec_sha256':fingerprint, 'created_at':utcnow()}
            self.write(run_id, record)
            return record

    def list(self):
        return [json.loads(path.read_text()) for path in sorted(self.root.glob('*.json'))]
