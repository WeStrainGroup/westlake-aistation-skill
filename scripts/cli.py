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
    root.add_argument('--version', action='version', version='westlake-aistation 0.2.0-preview.2')
    root.add_argument('--profile', type=Path, help='Private, nonsecret connection profile')
    root.add_argument('--state-dir', type=Path, help='Override the account-scoped run registry')
    root.add_argument('--non-interactive', action='store_true', help='Never prompt for a password')
    commands = root.add_subparsers(dest='command', required=True)
    init = commands.add_parser('init', help='Create a private connection profile; no password is saved')
    init.add_argument('--from-file', type=Path, help='Validate and copy a completed profile template')
    init.add_argument('--connection-only', action='store_true', help='Set up trust/account before choosing compute resources')
    init.add_argument('--portal', help='Your HTTPS portal origin')
    init.add_argument('--account', help='Your own platform account')
    init.add_argument('--server-cert', type=Path, help='One independently trusted server PEM; derives its SHA-256 pin locally')
    init.add_argument('--storage', choices=('master', 'ssdwork'), default='master')
    configure = commands.add_parser('configure', help='Complete or update your nonsecret execution settings')
    configure.add_argument('--group-id', dest='development_group_id')
    configure.add_argument('--image')
    configure.add_argument('--gpu-model', dest='card_type')
    configure.add_argument('--ssh-fingerprint', dest='ssh_host_fingerprint')
    configure.add_argument('--remote-python')
    configure.add_argument('--storage', choices=('master', 'ssdwork', 'custom'))
    configure.add_argument('--storage-mount')
    configure.add_argument('--persistent-root')
    configure.add_argument('--training-group-id')
    login = commands.add_parser('login', help='Verify credentials using hidden password input')
    login.add_argument('--remember', action='store_true', help='Save in the system credential store after login succeeds')
    doctor = commands.add_parser('doctor', help='Validate local setup without allocating resources')
    doctor.add_argument('--online', action='store_true', help='Also authenticate and query live resources')
    resources = commands.add_parser('resources', help='Query live resources')
    resources.add_argument('--group', choices=('development', 'training', 'all'), default='all')
    resources.add_argument('--format', choices=('json', 'table'), default='json')
    job = commands.add_parser('job', help='Generate a reviewable job JSON without allocating resources')
    script = job.add_mutually_exclusive_group(required=True)
    script.add_argument('--script', type=Path)
    script.add_argument('--remote-script')
    job.add_argument('--run-id')
    job.add_argument('--cpus', type=int, default=12)
    job.add_argument('--gpus', type=int, default=1)
    job.add_argument('--minutes', type=float, default=10)
    job.add_argument('--purpose', choices=('development', 'training'), default='development')
    job.add_argument('--download', nargs=2, action='append', default=[], metavar=('REMOTE', 'LOCAL'))
    job.add_argument('--out', type=Path, required=True)
    job.add_argument('argv', nargs=argparse.REMAINDER)
    guide = commands.add_parser('task-guide', help='Export a manual task-management handoff, never submit an unverified native API request')
    guide.add_argument('--spec', type=Path, required=True)
    guide.add_argument('--out', type=Path, required=True)
    commands.add_parser('runs', help='Read local run records without authentication')
    for name in ('plan', 'run', 'submit'):
        command = commands.add_parser(name)
        command.add_argument('--spec', type=Path, required=True)
        if name == 'plan':
            command.add_argument('--online', action='store_true', help='Also authenticate and check current resource availability')
        if name != 'plan':
            command.add_argument('--execute', action='store_true')
    create = commands.add_parser('env-create', help='Allocate a manual debugging environment')
    create.add_argument('--run-id', required=True)
    create.add_argument('--cpus', type=int, default=12)
    create.add_argument('--gpus', type=int, default=1)
    create.add_argument('--ready-timeout', type=int, default=150)
    create.add_argument('--execute', action='store_true')
    for name in ('status', 'watch', 'stop', 'reconcile', 'collect', 'logs', 'metrics', 'exec', 'put', 'get', 'storage-check'):
        command = commands.add_parser(name)
        command.add_argument('--run-id', required=True)
        if name == 'storage-check':
            command.add_argument('--read-only', action='store_true', help='Inspect the existing mount without the tiny write/read/remove probe')
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
    from config import certificate_pin, default_profile_path, execution_requirements, validate_profile, write_profile
    requested = (args.profile or default_profile_path()).expanduser()
    target = requested.parent.resolve() / requested.name
    if target.exists() or target.is_symlink():
        raise FileExistsError('Profile already exists; edit it locally instead of overwriting it')
    if args.from_file:
        source = args.from_file.expanduser().resolve()
        profile = validate_profile(json.loads(source.read_text()), base_dir=source.parent,
                                   require_execution=not args.connection_only)
    else:
        interactive = not args.non_interactive and sys.stdin.isatty()
        if not interactive and not (args.portal and args.account and args.server_cert and args.connection_only):
            raise RuntimeError('Interactive setup needs a terminal; use init --from-file for nonsecret configuration')
        values = {'portal': args.portal or input('AIStation HTTPS portal URL: ').strip(),
                  'account': args.account or input('Your AIStation account: ').strip()}
        server_cert = args.server_cert or Path(input('Independently trusted server certificate PEM path: ').strip())
        values.update(ca_file=str(Path(server_cert).expanduser().resolve()),
                      certificate_sha256=certificate_pin(server_cert), storage=args.storage)
        if not args.connection_only:
            values.update(_execution_prompts(values))
        profile = validate_profile(values, base_dir=Path.cwd(), require_execution=not args.connection_only)
    write_profile(target, profile)
    return {'configured': True, 'profile': str(target), 'password_saved': False,
            'execution_ready': not bool(execution_requirements(profile)),
            'next': 'login --remember; resources --group all --format table; configure'}


def _execution_prompts(current):
    fields = [('development_group_id', 'Development resource group ID (see resources --group all)'),
              ('image', 'Container image'), ('card_type', 'GPU model (blank for CPU-only)'),
              ('ssh_host_fingerprint', 'Independently verified container SSH fingerprint (SHA256:...)')]
    result = {}
    for key, label in fields:
        default = current.get(key, '')
        answer = input(label + (f' [{default}]' if default else '') + ': ').strip()
        if answer or default:
            result[key] = answer or default
    storage = current.get('storage', 'master')
    print('Storage: master is capacity storage; ssdwork uses billed SSD quota. No data is moved automatically.')
    result['storage'] = input(f'Storage master/ssdwork/custom [{storage}]: ').strip() or storage
    if result['storage'] == 'custom':
        result['storage_mount'] = input('Independently verified custom mount point: ').strip()
        result['persistent_root'] = input('Run root below that mount point: ').strip()
    return result


def configure_profile(args):
    from config import load_profile, validate_profile, write_profile
    previous = load_profile(args.profile, require_execution=False)
    target = Path(previous['_path'])
    values = {key: value for key, value in previous.items() if not key.startswith('_')}
    fields = ('development_group_id', 'image', 'card_type', 'ssh_host_fingerprint', 'remote_python',
              'storage', 'storage_mount', 'persistent_root', 'training_group_id')
    changes = {key: getattr(args, key) for key in fields if getattr(args, key, None) is not None}
    if not changes:
        if args.non_interactive or not sys.stdin.isatty():
            raise RuntimeError('Supply configure flags, or use an interactive terminal')
        changes = _execution_prompts(values)
    if 'storage' in changes and changes['storage'] != values.get('storage'):
        values.pop('storage_mount', None); values.pop('persistent_root', None)
    values.update(changes)
    profile = validate_profile(values, target.parent, require_execution=True)
    write_profile(target, profile, replace=True)
    return {'configured': True, 'execution_ready': True, 'profile': str(target), 'storage': profile['storage'],
            'persistent_root': profile['persistent_root'], 'data_moved': False, 'password_saved': False}


def resource_table(nodes):
    if not nodes:
        return 'No visible resources. Check account/resource-group permissions.'
    lines = []
    previous = None
    for node in sorted(nodes, key=lambda row: (str(row.get('group_id')), str(row.get('node')))):
        identity = node.get('group_id')
        if identity != previous:
            lines.append(f"{node.get('group', '')} | group_id={identity}")
            lines.append('  NODE                 FREE CPU  FREE GPU  MODEL')
            previous = identity
        lines.append(f"  {str(node.get('node', '')):<20} {str(node.get('available_cpus', '?')):>8} {str(node.get('available_gpus', '?')):>9}  {node.get('card_type', '')}")
    return '\n'.join(lines)


def main(argv=None):
    args = parser().parse_args(argv)
    if args.command == 'init':
        result = initialize(args)
    elif args.command == 'configure':
        result = configure_profile(args)
    else:
        from config import execution_requirements, load_profile, state_directory
        execution_commands = {'plan', 'run', 'submit', 'env-create', 'exec', 'put', 'get', 'metrics', 'storage-check'}
        profile = load_profile(args.profile, require_execution=args.command in execution_commands)
        if args.command == 'job':
            from jobs import create_job, write_json_exclusive
            command_args = args.argv[1:] if args.argv and args.argv[0] == '--' else args.argv
            spec = create_job(profile, script=args.script, remote_script=args.remote_script, run_id=args.run_id,
                              cpus=args.cpus, gpus=args.gpus, minutes=args.minutes, downloads=args.download,
                              argv=command_args, purpose=args.purpose)
            write_json_exclusive(args.out, spec)
            result = {'written': str(args.out.resolve()), 'run_id': spec['run_id'], 'purpose': args.purpose,
                      'submitted': False, 'next': 'task-guide' if args.purpose == 'training' else 'plan'}
        elif args.command == 'task-guide':
            from jobs import render_task_guide
            text = render_task_guide(json.loads(args.spec.read_text()), profile)
            requested = args.out.expanduser()
            target = requested.parent.resolve() / requested.name
            target.parent.mkdir(parents=True, exist_ok=True)
            temporary = None
            try:
                with tempfile.NamedTemporaryFile('w', dir=target.parent, delete=False) as handle:
                    temporary = Path(handle.name)
                    os.chmod(temporary, 0o600)
                    handle.write(text); handle.flush(); os.fsync(handle.fileno())
                os.link(temporary, target)
            finally:
                if temporary:
                    temporary.unlink(missing_ok=True)
            result = {'written': str(target), 'submitted': False, 'native_api_enabled': False}
        elif args.command == 'runs':
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
        elif args.command == 'doctor':
            import importlib.util
            missing = [name for name in ('gmssl', 'paramiko', 'Cryptodome') if importlib.util.find_spec(name) is None]
            result = {'profile_valid': True, 'missing_dependencies': missing,
                      'python': sys.version.split()[0], 'state_dir': str(state_directory(profile)),
                      'execution_ready': not bool(execution_requirements(profile)),
                      'missing_execution_fields': execution_requirements(profile),
                      'storage': {'kind': profile.get('storage'), 'mount': profile.get('storage_mount'),
                                  'root': profile.get('persistent_root'), 'mount_checked': False, 'quota_checked': False},
                      'network_accessed': False, 'resources_allocated': False}
            if args.online:
                if missing:
                    raise RuntimeError('Install missing dependencies before online diagnostics')
                from cluster import Cluster
                cluster = Cluster(args.state_dir, profile=profile, interactive=not args.non_interactive)
                try:
                    result['resources'] = cluster.resources(None)
                    result['network_accessed'] = True
                finally:
                    cluster.close()
        else:
            from cluster import Cluster, TERMINAL, prepare_spec
            if args.command in ('plan', 'run', 'submit'):
                spec = prepare_spec(json.loads(args.spec.read_text()), profile)
            cluster = Cluster(args.state_dir, profile=profile, interactive=not args.non_interactive)
            if args.command == 'resources':
                group = getattr(args, 'group', 'development')
                group_id = None if group == 'all' else profile.get(group + '_group_id')
                if group != 'all' and not group_id:
                    raise ValueError('This resource group has not been configured')
                result = cluster.resources(group_id)
            elif args.command in ('plan', 'run', 'submit'):
                if args.command == 'plan' or not args.execute:
                    result = cluster.plan(spec, online=getattr(args, 'online', False))
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
            elif args.command == 'storage-check':
                result = cluster.storage_check(args.run_id, write_probe=not args.read_only)
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
    if args.command == 'resources' and args.format == 'table':
        print(resource_table(result))
    else:
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
