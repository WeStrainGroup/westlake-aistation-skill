"""Local installation transactions in temporary directories; no credentials/network."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

SCRIPTS = Path(__file__).resolve().parents[1] / 'scripts'
sys.path.insert(0, str(SCRIPTS))
import install_local


class InstallTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='wlai install test ')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.source = self.root / 'source checkout'
        self.skill = self.root / 'codex' / 'skills' / 'westlake-aistation'
        self.versions = self.root / 'codex' / 'skill-versions' / 'westlake-aistation'
        self.canonical = self.root / 'old tool directory' / 'wlai'
        self.runtime = self.root / 'python runtime with spaces'
        self.runtime.symlink_to(sys.executable)
        self.make_source('1.0')

    def make_source(self, version):
        for name in ('scripts', 'agents', 'references', 'examples'):
            (self.source / name).mkdir(parents=True, exist_ok=True)
        (self.source / 'SKILL.md').write_text('---\nname: westlake-aistation\ndescription: Test fixture\n---\n')
        (self.source / 'README.md').write_text('Public instructions only.\n')
        (self.source / 'LICENSE').write_text('Test license.\n')
        (self.source / 'agents' / 'openai.yaml').write_text('interface:\n  display_name: "Test"\n')
        (self.source / 'references' / 'usage.md').write_text('Use your own configuration.\n')
        (self.source / 'references' / 'profile.example.json').write_text('{"account":"YOUR_ACCOUNT"}\n')
        (self.source / 'examples' / 'gpu_smoke.py').write_text('print("public GPU smoke example")\n')
        (self.source / 'examples' / 'job.example.json').write_text('{"name":"public-example"}\n')
        cli = f'''import argparse,json,os,sys,time
from pathlib import Path
p=argparse.ArgumentParser()
p.add_argument('--version',action='version',version={'fixture skill '+version!r})
args,other=p.parse_known_args()
if other and other[0]=='controller':
    ready,proceed=Path(other[1]),Path(other[2]);ready.write_text('ready')
    deadline=time.monotonic()+5
    while not proceed.exists() and time.monotonic()<deadline:time.sleep(0.01)
    print((Path(__file__).resolve().parent/'remote_runner.py').read_text(),flush=True)
else:
    print(json.dumps({{'argv':other,'profile':os.environ.get('WLAI_PROFILE'),'state_root':os.environ.get('WLAI_STATE_ROOT'),'file':__file__}}))
'''
        (self.source / 'scripts' / 'cli.py').write_text(cli)
        (self.source / 'scripts' / 'remote_runner.py').write_text('runner for ' + version + '\n')
        (self.source / 'scripts' / 'requirements.txt').write_text('# fixture dependencies\n')
        (self.source / 'scripts' / 'wlai').write_text('#!/bin/sh\nexit 0\n')
        (self.source / 'scripts' / 'wlai').chmod(0o700)

    def install(self, **kwargs):
        return install_local.install(source_dir=kwargs.pop('source_dir', self.source),
            skill_dir=self.skill, versions_dir=self.versions, runtime_python=self.runtime,
            cli_path=self.canonical, **kwargs)

    def commit_git_source(self):
        command = ['git', '-C', str(self.source), '-c', 'core.hooksPath=/dev/null']
        subprocess.run([*command, 'init', '-q'], check=True, capture_output=True, text=True)
        subprocess.run([*command, 'add', '.'], check=True, capture_output=True, text=True)
        subprocess.run([*command, '-c', 'user.name=Offline Test', '-c', 'user.email=offline@example.invalid',
                        '-c', 'commit.gpgsign=false', 'commit', '-qm', 'Reviewed fixture'],
                       check=True, capture_output=True, text=True)
        return subprocess.check_output([*command, 'rev-parse', 'HEAD'], text=True).strip()

    @unittest.skipUnless(shutil.which('git'), 'Git is needed for checkout validation')
    def test_clean_committed_git_checkout_installs_and_snapshot_rolls_back_when_checkout_is_dirty(self):
        revision = self.commit_git_source()
        first = self.install()
        self.assertEqual(first['revision'], revision)
        (self.source / 'README.md').write_text('Uncommitted source changes.\n')
        rollback = self.install(source_dir=first['version_dir'])
        self.assertEqual(rollback['revision'], revision)
        self.assertEqual(rollback['version_dir'], first['version_dir'])
        self.assertEqual(rollback['content_hash'], first['content_hash'])

    @unittest.skipUnless(shutil.which('git'), 'Git is needed for checkout validation')
    def test_git_checkout_with_tracked_changes_is_rejected_before_installation(self):
        self.commit_git_source()
        (self.source / 'README.md').write_text('Uncommitted source changes.\n')
        with self.assertRaisesRegex(install_local.InstallationError, 'tracked or untracked changes'):
            self.install()
        self.assertFalse(self.skill.exists())
        self.assertFalse(self.versions.exists())
        self.assertFalse(self.canonical.exists())

    @unittest.skipUnless(shutil.which('git'), 'Git is needed for checkout validation')
    def test_git_checkout_with_untracked_files_is_rejected_before_installation(self):
        self.commit_git_source()
        (self.source / 'local-note.txt').write_text('Untracked source file.\n')
        with self.assertRaisesRegex(install_local.InstallationError, 'tracked or untracked changes'):
            self.install()
        self.assertFalse(self.skill.exists())
        self.assertFalse(self.versions.exists())
        self.assertFalse(self.canonical.exists())

    def test_source_without_git_history_stays_unversioned(self):
        self.assertEqual(self.install()['revision'], 'unversioned')

    def test_initial_install_uses_private_metadata_and_executable_wrappers(self):
        profile = self.root / 'private settings' / 'profile.json'
        state_root = self.root / 'private state with spaces'
        metadata = self.install(profile=profile, state_root=state_root)
        self.assertTrue(self.skill.is_symlink())
        stored = json.loads((self.skill / 'INSTALLATION.json').read_text())
        for key in ('source_version', 'revision', 'content_hash', 'installed_cli', 'skill_path', 'runtime'):
            self.assertEqual(stored[key], metadata[key])
        self.assertEqual((self.skill / 'INSTALLATION.json').stat().st_mode & 0o777, 0o600)
        self.assertFalse(profile.exists(), 'Installer read/created a private profile')
        self.assertFalse(state_root.exists(), 'Installer read/created controller state')
        self.assertEqual(stored['state_root'], str(state_root))
        self.assertEqual(metadata['runtime'], str(self.runtime), 'venv/runtime symlink spelling was lost')
        result = subprocess.run([str(self.skill / 'scripts' / 'wlai'), '--version'], capture_output=True, text=True, timeout=5)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), metadata['source_version'])
        env = dict(os.environ); env.pop('WLAI_PROFILE', None); env.pop('WLAI_STATE_ROOT', None)
        run = subprocess.run([str(self.canonical), 'hello world'], capture_output=True, text=True, env=env, timeout=5)
        observed = json.loads(run.stdout)
        self.assertEqual(observed['argv'], ['hello world'])
        self.assertEqual(observed['profile'], str(profile))
        self.assertEqual(observed['state_root'], str(state_root))
        self.assertEqual(observed['file'], metadata['installed_cli'])
        self.assertEqual((Path(metadata['version_dir']) / 'scripts' / 'cli.py').stat().st_mode & 0o222, 0)

    def test_existing_environment_profile_overrides_wrapper_default(self):
        self.install(profile=self.root / 'default profile.json', state_root=self.root / 'default state')
        env = dict(os.environ, WLAI_PROFILE='independent profile with spaces.json', WLAI_STATE_ROOT='independent state')
        result = subprocess.run([str(self.canonical)], capture_output=True, text=True, env=env, timeout=5)
        self.assertEqual(json.loads(result.stdout)['profile'], env['WLAI_PROFILE'])
        self.assertEqual(json.loads(result.stdout)['state_root'], env['WLAI_STATE_ROOT'])

    def test_secret_files_state_and_tests_are_not_copied(self):
        private_files = ['profile.json', 'server.pem', '.env', 'ADMIN_ISSUES.md',
                         'scripts/profile.json', 'scripts/.venv/secret.py',
                         'scripts/__pycache__/secret.py', 'references/profile.json',
                         'references/server.pem', 'agents/personal.yaml',
                         'examples/private.json', 'examples/personal.py', 'examples/data/input.txt',
                         'tests/test_secret.py', 'state/run.json', 'runs/result.json']
        for name in private_files:
            path = self.source / name; path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text('synthetic-private-file-value')
        result = self.install()
        version = Path(result['version_dir'])
        contents = '\n'.join(path.read_text() for path in version.rglob('*') if path.is_file())
        self.assertNotIn('synthetic-private-file-value', contents)
        self.assertFalse((version / '.git').exists())
        self.assertTrue((version / 'references' / 'profile.example.json').exists())

    def test_only_named_public_examples_are_installed_and_available_through_skill(self):
        result = self.install()
        version = Path(result['version_dir'])
        for relative in ('examples/gpu_smoke.py', 'examples/job.example.json'):
            self.assertEqual((version / relative).read_bytes(), (self.source / relative).read_bytes())
            self.assertEqual((self.skill / relative).read_bytes(), (self.source / relative).read_bytes())
        self.assertTrue((self.skill / 'examples').is_symlink())
        self.assertEqual({p.name for p in (version / 'examples').iterdir()}, {'gpu_smoke.py', 'job.example.json'})

    def test_legacy_wrapper_skill_and_canonical_file_are_preserved_in_backups(self):
        (self.skill / 'scripts').mkdir(parents=True)
        (self.skill / 'scripts' / 'wlai').write_text('old wrapper')
        (self.skill / 'personal-note.txt').write_text('keep user note')
        (self.skill / 'profile.json').write_text('synthetic old private profile')
        self.canonical.parent.mkdir(parents=True)
        self.canonical.write_text('old canonical wrapper'); self.canonical.chmod(0o700)
        sibling = self.canonical.parent / 'private-config.json'; sibling.write_text('keep old config')
        result = self.install()
        backup = Path(result['backup_skill'])
        self.assertNotIn(self.skill.parent, backup.parents)
        self.assertEqual((backup / 'personal-note.txt').read_text(), 'keep user note')
        self.assertEqual((backup / 'profile.json').read_text(), 'synthetic old private profile')
        self.assertEqual(Path(result['backup_cli']).read_text(), 'old canonical wrapper')
        self.assertEqual(sibling.read_text(), 'keep old config')
        self.assertFalse((Path(result['version_dir']) / 'profile.json').exists())

    def test_upgrade_keeps_old_controller_root_immutable_and_supports_rollback(self):
        first = self.install()
        old_cli, old_runner = Path(first['installed_cli']), Path(first['version_dir']) / 'scripts' / 'remote_runner.py'
        old_bytes = old_cli.read_bytes()
        ready, proceed = self.root / 'controller-ready', self.root / 'controller-proceed'
        child = subprocess.Popen([str(self.runtime), str(old_cli), 'controller', str(ready), str(proceed)],
                                 stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        try:
            deadline = time.monotonic() + 3
            while not ready.exists() and time.monotonic() < deadline: time.sleep(0.01)
            self.assertTrue(ready.exists())
            (self.skill / 'personal-note.txt').write_text('keep unknown activation file')
            self.make_source('2.0')
            second = self.install()
            self.assertNotEqual(first['version_dir'], second['version_dir'])
            self.assertEqual(old_cli.read_bytes(), old_bytes)
            self.assertEqual(old_runner.read_text(), 'runner for 1.0\n')
            self.assertEqual((Path(second['backup_skill']) / 'personal-note.txt').read_text(), 'keep unknown activation file')
            proceed.touch()
            out, err = child.communicate(timeout=3)
            self.assertEqual(child.returncode, 0, err)
            self.assertEqual(out.strip(), 'runner for 1.0')
            rollback = self.install(source_dir=first['version_dir'])
            self.assertEqual(rollback['version_dir'], first['version_dir'])
            self.assertEqual(rollback['content_hash'], first['content_hash'])
            self.assertIn('fixture skill 1.0', subprocess.check_output([str(self.canonical), '--version'], text=True))
        finally:
            if child.poll() is None: child.kill(); child.communicate()

    def test_activation_failure_restores_previous_routes(self):
        first = self.install()
        previous_skill_target = os.readlink(self.skill)
        previous_cli = self.canonical.read_bytes()
        self.make_source('2.0')
        activate = install_local._activate_entry
        def fail_canonical(prepared, target):
            if target == self.canonical:
                raise OSError('simulated canonical activation failure')
            return activate(prepared, target)
        with patch('install_local._activate_entry', side_effect=fail_canonical):
            with self.assertRaises(install_local.InstallationError): self.install()
        self.assertEqual(os.readlink(self.skill), previous_skill_target)
        self.assertEqual(self.canonical.read_bytes(), previous_cli)
        self.assertEqual(json.loads((self.skill / 'INSTALLATION.json').read_text())['content_hash'], first['content_hash'])

    def test_failed_first_migration_restores_legacy_directory_without_losing_files(self):
        (self.skill / 'scripts').mkdir(parents=True)
        (self.skill / 'scripts' / 'wlai').write_text('old wrapper')
        (self.skill / 'personal-note.txt').write_text('legacy user content')
        self.canonical.parent.mkdir(parents=True)
        self.canonical.write_text('old CLI'); self.canonical.chmod(0o700)
        activate = install_local._activate_entry
        def fail_canonical(prepared, target):
            if target == self.canonical: raise OSError('simulated failure')
            return activate(prepared, target)
        with patch('install_local._activate_entry', side_effect=fail_canonical):
            with self.assertRaises(install_local.InstallationError): self.install()
        self.assertFalse(self.skill.is_symlink())
        self.assertEqual((self.skill / 'personal-note.txt').read_text(), 'legacy user content')
        self.assertEqual(self.canonical.read_text(), 'old CLI')
        self.assertEqual(self.canonical.stat().st_mode & 0o777, 0o700)

    def test_relative_old_links_have_usable_backups_and_exact_rollback(self):
        old_skill = self.root / 'old relative skill'; old_skill.mkdir()
        (old_skill / 'SKILL.md').write_text('old relative skill data')
        self.skill.parent.mkdir(parents=True)
        skill_target = os.path.relpath(old_skill, self.skill.parent)
        self.skill.symlink_to(skill_target, target_is_directory=True)
        old_cli = self.root / 'old relative cli'; old_cli.write_text('old relative CLI data')
        self.canonical.parent.mkdir(parents=True)
        cli_target = os.path.relpath(old_cli, self.canonical.parent)
        self.canonical.symlink_to(cli_target)
        activate = install_local._activate_entry
        def fail_canonical(prepared, target):
            if target == self.canonical: raise OSError('simulated failure')
            return activate(prepared, target)
        with patch('install_local._activate_entry', side_effect=fail_canonical):
            with self.assertRaises(install_local.InstallationError): self.install()
        self.assertEqual(os.readlink(self.skill), skill_target)
        self.assertEqual(os.readlink(self.canonical), cli_target)
        result = self.install()
        self.assertEqual((Path(result['backup_skill']) / 'SKILL.md').read_text(), 'old relative skill data')
        self.assertEqual(Path(result['backup_cli']).read_text(), 'old relative CLI data')
        self.assertEqual(result['previous_skill_link'], skill_target)
        self.assertEqual(result['previous_cli_link'], cli_target)

    def test_dry_run_does_not_create_targets_or_copy_source(self):
        result = self.install(dry_run=True)
        self.assertTrue(result['dry_run'])
        self.assertFalse(self.skill.exists())
        self.assertFalse(self.versions.exists())
        self.assertFalse(self.canonical.exists())

    def test_explicit_stopped_legacy_migration_preserves_the_complete_old_directory(self):
        (self.skill / 'scripts').mkdir(parents=True)
        (self.skill / 'scripts' / 'cli.py').write_text('old stopped controller code')
        (self.skill / 'unknown-user-file').write_text('keep this file')
        result = self.install(allow_legacy_migration=True)
        self.assertEqual((Path(result['backup_skill']) / 'unknown-user-file').read_text(), 'keep this file')
        self.assertEqual((Path(result['backup_skill']) / 'scripts' / 'cli.py').read_text(), 'old stopped controller code')

    def test_a_directory_given_as_canonical_cli_is_not_replaced(self):
        self.canonical.mkdir(parents=True)
        (self.canonical / 'user-file').write_text('keep directory contents')
        with self.assertRaises(install_local.InstallationError): self.install()
        self.assertEqual((self.canonical / 'user-file').read_text(), 'keep directory contents')
        self.assertFalse(self.versions.exists())

    def test_invalid_existing_targets_and_inplace_source_layout_are_not_deleted(self):
        self.skill.parent.mkdir(parents=True)
        self.skill.write_text('unrelated existing file')
        with self.assertRaises(install_local.InstallationError): self.install()
        self.assertEqual(self.skill.read_text(), 'unrelated existing file')
        self.skill.unlink(); (self.skill / 'scripts').mkdir(parents=True)
        (self.skill / 'scripts' / 'cli.py').write_text('legacy controller source')
        with self.assertRaises(install_local.InstallationError): self.install()
        self.assertEqual((self.skill / 'scripts' / 'cli.py').read_text(), 'legacy controller source')
        self.assertFalse(self.versions.exists())

    def test_source_symlinks_and_modified_immutable_versions_are_rejected(self):
        outside = self.root / 'outside.py'; outside.write_text('private outside source')
        (self.source / 'scripts' / 'linked.py').symlink_to(outside)
        with self.assertRaises(install_local.InstallationError): self.install()
        (self.source / 'scripts' / 'linked.py').unlink()
        installed = self.install()
        cli = Path(installed['installed_cli']); cli.chmod(0o600); cli.write_text('tampered')
        with self.assertRaises(install_local.InstallationError): self.install()
        self.assertEqual(cli.read_text(), 'tampered', 'Installer overwrote an existing version')


if __name__ == '__main__':
    unittest.main()
