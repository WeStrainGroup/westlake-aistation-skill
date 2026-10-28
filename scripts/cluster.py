"""AIStation resources, automatic SSH authentication, and bounded managed runs."""
import base64
from contextlib import contextmanager
import datetime
import errno
import hashlib
import json
import os
import posixpath
import random
import re
from pathlib import Path, PurePosixPath
from urllib.parse import urlsplit
import secrets
import shlex
import subprocess
import sys
import time

from Cryptodome.Cipher import AES
from Cryptodome.Util.Padding import unpad
from gmssl.sm2 import CryptSM2
import paramiko

from api import AIStation, ApiError, rows
from config import load_profile, state_directory
from credentials import get_password
from registry import Registry, utcnow, validate_run_id, profile_identity

ROOT = Path(__file__).resolve().parent
TERMINAL = {"SUCCEEDED", "FAILED", "TIMED_OUT", "CANCELLED", "BLOCKED", "COLLECTION_FAILED"}


class RemoteExecutionTimeout(TimeoutError):
    """Safe default message; partial job output is opt-in diagnostic data only."""
    def __init__(self, stdout, stderr):
        super().__init__('SSH execution deadline exceeded')
        self.stdout = bytes(stdout[-65536:]).decode(errors='replace')
        self.stderr = bytes(stderr[-65536:]).decode(errors='replace')


def sha256(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def prepare_spec(source, profile=None):
    profile = profile or load_profile()
    spec = json.loads(json.dumps(source))
    run_id = spec.get("run_id")
    validate_run_id(run_id)
    spec.setdefault("backend", "environment")
    spec.setdefault("cpus", 12)
    spec.setdefault("gpus", 1)
    spec.setdefault("memory_gb", 0)
    spec.setdefault("image", profile["image"])
    spec.setdefault("resource_group_id", profile["development_group_id"])
    spec.setdefault("framework", profile.get("framework", "pytorch"))
    if spec["gpus"]:
        spec.setdefault("card_type", profile.get("card_type", ""))
    spec.setdefault("max_seconds", 60)
    spec.setdefault("ready_timeout", 150)
    spec.setdefault("uploads", [])
    spec.setdefault("downloads", [])
    for key in ("resource_group_id", "card_type"):
        if key in spec and (not isinstance(spec[key], str) or not spec[key].strip()):
            raise ValueError(f"{key} must be a nonempty string copied from live resource metadata")
    for key, low, high in (("cpus", 1, 500), ("gpus", 0, 25), ("max_seconds", 1, 13800),
                           ("ready_timeout", 1, 1800), ("memory_gb", 0, 1024)):
        if type(spec[key]) is not int or not low <= spec[key] <= high:
            raise ValueError(f"{key} must be an integer from {low} to {high}")
    if spec["max_seconds"] + spec["ready_timeout"] + 60 > min(14400, profile.get("max_environment_seconds", 14400)):
        raise ValueError("Program, readiness and cleanup allowance must fit the 4-hour development limit")
    if spec["backend"] != "environment":
        raise ValueError("Only the verified environment backend is enabled")
    if not isinstance(spec.get("argv"), list) or not spec["argv"] or not all(
            isinstance(item, str) and "\x00" not in item for item in spec["argv"]):
        raise ValueError("argv must be a nonempty JSON list of command arguments")
    if not spec["argv"][0].startswith("/"):
        raise ValueError("Use the absolute path of the remote executable")
    spec["run_dir"] = posixpath.join(profile["persistent_root"], run_id)
    if not all(isinstance(spec[key], list) for key in ("uploads", "downloads")):
        raise ValueError("uploads and downloads must be lists")
    for entry in spec["uploads"]:
        path = Path(entry["local"]).expanduser().resolve()
        if not path.is_file():
            raise ValueError(f"Upload source is not a regular file: {path}")
        entry["local"] = str(path)
        digest = sha256(path)
        if entry.get("sha256") not in (None, digest):
            raise ValueError("Upload source changed after planning")
        entry["sha256"] = digest
        entry["remote"] = posixpath.normpath(entry["remote"].replace("{run_dir}", spec["run_dir"]))
        if not entry["remote"].startswith(spec["run_dir"] + "/"):
            raise ValueError("Automatic run uploads must stay inside this run's unique directory")
    for entry in spec["downloads"]:
        entry["remote"] = posixpath.normpath(entry["remote"].replace("{run_dir}", spec["run_dir"]))
        if not entry["remote"].startswith("/") or "\x00" in entry["remote"]:
            raise ValueError("Download source must be an absolute remote path")
        entry["local"] = str(Path(entry["local"]).expanduser().resolve())
    spec["argv"] = [value.replace("{run_dir}", spec["run_dir"]) for value in spec["argv"]]
    return spec


class Cluster:
    def __init__(self, state_dir=None, profile=None, interactive=True):
        self.profile = profile or load_profile()
        self.registry = Registry(state_dir or state_directory(self.profile), profile_identity(self.profile))
        self.interactive = interactive
        self._client = None
        self._user = None

    @property
    def client(self):
        if self._client is None:
            provider = lambda: get_password(self.profile, interactive=self.interactive)
            client = AIStation(self.profile, password_provider=provider)
            self._user = client.login(self.profile["account"], provider())
            self._client = client
        return self._client

    @property
    def user(self):
        self.client
        return self._user

    def close(self):
        if self._client is not None:
            self._client.close()

    def _check_cancelled(self, run_id):
        record = self.registry.get(run_id)
        if record.get("cancel_requested"):
            raise InterruptedError("Cancellation requested; refusing to launch work")

    def pages(self, endpoint, params=None):
        result = []
        for page in range(1, 51):
            query = dict(params or {}, page=page, pageSize=100)
            data = self.client.call("GET", endpoint, params=query)
            batch = rows(data); result.extend(batch)
            total = data.get("total") if isinstance(data, dict) else None
            if not batch or len(batch) < 100 or isinstance(total, int) and len(result) >= total:
                return result
        raise RuntimeError("Pagination exceeded 50 pages; refine the resource filter")

    def resources(self, group_id=None):
        query = {"getUsage": 1}
        if group_id:
            query["groupId"] = group_id
        nodes = self.pages("/api/iresource/v1/node", query)
        return [{"node": row.get("nodeName"), "group_id": row.get("groupId"),
                 "group": row.get("groupName"), "card_type": row.get("cardType"),
                 "card_kind": row.get("cardKind"),
                 "cpu_total": row.get("cpu"), "cpu_used": row.get("cpuUsage"),
                 "gpu_total": row.get("acceleratorCard"),
                 "gpu_used": row.get("acceleratorCardUsage"),
                 "available_cpus": max(0, int(row.get("cpu") or 0) - int(row.get("cpuUsage") or 0)),
                 "available_gpus": max(0, int(row.get("acceleratorCard") or 0) - int(row.get("acceleratorCardUsage") or 0))}
                for row in nodes]

    def environment(self, environment_id):
        return self.client.call("GET", f"/api/iresource/v1/work-platform/{environment_id}/detail")

    def env_payload(self, spec):
        return {"wpType": "COMMON_WP", "jupyterType": 0, "wpName": "wl_" + spec["run_id"],
                "groupId": spec.get("resource_group_id", self.profile["development_group_id"]), "wpPodNum": 1,
                "frameWork": spec.get("framework", self.profile.get("framework", "pytorch")), "imageType": "INNER_IMAGE", "image": spec["image"],
                "cpu": spec["cpus"], "memory": spec["memory_gb"], "acceleratorCard": spec["gpus"],
                "acceleratorCardKind": "GPU" if spec["gpus"] else "CPU",
                "acceleratorCardType": spec.get("card_type", self.profile.get("card_type", "")) if spec["gpus"] else "",
                "acceleratorCardMemory": 0, "shmSize": 4, "command": "", "switchType": "ib",
                "models": [], "ports": [], "volumes": [], "nodeList": [], "enUpdateDataSet": 0}

    def plan(self, spec):
        record = self.registry.reserve(spec["run_id"], spec, "run")
        if record["state"] == "PLANNED":
            for entry in spec["downloads"]:
                if Path(entry["local"]).exists():
                    raise ValueError(f"Download destination already exists: {entry['local']}")
        group = spec.get("resource_group_id", self.profile["development_group_id"]) if spec["backend"] == "environment" else self.profile.get("training_group_id")
        nodes = self.resources(group)
        eligible = [node["node"] for node in nodes if node["available_cpus"] >= spec["cpus"]
                    and node["available_gpus"] >= spec["gpus"]
                    and (not spec["gpus"] or node["card_type"] == spec.get("card_type", self.profile.get("card_type", "")))]
        return {"run_id": spec["run_id"], "state": record["state"], "spec_sha256": record["spec_sha256"],
                "spec": spec, "eligible_nodes_now": eligible,
                "resource_request": self.env_payload(spec) if spec["backend"] == "environment" else None,
                "program_gpu_hours_budget": round(spec["gpus"] * spec["max_seconds"] / 3600, 4),
                "submitted": bool(record.get("resource_id"))}

    def create_environment(self, run_id):
        # The lock spans the request so two agents cannot both submit this run_id.
        with self.registry.operation(run_id, "allocation"):
            self._check_cancelled(run_id)
            record = self.registry.get(run_id)
            if record.get("resource_id"):
                return record
            if record["state"] != "PLANNED":
                raise RuntimeError("Creation outcome needs reconcile; never resubmit an ambiguous POST")
            record = self.registry.update(run_id, state="SUBMITTING", resource_type="work-platform")
            try:
                created = self.client.call("POST", "/api/iresource/v1/work-platform/",
                                           body=self.env_payload(record["spec"]))
            except BaseException:
                self.registry.update(run_id, state="UNKNOWN", uncertain_action="allocation")
                raise
            resource_id = (created.get("wpId") or created.get("id")) if isinstance(created, dict) else created if isinstance(created, str) else None
            if not resource_id:
                matching = [row for row in self.pages("/api/iresource/v1/work-platform/history")
                            if row.get("wpName") == "wl_" + run_id]
                if len(matching) == 1:
                    resource_id = matching[0]["wpId"]
            if not resource_id:
                self.registry.update(run_id, state="UNKNOWN", uncertain_action="allocation")
                raise RuntimeError("No unique environment ID returned; reconcile by run_id before retrying")
            with self.registry.lock(run_id):
                current = self.registry.get(run_id)
                current.update(resource_id=resource_id, state='CANCELLED' if current.get('cancel_requested') else 'ALLOCATING')
                self.registry.write(run_id,current)
                return current

    def wait_environment(self, run_id):
        record = self.registry.get(run_id); spec = record["spec"]
        deadline = time.monotonic() + spec["ready_timeout"]
        previous = None
        while time.monotonic() < deadline:
            self._check_cancelled(run_id)
            detail = self.environment(record["resource_id"])
            state = detail.get("wpStatus")
            if detail.get("userId") != self.user["userId"]:
                raise RuntimeError("Environment belongs to a different account")
            if state != previous:
                self.registry.update(run_id, platform_state=state)
                previous = state
            if state == "Running":
                # Pending/ImagePulling detail can omit allocated fields. Only a complete,
                # running allocation is eligible to execute any user code.
                if detail.get("cpu") != spec["cpus"] or detail.get("image") != spec["image"]:
                    raise RuntimeError("Allocated CPU/image differs from the plan")
                if detail.get("acceleratorCard") != spec["gpus"] or spec["gpus"] and detail.get("acceleratorCardKind") != "GPU":
                    raise RuntimeError("Allocated resources differ from the plan; refusing to run the program")
                validation = {}
                if spec["gpus"]:
                    expected = spec.get("card_type", self.profile.get("card_type", ""))
                    reported = detail.get("acceleratorCardType")
                    validation = {'source':'platform-detail','expected_model':expected,'reported_type':reported}
                    if reported != expected:
                        # This deployment sometimes returns the count alias GPU:N instead of a model.
                        # Only that observed alias permits hardware verification; unknown types still fail.
                        match = re.fullmatch(r'GPU:([0-9]+)',reported or '')
                        if not match or int(match.group(1)) != spec['gpus']:
                            raise RuntimeError("Allocated GPU model differs from the plan")
                        names = self.verify_gpu_devices(record['resource_id'],expected,spec['gpus'])
                        validation.update(source='nvidia-smi',actual_gpu_names=names)
                return self.registry.update(run_id, state="READY", platform_state=state,
                    gpu_verification=validation,actual_gpu_names=validation.get('actual_gpu_names',[]))
            if state in ("Failed", "Halt", "Pause"):
                raise RuntimeError(f"Environment became {state}")
            time.sleep(3)
        raise TimeoutError("Environment readiness deadline exceeded")

    def verify_gpu_devices(self, environment_id, expected_model, expected_count):
        with self.ssh(environment_id) as client:
            result = self.execute(client,['nvidia-smi','--query-gpu=name','--format=csv,noheader'],20)
        names = [line.strip() for line in result['stdout'].splitlines() if line.strip()]
        def normalized(value):
            # Only display separators are ignored, never model numbers or suffixes.
            return re.sub(r'[ \t_-]','',value).lower()
        if result['exit_code'] or len(names) != expected_count or not all(
                normalized(name) == normalized(expected_model) for name in names):
            raise RuntimeError('Physical GPU count/model differs from the plan; refusing to run the program')
        return names

    def ssh_credentials(self, environment_id):
        public = self.client.call("GET", "/api/ibase/v1/system/secret")
        public = public[2:] if len(public) == 130 else public
        key = secrets.token_hex(8).encode()
        encrypted = "04" + CryptSM2(public_key=public, private_key="", mode=0).encrypt(key).hex()
        data = self.client.call("GET", f"/api/iresource/v1/work-platform/{environment_id}/pod/default/shell",
                                params={"k": encrypted})
        password = unpad(AES.new(key, AES.MODE_CBC, b"87EEA6BC8A6383B3").decrypt(
            bytes.fromhex(data["password"])), 16).decode()
        command = shlex.split(data["sshCommand"])
        target = next(part for part in command if "@" in part)
        user, host = target.split("@", 1)
        port = int(command[command.index("-p") + 1])
        expected_host = self.profile.get("ssh_host", urlsplit(self.profile["portal"]).hostname)
        if host != expected_host or user != self.profile.get("ssh_user", "root") or port == 22 or not 1 <= port <= 65535:
            raise RuntimeError("Unexpected SSH endpoint from platform")
        return host, port, user, password

    @contextmanager
    def ssh(self, environment_id):
        detail = self.environment(environment_id)
        if detail.get('userId') != self.user['userId']:
            raise RuntimeError('Refusing SSH access to another account environment')
        if detail.get('wpStatus') != 'Running':
            raise RuntimeError('SSH requires a running environment')
        expected = self.profile["ssh_host_fingerprint"]

        class PinnedHost(paramiko.MissingHostKeyPolicy):
            def missing_host_key(self, client, hostname, key):
                fingerprint = "SHA256:" + base64.b64encode(hashlib.sha256(key.asbytes()).digest()).decode().rstrip("=")
                if fingerprint != expected:
                    raise RuntimeError("SSH host identity changed; verify it before updating the profile")

        host, port, user, password = self.ssh_credentials(environment_id)
        client = paramiko.SSHClient(); client.set_missing_host_key_policy(PinnedHost())
        try:
            client.connect(host, port=port, username=user, password=password,
                           look_for_keys=False, allow_agent=False, timeout=15,
                           auth_timeout=15, banner_timeout=15, channel_timeout=30)
            del password
            client.get_transport().set_keepalive(30)
            yield client
        finally:
            client.close()

    @staticmethod
    def _open_sftp(client, timeout=30):
        sftp = client.open_sftp()
        sftp.get_channel().settimeout(timeout)
        return sftp

    @staticmethod
    def execute(client, argv, max_seconds=60):
        command = shlex.join(["timeout", "--kill-after=5", str(max_seconds), *argv])
        _, stdout, _ = client.exec_command(command, timeout=max_seconds + 15)
        channel = stdout.channel; out = bytearray(); err = bytearray()
        deadline = time.monotonic() + max_seconds + 15
        while True:
            if channel.recv_ready(): out.extend(channel.recv(65536))
            if channel.recv_stderr_ready(): err.extend(channel.recv_stderr(65536))
            if channel.exit_status_ready() and not channel.recv_ready() and not channel.recv_stderr_ready():
                return {"exit_code": channel.recv_exit_status(), "stdout": out.decode(errors="replace"),
                        "stderr": err.decode(errors="replace")}
            if time.monotonic() > deadline:
                channel.close(); raise RemoteExecutionTimeout(out,err)
            time.sleep(0.05)

    def _publish_upload(self, client, remote, write, expected_hash=None):
        """Verify a temporary remote object, then publish with an exclusive hard link."""
        temporary = remote + '.part-' + secrets.token_hex(8)
        sftp = self._open_sftp(client)
        digest = hashlib.sha256()
        try:
            with sftp.open(temporary, 'wx') as target:
                target.set_pipelined(True)
                write(target, digest)
            value = digest.hexdigest()
            if expected_hash is not None and value != expected_hash:
                raise ValueError('Upload source changed after planning')
            check = self.execute(client, ['sha256sum', '--', temporary])
            if check['exit_code'] or not check['stdout'].split() or check['stdout'].split()[0] != value:
                raise RuntimeError('Uploaded file checksum mismatch')
            publish = self.execute(client, ['ln', '--', temporary, remote])
            if publish['exit_code']:
                raise FileExistsError('Remote destination exists or exclusive publication failed')
            return value
        finally:
            try: sftp.remove(temporary)
            except OSError: pass
            sftp.close()

    def put_bytes(self, client, remote, data):
        def write(target, digest):
            target.write(data); digest.update(data)
        return self._publish_upload(client, remote, write)

    def put(self, client, local, remote, expected_hash=None):
        local = Path(local)
        if expected_hash is not None and sha256(local) != expected_hash:
            raise ValueError('Upload source changed after planning')
        def write(target, digest):
            with local.open('rb') as source:
                for block in iter(lambda: source.read(1024 * 1024), b''):
                    target.write(block); digest.update(block)
        digest = self._publish_upload(client, remote, write, expected_hash)
        return {'local':str(local), 'remote':remote, 'sha256':digest}

    def get(self, client, remote, local, accept_identical=False):
        local = Path(local); local.parent.mkdir(parents=True, exist_ok=True)
        sftp = self._open_sftp(client)
        temporary = local.parent / ('.' + local.name + '.' + secrets.token_hex(8) + '.part')
        try:
            check = self.execute(client, ['sha256sum', '--', remote])
            if check['exit_code'] or not check['stdout'].split():
                raise FileNotFoundError('Remote source is unavailable')
            expected = check['stdout'].split()[0]
            if local.exists():
                digest = sha256(local)
                if accept_identical and digest == expected:
                    return {'local':str(local), 'remote':remote, 'sha256':digest, 'reused':True}
                raise FileExistsError(str(local))
            digest = hashlib.sha256()
            with temporary.open('xb') as target, sftp.open(remote, 'rb') as source:
                for block in iter(lambda: source.read(1024 * 1024), b''):
                    target.write(block); digest.update(block)
                target.flush(); os.fsync(target.fileno())
            value = digest.hexdigest()
            # Compare before/after to detect a result file changing during transfer.
            check = self.execute(client, ['sha256sum', '--', remote])
            if check['exit_code'] or not check['stdout'].split() or expected != value or check['stdout'].split()[0] != value:
                raise RuntimeError('Downloaded file checksum mismatch or remote source changed')
            os.link(temporary, local)
            return {'local':str(local), 'remote':remote, 'sha256':value}
        finally:
            sftp.close()
            temporary.unlink(missing_ok=True)

    def launch(self, run_id):
        record = self.registry.get(run_id); spec = record['spec']; root = spec['run_dir']
        self._check_cancelled(run_id)
        if record.get('launch_attempted'):
            # A lost response after nohup is ambiguous: inspect durable status, never send nohup again.
            state = self.remote_status(run_id)
            if state.get('result'):
                return state
            self.registry.update(run_id, state='UNKNOWN', uncertain_action='launch')
            raise RuntimeError('Prior launch is ambiguous; reconcile remote status without relaunching')
        for entry in spec['uploads']:
            if sha256(entry['local']) != entry['sha256']:
                raise ValueError('Upload source changed after planning')
        with self.ssh(record['resource_id']) as client:
            result = self.execute(client, ['mkdir', '-p', root])
            if result['exit_code']: raise RuntimeError('Cannot create persistent run directory')
            for entry in spec['uploads']:
                self._check_cancelled(run_id)
                parent = str(PurePosixPath(entry['remote']).parent)
                result = self.execute(client, ['mkdir', '-p', parent])
                if result['exit_code']: raise RuntimeError('Cannot create upload directory')
                self.put(client, entry['local'], entry['remote'], entry['sha256'])
            self.put_bytes(client, root + '/job.json', json.dumps({
                'argv':spec['argv'], 'max_seconds':spec['max_seconds']}).encode())
            self.put_bytes(client, root + '/runner.py', (ROOT / 'remote_runner.py').read_bytes())
            self._check_cancelled(run_id)
            # Persist intention before execution. Any interruption after this point requires reconciliation.
            self.registry.update(run_id, launch_attempted=True, launch_requested_at=utcnow())
            command = ('nohup ' + shlex.join([self.profile.get('remote_python', '/opt/conda/bin/python'), root + '/runner.py', root]) +
                       ' > ' + shlex.quote(root + '/launcher.log') + ' 2>&1 < /dev/null & echo $!')
            try:
                _, out, err = client.exec_command(command, timeout=15)
                pid = out.read().decode().strip()
                # Consume stderr but never include arbitrary remote contents in an exception.
                err.read()
                if out.channel.recv_exit_status() or not pid.isdigit():
                    raise RuntimeError('Remote runner launch was not confirmed')
            except BaseException:
                self.registry.update(run_id, state='UNKNOWN', uncertain_action='launch')
                raise
        return self.registry.update(run_id, state='RUNNING', remote_runner_pid=int(pid), launched_at=utcnow())

    def remote_status(self, run_id, client=None, sftp=None):
        record = self.registry.get(run_id)
        if client is None:
            with self.ssh(record['resource_id']) as session:
                return self.remote_status(run_id, session)
        own_sftp = sftp is None
        if own_sftp: sftp = self._open_sftp(client)
        try:
            with sftp.open(record['spec']['run_dir'] + '/status.json', 'rb') as source:
                state = json.loads(source.read())
            values = {'compute_state':state['state'], 'result':state}
            if not record.get('cancel_requested') and record['state'] != 'COLLECTION_FAILED':
                values['state'] = state['state']
            if state.get('runner_pid'): values['remote_runner_pid'] = state['runner_pid']
            if record.get('result') == state and all(record.get(k) == v for k,v in values.items()):
                return record
            return self.registry.update(run_id, **values)
        except FileNotFoundError:
            return record
        finally:
            if own_sftp: sftp.close()

    def stop_environment(self, run_id, timeout=60):
        with self.registry.operation(run_id, 'stop'):
            record = self.registry.get(run_id)
            if not record.get('resource_id') or record.get('resource_released'):
                return record
            resource_id = record['resource_id']
            detail = self.environment(resource_id)
            if detail.get('userId') != self.user['userId']:
                raise RuntimeError('Refusing to stop an environment owned by another account')
            released = ('Pause', 'Halt', 'Failed')
            current = detail.get('wpStatus')
            deadline = time.monotonic() + timeout
            pause_sent = False
            if current not in released:
                self.registry.update(run_id,stop_requested=True,resource_released=False)
            while current not in released:
                # This deployment does not reliably accept pause while scheduling.
                # Wait within the same deadline, never DELETE or launch user code.
                if current == 'Running' and not pause_sent:
                    try:
                        self.client.call('PUT', '/api/iresource/v1/work-platform/pause/' + resource_id)
                    except BaseException:
                        self.registry.update(run_id,stop_uncertain=True)
                        raise
                    pause_sent = True
                remaining = deadline-time.monotonic()
                if remaining <= 0:
                    self.registry.update(run_id, resource_released=False, stop_uncertain=True)
                    raise TimeoutError('Resource release is not confirmed; inspect status before retrying')
                time.sleep(min(2,remaining))
                detail = self.environment(resource_id)
                if detail.get('userId') != self.user['userId']:
                    raise RuntimeError('Environment ownership changed during stop')
                current = detail.get('wpStatus')
            return self.registry.update(run_id, platform_state=current, final_resource_state=current,
                                        resource_released=True, stop_uncertain=False)

    def cancel(self, run_id):
        record = self.registry.get(run_id)
        if record['state'] in TERMINAL and record.get('resource_released'):
            return record
        compute = record.get('compute_state',record.get('result',{}).get('state'))
        preserve_state = record['state'] in TERMINAL or compute in TERMINAL
        values = {'cancel_requested':True}
        if not preserve_state: values['state']='CANCELLED'
        self.registry.update(run_id,**values)
        # A POST already in flight may return its resource ID after cancellation was requested.
        # Wait for that one allocation before stopping; never leave it orphaned.
        with self.registry.operation(run_id,'allocation'):
            current = self.registry.get(run_id)
            if not current.get('resource_id'):
                if current.get('uncertain_action') == 'allocation':
                    raise RuntimeError('Allocation outcome is uncertain; reconcile before stopping')
                return self.registry.update(run_id,resource_released=True,final_resource_state='NotAllocated')
            result = self.stop_environment(run_id)
            if not preserve_state:
                result=self.registry.update(run_id,state='CANCELLED')
            return result

    def finish(self, run_id):
        with self.registry.operation(run_id, 'collection'):
            record = self.registry.get(run_id); spec = record['spec']
            if record.get('collection_complete'): return record
            compute = record.get('compute_state',record.get('result',{}).get('state'))
            if compute not in ('SUCCEEDED','FAILED','TIMED_OUT','CANCELLED'):
                raise RuntimeError('Computation is not confirmed finished; refusing premature collection')
            receipt_dir = self.registry.root / (run_id + '_files')
            receipt_dir.mkdir(exist_ok=True, mode=0o700)
            transfers = []
            try:
                with self.ssh(record['resource_id']) as client:
                    for filename in ('status.json', 'stdout.log', 'stderr.log', 'launcher.log'):
                        target = receipt_dir / filename
                        try:
                            transfers.append(self.get(client, spec['run_dir']+'/'+filename, target, accept_identical=True))
                        except FileNotFoundError:
                            if filename == 'status.json': raise
                    # Failed computations often do not produce declared scientific results. Always collect logs first.
                    if record.get('compute_state', record['state']) == 'SUCCEEDED':
                        for entry in spec['downloads']:
                            transfers.append(self.get(client, entry['remote'], entry['local'], accept_identical=True))
            except BaseException as error:
                self.registry.update(run_id, state='COLLECTION_FAILED', collection_error=type(error).__name__,
                                     collection_complete=False, receipt_dir=str(receipt_dir), downloads=transfers)
                raise
            values = {'downloads':transfers, 'receipt_dir':str(receipt_dir), 'collection_complete':True}
            if record['state'] == 'COLLECTION_FAILED':
                values['state'] = record.get('compute_state', record.get('result',{}).get('state','FAILED'))
            return self.registry.update(run_id, **values)

    def collect(self, run_id):
        with self.registry.controller(run_id) as owner:
            if not owner: return dict(self.registry.get(run_id), controller_active=True)
            record = self.registry.get(run_id)
            if record.get('collection_complete') and record.get('resource_released'): return record
            compute = record.get('compute_state', record.get('result',{}).get('state'))
            if compute not in TERMINAL:
                record = self.remote_status(run_id)
                compute = record.get('compute_state', record.get('result',{}).get('state'))
            if compute not in TERMINAL:
                raise RuntimeError('Computation is still running or unconfirmed; collect only after it finishes')
            record = self.finish(run_id)
            if record.get('result',{}).get('state') in TERMINAL or record.get('compute_state') in TERMINAL:
                return self.stop_environment(run_id)
            return record

    def watch(self, run_id, interval=3, cleanup=False, collect=False):
        if interval < 1: raise ValueError('Monitoring interval must be at least one second')
        record = self.registry.get(run_id); spec = record['spec']
        # Persisted local deadline prevents reconnect/recovery from extending the resource lease.
        if record.get('monitor_deadline_epoch'):
            remaining = record['monitor_deadline_epoch'] - time.time()
        else:
            remaining = spec['max_seconds'] + 60
            if collect:
                self.registry.update(run_id, monitor_deadline_epoch=time.time()+remaining)
        deadline = time.monotonic() + max(0, remaining)
        attempt = 0
        try:
            while time.monotonic() < deadline:
                try:
                    with self.ssh(record['resource_id']) as client:
                        sftp = self._open_sftp(client, min(30,max(0.1,deadline-time.monotonic())))
                        try:
                            while time.monotonic() < deadline:
                                sftp.get_channel().settimeout(min(30,max(0.1,deadline-time.monotonic())))
                                record = self.remote_status(run_id, client, sftp)
                                attempt = 0
                                if record['state'] in TERMINAL:
                                    return self.finish(run_id) if collect else record
                                time.sleep(min(interval, max(0, deadline-time.monotonic())))
                        finally:
                            sftp.close()
                except (OSError, EOFError, paramiko.SSHException) as error:
                    network_error = isinstance(error, (EOFError, ConnectionError, TimeoutError, paramiko.SSHException)) or getattr(error,'errno',None) in (
                        errno.ECONNRESET, errno.ETIMEDOUT, errno.EHOSTUNREACH, errno.ENETDOWN,
                        errno.ENETUNREACH, errno.ECONNREFUSED, errno.EPIPE)
                    if not network_error or isinstance(error, paramiko.AuthenticationException): raise
                    attempt += 1
                    self.registry.update(run_id, last_monitor_error=type(error).__name__, last_monitor_retry=utcnow())
                    delay = min(15, 2 ** min(attempt-1,4)) * random.uniform(0.8,1.2)
                    time.sleep(min(delay, max(0,deadline-time.monotonic())))
            raise TimeoutError('Monitoring deadline exceeded')
        finally:
            if cleanup and self.registry.get(run_id)['state'] != 'COLLECTION_FAILED':
                self.stop_environment(run_id)

    def run(self, spec):
        run_id = spec['run_id']
        self.registry.reserve(run_id, spec, 'run')
        with self.registry.controller(run_id) as owner:
            if not owner: return dict(self.registry.get(run_id), controller_active=True)
            record = self.registry.get(run_id)
            if record['state'] in TERMINAL:
                if record['state'] == 'COLLECTION_FAILED': return record
                if record.get('resource_id') and not record.get('resource_released'):
                    # A controller may have died after observing completion but before collecting files.
                    if (record.get('result') or record.get('remote_runner_pid')) and not record.get('collection_complete'):
                        self.finish(run_id)
                    self.stop_environment(run_id)
                return self.registry.get(run_id)
            if record['state'] == 'UNKNOWN':
                raise RuntimeError('Uncertain operation requires reconcile; never blindly repeat it')
            return self._run_owned(spec)

    def submit(self, spec):
        """Launch a background controller only when a secure reusable credential exists."""
        run_id = spec['run_id']
        self.registry.reserve(run_id, spec, 'run')
        with self.registry.operation(run_id, 'submission'):
            record = self.registry.get(run_id)
            if record['state'] in TERMINAL or record['state'] == 'UNKNOWN': return record
            if self.registry.controller_active(run_id): return dict(record, supervisor_reused=True)
            profile_path = self.profile.get('_path')
            if not profile_path:
                raise ValueError('Background submission requires a saved profile path')
            # Check before spawning so a detached process cannot wait for hidden stdin.
            get_password(self.profile, interactive=False)
            self.plan(spec)
            requests = self.registry.root / 'requests'; requests.mkdir(exist_ok=True, mode=0o700)
            request = requests / (run_id + '.json')
            if not request.exists():
                with request.open('x') as output:
                    os.chmod(request,0o600); json.dump(spec,output,ensure_ascii=False,indent=2)
            logfile = self.registry.root / (run_id + '.controller.log')
            with logfile.open('ab') as output:
                os.chmod(logfile,0o600)
                process = subprocess.Popen([sys.executable,str(ROOT/'cli.py'),'--profile',str(profile_path),
                    '--state-dir',str(self.registry.root),'--non-interactive','run','--spec',str(request),'--execute'],
                    stdin=subprocess.DEVNULL,stdout=output,stderr=output,start_new_session=True)
            self.registry.update(run_id,supervisor_pid=process.pid,supervisor_log=str(logfile))
            # A flock, not kill(pid,0), proves ownership. PID reuse is never accepted as controller identity.
            until = time.monotonic()+2
            while time.monotonic()<until:
                if self.registry.controller_active(run_id) or process.poll() is not None: break
                time.sleep(0.02)
            return self.registry.get(run_id)

    def _run_owned(self, spec):
        run_id = spec['run_id']
        self._check_cancelled(run_id)
        if spec['backend'] != 'environment': raise RuntimeError('Native GPU training backend remains disabled')
        record = self.registry.get(run_id)
        if not record.get('resource_id'):
            plan = self.plan(spec)
            if not plan['eligible_nodes_now']: raise RuntimeError('No node currently meets the requested CPU/GPU resources')
        try:
            record = self.create_environment(run_id)
            self.wait_environment(run_id)
            if not record.get('remote_runner_pid'): self.launch(run_id)
            self.watch(run_id, collect=True)
        except BaseException as error:
            current = self.registry.get(run_id)
            if current['state'] not in ('CANCELLED','UNKNOWN','COLLECTION_FAILED'):
                self.registry.update(run_id,state='TIMED_OUT' if isinstance(error,TimeoutError) else 'FAILED',error=type(error).__name__)
                # An unconfirmed/running computation must not be marked fully collected.
                if current.get('compute_state',current.get('result',{}).get('state')) in ('SUCCEEDED','FAILED','TIMED_OUT','CANCELLED'):
                    try: self.finish(run_id)
                    except Exception: pass
                elif current.get('remote_runner_pid'):
                    try:
                        snapshot = self.logs(run_id)
                        diagnostics = self.registry.root/'diagnostics'
                        diagnostics.mkdir(exist_ok=True,mode=0o700)
                        diagnostic = diagnostics/(run_id+'.json')
                        with diagnostic.open('x') as output:
                            os.chmod(diagnostic,0o600); json.dump(snapshot,output,ensure_ascii=False)
                        self.registry.update(run_id,diagnostic_log=str(diagnostic))
                    except Exception: pass
            raise
        finally:
            current = self.registry.get(run_id)
            if current.get('resource_id') and current['state'] not in ('UNKNOWN','COLLECTION_FAILED'):
                try: self.stop_environment(run_id)
                except Exception as error:
                    self.registry.update(run_id,cleanup_error=type(error).__name__,resource_released=False)
                    raise
        return self.registry.get(run_id)

    def logs(self, run_id, lines=100):
        if lines < 1: raise ValueError('lines must be positive')
        record = self.registry.get(run_id)
        local = Path(record.get('receipt_dir', self.registry.root/(run_id+'_files')))
        if (local/'stdout.log').exists():
            from collections import deque
            result = {}
            for name in ('stdout.log','stderr.log'):
                if (local/name).exists():
                    with (local/name).open(errors='replace') as handle:
                        result[name] = ''.join(deque(handle,maxlen=lines)).rstrip('\n')
            return result
        with self.ssh(record['resource_id']) as client:
            return self.execute(client,['tail','-n',str(lines),record['spec']['run_dir']+'/stdout.log',record['spec']['run_dir']+'/stderr.log'])

    def metrics(self, run_id):
        record = self.registry.get(run_id)
        with self.ssh(record["resource_id"]) as client:
            result = self.execute(client, ["nvidia-smi", "--query-gpu=name,uuid,utilization.gpu,memory.used,memory.total",
                                           "--format=csv,noheader,nounits"])
            return {"run_id": run_id, "observed_at": utcnow(), "gpu_metrics": result}

    def reconcile(self, run_id):
        record = self.registry.get(run_id)
        if not record.get("resource_id"):
            matches = [row for row in self.pages("/api/iresource/v1/work-platform/history")
                       if row.get("wpName") == "wl_" + run_id]
            if len(matches) != 1:
                raise RuntimeError("Cannot identify exactly one environment; no creation or deletion attempted")
            detail = self.environment(matches[0]["wpId"])
            if detail.get('userId') != self.user['userId']:
                raise RuntimeError('Refusing to reconcile another account environment')
            record = self.registry.update(run_id, resource_id=matches[0]["wpId"], resource_type="work-platform",
                                          state="ALLOCATING", uncertain_action=None)
        detail = self.environment(record["resource_id"])
        if detail.get('userId') != self.user['userId']:
            raise RuntimeError('Environment belongs to a different account')
        record = self.registry.update(run_id, platform_state=detail.get("wpStatus"))
        if record.get('launch_attempted') and detail.get('wpStatus') == 'Running':
            record = self.remote_status(run_id)
            if not record.get('result'):
                return self.registry.update(run_id,state='UNKNOWN',uncertain_action='launch')
            return self.registry.update(run_id,uncertain_action=None)
        return record
