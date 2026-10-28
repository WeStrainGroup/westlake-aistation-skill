"""Stage native startup files through one short, owned CPU-only environment."""
import hashlib
import json
from pathlib import Path
import sys
import tempfile
import time

from paramiko.ssh_exception import NoValidConnectionsError
from cluster import Cluster, prepare_spec, safe_error
from native import prepare_task_spec, build_task_payload, _runner_manifest_bytes, spec_fingerprint


def stage_task(profile, spec, state_dir, interactive=True, execute=False):
    """Prepare short immutable guard paths; allocate only with explicit execute."""
    missing = [key for key in ('development_group_id', 'ssh_host_fingerprint') if not profile.get(key)]
    if missing:
        raise ValueError('task-stage needs a one-time CPU staging configuration: ' + ', '.join(missing))
    original = prepare_task_spec(spec, profile)
    runner = Path(__file__).with_name('native_runner.py')
    runner_hash = hashlib.sha256(runner.read_bytes()).hexdigest()
    staging_id = 'st' + hashlib.sha256((original['run_id'] + ':' + runner_hash).encode()).hexdigest()[:12]
    staging_profile = dict(profile, persistent_root=original['storage_mount'] + '/.wlai',
                           storage_mount=original['storage_mount'], image=original['image'])
    staging_spec = prepare_spec(dict(run_id=staging_id, argv=['/bin/true'], cpus=1, gpus=0,
                                    max_seconds=60, ready_timeout=150), staging_profile)
    staged = prepare_task_spec(dict(original, guard_script=staging_spec['run_dir'] + '/g.py',
        guard_manifest=staging_spec['run_dir'] + '/m.json', command_max_bytes=1000), profile)
    manifest = _runner_manifest_bytes(staged)
    manifest_hash = hashlib.sha256(manifest).hexdigest()
    fingerprint = spec_fingerprint(staged)
    payload = build_task_payload(staged)
    result = {'run_id': staged['run_id'], 'stage_run_id': staging_id, 'executed': False,
              'staged_spec': staged, 'command_bytes': len(payload['command'].encode('utf-8')),
              'guard_sha256': runner_hash, 'manifest_sha256': manifest_hash,
              'staged_spec_sha256': fingerprint,
              'staging_resources': {'cpus': 1, 'gpus': 0}, 'resource_released': None}
    if not execute:
        return result
    cluster = Cluster(state_dir, profile=staging_profile, interactive=interactive)
    try:
        with cluster.registry.operation(staging_id, 'submission'):
            try:
                previous = cluster.registry.get(staging_id)
            except FileNotFoundError:
                previous = None
            if previous is not None:
                matching = (previous.get('staging_guard_sha256') == runner_hash
                    and previous.get('staging_manifest_sha256') == manifest_hash
                    and previous.get('staging_spec_sha256') == fingerprint)
                if previous.get('staging_completed') and previous.get('resource_released') and matching:
                    result.update(previous.get('staging_result', {}), executed=True, reused=True, resource_released=True)
                    return result
                raise ValueError('Previous staging attempt is incomplete or differs; inspect its release and use a new science run_id')
            cluster.plan(staging_spec)
            primary = None
            cleanup_failure = None
            try:
                cluster.create_environment(staging_id)
                environment = cluster.wait_environment(staging_id)
                deadline = time.monotonic() + 60
                with tempfile.TemporaryDirectory(prefix='wlai-stage-') as directory:
                    local_manifest = Path(directory) / 'm.json'
                    local_manifest.write_bytes(manifest); local_manifest.chmod(0o600)
                    while True:
                        try:
                            with cluster.ssh(environment['resource_id']) as client:
                                cluster._storage_check(staging_id, client, True)
                                guard = cluster.put(client, runner, staged['guard_script'], expected_hash=runner_hash)
                                params = cluster.put(client, local_manifest, staged['guard_manifest'], expected_hash=manifest_hash)
                            break
                        except NoValidConnectionsError:
                            remaining = deadline - time.monotonic()
                            if remaining <= 0:
                                raise TimeoutError('Staging SSH did not become ready within 60 seconds') from None
                            time.sleep(min(2, remaining))
                if guard.get('sha256') != runner_hash or params.get('sha256') != manifest_hash:
                    raise RuntimeError('Staging did not return matching guard and manifest SHA-256 evidence')
                result.update(executed=True, guard_upload=guard, manifest_upload=params)
                cluster.registry.update(staging_id, staging_completed=True, staging_guard_sha256=runner_hash,
                    staging_manifest_sha256=manifest_hash, staging_spec_sha256=fingerprint,
                    staging_result=json.loads(json.dumps(result)))
            except BaseException as error:
                primary = error
                raise
            finally:
                try:
                    record = cluster.registry.get(staging_id)
                    if record.get('resource_id'):
                        cleanup = cluster.stop_environment(staging_id)
                        result['cleanup'] = {key: cleanup.get(key) for key in
                            ('resource_released', 'final_resource_state', 'platform_state')}
                        result['resource_released'] = bool(cleanup.get('resource_released'))
                        if not result['resource_released']:
                            raise RuntimeError('Staging environment release was not confirmed')
                except Exception as error:
                    if primary is None:
                        cleanup_failure = error
                    else:
                        primary.cleanup_error = safe_error(error)
                if cleanup_failure is not None:
                    raise cleanup_failure
            cluster.registry.update(staging_id, staging_result=json.loads(json.dumps(result)))
            return result
    finally:
        active_error = sys.exc_info()[1]
        try:
            cluster.close()
        except Exception as error:
            if active_error is None:
                raise
            active_error.close_error = safe_error(error)
