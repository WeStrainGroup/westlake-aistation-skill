#!/usr/bin/env python3
"""Install a local immutable skill snapshot and atomically switch its wrappers.

This installer does not read profiles, query credential stores, contact the network,
or change existing CLI source directories. A source snapshot can be reinstalled to
roll back without rewriting a version used by an older controller.
"""
import argparse
import ast
import datetime
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import uuid


ROOT = Path(__file__).resolve().parent.parent
_TOP_FILES = {'SKILL.md', 'README.md', 'LICENSE'}
_EXAMPLE_FILES = {'examples/gpu_smoke.py', 'examples/job.example.json'}
_EXCLUDED = {'.git', '.venv', '__pycache__', 'tests', 'state', 'logs', 'runs', 'results', 'validation', 'receipts'}


class InstallationError(RuntimeError):
    pass


def _absolute(path, *, preserve_leaf=False):
    requested = Path(path).expanduser()
    if preserve_leaf:
        return requested.parent.resolve() / requested.name
    # Preserve a venv interpreter symlink: resolving it would select base Python.
    return Path(os.path.abspath(requested))


def _inside(path, parent):
    return path == parent or parent in path.parents


def _allowed(relative):
    parts = relative.parts
    if any(part in _EXCLUDED or part.startswith('.') for part in parts):
        return False
    if len(parts) == 1:
        return parts[0] in _TOP_FILES
    if parts[0] == 'agents':
        return relative.as_posix() == 'agents/openai.yaml'
    if parts[0] == 'scripts':
        return relative.suffix == '.py' or relative.as_posix() in {'scripts/wlai', 'scripts/requirements.txt'}
    if parts[0] == 'references':
        return relative.suffix in {'.md', '.txt'} or relative.name in {'profile.example.json', 'connection.example.json'}
    if parts[0] == 'examples':
        return relative.as_posix() in _EXAMPLE_FILES
    return False


def source_manifest(source):
    source = Path(source).expanduser().resolve()
    manifest = []
    candidates = []
    for directory, names, files in os.walk(source, followlinks=False):
        folder = Path(directory)
        if folder == source:
            names[:] = [name for name in names if name in {'agents', 'references', 'scripts', 'examples'}]
        else:
            names[:] = [name for name in names if name not in _EXCLUDED and not name.startswith('.')]
        for name in names:
            if (folder / name).is_symlink():
                raise InstallationError('Refusing a symlink in publishable source: ' + (folder / name).relative_to(source).as_posix())
        candidates.extend(folder / name for name in files)
    for path in sorted(candidates):
        relative = path.relative_to(source)
        if not _allowed(relative):
            continue
        if path.is_symlink():
            raise InstallationError('Refusing a symlink in publishable source: ' + relative.as_posix())
        if not path.is_file():
            continue
        data = path.read_bytes()
        manifest.append({'path': relative.as_posix(), 'sha256': hashlib.sha256(data).hexdigest(),
                         'size': len(data), 'executable': bool(path.stat().st_mode & 0o111)})
    names = {entry['path'] for entry in manifest}
    if not {'SKILL.md', 'scripts/cli.py', 'scripts/wlai'} <= names:
        raise InstallationError('Source must contain SKILL.md, scripts/cli.py and scripts/wlai')
    content_hash = hashlib.sha256(json.dumps(manifest, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
    return manifest, content_hash


def _source_version(source):
    """Read the literal argparse --version string without executing source code."""
    try:
        tree = ast.parse((source / 'scripts' / 'cli.py').read_text(encoding='utf-8'))
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and any(isinstance(arg, ast.Constant) and arg.value == '--version' for arg in node.args):
                for keyword in node.keywords:
                    if keyword.arg == 'version' and isinstance(keyword.value, ast.Constant) and isinstance(keyword.value.value, str):
                        return keyword.value.value
    except (OSError, UnicodeError, SyntaxError):
        pass
    raise InstallationError('Could not identify the literal CLI --version string')


def _revision(source, content_hash):
    marker = source / 'SOURCE.json'
    if marker.is_file():
        try:
            previous = json.loads(marker.read_text())
            if previous['content_hash'] != content_hash:
                raise InstallationError('An immutable source snapshot was modified')
            revision = previous['revision']
            if revision == 'unversioned' or re.fullmatch(r'[0-9a-f]{40,64}', revision):
                return revision
        except (KeyError, ValueError, TypeError):
            raise InstallationError('Invalid immutable source metadata') from None
    try:
        result = subprocess.run(['git', '-C', str(source), 'rev-parse', 'HEAD'],
                                capture_output=True, text=True, timeout=5, check=False)
        revision = result.stdout.strip()
        if result.returncode == 0 and re.fullmatch(r'[0-9a-f]{40,64}', revision):
            try:
                status = subprocess.run(['git', '-C', str(source), 'status', '--porcelain',
                                         '--untracked-files=normal'],
                                        capture_output=True, text=True, timeout=5, check=False)
            except (OSError, subprocess.TimeoutExpired):
                raise InstallationError('Could not verify a clean Git checkout; no installation attempted') from None
            if status.returncode != 0:
                raise InstallationError('Could not verify a clean Git checkout; no installation attempted')
            if status.stdout.strip():
                raise InstallationError('Source Git checkout has tracked or untracked changes; install a clean reviewed commit')
            return revision
    except (OSError, subprocess.TimeoutExpired):
        pass
    return 'unversioned'


def _write_json(path, values, mode=0o600):
    with Path(path).open('x', encoding='utf-8') as handle:
        os.chmod(handle.name, mode)
        json.dump(values, handle, ensure_ascii=False, indent=2)
        handle.write('\n'); handle.flush(); os.fsync(handle.fileno())


def _freeze(root):
    for path in root.rglob('*'):
        if path.is_file():
            os.chmod(path, 0o500 if path.stat().st_mode & 0o111 else 0o400)
    for path in sorted((p for p in root.rglob('*') if p.is_dir()), key=lambda p: len(p.parts), reverse=True):
        os.chmod(path, 0o500)
    os.chmod(root, 0o500)


def _verify_snapshot(target, manifest, content_hash):
    if target.is_symlink() or not target.is_dir():
        raise InstallationError('Immutable version target is not a regular directory')
    try:
        previous = json.loads((target / 'SOURCE.json').read_text())
        if previous['content_hash'] != content_hash:
            raise InstallationError('Version identifier collision or modified metadata')
        actual_files = {p.relative_to(target).as_posix() for p in target.rglob('*') if p.is_file() or p.is_symlink()}
        if actual_files != {entry['path'] for entry in manifest} | {'SOURCE.json'}:
            raise InstallationError('Immutable version contains unexpected or missing files')
        for entry in manifest:
            path = target / entry['path']
            if path.is_symlink() or hashlib.sha256(path.read_bytes()).hexdigest() != entry['sha256']:
                raise InstallationError('Immutable source content changed: ' + entry['path'])
            if path.stat().st_mode & 0o222:
                raise InstallationError('Immutable source file is writable: ' + entry['path'])
    except (OSError, KeyError, ValueError):
        raise InstallationError('Could not verify an existing immutable version') from None


def _snapshot(source, versions_dir, version_id, manifest, source_info):
    target = versions_dir / version_id
    if target.exists() or target.is_symlink():
        _verify_snapshot(target, manifest, source_info['content_hash'])
        return target
    temporary = Path(tempfile.mkdtemp(prefix='.snapshot-', dir=versions_dir))
    try:
        for entry in manifest:
            destination = temporary / entry['path']
            destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            shutil.copyfile(source / entry['path'], destination)
            os.chmod(destination, 0o700 if entry['executable'] else 0o600)
            if hashlib.sha256(destination.read_bytes()).hexdigest() != entry['sha256']:
                raise InstallationError('Source changed during installation; no activation attempted')
        _write_json(temporary / 'SOURCE.json', dict(source_info, files=manifest))
        _freeze(temporary)
        os.rename(temporary, target)
        return target
    finally:
        if temporary.exists():
            # Only our own unfinished snapshot is removed, never a version or user target.
            for path in temporary.rglob('*'):
                if not path.is_symlink():
                    os.chmod(path, 0o700 if path.is_dir() else 0o600)
            os.chmod(temporary, 0o700)
            shutil.rmtree(temporary)


def _wrapper(runtime, installed_cli, profile, state_root=None):
    lines = ['#!/bin/sh', 'set -eu', 'export PYTHONDONTWRITEBYTECODE=1']
    for name, value in (('WLAI_PROFILE', profile), ('WLAI_STATE_ROOT', state_root)):
        if value is not None:
            lines += ['if [ -z "${' + name + ':-}" ]; then',
                      '    ' + name + '=' + shlex.quote(str(value)), '    export ' + name, 'fi']
    lines.append('exec ' + shlex.quote(str(runtime)) + ' ' + shlex.quote(str(installed_cli)) + ' "$@"')
    return '\n'.join(lines) + '\n'


def _exists(path):
    return path.exists() or path.is_symlink()


def _preflight(skill_dir, versions_dir, cli_path, runtime, profile, allow_legacy_migration=False, state_root=None):
    if _inside(skill_dir, versions_dir) or _inside(versions_dir, skill_dir):
        raise InstallationError('Skill and immutable version directories must be separate')
    if not runtime.is_file() or not os.access(runtime, os.X_OK):
        raise InstallationError('Runtime Python must be an existing executable')
    if _inside(runtime, skill_dir) or profile is not None and _inside(profile, skill_dir):
        raise InstallationError('Keep the runtime and private profile outside the replaceable skill directory')
    if state_root is not None and (_inside(state_root, skill_dir) or _inside(state_root, versions_dir)):
        raise InstallationError('Keep mutable controller state outside the replaceable skill and immutable versions')
    if _exists(skill_dir) and not (skill_dir.is_dir() or skill_dir.is_symlink()):
        raise InstallationError('Existing skill target is not a directory or symlink; it was not changed')
    legacy_sources = ('cli.py', 'remote_runner.py', 'scripts/cli.py', 'scripts/remote_runner.py')
    if not allow_legacy_migration and skill_dir.is_dir() and not skill_dir.is_symlink() and any((skill_dir / name).exists() for name in legacy_sources):
        raise InstallationError('Legacy in-place source layout detected; finish its controllers, then explicitly use --allow-legacy-migration from a separate checkout')
    if cli_path is not None:
        if _inside(cli_path, skill_dir) or _inside(cli_path, versions_dir):
            raise InstallationError('Optional canonical CLI must be outside the skill and immutable version directories')
        if cli_path.is_dir():
            raise InstallationError('Canonical CLI target is a directory; it was not changed')


def _activate_entry(prepared, target):
    os.replace(prepared, target)


def install(*, source_dir=ROOT, skill_dir, versions_dir, runtime_python, cli_path=None,
            profile=None, state_root=None, dry_run=False, allow_legacy_migration=False):
    source = Path(source_dir).expanduser().resolve()
    skill_dir = _absolute(skill_dir, preserve_leaf=True)
    versions_dir = Path(versions_dir).expanduser().resolve()
    runtime = _absolute(runtime_python)
    cli_path = _absolute(cli_path, preserve_leaf=True) if cli_path is not None else None
    profile = _absolute(profile) if profile is not None else None
    state_root = _absolute(state_root) if state_root is not None else None
    _preflight(skill_dir, versions_dir, cli_path, runtime, profile, allow_legacy_migration, state_root)
    manifest, content_hash = source_manifest(source)
    source_version, revision = _source_version(source), _revision(source, content_hash)
    slug = re.sub(r'[^A-Za-z0-9._-]+', '-', source_version).strip('-_.')[:64] or 'snapshot'
    version_id = slug + '-' + revision[:12] + '-' + content_hash[:16]
    version_dir = versions_dir / version_id
    metadata = {'source_version': source_version, 'revision': revision, 'content_hash': content_hash,
                'version_dir': str(version_dir), 'installed_cli': str(version_dir / 'scripts' / 'cli.py'),
                'skill_path': str(skill_dir), 'runtime': str(runtime),
                'cli_path': str(cli_path) if cli_path else None, 'profile': str(profile) if profile else None,
                'state_root': str(state_root) if state_root else None,
                'files_copied': len(manifest), 'dry_run': bool(dry_run),
                'allow_legacy_migration': bool(allow_legacy_migration)}
    if dry_run:
        return metadata
    versions_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    skill_dir.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with (versions_dir / '.install.lock').open('a+') as lock:
        os.chmod(lock.name, 0o600)
        fcntl.flock(lock, fcntl.LOCK_EX)
        _preflight(skill_dir, versions_dir, cli_path, runtime, profile, allow_legacy_migration, state_root)
        source_info = {key: metadata[key] for key in ('source_version', 'revision', 'content_hash')}
        version_dir = _snapshot(source, versions_dir, version_id, manifest, source_info)
        backup_root = versions_dir.parent / 'backups' / versions_dir.name
        if _inside(backup_root, skill_dir):
            raise InstallationError('Backup directory must be outside skill discovery')
        backup_root.mkdir(parents=True, exist_ok=True, mode=0o700)
        if skill_dir.is_dir() and not skill_dir.is_symlink() and skill_dir.parent.stat().st_dev != backup_root.stat().st_dev:
            raise InstallationError('Legacy directory migration requires backup and skill paths on the same filesystem')
        stamp = datetime.datetime.now(datetime.timezone.utc).strftime('%Y%m%dT%H%M%S') + '-' + uuid.uuid4().hex[:10]
        backup_skill = backup_root / ('skill-' + stamp) if _exists(skill_dir) else None
        backup_cli = backup_root / ('cli-' + stamp) if cli_path is not None and _exists(cli_path) else None
        metadata.update(backup_skill=str(backup_skill) if backup_skill else None,
                        backup_cli=str(backup_cli) if backup_cli else None)
        if skill_dir.is_symlink():
            metadata['previous_skill_link'] = os.readlink(skill_dir)
        if cli_path is not None and cli_path.is_symlink():
            metadata['previous_cli_link'] = os.readlink(cli_path)
        activation_root = versions_dir / '.activations'
        activation_root.mkdir(exist_ok=True, mode=0o700)
        activation = activation_root / stamp
        activation.mkdir(mode=0o700)
        for name in ('SKILL.md', 'README.md', 'LICENSE', 'agents', 'references', 'examples'):
            if (version_dir / name).exists():
                (activation / name).symlink_to(version_dir / name, target_is_directory=(version_dir / name).is_dir())
        (activation / 'scripts').mkdir(mode=0o700)
        wrapper = _wrapper(runtime, version_dir / 'scripts' / 'cli.py', profile, state_root)
        (activation / 'scripts' / 'wlai').write_text(wrapper)
        os.chmod(activation / 'scripts' / 'wlai', 0o700)
        _write_json(activation / 'INSTALLATION.json', metadata)
        prepared_skill = skill_dir.parent / ('.wlai-link-' + uuid.uuid4().hex)
        prepared_skill.symlink_to(activation, target_is_directory=True)
        prepared_cli = None
        skill_moved = skill_activated = cli_activated = False
        try:
            if cli_path is not None:
                cli_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                with tempfile.NamedTemporaryFile('w', dir=cli_path.parent, prefix='.wlai-cli-', delete=False) as handle:
                    prepared_cli = Path(handle.name); handle.write(wrapper); handle.flush(); os.fsync(handle.fileno())
                os.chmod(prepared_cli, 0o700)
            if backup_skill is not None:
                if skill_dir.is_symlink():
                    backup_skill.symlink_to(skill_dir.resolve(), target_is_directory=True)
                else:
                    os.replace(skill_dir, backup_skill); skill_moved = True
            if backup_cli is not None:
                if cli_path.is_symlink():
                    backup_cli.symlink_to(cli_path.resolve())
                else:
                    shutil.copy2(cli_path, backup_cli)
            _activate_entry(prepared_skill, skill_dir); skill_activated = True
            if cli_path is not None:
                _activate_entry(prepared_cli, cli_path); cli_activated = True
            return metadata
        except BaseException as error:
            recovery_errors = []
            try:
                if cli_activated:
                    if cli_path.is_symlink() or cli_path.read_text() != wrapper:
                        raise InstallationError('Canonical CLI changed unexpectedly; backup retained')
                    cli_path.unlink()
                    if backup_cli is not None:
                        if backup_cli.is_symlink():
                            cli_path.symlink_to(metadata.get('previous_cli_link', os.readlink(backup_cli)))
                        else:
                            shutil.copy2(backup_cli, cli_path)
                if skill_activated:
                    if not skill_dir.is_symlink() or Path(os.readlink(skill_dir)) != activation:
                        raise InstallationError('Skill target changed unexpectedly; backup retained')
                    skill_dir.unlink()
                    if backup_skill is not None:
                        if backup_skill.is_symlink():
                            skill_dir.symlink_to(metadata.get('previous_skill_link', os.readlink(backup_skill)), target_is_directory=True)
                        else:
                            os.replace(backup_skill, skill_dir)
                elif skill_moved:
                    os.replace(backup_skill, skill_dir)
            except Exception as rollback_error:
                recovery_errors.append(type(rollback_error).__name__)
            message = 'Installation activation failed; prior entry points were restored'
            if recovery_errors:
                message = 'Installation activation failed; inspect retained backups at ' + str(backup_root)
            raise InstallationError(message + ' (' + type(error).__name__ + ')') from None
        finally:
            prepared_skill.unlink(missing_ok=True)
            if prepared_cli is not None:
                prepared_cli.unlink(missing_ok=True)


def parser():
    codex_home = Path(os.environ.get('CODEX_HOME', str(Path.home() / '.codex'))).expanduser()
    root = argparse.ArgumentParser(description=__doc__)
    root.add_argument('--source-dir', type=Path, default=ROOT, help='Reviewed source checkout, or an existing immutable snapshot for rollback')
    root.add_argument('--skill-dir', type=Path, default=codex_home / 'skills' / 'westlake-aistation')
    root.add_argument('--versions-dir', type=Path, default=codex_home / 'skill-versions' / 'westlake-aistation')
    root.add_argument('--runtime-python', type=Path, required=True)
    root.add_argument('--cli-path', type=Path)
    root.add_argument('--profile', type=Path, help='Default profile path only; this file is never read or copied')
    root.add_argument('--state-root', type=Path, help='Default WLAI_STATE_ROOT only; this directory is never read or created')
    root.add_argument('--dry-run', action='store_true')
    root.add_argument('--allow-legacy-migration', action='store_true', help='Migrate an old in-place source directory only after its controllers have ended')
    return root


def main(argv=None):
    args = parser().parse_args(argv)
    result = install(source_dir=args.source_dir, skill_dir=args.skill_dir, versions_dir=args.versions_dir,
                     runtime_python=args.runtime_python, cli_path=args.cli_path, profile=args.profile,
                     state_root=args.state_root, dry_run=args.dry_run,
                     allow_legacy_migration=args.allow_legacy_migration)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except (Exception, KeyboardInterrupt) as error:
        print(json.dumps({'error': type(error).__name__, 'message': str(error)}, ensure_ascii=False), file=sys.stderr)
        raise SystemExit(1)
