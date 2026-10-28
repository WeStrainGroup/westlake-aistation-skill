#!/usr/bin/env python3
"""Portable JSON CLI for bounded AIStation development-environment jobs."""
import argparse
import getpass
import json
import os
from pathlib import Path
import sys
import tempfile


def parser():
    root = argparse.ArgumentParser(description=__doc__)
    root.add_argument('--version', action='version', version='westlake-aistation 0.1.0-preview.1')
    root.add_argument('--profile', type=Path, help='Private, nonsecret connection profile')
    root.add_argument('--state-dir', type=Path, help='Override the account-scoped run registry')
    root.add_argument('--non-interactive', action='store_true', help='Never prompt for a password')
    commands = root.add_subparsers(dest='command', required=True)
    init = commands.add_parser('init', help='Create a private connection profile; no password is saved')
    init.add_argument('--from-file', type=Path, help='Validate and copy a completed profile template')
    login = commands.add_parser('login', help='Verify credentials using hidden password input')
    login.add_argument('--remember', action='store_true', help='Save in the system credential store after login succeeds')
    doctor = commands.add_parser('doctor', help='Validate local setup without allocating resources')
    doctor.add_argument('--online', action='store_true', help='Also authenticate and query live resources')
    resources = commands.add_parser('resources', help='Query live resources')
    resources.add_argument('--group', choices=('development', 'training', 'all'), default='development')
    commands.add_parser('runs', help='Read local run records without authentication')
    for name in ('plan', 'run', 'submit'):
        command = commands.add_parser(name)
        command.add_argument('--spec', type=Path, required=True)
        if name != 'plan':
            command.add_argument('--execute', action='store_true')
    create = commands.add_parser('env-create', help='Allocate a manual debugging environment')
    create.add_argument('--run-id', required=True)
    create.add_argument('--cpus', type=int, default=12)
    create.add_argument('--gpus', type=int, default=1)
    create.add_argument('--ready-timeout', type=int, default=150)
    create.add_argument('--execute', action='store_true')
    for name in ('status', 'watch', 'stop', 'reconcile', 'collect', 'logs', 'metrics', 'exec', 'put', 'get'):
        command = commands.add_parser(name)
        command.add_argument('--run-id', required=True)
        if name == 'status':
            command.add_argument('--refresh', action='store_true', help='Also query platform state')
        if name == 'logs':
            command.add_argument('--lines', type=int, default=100)
        if name == 'watch':
            command.add_argument('--interval', type=float, default=3)
        if name == 'exec':
            command.add_argument('--timeout', type=int, default=60)
            command.add_argument('argv', nargs=argparse.REMAINDER)
        if name in ('put', 'get'):
            command.add_argument('--local', type=Path, required=True)
            command.add_argument('--remote', required=True)
    for name in ('native-status', 'native-logs'):
        command = commands.add_parser(name)
        command.add_argument('--id', required=True)
    return root


def initialize(args):
    from config import default_profile_path, validate_profile
    target = (args.profile or default_profile_path()).expanduser().resolve()
    if target.exists():
        raise FileExistsError('Profile already exists; edit it locally instead of overwriting it')
    if args.from_file:
        source = args.from_file.expanduser().resolve()
        profile = validate_profile(json.loads(source.read_text()), base_dir=source.parent)
    else:
        if args.non_interactive or not sys.stdin.isatty():
            raise RuntimeError('Interactive setup needs a terminal; use init --from-file for nonsecret configuration')
        prompts = [
            ('portal', 'AIStation HTTPS portal URL'),
            ('account', 'Your AIStation account'),
            ('ca_file', 'Trusted CA/server certificate PEM path'),
            ('certificate_sha256', 'Verified HTTPS certificate SHA-256 (64 hex characters)'),
            ('ssh_host_fingerprint', 'Verified container SSH host fingerprint (SHA256:...)'),
            ('development_group_id', 'Development resource group ID'),
            ('image', 'Container image'),
            ('card_type', 'GPU model name reported by the platform'),
            ('persistent_root', 'Your persistent run root (absolute remote path)'),
        ]
        profile = validate_profile({key: input(label + ': ').strip() for key, label in prompts}, base_dir=Path.cwd())
    profile = {key: value for key, value in profile.items() if not key.startswith('_')}
    target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile('w', dir=target.parent, delete=False) as handle:
            temporary = Path(handle.name)
            json.dump(profile, handle, indent=2)
            handle.write('\n'); handle.flush(); os.fsync(handle.fileno())
        temporary.chmod(0o600)
        os.link(temporary, target)
    finally:
        if temporary:
            temporary.unlink(missing_ok=True)
    return {'configured': True, 'profile': str(target), 'password_saved': False}


def main(argv=None):
    args = parser().parse_args(argv)
    if args.command == 'init':
        result = initialize(args)
    else:
        from config import load_profile, state_directory
        profile = load_profile(args.profile)
        if args.command == 'runs':
            from registry import Registry, profile_identity
            result = Registry(args.state_dir or state_directory(profile), profile_identity(profile)).list()
        elif args.command == 'login':
            from api import AIStation
            from credentials import get_password, store_password
            if args.non_interactive:
                password = get_password(profile, interactive=False)
            else:
                if not sys.stdin.isatty():
                    raise RuntimeError('Login needs a terminal for hidden password input')
                password = getpass.getpass('AIStation password (hidden): ')
                if not password:
                    raise ValueError('Password must not be empty')
            client = AIStation(profile)
            try:
                client.login(profile['account'], password)
                if args.remember:
                    store_password(profile, password)
            finally:
                client.close()
                del password
            result = {'authenticated': True, 'remembered': args.remember, 'token_saved': False}
        elif args.command == 'doctor' and not args.online:
            import importlib.util
            missing = [name for name in ('gmssl', 'paramiko', 'Cryptodome') if importlib.util.find_spec(name) is None]
            result = {'profile_valid': True, 'missing_dependencies': missing,
                      'python': sys.version.split()[0], 'state_dir': str(state_directory(profile)),
                      'network_accessed': False, 'resources_allocated': False}
        else:
            from cluster import Cluster, TERMINAL, prepare_spec
            cluster = Cluster(args.state_dir, profile=profile, interactive=not args.non_interactive)
            if args.command in ('resources', 'doctor'):
                group = getattr(args, 'group', 'development')
                group_id = None if group == 'all' else profile.get(group + '_group_id')
                if group != 'all' and not group_id:
                    raise ValueError('This resource group has not been configured')
                result = cluster.resources(group_id)
            elif args.command in ('plan', 'run', 'submit'):
                spec = prepare_spec(json.loads(args.spec.read_text()), profile)
                if args.command == 'plan' or not args.execute:
                    result = cluster.plan(spec)
                elif args.command == 'run':
                    result = cluster.run(spec)
                else:
                    from credentials import password_available
                    if not password_available(profile):
                        raise RuntimeError('Background submit requires a system credential store; run login --remember, or use foreground run')
                    result = cluster.submit(spec)
            elif args.command == 'env-create':
                spec = prepare_spec({'run_id': args.run_id, 'argv': ['/bin/true'],
                                     'cpus': args.cpus, 'gpus': args.gpus,
                                     'ready_timeout': args.ready_timeout}, profile)
                result = cluster.plan(spec)
                if args.execute:
                    try:
                        cluster.create_environment(args.run_id)
                        result = cluster.wait_environment(args.run_id)
                    except BaseException:
                        if cluster.registry.get(args.run_id).get('resource_id'):
                            cluster.stop_environment(args.run_id)
                        raise
            elif args.command == 'status':
                result = cluster.registry.get(args.run_id)
                if result.get('resource_id') and (args.refresh or not result.get('resource_released')):
                    detail = cluster.environment(result['resource_id'])
                    platform = {key: detail.get(key) for key in ('wpId', 'wpStatus', 'cpu', 'acceleratorCard', 'acceleratorCardKind')}
                    if detail.get('wpStatus') == 'Running' and result.get('remote_runner_pid'):
                        result = cluster.remote_status(args.run_id)
                    result = dict(result, platform=platform)
            elif args.command == 'watch':
                if args.interval < 1:
                    raise ValueError('Polling interval must be at least one second')
                record = cluster.registry.get(args.run_id)
                result = record if record['state'] in TERMINAL else cluster.watch(args.run_id, args.interval)
            elif args.command == 'stop':
                result = cluster.cancel(args.run_id)
            elif args.command == 'reconcile':
                result = cluster.reconcile(args.run_id)
            elif args.command == 'collect':
                result = cluster.collect(args.run_id)
            elif args.command == 'logs':
                if args.lines < 1:
                    raise ValueError('Log line count must be positive')
                result = cluster.logs(args.run_id, args.lines)
            elif args.command == 'metrics':
                result = cluster.metrics(args.run_id)
            elif args.command in ('exec', 'put', 'get'):
                record = cluster.registry.get(args.run_id)
                with cluster.ssh(record['resource_id']) as client:
                    if args.command == 'exec':
                        command = args.argv[1:] if args.argv and args.argv[0] == '--' else args.argv
                        if not command or not 1 <= args.timeout <= 13800:
                            raise ValueError('Supply a command and a timeout from 1 to 13800 seconds')
                        result = cluster.execute(client, command, args.timeout)
                    elif args.command == 'put':
                        result = cluster.put(client, args.local.resolve(), args.remote)
                    else:
                        result = cluster.get(client, args.remote, args.local.resolve())
            elif args.command == 'native-status':
                from api import rows
                found = [row for flag in (0, 3) for row in rows(cluster.client.task(args.id, flag)) if row.get('id') == args.id]
                if not found:
                    raise ValueError('Task ID not found')
                result = {key: found[0].get(key) for key in ('id', 'name', 'status', 'config', 'acceleratorCardKind', 'acceleratorCardType', 'runTime')}
            elif args.command == 'native-logs':
                result = cluster.client.task_log(args.id)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    if isinstance(result, dict) and result.get('state') in ('FAILED', 'TIMED_OUT', 'BLOCKED', 'COLLECTION_FAILED', 'UNKNOWN'):
        return 2
    if isinstance(result, dict) and result.get('missing_dependencies'):
        return 1
    return 0


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except (Exception, KeyboardInterrupt) as error:
        print(json.dumps({'error': type(error).__name__, 'message': str(error)}, ensure_ascii=False), file=sys.stderr)
        raise SystemExit(1)
