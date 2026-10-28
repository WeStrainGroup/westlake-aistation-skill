"""Local job preparation for development environments and staged native tasks."""
import datetime
import json
import math
import os
from pathlib import Path, PurePosixPath
import re
import secrets
import shlex
import tempfile


_DEVELOPMENT_BUDGETS = {
    'ready_timeout': 150,
    'preparation_seconds': 300,
    'collection_seconds': 120,
    'cleanup_seconds': 60,
}


def _text(value, name):
    if not isinstance(value, str) or not value or any(ord(char) < 32 for char in value):
        raise ValueError(f'{name} must be a nonempty string without control characters')
    return value


def _absolute_remote(value, name):
    value = _text(value, name)
    path = PurePosixPath(value)
    if not path.is_absolute() or '..' in path.parts or str(path) == '/':
        raise ValueError(f'{name} must be an absolute POSIX path without parent traversal')
    return str(path)


def _run_id(value):
    if value is None:
        timestamp = datetime.datetime.now(datetime.timezone.utc).strftime('%Y%m%dT%H%M%SZ')
        return 'job_' + timestamp + '_' + secrets.token_hex(3)
    if not isinstance(value, str) or not re.fullmatch(r'[A-Za-z0-9_][A-Za-z0-9_-]{0,47}', value):
        raise ValueError('run_id must contain 1-48 letters, numbers, underscores or hyphens')
    return value


def _runtime_seconds(minutes):
    if (isinstance(minutes, bool) or not isinstance(minutes, (int, float))
            or isinstance(minutes, float) and not math.isfinite(minutes)):
        raise ValueError('minutes must be a finite positive number')
    seconds = minutes * 60
    if seconds < 1 or (isinstance(seconds, float) and not seconds.is_integer()):
        raise ValueError('minutes must express at least one whole second')
    return int(seconds)


def create_job(profile, *, script=None, remote_script=None, run_id=None, cpus=12,
               gpus=1, minutes=None, max_seconds=None, nodes=1, downloads=(), argv=(),
               purpose='development'):
    """Build a local spec; training uses staged persistent files and the native backend.

    Use either minutes or max_seconds. With neither provided, the program budget
    is ten minutes. This generator supports exactly one node, including multiple
    GPUs on that node; it does not construct a distributed launcher.
    """
    if purpose not in ('development', 'training'):
        raise ValueError('purpose must be development or training')
    if (script is None) == (remote_script is None):
        raise ValueError('Choose exactly one local script or absolute remote script')
    if type(nodes) is not int or nodes != 1:
        raise ValueError('Only one node per task is supported; independent tasks are not multi-node DDP')
    if purpose == 'training' and script is not None:
        raise ValueError('Stage the production script and inputs in verified persistent storage first, then use remote_script; training does not upload local files or fall back to a development environment')
    if purpose == 'training' and downloads:
        raise ValueError('Native training outputs remain in persistent storage; automatic downloads are not supported, so collect them separately after validating task completion')
    if type(cpus) is not int or not 1 <= cpus <= 500:
        raise ValueError('cpus must be an integer from 1 to 500')
    if type(gpus) is not int or not 0 <= gpus <= 25:
        raise ValueError('gpus must be an integer from 0 to 25')
    if not isinstance(argv, (list, tuple)) or not all(isinstance(item, str) and '\x00' not in item for item in argv):
        raise ValueError('argv must be a list of program arguments without NUL characters')
    if max_seconds is not None:
        if minutes is not None:
            raise ValueError('Choose minutes or max_seconds, not both')
        if type(max_seconds) is not int or max_seconds < 1:
            raise ValueError('max_seconds must be a positive integer')
        seconds = max_seconds
    else:
        seconds = _runtime_seconds(10 if minutes is None else minutes)
    if purpose == 'development':
        limit = profile.get('max_environment_seconds', 14400)
        if type(limit) is not int or not 1 <= limit <= 14400:
            raise ValueError('Development environment limit must be an integer no greater than 14400 seconds')
        overhead = sum(_DEVELOPMENT_BUDGETS[key] for key in ('preparation_seconds', 'collection_seconds', 'cleanup_seconds'))
        if seconds > 13800 or seconds + overhead > limit:
            raise ValueError('Program plus preparation, collection and cleanup must fit the development limit; program maximum is 13800 seconds')
    interpreter = _absolute_remote(profile.get('remote_python', '/opt/conda/bin/python'), 'remote_python')
    uploads = []
    if script is not None:
        local = Path(script).expanduser().resolve()
        if not local.is_file():
            raise ValueError('Local script must be an existing regular file')
        _text(local.name, 'script filename')
        if '{run_dir}' in local.name:
            raise ValueError('Script filename must not contain the run directory placeholder')
        program = '{run_dir}/' + local.name
        uploads.append({'local': str(local), 'remote': program})
    else:
        program = _absolute_remote(remote_script, 'remote_script')
    transfers = []
    for entry in downloads:
        if isinstance(entry, dict):
            remote, local = entry.get('remote'), entry.get('local')
        elif isinstance(entry, (list, tuple)) and len(entry) == 2:
            remote, local = entry
        else:
            raise ValueError('Each download must provide a remote path and a local path')
        remote = _text(remote, 'download remote path')
        if remote.startswith('{run_dir}/'):
            if '..' in PurePosixPath(remote).parts:
                raise ValueError('Download placeholders must not contain parent traversal')
        else:
            remote = _absolute_remote(remote, 'download remote path')
        _text(os.fspath(local) if isinstance(local, os.PathLike) else local, 'download local path')
        transfers.append({'remote': remote, 'local': str(Path(local).expanduser().resolve())})
    result = {
        'run_id': _run_id(run_id),
        'purpose': purpose,
        'backend': 'environment' if purpose == 'development' else 'native',
        'cpus': cpus,
        'gpus': gpus,
        'max_seconds': seconds,
        'argv': [interpreter, '-u', program, *argv],
        'uploads': uploads,
        'downloads': transfers,
    }
    if purpose == 'development':
        result.update(_DEVELOPMENT_BUDGETS)
    else:
        # The production interpreter belongs to the configured image or an
        # already provisioned persistent environment. Never invent an upload or
        # silently borrow a development-only resource group.
        from native import prepare_task_spec
        result.update(nodes=nodes, image=profile.get('image'),
                      resource_group_id=profile.get('training_group_id'))
        if gpus:
            result['card_type'] = profile.get('card_type')
        result = prepare_task_spec(result, profile)
    return result


def write_json_exclusive(path, data):
    """Atomically publish one private JSON file without replacing an existing path."""
    candidate = Path(path).expanduser()
    # Resolve the parent only: an existing dangling symlink must still count as
    # an existing destination, rather than redirecting publication elsewhere.
    target = candidate.parent.resolve() / candidate.name
    target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    content = json.dumps(data, ensure_ascii=False, indent=2) + '\n'
    temporary = None
    try:
        with tempfile.NamedTemporaryFile('w', encoding='utf-8', dir=target.parent,
                                         prefix='.' + target.name + '.', delete=False) as handle:
            temporary = Path(handle.name)
            temporary.chmod(0o600)
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.link(temporary, target)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
    return target


def _cell(value):
    return str(value).replace('\\', '\\\\').replace('|', '\\|').replace('\n', ' ').replace('\r', ' ')


def _code_block(value):
    ticks = max((len(match.group()) for match in re.finditer(r'`+', value)), default=0)
    fence = '`' * max(3, ticks + 1)
    return fence + 'text\n' + value + '\n' + fence


def render_task_guide(spec, profile):
    """Render human-entered task-form fields; this is never a native API payload."""
    if not isinstance(spec, dict):
        raise ValueError('Job specification must be a JSON object')
    if not spec.get('run_id'):
        raise ValueError('A run_id is required to export the task guide')
    run_id = _run_id(spec['run_id'])
    root = _absolute_remote(profile['persistent_root'], 'persistent_root')
    if root == '/':
        raise ValueError('persistent_root must be a personal directory')
    run_dir = _absolute_remote(spec.get('run_dir', root + '/' + run_id), 'run_dir')
    source_argv = spec.get('argv')
    if not isinstance(source_argv, list) or not source_argv or not all(isinstance(item, str) and '\x00' not in item for item in source_argv):
        raise ValueError('Job argv must be a nonempty argument list')
    command = [item.replace('{run_dir}', run_dir) for item in source_argv]
    _absolute_remote(command[0], 'command executable')
    seconds = spec.get('max_seconds')
    if type(seconds) is not int or seconds < 1:
        raise ValueError('A requested program duration in seconds is required')
    is_training = spec.get('purpose') == 'training' or spec.get('backend') == 'native-guide'
    group = (spec.get('resource_group_id') if is_training else None) or profile.get('training_group_id')
    group = group or '待在资源管理中选择可用训练资源组；核对型号、配额和时限'
    image = spec.get('image') or profile.get('image') or '待选择已调试的镜像'
    cpus, gpus = spec.get('cpus'), spec.get('gpus')
    if type(cpus) is not int or cpus < 1 or type(gpus) is not int or gpus < 0:
        raise ValueError('Explicit CPU and GPU counts are required')
    lines = [
        '# AIStation 正式任务网页填写清单',
        '',
        '此文件用于人工审阅后填写“业务管理 → 任务管理 → 创建”。它不是平台 JSON 导入文件，也不是可提交的原生训练 API 请求；生成清单不会申请资源或提交任务。',
        '',
        '| 网页字段或检查项 | 当前计划 |',
        '| --- | --- |',
        '| 任务名称 | ' + _cell(run_id) + ' |',
        '| 镜像 | ' + _cell(image) + ' |',
        '| 训练资源组 | ' + _cell(group) + ' |',
        '| CPU 核数 | ' + str(cpus) + ' |',
        '| GPU 卡数 | ' + str(gpus) + ' |',
        '| GPU 型号 | ' + _cell(spec.get('card_type') or profile.get('card_type') or '按资源组核对') + ' |',
        '| 个人持久根目录 | ' + _cell(root) + ' |',
        '| 本次运行目录 | ' + _cell(run_dir) + ' |',
        '| 程序时长计划 | ' + str(seconds) + ' 秒；提交前核对所选组允许的时限 |',
        '| checkpoint 与恢复 | 由用户程序实现，并先验证保存和恢复路径 |',
        '',
        '## 运行命令',
        '',
        _code_block(shlex.join(command)),
        '',
        '确认解释器、脚本、输入数据和依赖已经位于所选镜像及挂载目录中。需要的 Conda 环境使用个人持久目录中的解释器绝对路径。',
    ]
    if spec.get('uploads'):
        lines += ['', '## 提交前准备的本地脚本', '', '| 本机文件 | 预期持久路径 |', '| --- | --- |']
        for entry in spec['uploads']:
            remote = _text(entry['remote'], 'upload remote path').replace('{run_dir}', run_dir)
            lines.append('| ' + _cell(entry['local']) + ' | ' + _cell(remote) + ' |')
        lines += ['', '请先完成上传并确认内容。此清单不执行文件传输。']
    if spec.get('downloads'):
        lines += ['', '## 计划回收的结果', '', '| 集群路径 | 本机目标 |', '| --- | --- |']
        for entry in spec['downloads']:
            remote = _text(entry['remote'], 'download remote path').replace('{run_dir}', run_dir)
            lines.append('| ' + _cell(remote) + ' | ' + _cell(entry['local']) + ' |')
    lines += [
        '',
        '## 提交与结束检查',
        '',
        '- 在所选训练资源组中确认 CPU/GPU 能共同满足申请，核对实际可用资源和本组运行时限；不能将“五天”视为所有资源组的默认承诺。',
        '- 确认 checkpoint、日志和最终结果写入个人持久挂载目录，准备节点故障后的恢复流程。',
        '- 提交后检查容器的实际 GPU 分配、日志、任务状态和结果；结束后验证输出并确认资源释放。',
        '',
        '官方手册：[任务提交与管理](https://hpc.westlake.edu.cn/document/ai/aistation/6.task.html)、[常见问题](https://hpc.westlake.edu.cn/document/ai/aistation/9.faq.html)。',
        '',
    ]
    return '\n'.join(lines)
