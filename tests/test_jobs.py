"""Local job generation and task-form handoff, with no credentials or network."""
import json
import os
from pathlib import Path
import re
import stat
import sys
import tempfile
import unittest
from unittest.mock import patch

SCRIPTS = Path(__file__).resolve().parents[1] / 'scripts'
sys.path.insert(0, str(SCRIPTS))
from jobs import create_job, render_task_guide, write_json_exclusive


class JobTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.script = self.root / 'my script.py'
        self.script.write_text("print('example')\n")
        self.profile = {
            'remote_python': '/sample_user/envs/project/bin/python',
            'persistent_root': '/sample_user/runs',
            'max_environment_seconds': 14400,
            'image': 'registry.example.edu/team/pytorch:tag',
            'card_type': 'Example GPU',
            'training_group_id': 'example-training-group',
        }

    def local_job(self, **kwargs):
        return create_job(self.profile, script=self.script, **kwargs)

    def test_local_script_and_argument_boundaries_are_preserved(self):
        arguments = ['--label', 'a b', '--count', '4', '--unicode', '样本']
        job = self.local_job(run_id='example_run', argv=arguments)
        self.assertEqual(job['argv'], [self.profile['remote_python'], '-u', '{run_dir}/my script.py', *arguments])
        self.assertEqual(job['uploads'], [{'local': str(self.script.resolve()), 'remote': '{run_dir}/my script.py'}])
        self.assertEqual(job['backend'], 'environment')
        self.assertEqual(job['purpose'], 'development')
        self.assertEqual(job['max_seconds'], 600)
        self.assertEqual(job['preparation_seconds'], 300)
        self.assertEqual(job['collection_seconds'], 120)
        self.assertEqual(job['cleanup_seconds'], 60)
        self.assertLessEqual(job['ready_timeout'], job['preparation_seconds'])

    def test_remote_script_does_not_create_upload(self):
        job = create_job(self.profile, remote_script='/sample_user/project/main.py', argv=['--dry-run'])
        self.assertEqual(job['uploads'], [])
        self.assertEqual(job['argv'][-2:], ['/sample_user/project/main.py', '--dry-run'])

    def test_auto_run_ids_are_unique_and_registry_compatible(self):
        first, second = self.local_job(), self.local_job()
        self.assertNotEqual(first['run_id'], second['run_id'])
        self.assertRegex(first['run_id'], r'^job_\d{8}T\d{6}Z_[0-9a-f]{6}$')
        self.assertLessEqual(len(first['run_id']), 48)

    def test_script_selector_and_remote_path_are_validated(self):
        for kwargs in ({}, {'script': self.script, 'remote_script': '/sample/main.py'},
                       {'remote_script': 'relative/main.py'}, {'remote_script': '/sample/../main.py'},
                       {'script': self.root / 'missing.py'}):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                create_job(self.profile, **kwargs)

    def test_resources_and_program_arguments_are_validated(self):
        for kwargs in ({'cpus': True}, {'cpus': 0}, {'gpus': -1}, {'gpus': 26},
                       {'argv': 'not-a-list'}, {'argv': ['invalid\x00argument']}, {'run_id': '../bad'},
                       {'purpose': 'unsupported'}):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                self.local_job(**kwargs)

    def test_program_and_preparation_collection_cleanup_must_fit(self):
        maximum = self.local_job(minutes=230)
        self.assertEqual(maximum['max_seconds'], 13800)
        with self.assertRaises(ValueError):
            self.local_job(minutes=231)
        limited = dict(self.profile, max_environment_seconds=600)
        with self.assertRaises(ValueError):
            create_job(limited, script=self.script, minutes=10)
        self.assertEqual(create_job(limited, script=self.script, minutes=2)['max_seconds'], 120)

    def test_fractional_minutes_require_whole_seconds(self):
        self.assertEqual(self.local_job(minutes=0.1)['max_seconds'], 6)
        for minutes in (0, -1, True, float('inf'), float('nan'), 0.001):
            with self.subTest(minutes=minutes), self.assertRaises(ValueError):
                self.local_job(minutes=minutes)

    def test_download_placeholders_are_kept_for_later_preparation(self):
        destination = self.root / 'results' / 'result.json'
        job = self.local_job(downloads=[('{run_dir}/result.json', destination),
                                        {'remote': '/sample_user/existing.txt', 'local': self.root / 'existing.txt'}])
        self.assertEqual(job['downloads'][0], {'remote': '{run_dir}/result.json', 'local': str(destination.resolve())})
        self.assertFalse(destination.parent.exists())

    def test_invalid_downloads_are_rejected(self):
        for remote in ('relative.txt', '{run_dir}/../escape.txt', '/sample/../escape.txt'):
            with self.subTest(remote=remote), self.assertRaises(ValueError):
                self.local_job(downloads=[(remote, self.root / 'result')])
        with self.assertRaises(ValueError):
            self.local_job(downloads=[['only-one-value']])

    def test_training_is_explicitly_non_executable_and_has_no_five_day_assumption(self):
        job = self.local_job(purpose='training', minutes=10000)
        self.assertEqual(job['backend'], 'native-guide')
        self.assertIs(job['executable'], False)
        self.assertEqual(job['purpose'], 'training')
        self.assertEqual(job['max_seconds'], 600000)
        self.assertEqual(job['resource_group_id'], self.profile['training_group_id'])
        self.assertNotIn('ready_timeout', job)

    def test_exclusive_json_is_private_and_never_overwrites(self):
        target = self.root / 'private' / 'job.json'
        job = self.local_job()
        self.assertEqual(write_json_exclusive(target, job), target.resolve())
        self.assertEqual(json.loads(target.read_text()), job)
        self.assertEqual(stat.S_IMODE(target.stat().st_mode), 0o600)
        before = target.read_bytes()
        with self.assertRaises(FileExistsError):
            write_json_exclusive(target, {'changed': True})
        self.assertEqual(target.read_bytes(), before)
        self.assertEqual([item.name for item in target.parent.iterdir()], ['job.json'])

    def test_failed_publication_leaves_no_partial_json(self):
        target = self.root / 'job.json'
        with patch('jobs.os.link', side_effect=PermissionError('simulated')):
            with self.assertRaises(PermissionError):
                write_json_exclusive(target, {'valid': True})
        self.assertFalse(target.exists())
        self.assertFalse(list(self.root.glob('.job.json.*')))

    def test_existing_dangling_symlink_is_not_followed(self):
        destination = self.root / 'job.json'
        other = self.root / 'other.json'
        destination.symlink_to(other)
        with self.assertRaises(FileExistsError):
            write_json_exclusive(destination, {'new': True})
        self.assertTrue(destination.is_symlink())
        self.assertFalse(other.exists())

    def test_task_guide_has_expanded_absolute_command_and_manual_handoff(self):
        job = self.local_job(run_id='review_job', purpose='training', argv=['--label', 'a b'],
                             downloads=[('{run_dir}/result.json', self.root / 'result.json')])
        guide = render_task_guide(job, self.profile)
        self.assertIn('不是平台 JSON 导入文件', guide)
        self.assertIn('不是可提交的原生训练 API 请求', guide)
        self.assertIn('example-training-group', guide)
        self.assertIn("'/sample_user/runs/review_job/my script.py'", guide)
        self.assertIn("--label 'a b'", guide)
        self.assertIn('/sample_user/runs/review_job/result.json', guide)
        self.assertNotIn('{run_dir}', guide)
        self.assertIn('checkpoint', guide)
        self.assertIn('核对所选组允许的时限', guide)
        self.assertIn('/document/ai/aistation/6.task.html', guide)

    def test_development_group_is_not_exported_as_training_group(self):
        job = self.local_job(run_id='review_job')
        job['resource_group_id'] = 'development-only-group'
        guide = render_task_guide(job, self.profile)
        self.assertIn('example-training-group', guide)
        self.assertNotIn('development-only-group', guide)

    def test_guide_with_unknown_training_group_requires_confirmation(self):
        profile = dict(self.profile)
        profile.pop('training_group_id')
        guide = render_task_guide(self.local_job(run_id='review_job'), profile)
        self.assertIn('待在资源管理中选择可用训练资源组', guide)
        self.assertIn('不能将“五天”视为所有资源组的默认承诺', guide)

    def test_guide_does_not_copy_unrelated_profile_fields(self):
        profile = dict(self.profile, password='synthetic-hidden-value', token='synthetic-session-value')
        guide = render_task_guide(self.local_job(run_id='review_job'), profile)
        self.assertNotIn('synthetic-hidden-value', guide)
        self.assertNotIn('synthetic-session-value', guide)

    def test_guide_requires_a_known_run_and_valid_command(self):
        job = self.local_job()
        for key, value in (('run_id', None), ('argv', []), ('argv', ['relative-python']), ('max_seconds', 0)):
            bad = dict(job, **{key: value})
            with self.subTest(key=key, value=value), self.assertRaises(ValueError):
                render_task_guide(bad, self.profile)


if __name__ == '__main__':
    unittest.main()
